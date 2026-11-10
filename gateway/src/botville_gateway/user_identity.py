# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""终端用户身份校验（#124 §1.2.3 / moments.md §3）。

/moments* 接受两种凭据——按 Authorization scheme 显式区分，禁止先试后试：

  ``Authorization: MatrixOpenID <token>``
      调 federation userinfo（不带 X-Matrix 签名）取 ``sub``。
      基址来自 ``GATEWAY_HOMESERVER_FEDERATION_URL``，空则回落 ``GATEWAY_HOMESERVER_URL``。

  ``Authorization: Bearer <Matrix access token>``
      调 CS API whoami 取 ``user_id``。
      始终使用 ``GATEWAY_HOMESERVER_URL``（CS 地址），不走 federation URL。

站点/调用方 bearer（``GATEWAY_AUTH_TOKEN`` / caller token）发到 /moments* 时，
homeserver whoami 不认识它，会返回 401，网关转为 ``auth.invalid_token``——凭 homeserver
的拒绝实现分离，本模块不在本地做区分，也**绝不**把 caller/legacy token 直接放行。

**缓存**：键为 ``SHA-256(token bytes)``（不缓存明文、不入日志）。

- ``config_ttl = GATEWAY_OPENID_CACHE_TTL_SECONDS``：0 = 不缓存。
- 成功 TTL = ``min(config_ttl, 60, expires_in_if_known)``。``expires_in`` 只在
  userinfo 响应带该字段时用来收紧；标准 userinfo 响应通常只有 ``sub``，未携带时
  不收紧，只受 config_ttl 与 60s 上限约束。whoami 没有该字段，同理。
- 失败负缓存（**仅明确 4xx**）TTL = ``min(5, config_ttl)``；config_ttl=0 → 也不缓存。
  5xx / 超时 / 连接失败**不缓存**，调用方可重试（返回 VERIFY_SERVER_ERROR）。

**Single-flight**：同一 token（以哈希为键）的并发校验合并为一次 HS 请求，
leader 完成后结果广播给所有等待者，防止缓存击穿打爆 homeserver
（参考 matrix_appservice.py 里 healthy() 的 single-flight 思路）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from .config import Config

logger = logging.getLogger(__name__)

# 对外结果码（http_server 据此映射到 HTTP 状态码）。
VERIFY_OK = "ok"
VERIFY_MISSING = "missing"              # 401 auth.missing
VERIFY_INVALID = "invalid"              # 401 auth.invalid_token
VERIFY_SERVER_ERROR = "server_error"    # 500 internal.error（不缓存，可重试）

# 失败负缓存上限（秒）：远小于成功 TTL，避免短暂 HS 故障持续锁定有效用户。
FAIL_CACHE_MAX_TTL = 5

# 成功缓存硬上限（秒）；与契约 §1.2.3 一致。
SUCCESS_CACHE_MAX_TTL = 60

# userinfo / whoami 请求超时（秒）。
HS_TIMEOUT = 5.0

# 缓存过期用单调时钟（不受系统时间回拨影响）；模块级绑定便于单测推进时间。
_monotonic = __import__("time").monotonic


@dataclass(frozen=True)
class _CacheEntry:
    """进程内缓存条目（成功与失败负缓存共用）。"""

    user_id: Optional[str]   # 成功时为 Matrix user_id；负缓存时为 None
    is_success: bool
    expires_at: float         # _monotonic() 时间戳


class _Flight:
    """Single-flight 信号量：携带最终 (code, user_id)，供 follower 读取。"""

    __slots__ = ("event", "result")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: Optional[Tuple[str, Optional[str]]] = None


