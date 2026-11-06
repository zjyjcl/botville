"""botville App 扫码配对（pairing QR）契约参考实现 —— 仅解析与校验。

对应契约：docs/contracts/pairing-qr.md（issue #128 PR-a，仅契约）
机器可读规范：docs/contracts/pairing-redeem.openapi.yaml（冲突以它为准）
示例：docs/contracts/examples/pairing-qr/
Python 标准库 only；无网络、无存储、无副作用。

三个入口（不合法一律抛异常，绝不部分接受）：

  parse_pair_uri(str) -> dict          解析 botville://pair?v=1… 二维码 URI
                                       （失败抛 PairUriError；exp 转成 int，
                                       gw/fp 可选，仅在出现时包含在结果里）
  validate_redeem_response(d, hs)      校验 200 兑换响应；homeserver 必须与
                                       二维码中的 hs 逐字符一致（失败抛
                                       PairRedeemError），返回仅含已知字段的副本；
                                       gateway 为 null 表示签发时无 gw
                                       （chat-only 配对）；features_url 可选
                                       （chat-only 响应应省略，出现时仍须
                                       https URL）
  validate_error(d)                    校验 {errcode, error[, retry_after_ms]}
                                       错误体；BV_PAIR_RATE_LIMITED 必带
                                       retry_after_ms（失败抛 PairRedeemError）

Run tests:
    python3 -m unittest discover -s server/tests -p "test_pairing_qr_contract.py" -v
"""

import base64
import re
import string
import urllib.parse

PAIR_URI_PREFIX = "botville://pair?"
URI_VERSION = "1"  # v 参数唯一合法值

# URI 参数：全部已知参数 + 全部必填参数（gw/fp 可选——无网关部署可省略 gw，
# 见 pairing-qr.md §5.1.1「无网关时的行为」）。
KNOWN_PARAMS = frozenset({"v", "hs", "gw", "fp", "code", "exp"})
REQUIRED_PARAMS = ("v", "hs", "code", "exp")

CODE_CHARS = 43  # 32 字节 base64url 无填充；43 字符解码恰好 32 字节
_B64URL = frozenset(string.ascii_letters + string.digits + "-_")
FP_PREFIX = "sha256:"

# exp：规范十进制 Unix 秒（无前导零，1 .. 10^10-1，覆盖到 2286 年）。
_EXP_RE = re.compile(r"^[1-9][0-9]{0,9}$")

AUTH_TYPES = ("registration_token", "login_token")

# 契约错误码 -> HTTP 状态码（pairing-qr.md §4.4）。
ERROR_STATUS = {
    "BV_PAIR_INVALID": 400,
    "BV_PAIR_USED": 409,
    "BV_PAIR_EXPIRED": 410,
    "BV_PAIR_REVOKED": 410,
    "BV_PAIR_RATE_LIMITED": 429,
}


class PairContractError(ValueError):
    """契约校验失败基类（URI 或兑换响应不合规）。"""


class PairUriError(PairContractError):
    """botville://pair URI 不合规 —— 整体拒绝，绝不部分接受。"""


class PairRedeemError(PairContractError):
    """兑换响应（200 或错误体）不合规。"""


def _has_ctrl_or_space(value):
    return any(ch.isspace() or ord(ch) < 0x20 or ch == "\x7f" for ch in value)


def _check_b64url_no_pad(value, what, error):
    """43 字符 base64url 无填充，且为规范编码（解码后重编码与原文一致）。

    返回解码出的原始字节（长度必为 32：43*6//8 且规范编码）。
    """
    if len(value) != CODE_CHARS:
        raise error("%s: length must be %d, got %d" % (what, CODE_CHARS, len(value)))
    bad = set(value) - _B64URL
    if bad:
        raise error("%s: characters outside base64url alphabet: %s" % (what, sorted(bad)))
    # 43 字符补 1 个 '=' 凑 4 的倍数；字母表已预先校验，解码不会遇到非法字符。
    raw = base64.urlsafe_b64decode(value + "=")
    if base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != value:
        raise error("%s: non-canonical base64url encoding (trailing bits must be zero)" % what)
    return raw


