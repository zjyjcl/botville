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
- 失败负缓存（**仅对 401/403（及域不匹配）负缓存**）TTL = ``min(5, config_ttl)``；config_ttl=0 → 也不缓存。
  其他 4xx / 5xx / 超时 / 连接失败**不缓存**，调用方可重试（返回 VERIFY_SERVER_ERROR）。

**Single-flight**：同一 token（以哈希为键）的并发校验合并为一次 HS 请求，
leader 完成后结果广播给所有等待者，防止缓存击穿打爆 homeserver
（参考 matrix_appservice.py 里 healthy() 的 single-flight 思路）。

leader 意外异常时 follower 看到的是 **leader 的分类结果**，不是异常：

- HS 调用抛普通 ``Exception`` → leader 归为 ``VERIFY_SERVER_ERROR``（不缓存），
  写进 ``flight.result`` 广播给 follower；follower 不自行重试。
- HS 调用期间抛 ``BaseException``（如 ``KeyboardInterrupt``）→ ``finally`` 清
  inflight 并给 follower 补 ``VERIFY_SERVER_ERROR``，leader 仍把原异常重新抛出。
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

# 进程内缓存的条目数上限（成功与失败负缓存共用）。纯防御性：每台网关的
# 活跃真人 token 远少于此，正常流量永远碰不到；它只防止 token 被持续轮换
# 时字典无界增长。条目过期后只在同 key 下次插入时才被顺带清理，所以这里
# 用模块级常量（不新增配置项），插入时先清过期项、仍超限则按 ``expires_at``
# 最早优先淘汰。
CACHE_MAX_ENTRIES = 4096

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
    """线程安全身份验证器；详情使用独立实例保留跨服身份以统一返回 404。"""

    def __init__(self, config: Config, *, allow_foreign_identity: bool = False) -> None:
        # Detail-only verifier: retain verified foreign identities for a uniform 404.
        # Separate instance/cache prevents relaxing any existing endpoint policy.
        self._allow_foreign_identity = allow_foreign_identity
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
    def _evict_and_insert(self, key: bytes, entry: _CacheEntry) -> None:
        """过期清理 + 上限淘汰后插入缓存条目（**必须在 self._lock 下调用**）。

        1. 顺带清理已过期项（lazy expiry，避免无限堆积）。
        2. 清理后仍达 ``CACHE_MAX_ENTRIES``：按 ``expires_at`` 最早优先逐条淘汰，
           直到有位置为止。正常流量下不会触发（活跃 token 远小于上限）。
        """
        now = _monotonic()
        expired = [k for k, e in self._cache.items() if e.expires_at <= now]
        for k in expired:
            del self._cache[k]
        while len(self._cache) >= CACHE_MAX_ENTRIES:
            oldest = min(self._cache, key=lambda k: self._cache[k].expires_at)
            del self._cache[oldest]
        self._cache[key] = entry

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

        # ── 3. Leader 执行 HS 调用 + 写缓存 ─────────────────────────────────
        #
        # follower 看到什么（本段语义，勿破坏）：
        #   - HS 调用抛普通 ``Exception`` → 归为 ``VERIFY_SERVER_ERROR``（不缓存），
        #     该元组随后写进 ``flight.result``；follower 拿到的是 **leader 的
        #     分类结果**，既不自己重试，也吃不到异常。
        #   - HS 调用期间抛 ``BaseException``（如 ``KeyboardInterrupt``）→ 不做
        #     分类，``finally`` 里补一个 ``VERIFY_SERVER_ERROR`` 给 follower；
        #     leader 自己仍把原异常重新抛出（不吞掉 ``KeyboardInterrupt``）。
        #
        # ``try/finally`` 必须包住「HS 调用 + 写缓存」整段。若只包写缓存段，
        # HS 调用期间被 ``BaseException`` 打断时 inflight 会残留：follower 既
        # 拿不到结果，也等不到唤醒，只能干等到 ``HS_TIMEOUT + 2`` 超时。
        result: Optional[Tuple[str, Optional[str]]] = None
        expires_in: Optional[int] = None
        try:
            try:
                if scheme == "openid":
                    result, expires_in = self._do_userinfo(token)
                else:
                    result, expires_in = self._do_whoami(token)
            except Exception:  # 意外异常：不缓存，可重试
                logger.debug("user identity lookup raised unexpectedly")
                result, expires_in = (VERIFY_SERVER_ERROR, None), None

            # ── 4. 写缓存 ───────────────────────────────────────────────────
            code, user_id = result
            if self._ttl > 0:
                now = _monotonic()
                if code == VERIFY_OK and user_id is not None:
                    ttl = min(self._ttl, SUCCESS_CACHE_MAX_TTL)
                    if expires_in is not None and expires_in > 0:
                        # 「若已知则剩余有效期」：只收紧，不延长。
                        ttl = min(ttl, expires_in)
                    with self._lock:
                        self._evict_and_insert(
                            key, _CacheEntry(user_id, True, now + ttl)
                        )
                elif code == VERIFY_INVALID:
                    fail_ttl = min(FAIL_CACHE_MAX_TTL, self._ttl)
                    if fail_ttl > 0:
                        with self._lock:
                            self._evict_and_insert(
                                key, _CacheEntry(None, False, now + fail_ttl)
                            )
        finally:
            # ── 5. 清 inflight + 通知 followers（任何退出路径都要执行）───────
            # inflight 必须在 finally 里清除并 set()，否则：
            #   - 写缓存或其他路径抛异常 → inflight 残留 → follower 永远等到超时；
            #   - 新的同 token 请求无法成为 leader，直到超时才降级 server_error。
            if result is None:
                # 还没有分类结果（HS 调用被 BaseException 打断）：给 follower
                # 一个已分类的结果，别让它们等到超时或吃到异常。异常本身仍由
                # leader 在 finally 返回后继续向上抛出。
                result = (VERIFY_SERVER_ERROR, None)
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

        默认拒绝跨服身份；详情专用实例保留 homeserver 已验证的跨服身份，
        由 persona_detail 的可见性门禁统一拒绝为 404，不授予读取权限。

        ``GATEWAY_MATRIX_SERVER_NAME`` 未配置时 **fail closed** 返回
        VERIFY_INVALID——不放行 server_name 未知的情况，也不为此新增启动必填
        （memory 模式仍可无该配置启动）。
        """
        if not self._server_name:
            logger.debug(
                "check_domain: GATEWAY_MATRIX_SERVER_NAME not set, failing closed"
            )
            return VERIFY_INVALID, None

        # Matrix user_id 格式：@localpart:server_name。server name **可带端口**
        # （如 example.invalid:8448），因此必须在第一个 `:` 处切分（与
        # matrix_appservice.py 的 ``user_id[1:].split(":", 1)`` 一致）：
        # `rpartition(":")` 会把端口误当成域，合法用户被判 401 并进负缓存。
        if not matrix_id.startswith("@"):
            return VERIFY_INVALID, None

        localpart, sep, server_name = matrix_id[1:].partition(":")
        if not sep or not localpart or not server_name:
            return VERIFY_INVALID, None

        if not self._allow_foreign_identity and server_name.lower() != self._server_name.lower():
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