class UserIdentityVerifier:
    """进程内终端用户身份校验器。GatewayServer 持有一个实例（线程安全）。"""

    def __init__(self, config: Config) -> None:
        self._cs_base = (config.homeserver_url or "").rstrip("/")
        fed_url = config.homeserver_federation_url or config.homeserver_url or ""
        self._fed_base = fed_url.rstrip("/")
        self._server_name = config.matrix_server_name  # 可为 None（fail closed）
        self._ttl = config.openid_cache_ttl_seconds     # 0 = 不缓存

        self._cache: Dict[bytes, _CacheEntry] = {}
        self._inflight: Dict[bytes, _Flight] = {}
        self._lock = threading.Lock()

        # 不跟随重定向，不吃代理配置（与 matrix_appservice.py 保持一致）。
        self._opener = urllib.request.build_opener(
            _NoRedirectHandler, urllib.request.ProxyHandler({})
        )

    # ---------------------------------------------------------------- public
    def verify(self, auth_header: Optional[str]) -> Tuple[str, Optional[str]]:
        """校验 Authorization 头，返回 ``(result_code, user_id)``。

        result_code 取值：VERIFY_OK / VERIFY_MISSING /
        VERIFY_INVALID / VERIFY_SERVER_ERROR。user_id 仅在 VERIFY_OK 时有值。
        """
        if not auth_header:
            return VERIFY_MISSING, None

        # 按 scheme 分流：这是唯一的凭据种类判定点，**不做**「先试 OpenID
        # 再试 whoami」的串行回退（§1.2.3）。
        if auth_header.startswith("MatrixOpenID "):
            raw_token = auth_header[len("MatrixOpenID "):].strip()
            if not raw_token:
                return VERIFY_MISSING, None
            return self._verify_with_cache(raw_token, scheme="openid")

        if auth_header.startswith("Bearer "):
            raw_token = auth_header[len("Bearer "):].strip()
            if not raw_token:
                return VERIFY_MISSING, None
            return self._verify_with_cache(raw_token, scheme="whoami")

        # 有头但不是 MatrixOpenID / Bearer：没有任何合法凭据方案。
        return VERIFY_MISSING, None

    # --------------------------------------------------------------- private
    @staticmethod
    def _token_hash(token: str) -> bytes:
        """SHA-256(token UTF-8 bytes)：32 字节，不含明文。"""
        return hashlib.sha256(token.encode("utf-8")).digest()

    def _verify_with_cache(
        self, token: str, *, scheme: str
    ) -> Tuple[str, Optional[str]]:
        """带缓存与 single-flight 的校验入口。"""
        key = self._token_hash(token)

        # ── 1. 缓存命中 ──────────────────────────────────────────────────────
        with self._lock:
            entry = self._cache.get(key)
            if entry is not None and _monotonic() < entry.expires_at:
                if entry.is_success:
                    return VERIFY_OK, entry.user_id
                return VERIFY_INVALID, None

            # ── 2. Single-flight：leader 还是 follower？───────────────────────
            flight = self._inflight.get(key)
            is_leader = flight is None
            if flight is None:
                flight = _Flight()
                self._inflight[key] = flight

        if not is_leader:
            # follower：等 leader 完成；等待超时按 server_error 处理（不缓存）。
            if not flight.event.wait(timeout=HS_TIMEOUT + 2) or flight.result is None:
                return VERIFY_SERVER_ERROR, None
            return flight.result

        # ── 3. Leader 执行 HS 调用 ───────────────────────────────────────────
        try:
            if scheme == "openid":
                result, expires_in = self._do_userinfo(token)
            else:
                result, expires_in = self._do_whoami(token)
        except Exception:  # 意外异常：不缓存，可重试
            logger.debug("user identity lookup raised unexpectedly")
            result, expires_in = (VERIFY_SERVER_ERROR, None), None

        # ── 4. 写缓存：ok → 成功缓存；invalid → 负缓存；server_error → 不缓存 ──
        code, user_id = result
        if self._ttl > 0:
            now = _monotonic()
            if code == VERIFY_OK and user_id is not None:
                ttl = min(self._ttl, SUCCESS_CACHE_MAX_TTL)
                if expires_in is not None and expires_in > 0:
                    # 「若已知则剩余有效期」：只收紧，不延长。
                    ttl = min(ttl, expires_in)
                with self._lock:
                    self._cache[key] = _CacheEntry(user_id, True, now + ttl)
            elif code == VERIFY_INVALID:
                fail_ttl = min(FAIL_CACHE_MAX_TTL, self._ttl)
                if fail_ttl > 0:
                    with self._lock:
                        self._cache[key] = _CacheEntry(None, False, now + fail_ttl)

        # ── 5. 通知 followers，清除 inflight 记录 ────────────────────────────
        flight.result = result
        with self._lock:
            if self._inflight.get(key) is flight:
                del self._inflight[key]
        flight.event.set()

        return result

    # ---------------------------------------------------------------- HS calls
    def _do_userinfo(
        self, token: str
    ) -> Tuple[Tuple[str, Optional[str]], Optional[int]]:
        """调 federation userinfo，返回 ((result_code, user_id), expires_in)。

        ``expires_in`` 只在响应带该字段时返回（标准响应通常只有 ``sub``）；
        未携带时返回 None，成功 TTL 仅受配置与 60s 上限约束。
        """
        if not self._fed_base:
            logger.debug("userinfo: no federation base URL configured, failing")
            return (VERIFY_INVALID, None), None

        url = (
            self._fed_base
            + "/_matrix/federation/v1/openid/userinfo?"
            + urllib.parse.urlencode({"access_token": token})
        )
        # 不带 X-Matrix 签名、不带 Authorization 头（§1.2.3）。
        req = urllib.request.Request(url, method="GET")
        req.add_header("Accept", "application/json")
        try:
            with self._opener.open(req, timeout=HS_TIMEOUT) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return (VERIFY_INVALID, None), None
            # 其他 4xx / 5xx：不缓存，可重试。
            logger.debug("userinfo: homeserver returned %d", exc.code)
            return (VERIFY_SERVER_ERROR, None), None
        except OSError as exc:
            logger.debug("userinfo: connection error: %s", type(exc).__name__)
            return (VERIFY_SERVER_ERROR, None), None

        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return (VERIFY_SERVER_ERROR, None), None
        if not isinstance(data, dict):
            return (VERIFY_SERVER_ERROR, None), None

        sub = data.get("sub")
        if not sub or not isinstance(sub, str):
            return (VERIFY_INVALID, None), None

        code, uid = self._check_domain(sub)
        if code != VERIFY_OK:
            return (code, None), None

        expires_in = data.get("expires_in")
        if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
            return (VERIFY_OK, uid), (int(expires_in) if expires_in > 0 else None)
        return (VERIFY_OK, uid), None

    def _do_whoami(
        self, token: str
    ) -> Tuple[Tuple[str, Optional[str]], Optional[int]]:
        """调 CS API whoami，返回 ((result_code, user_id), expires_in=None)。"""
        if not self._cs_base:
            return (VERIFY_INVALID, None), None

        url = self._cs_base + "/_matrix/client/v3/account/whoami"
        req = urllib.request.Request(url, method="GET")
        req.add_header("Accept", "application/json")
        # 以该 Matrix access token 发起请求（token 只在这里出现，不入日志）。
        req.add_header("Authorization", "Bearer " + token)
        try:
            with self._opener.open(req, timeout=HS_TIMEOUT) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return (VERIFY_INVALID, None), None
            logger.debug("whoami: homeserver returned %d", exc.code)
            return (VERIFY_SERVER_ERROR, None), None
        except OSError as exc:
            logger.debug("whoami: connection error: %s", type(exc).__name__)
            return (VERIFY_SERVER_ERROR, None), None

        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return (VERIFY_SERVER_ERROR, None), None
        if not isinstance(data, dict):
            return (VERIFY_SERVER_ERROR, None), None

        user_id = data.get("user_id")
        if not user_id or not isinstance(user_id, str):
            return (VERIFY_INVALID, None), None

        code, uid = self._check_domain(user_id)
        return (code, uid), None

    def _check_domain(self, matrix_id: str) -> Tuple[str, Optional[str]]:
        """校验 Matrix user_id 的域（§1.2.3，大小写归一）。

        ``GATEWAY_MATRIX_SERVER_NAME`` 未配置时 **fail closed** 返回
        VERIFY_INVALID——不放行 server_name 未知的情况，也不为此新增启动必填
        （memory 模式仍可无该配置启动）。
        """
        if not self._server_name:
            logger.debug(
                "check_domain: GATEWAY_MATRIX_SERVER_NAME not set, failing closed"
            )
            return VERIFY_INVALID, None

        # Matrix user_id 格式：@localpart:domain
        if not matrix_id.startswith("@") or ":" not in matrix_id:
            return VERIFY_INVALID, None

        _, _, domain = matrix_id.rpartition(":")
        if domain.lower() != self._server_name.lower():
            return VERIFY_INVALID, None

        return VERIFY_OK, matrix_id


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """禁止跟随重定向：重定向在这里永远是配置错误（与 matrix_appservice.py 一致）。"""

    def redirect_request(  # type: ignore[no-untyped-def]
        self, req, fp, code, msg, headers, newurl
    ):
        raise urllib.error.HTTPError(
            req.full_url, code, "redirects are not followed", headers, fp
        )