def _check_https_url(value, what, error):
    """hs/gw/features_url 等：小写 https:// 绝对 URL，无 userinfo/query/fragment。"""
    if not isinstance(value, str):
        raise error("%s: must be a string" % what)
    if not value.startswith("https://"):
        raise error("%s: must start with https:// (lowercase), got %r" % (what, value))
    if _has_ctrl_or_space(value):
        raise error("%s: control or whitespace characters not allowed" % what)
    if "%" in value:
        raise error("%s: percent-encoding is not used in canonical form" % what)
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port  # 非法端口在访问该属性时才抛 ValueError
    except ValueError as exc:
        raise error("%s: unparseable URL (%s)" % (what, exc))
    if parts.username is not None or parts.password is not None:
        raise error("%s: userinfo not allowed" % what)
    if not parts.hostname:
        raise error("%s: host is required" % what)
    if parts.query:
        raise error("%s: query not allowed" % what)
    if parts.fragment:
        raise error("%s: fragment not allowed" % what)
    return value


def _check_fp(value, error):
    """fp = "sha256:" + 43 字符 base64url（sha256 摘要，32 字节）。"""
    if not value.startswith(FP_PREFIX):
        raise error("fp: must start with %r" % FP_PREFIX)
    raw = _check_b64url_no_pad(value[len(FP_PREFIX):], "fp digest", error)
    if len(raw) != 32:
        raise error("fp: digest must decode to 32 bytes (sha256)")
    return value


def parse_pair_uri(uri):
    """解析并整体校验 botville://pair?v=1&… 二维码 URI。

    返回 {"v", "hs", "code", "exp"(int)[, "gw", "fp"]}；任一规则不满足
    （重复参数/缺必填/未知参数（含未知 v）/格式错误/percent 编码/fragment/
    空白或控制字符）抛 PairUriError。gw 为可选参数：省略时结果不含 "gw" 键，
    出现时仍须通过 §3.2 的 https URL 校验。解析只做格式校验：exp 是否已过期由
    App（对照本地时钟）与服务端（对照签发记录）另行判定。
    """
    if not isinstance(uri, str):
        raise PairUriError("uri must be a string")
    uri = uri.strip()  # 允许去除首尾空白（扫码文本常带换行）
    if not uri.startswith(PAIR_URI_PREFIX):
        raise PairUriError("must start with %r" % PAIR_URI_PREFIX)
    query = uri[len(PAIR_URI_PREFIX):]
    if not query:
        raise PairUriError("empty query")
    if "#" in query:
        raise PairUriError("fragment is not allowed")
    if "%" in query:
        raise PairUriError("percent-encoding is not used in this scheme; '%' rejected")
    if _has_ctrl_or_space(query):
        raise PairUriError("control or whitespace characters not allowed")

    params = {}
    for part in query.split("&"):
        if part == "":
            raise PairUriError("empty parameter (empty segment between '&')")
        if "=" not in part:
            raise PairUriError("parameter without '=': %r" % part)
        key, value = part.split("=", 1)
        if key in params:
            raise PairUriError("duplicate parameter: %r" % key)
        params[key] = value

    unknown = sorted(set(params) - KNOWN_PARAMS)
    if unknown:
        raise PairUriError("unknown parameter(s): %s" % unknown)
    missing = [name for name in REQUIRED_PARAMS if name not in params]
    if missing:
        raise PairUriError("missing required parameter(s): %s" % missing)

    if params["v"] != URI_VERSION:
        raise PairUriError("unsupported v: %r (only %r)" % (params["v"], URI_VERSION))
    _check_https_url(params["hs"], "hs", PairUriError)
    if "gw" in params:  # 可选；出现时仍须 https 绝对 URL（§3.2）
        _check_https_url(params["gw"], "gw", PairUriError)
    _check_b64url_no_pad(params["code"], "code", PairUriError)
    if not _EXP_RE.match(params["exp"]):
        raise PairUriError("exp: must be canonical decimal Unix seconds (no leading zero)")

    result = {name: params[name] for name in ("v", "hs", "code")}
    result["exp"] = int(params["exp"])
    if "gw" in params:
        result["gw"] = params["gw"]
    if "fp" in params:
        _check_fp(params["fp"], PairUriError)
        result["fp"] = params["fp"]
    return result


def validate_redeem_response(resp, expected_hs):
    """校验 POST /_botville/pair/redeem 的 200 响应。

    expected_hs 必须来自 parse_pair_uri 的结果（即二维码中的 hs）。响应的
    homeserver 与它**逐字符**比对（不做任何规范化：多一个斜杠也算不一致），
    不一致抛 PairRedeemError —— 服务端必须返回签发时保存的 hs 原文，不得按
    当前配置重算。gateway 字段必须出现：签发时有 gw 则为该 gw 原文（须 https），
    无 gw 则为 null（chat-only 配对）。features_url **可选**（OpenAPI 不列入
    required，#128/#132 Option A）：gateway 为 null 的响应应省略（App 须忽略
    之、不得据此启用网关功能）；出现时仍须 https URL（不可为 null；gateway
    非 null 时服务端应提供）。未声明字段按契约忽略（前向兼容），返回仅含
    已知字段的副本。
    """
    if not isinstance(resp, dict):
        raise PairRedeemError("redeem response must be a JSON object")
    for key in ("v", "homeserver", "gateway", "auth"):
        if key not in resp:
            raise PairRedeemError("missing field: %s" % key)
    if resp["v"] != URI_VERSION:
        raise PairRedeemError("unsupported v: %r (only %r)" % (resp["v"], URI_VERSION))
    hs = resp["homeserver"]
    if not isinstance(hs, str) or hs != expected_hs:
        raise PairRedeemError(
            "homeserver mismatch: response %r != expected %r (byte-for-byte, no normalisation)"
            % (hs, expected_hs))
    _check_https_url(hs, "homeserver", PairRedeemError)
    gateway = resp["gateway"]
    if gateway is not None:  # null = 签发时无 gw（chat-only，pairing-qr.md §5.1.1）
        _check_https_url(gateway, "gateway", PairRedeemError)
    if "features_url" in resp:  # 可选；出现时仍须 https URL（值不可为 null）
        _check_https_url(resp["features_url"], "features_url", PairRedeemError)

    auth = resp["auth"]
    if not isinstance(auth, dict):
        raise PairRedeemError("auth must be an object")
    for key in ("type", "token", "expires_in_ms"):
        if key not in auth:
            raise PairRedeemError("auth: missing field: %s" % key)
    if auth["type"] not in AUTH_TYPES:
        raise PairRedeemError("auth.type must be one of %s" % (AUTH_TYPES,))
    if not isinstance(auth["token"], str) or not auth["token"]:
        raise PairRedeemError("auth.token must be a non-empty string")
    expires_in_ms = auth["expires_in_ms"]
    if isinstance(expires_in_ms, bool) or not isinstance(expires_in_ms, int) or expires_in_ms < 1:
        raise PairRedeemError("auth.expires_in_ms must be a positive integer (ms)")

    result = {
        "v": resp["v"],
        "homeserver": hs,
        "gateway": gateway,
        "auth": {
            "type": auth["type"],
            "token": auth["token"],
            "expires_in_ms": expires_in_ms,
        },
    }
    if "features_url" in resp:  # 可选字段：缺省时返回副本也不含它
        result["features_url"] = resp["features_url"]
    return result


def validate_error(err):
    """校验 {errcode, error[, retry_after_ms]} 错误体。

    返回仅含已知字段的副本。BV_PAIR_RATE_LIMITED 缺 retry_after_ms 抛
    PairRedeemError；retry_after_ms 出现在其他错误码上是允许的（可选）。
    """
    if not isinstance(err, dict):
        raise PairRedeemError("error body must be a JSON object")
    for key in ("errcode", "error"):
        if key not in err:
            raise PairRedeemError("missing field: %s" % key)
    errcode = err["errcode"]
    if errcode not in ERROR_STATUS:
        raise PairRedeemError("unknown errcode: %r (known: %s)" % (errcode, sorted(ERROR_STATUS)))
    if not isinstance(err["error"], str) or not err["error"]:
        raise PairRedeemError("error must be a non-empty string")

    result = {"errcode": errcode, "error": err["error"]}
    if "retry_after_ms" in err:
        retry = err["retry_after_ms"]
        if isinstance(retry, bool) or not isinstance(retry, int) or retry < 1:
            raise PairRedeemError("retry_after_ms must be a positive integer (ms)")
        result["retry_after_ms"] = retry
    if errcode == "BV_PAIR_RATE_LIMITED" and "retry_after_ms" not in result:
        raise PairRedeemError("BV_PAIR_RATE_LIMITED must carry retry_after_ms")
    return result
