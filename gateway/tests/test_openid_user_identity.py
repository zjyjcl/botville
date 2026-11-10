#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""#124 OpenID 网关实现验收测试：/moments* 的终端用户身份校验。

覆盖（契约 §1.2.3 / moments.md §3）：

  1. MatrixOpenID → federation userinfo 成功 / 无效 / 过期；
  2. Bearer → CS whoami 成功 / 无效；
  3. homeserver 5xx / 超时 / 不可达 → 500 internal.error，且**不缓存**；
  4. 成功缓存命中与过期；`expires_in` 收紧；配置 0 = 不缓存；
  5. 失败负缓存 5 秒（仅 4xx），5xx 不缓存；
  6. single-flight：并发同一 token 只打一次 HS；
  7. 两种 scheme 分流，禁止「先试 OpenID 再试 whoami」；
  8. 网关 / legacy token 不能当 /moments* 的用户身份；
  9. 路径前缀下仍工作；10. token 不泄露到日志；
 11. federation URL 空则回落 homeserver_url；显式时 userinfo 打该基址、whoami 仍打 CS；
 12. 配置 TTL 边界：0 合法，<0 / >60 / 非整数拒绝启动。

全部使用本地假 HTTPServer 与明显假 token，不含真实密钥。
运行：PYTHONPATH=gateway/src python3 -m unittest gateway.tests.test_openid_user_identity -v
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Tuple
from unittest import mock

GATEWAY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(GATEWAY_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from botville_gateway import user_identity  # noqa: E402
from botville_gateway.config import (  # noqa: E402
    CallerToken,
    Config,
    ConfigError,
    load_config,
)
from botville_gateway.http_server import build_server  # noqa: E402
from botville_gateway.user_identity import (  # noqa: E402
    VERIFY_INVALID,
    VERIFY_MISSING,
    VERIFY_OK,
    VERIFY_SERVER_ERROR,
    UserIdentityVerifier,
)

# ── 明显假凭据（不含真实密钥） ────────────────────────────────────────────────
SERVER_NAME = "localhost"
OPENID_TOKEN = "fake-openid-token-0000000000000001"
OPENID_TOKEN_2 = "fake-openid-token-0000000000000002"
MATRIX_TOKEN = "fake-matrix-access-token-0000000003"
GATEWAY_TOKEN = "gateway-site-token-0000000000000004"
CALLER_TOKEN = "caller-token-0000000000000000000005"
LEGACY_TOKEN = "legacy-site-token-00000000000000006"
USER_ID = "@alice:localhost"
USERINFO_PATH = "/_matrix/federation/v1/openid/userinfo"
WHOAMI_PATH = "/_matrix/client/v3/account/whoami"

_HMAC = b"test-hmac-secret-0123456789abcdef"


class _Clock:
    """可推进的假单调时钟（用于缓存 TTL 测试）。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeHomeserver:
    """可编程假 homeserver：同时充当 CS API 与 federation userinfo 端点。

    - `userinfo_status` / `userinfo_body`：userinfo 响应；
    - `whoami_tokens`：whoami 认可的 Matrix access token → user_id 映射；
      不在表里的 bearer 一律 401（模拟站点 token 被 HS 拒绝）；
    - `delay`：每个请求前 sleep（秒），用于超时 / single-flight 测试；
    - `requests`：全部收到的请求，含 method / path / query / authorization。
    """

    def __init__(self) -> None:
        outer = self
        self.requests: List[Dict[str, object]] = []
        self.userinfo_status: int = 200
        self.userinfo_body: Dict[str, object] = {"sub": USER_ID}
        self.whoami_tokens: Dict[str, str] = {MATRIX_TOKEN: USER_ID}
        self.delay: float = 0.0
        self._lock = threading.Lock()

        class _Server(ThreadingHTTPServer):
            daemon_threads = True
            request_queue_size = 64

        class Handler(BaseHTTPRequestHandler):
            def _respond(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                parsed, _, query = self.path.partition("?")
                with outer._lock:
                    outer.requests.append(
                        {
                            "method": self.command,
                            "path": parsed,
                            "query": urllib.parse.parse_qs(query),
                            "authorization": self.headers.get("Authorization"),
                        }
                    )
                if outer.delay:
                    import time as _time

                    _time.sleep(outer.delay)

                status, payload = 404, {"errcode": "M_UNRECOGNIZED"}
                if parsed == USERINFO_PATH:
                    status, payload = outer.userinfo_status, outer.userinfo_body
                elif parsed == WHOAMI_PATH:
                    auth = self.headers.get("Authorization") or ""
                    token = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
                    user_id = outer.whoami_tokens.get(token)
                    if user_id is None:
                        status, payload = 401, {"errcode": "M_UNKNOWN_TOKEN"}
                    else:
                        status, payload = 200, {"user_id": user_id}

                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # 超时测试里客户端先断开，正常现象

            do_GET = _respond

            def log_message(self, *args) -> None:  # 测试输出保持安静
                pass

        self.httpd = _Server(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return "http://{}:{}".format(host, port)

    def matching(self, path: str) -> List[Dict[str, object]]:
        with self._lock:
            return [r for r in self.requests if r["path"] == path]

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def _cfg(**overrides) -> Config:
    base = dict(
        host="127.0.0.1",
        port=0,
        dev_mode=True,
        auth_token=None,
        rate_limit_messages=60,
        silent_hours_enabled=False,       # 不受真实时刻影响
        hmac_secret=_HMAC,
        matrix_server_name=SERVER_NAME,
    )
    base.update(overrides)
    return Config(**base)


class _ServerFixture:
    """在后台线程里起 GatewayServer（端口 0 = 随机空闲端口）。"""

    def __init__(self, config: Config) -> None:
        self.server = build_server(config)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "_ServerFixture":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @property
    def base(self) -> str:
        host, port = self.server.server_address[:2]
        return "http://{}:{}".format(host, port)


def _http(method: str, url: str, headers: Optional[Dict[str, str]] = None,
          timeout: float = 10) -> Tuple[int, Optional[dict]]:
    req = urllib.request.Request(url, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, json.loads(raw) if raw else None


def _closed_port() -> int:
    """取一个当前无人监听的端口（用于不可达测试）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


# ============================================================================
# 1. 单元层：UserIdentityVerifier
# ============================================================================
class VerifierTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.hs = FakeHomeserver()
        self.addCleanup(self.hs.close)
        self.clock = _Clock()
        patcher = mock.patch.object(user_identity, "_monotonic", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make(self, **overrides) -> UserIdentityVerifier:
        overrides.setdefault("homeserver_url", self.hs.base_url)
        return UserIdentityVerifier(_cfg(**overrides))


class TestOpenIdSuccess(VerifierTestBase):
    def test_userinfo_success_returns_sub(self):
        verifier = self.make()
        code, user_id = verifier.verify("MatrixOpenID " + OPENID_TOKEN)
        self.assertEqual((code, user_id), (VERIFY_OK, USER_ID))
        req = self.hs.matching(USERINFO_PATH)
        self.assertEqual(len(req), 1)
        # 不带 X-Matrix 签名 / Authorization；token 在 query。
        self.assertIsNone(req[0]["authorization"])
        self.assertEqual(req[0]["query"]["access_token"], [OPENID_TOKEN])
        # MatrixOpenID 路径不得触碰 whoami。
        self.assertEqual(self.hs.matching(WHOAMI_PATH), [])

    def test_missing_header(self):
        verifier = self.make()
        self.assertEqual(verifier.verify(None), (VERIFY_MISSING, None))
        self.assertEqual(verifier.verify(""), (VERIFY_MISSING, None))
        self.assertEqual(self.hs.requests, [])

    def test_unknown_scheme_is_missing(self):
        verifier = self.make()
        self.assertEqual(verifier.verify("Basic abc"), (VERIFY_MISSING, None))
        self.assertEqual(verifier.verify("MatrixOpenID "), (VERIFY_MISSING, None))
        self.assertEqual(self.hs.requests, [])

    def test_domain_mismatch_is_invalid(self):
        self.hs.userinfo_body = {"sub": "@alice:other.example"}
        verifier = self.make()
        self.assertEqual(
            verifier.verify("MatrixOpenID " + OPENID_TOKEN), (VERIFY_INVALID, None)
        )

    def test_domain_compare_is_case_insensitive(self):
        self.hs.userinfo_body = {"sub": "@alice:LOCALHOST"}
        verifier = self.make()
        code, user_id = verifier.verify("MatrixOpenID " + OPENID_TOKEN)
        self.assertEqual(code, VERIFY_OK)

    def test_missing_server_name_fails_closed(self):
        self.hs.userinfo_body = {"sub": USER_ID}
        verifier = self.make(matrix_server_name=None)
        self.assertEqual(
            verifier.verify("MatrixOpenID " + OPENID_TOKEN), (VERIFY_INVALID, None)
        )
        # Bearer 路径同样 fail closed。
        self.assertEqual(
            verifier.verify("Bearer " + MATRIX_TOKEN), (VERIFY_INVALID, None)
        )

    def test_userinfo_without_sub_is_invalid(self):
        self.hs.userinfo_body = {"access_token": "x"}
        verifier = self.make()
        self.assertEqual(
            verifier.verify("MatrixOpenID " + OPENID_TOKEN), (VERIFY_INVALID, None)
        )


class TestInvalidAndExpired(VerifierTestBase):
    def test_userinfo_401_is_invalid(self):
        self.hs.userinfo_status = 401
        verifier = self.make()
        self.assertEqual(
            verifier.verify("MatrixOpenID " + OPENID_TOKEN), (VERIFY_INVALID, None)
        )

    def test_userinfo_403_is_invalid(self):
        self.hs.userinfo_status = 403
        verifier = self.make()
        self.assertEqual(
            verifier.verify("MatrixOpenID " + OPENID_TOKEN), (VERIFY_INVALID, None)
        )

    def test_expired_token_expressed_as_userinfo_401(self):
        """过期以 homeserver 的 401 表达（契约不要求网关自行解析有效期）。"""
        self.hs.userinfo_status = 401
        verifier = self.make()
        self.assertEqual(
            verifier.verify("MatrixOpenID " + OPENID_TOKEN), (VERIFY_INVALID, None)
        )

    def test_bearer_invalid_is_invalid_and_no_userinfo(self):
        verifier = self.make()
        self.assertEqual(
            verifier.verify("Bearer not-a-matrix-token"), (VERIFY_INVALID, None)
        )
        self.assertEqual(len(self.hs.matching(WHOAMI_PATH)), 1)
        self.assertEqual(self.hs.matching(USERINFO_PATH), [])

    def test_bearer_success_no_userinfo(self):
        verifier = self.make()
        self.assertEqual(
            verifier.verify("Bearer " + MATRIX_TOKEN), (VERIFY_OK, USER_ID)
        )
        self.assertEqual(self.hs.matching(USERINFO_PATH), [])


class TestServerErrors(VerifierTestBase):
    def test_5xx_is_server_error_and_not_cached(self):
        self.hs.userinfo_status = 503
        verifier = self.make()
        self.assertEqual(
            verifier.verify("MatrixOpenID " + OPENID_TOKEN),
            (VERIFY_SERVER_ERROR, None),
        )
        self.clock.advance(1)
        self.assertEqual(
            verifier.verify("MatrixOpenID " + OPENID_TOKEN),
            (VERIFY_SERVER_ERROR, None),
        )
        # 5xx 不缓存：每次都真的打 HS。
        self.assertEqual(len(self.hs.matching(USERINFO_PATH)), 2)

    def test_timeout_is_server_error_and_not_cached(self):
        self.hs.delay = 0.3
        verifier = self.make()
        with mock.patch.object(user_identity, "HS_TIMEOUT", 0.05):
            self.assertEqual(
                verifier.verify("MatrixOpenID " + OPENID_TOKEN),
                (VERIFY_SERVER_ERROR, None),
            )
            # 第二次仍超时：超时不缓存，每次都真的打 HS。
            self.assertEqual(
                verifier.verify("MatrixOpenID " + OPENID_TOKEN),
                (VERIFY_SERVER_ERROR, None),
            )
        self.assertEqual(len(self.hs.matching(USERINFO_PATH)), 2)

    def test_unreachable_is_server_error(self):
        verifier = self.make(homeserver_url="http://127.0.0.1:{}".format(_closed_port()))
        self.assertEqual(
            verifier.verify("MatrixOpenID " + OPENID_TOKEN),
            (VERIFY_SERVER_ERROR, None),
        )


class TestCaching(VerifierTestBase):
    def test_success_cache_hit_then_expiry(self):
        verifier = self.make(openid_cache_ttl_seconds=60)
        verifier.verify("MatrixOpenID " + OPENID_TOKEN)
        self.assertEqual(len(self.hs.matching(USERINFO_PATH)), 1)

        self.clock.advance(10)
        self.assertEqual(
            verifier.verify("MatrixOpenID " + OPENID_TOKEN), (VERIFY_OK, USER_ID)
        )
        self.assertEqual(len(self.hs.matching(USERINFO_PATH)), 1)  # 命中缓存

        self.clock.advance(51)  # 累计 61s > TTL 60
        verifier.verify("MatrixOpenID " + OPENID_TOKEN)
        self.assertEqual(len(self.hs.matching(USERINFO_PATH)), 2)

    def test_expires_in_tightens_ttl(self):
        self.hs.userinfo_body = {"sub": USER_ID, "expires_in": 2}
        verifier = self.make(openid_cache_ttl_seconds=60)
        verifier.verify("MatrixOpenID " + OPENID_TOKEN)
        self.assertEqual(len(self.hs.matching(USERINFO_PATH)), 1)

        self.clock.advance(3)  # 超过 expires_in=2
        verifier.verify("MatrixOpenID " + OPENID_TOKEN)
        self.assertEqual(len(self.hs.matching(USERINFO_PATH)), 2)

    def test_unknown_expires_in_only_bounded_by_config(self):
        """标准 userinfo 通常只有 sub：is not tightened beyond config/60."""
        self.hs.userinfo_body = {"sub": USER_ID}
        verifier = self.make(openid_cache_ttl_seconds=60)
        verifier.verify("MatrixOpenID " + OPENID_TOKEN)
        self.clock.advance(30)
        verifier.verify("MatrixOpenID " + OPENID_TOKEN)
        self.assertEqual(len(self.hs.matching(USERINFO_PATH)), 1)

    def test_ttl_zero_disables_success_and_failure_cache(self):
        verifier = self.make(openid_cache_ttl_seconds=0)
        verifier.verify("MatrixOpenID " + OPENID_TOKEN)
        verifier.verify("MatrixOpenID " + OPENID_TOKEN)
        self.assertEqual(len(self.hs.matching(USERINFO_PATH)), 2)

        self.hs.userinfo_status = 401
        verifier.verify("MatrixOpenID " + OPENID_TOKEN)
        verifier.verify("MatrixOpenID " + OPENID_TOKEN)
        self.assertEqual(len(self.hs.matching(USERINFO_PATH)), 4)

    def test_failure_negative_cache_is_short(self):
        self.hs.userinfo_status = 401
        verifier = self.make(openid_cache_ttl_seconds=60)
        self.assertEqual(
            verifier.verify("MatrixOpenID " + OPENID_TOKEN), (VERIFY_INVALID, None)
        )
        self.assertEqual(len(self.hs.matching(USERINFO_PATH)), 1)

        self.clock.advance(2)  # 负缓存 5s 内
        self.assertEqual(
            verifier.verify("MatrixOpenID " + OPENID_TOKEN), (VERIFY_INVALID, None)
        )
        self.assertEqual(len(self.hs.matching(USERINFO_PATH)), 1)

        self.clock.advance(4)  # 累计 6s > 5s
        verifier.verify("MatrixOpenID " + OPENID_TOKEN)
        self.assertEqual(len(self.hs.matching(USERINFO_PATH)), 2)

    def test_negative_cache_does_not_exceed_success_ttl(self):
        """失败 TTL = min(5, config)；config=1 时负缓存最多 1 秒。"""
        self.hs.userinfo_status = 401
        verifier = self.make(openid_cache_ttl_seconds=1)
        verifier.verify("MatrixOpenID " + OPENID_TOKEN)
        self.clock.advance(2)
        verifier.verify("MatrixOpenID " + OPENID_TOKEN)
        self.assertEqual(len(self.hs.matching(USERINFO_PATH)), 2)

    def test_cache_key_is_hash_not_plaintext(self):
        verifier = self.make()
        code, user_id = verifier.verify("MatrixOpenID " + OPENID_TOKEN)
        self.assertEqual(code, VERIFY_OK)
        # 缓存键是 SHA-256 摘要，不含明文 token。
        cache = verifier._cache  # noqa: SLF001 - 白盒断言缓存键形状
        self.assertEqual(len(cache), 1)
        key = next(iter(cache))
        self.assertIsInstance(key, bytes)
        self.assertEqual(len(key), 32)
        self.assertNotIn(OPENID_TOKEN.encode(), key)


class TestSingleFlight(VerifierTestBase):
    def test_concurrent_same_token_hits_hs_once(self):
        verifier = self.make(openid_cache_ttl_seconds=60)
        self.hs.delay = 0.4
        threads = 8
        barrier = threading.Barrier(threads)
        results: List[Tuple[str, Optional[str]]] = []
        errors: List[BaseException] = []

        def worker() -> None:
            try:
                barrier.wait(timeout=5)
                results.append(verifier.verify("MatrixOpenID " + OPENID_TOKEN))
            except BaseException as exc:  # pragma: no cover - 测试失败路径
                errors.append(exc)

        workers = [threading.Thread(target=worker) for _ in range(threads)]
        for t in workers:
            t.start()
        for t in workers:
            t.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), threads)
        self.assertTrue(all(r == (VERIFY_OK, USER_ID) for r in results))
        self.assertEqual(len(self.hs.matching(USERINFO_PATH)), 1)

    def test_concurrent_invalid_token_hits_hs_once(self):
        verifier = self.make(openid_cache_ttl_seconds=60)
        self.hs.userinfo_status = 401
        self.hs.delay = 0.3
        threads = 6
        barrier = threading.Barrier(threads)
        results: List[Tuple[str, Optional[str]]] = []

        def worker() -> None:
            barrier.wait(timeout=5)
            results.append(verifier.verify("MatrixOpenID " + OPENID_TOKEN_2))

        workers = [threading.Thread(target=worker) for _ in range(threads)]
        for t in workers:
            t.start()
        for t in workers:
            t.join(timeout=10)

        self.assertTrue(all(r == (VERIFY_INVALID, None) for r in results))
        self.assertEqual(len(self.hs.matching(USERINFO_PATH)), 1)


class TestFederationUrl(VerifierTestBase):
    def test_empty_federation_url_falls_back_to_homeserver_url(self):
        verifier = self.make(homeserver_url=self.hs.base_url,
                             homeserver_federation_url=None)
        self.assertEqual(verifier._fed_base, self.hs.base_url)  # noqa: SLF001
        verifier.verify("MatrixOpenID " + OPENID_TOKEN)
        self.assertEqual(len(self.hs.matching(USERINFO_PATH)), 1)

    def test_explicit_federation_url_used_only_for_userinfo(self):
        cs = FakeHomeserver()
        fed = FakeHomeserver()
        self.addCleanup(cs.close)
        self.addCleanup(fed.close)
        verifier = UserIdentityVerifier(
            _cfg(homeserver_url=cs.base_url, homeserver_federation_url=fed.base_url)
        )

        verifier.verify("MatrixOpenID " + OPENID_TOKEN)
        self.assertEqual(len(fed.matching(USERINFO_PATH)), 1)
        self.assertEqual(cs.matching(USERINFO_PATH), [])

        verifier.verify("Bearer " + MATRIX_TOKEN)
        self.assertEqual(len(cs.matching(WHOAMI_PATH)), 1)
        self.assertEqual(fed.matching(WHOAMI_PATH), [])


class TestNoTokenLeak(VerifierTestBase):
    def test_tokens_never_logged(self):
        records: List[str] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(self.format(record))

        handler = _Capture()
        handler.setLevel(logging.DEBUG)
        target = logging.getLogger("botville_gateway.user_identity")
        old_level = target.level
        target.setLevel(logging.DEBUG)
        target.addHandler(handler)
        self.addCleanup(target.removeHandler, handler)
        self.addCleanup(target.setLevel, old_level)

        verifier = self.make()
        verifier.verify("MatrixOpenID " + OPENID_TOKEN)          # 成功（写缓存）
        verifier.verify("MatrixOpenID " + OPENID_TOKEN)          # 缓存命中
        verifier.verify("Bearer not-a-matrix-token")             # 失败
        self.hs.userinfo_status = 401
        verifier.verify("MatrixOpenID " + OPENID_TOKEN_2)        # 无效

        joined = "\n".join(records)
        for secret in (OPENID_TOKEN, OPENID_TOKEN_2, "not-a-matrix-token"):
            self.assertNotIn(secret, joined)

    def test_cache_entry_repr_has_no_token(self):
        verifier = self.make()
        verifier.verify("MatrixOpenID " + OPENID_TOKEN)
        entry = next(iter(verifier._cache.values()))  # noqa: SLF001
        self.assertNotIn(OPENID_TOKEN, repr(entry))


# ============================================================================
# 2. 配置层：TTL 边界
# ============================================================================
class TestConfigTtl(unittest.TestCase):
    def _load(self, value: str) -> Config:
        env = {
            "GATEWAY_DEV_MODE": "true",
            "GATEWAY_OPENID_CACHE_TTL_SECONDS": value,
        }
        return load_config(env)

    def test_default_is_60(self):
        self.assertEqual(self._load("").openid_cache_ttl_seconds, 60)

    def test_zero_disables_cache(self):
        self.assertEqual(self._load("0").openid_cache_ttl_seconds, 0)

    def test_sixty_allowed(self):
        self.assertEqual(self._load("60").openid_cache_ttl_seconds, 60)

    def test_above_60_rejected(self):
        with self.assertRaises(ConfigError):
            self._load("61")

    def test_negative_rejected(self):
        with self.assertRaises(ConfigError):
            self._load("-1")

    def test_non_integer_rejected(self):
        with self.assertRaises(ConfigError):
            self._load("abc")

    def test_direct_config_above_60_rejected(self):
        with self.assertRaises(ConfigError):
            _cfg(openid_cache_ttl_seconds=61)

    def test_direct_config_negative_rejected(self):
        with self.assertRaises(ConfigError):
            _cfg(openid_cache_ttl_seconds=-1)

    def test_federation_url_plaintext_allowlist(self):
        # 明文 http 到带点域名拒绝；loopback / 无点容器名放行。
        with self.assertRaises(ConfigError):
            _cfg(homeserver_federation_url="http://synapse.example.com:8008")
        self.assertEqual(
            _cfg(homeserver_federation_url="http://synapse:8008")
            .homeserver_federation_url,
            "http://synapse:8008",
        )

    def test_federation_url_requires_scheme(self):
        with self.assertRaises(ConfigError):
            _cfg(homeserver_federation_url="synapse:8008")


# ============================================================================
# 3. HTTP 层：/moments* 鉴权与状态码
# ============================================================================
class MomentsHttpTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.hs = FakeHomeserver()
        self.addCleanup(self.hs.close)

    def server(self, **overrides) -> _ServerFixture:
        overrides.setdefault("homeserver_url", self.hs.base_url)
        return _ServerFixture(_cfg(**overrides))

    def get(self, base: str, path: str, auth: Optional[str]) -> Tuple[int, Optional[dict]]:
        headers = {"Authorization": auth} if auth else {}
        return _http("GET", base + path, headers=headers)


class TestMomentsHttpAuth(MomentsHttpTestBase):
    def test_openid_token_passes_auth_then_404(self):
        with self.server() as fx:
            status, body = self.get(fx.base, "/v1/moments", "MatrixOpenID " + OPENID_TOKEN)
        # 通过用户鉴权；本 PR 未实现动态资源 → 404（不是 401）。
        self.assertEqual(status, 404, body)
        self.assertEqual(len(self.hs.matching(USERINFO_PATH)), 1)
        self.assertEqual(self.hs.matching(WHOAMI_PATH), [])

    def test_bearer_matrix_token_passes_auth_then_404(self):
        with self.server() as fx:
            status, body = self.get(fx.base, "/v1/moments", "Bearer " + MATRIX_TOKEN)
        self.assertEqual(status, 404, body)
        self.assertEqual(len(self.hs.matching(WHOAMI_PATH)), 1)
        self.assertEqual(self.hs.matching(USERINFO_PATH), [])

    def test_missing_header_is_401_auth_missing(self):
        with self.server() as fx:
            status, body = self.get(fx.base, "/v1/moments", None)
        self.assertEqual(status, 401)
        self.assertEqual(body["code"], "auth.missing")

    def test_invalid_openid_is_401_and_never_tries_whoami(self):
        self.hs.userinfo_status = 401
        with self.server() as fx:
            status, body = self.get(
                fx.base, "/v1/moments", "MatrixOpenID " + OPENID_TOKEN
            )
        self.assertEqual(status, 401)
        self.assertEqual(body["code"], "auth.invalid_token")
        # 禁止「先试 OpenID 再试 whoami」：whoami 一次都不能打。
        self.assertEqual(self.hs.matching(WHOAMI_PATH), [])

    def test_invalid_bearer_is_401_and_never_tries_userinfo(self):
        with self.server() as fx:
            status, body = self.get(fx.base, "/v1/moments", "Bearer nope-nope")
        self.assertEqual(status, 401)
        self.assertEqual(body["code"], "auth.invalid_token")
        self.assertEqual(self.hs.matching(USERINFO_PATH), [])

    def test_homeserver_5xx_is_500_internal_error(self):
        self.hs.userinfo_status = 502
        with self.server() as fx:
            status, body = self.get(
                fx.base, "/v1/moments", "MatrixOpenID " + OPENID_TOKEN
            )
        self.assertEqual(status, 500)
        self.assertEqual(body["code"], "internal.error")

    def test_gateway_site_token_cannot_act_as_user(self):
        """站点级 bearer 是有效网关 token，但 /moments* 不得当成用户身份。"""
        with self.server(auth_token=GATEWAY_TOKEN, dev_mode=False) as fx:
            status, body = self.get(fx.base, "/v1/moments", "Bearer " + GATEWAY_TOKEN)
            self.assertEqual(status, 401)
            self.assertEqual(body["code"], "auth.invalid_token")
            # 同一 token 在其它接口仍然有效（鉴权模型未被改动）。
            ok_status, _ = self.get(fx.base, "/v1/limits", "Bearer " + GATEWAY_TOKEN)
            self.assertEqual(ok_status, 200)

    def test_caller_token_cannot_act_as_user(self):
        caller = CallerToken(caller_id="c1", token=CALLER_TOKEN, owner_id="owner1")
        with self.server(caller_tokens=(caller,), auth_token=LEGACY_TOKEN,
                         dev_mode=False) as fx:
            status, body = self.get(fx.base, "/v1/moments", "Bearer " + CALLER_TOKEN)
            self.assertEqual(status, 401)
            self.assertEqual(body["code"], "auth.invalid_token")
            # 其它接口仍可识别 caller token（不改 §1.2.2）。
            ok_status, _ = self.get(fx.base, "/v1/limits", "Bearer " + CALLER_TOKEN)
            self.assertEqual(ok_status, 200)

    def test_legacy_token_cannot_act_as_user(self):
        with self.server(auth_token=LEGACY_TOKEN, dev_mode=False) as fx:
            status, body = self.get(fx.base, "/v1/moments", "Bearer " + LEGACY_TOKEN)
        self.assertEqual(status, 401)
        self.assertEqual(body["code"], "auth.invalid_token")
        self.assertEqual(self.hs.matching(USERINFO_PATH), [])

    def test_all_moments_subpaths_require_user_auth(self):
        with self.server() as fx:
            for path in (
                "/v1/moments",
                "/v1/moments/m_1",
                "/v1/moments/m_1/comments",
                "/v1/moments/m_1/likes/me",
            ):
                status, body = self.get(fx.base, path, None)
                self.assertEqual(status, 401, path)
                self.assertEqual(body["code"], "auth.missing", path)

    def test_path_prefix_still_works(self):
        with self.server(http_path_prefix="/_botville/gateway") as fx:
            status, body = self.get(
                fx.base, "/_botville/gateway/v1/moments", "MatrixOpenID " + OPENID_TOKEN
            )
            self.assertEqual(status, 404, body)
            unauth, prob = self.get(fx.base, "/_botville/gateway/v1/moments", None)
            self.assertEqual(unauth, 401)
            self.assertEqual(prob["code"], "auth.missing")


class TestMomentsHttpMethods(MomentsHttpTestBase):
    def test_patch_and_delete_auth_then_405(self):
        with self.server() as fx:
            for method in ("PATCH", "DELETE"):
                authed = _http(
                    method, fx.base + "/v1/moments/m_1",
                    headers={"Authorization": "MatrixOpenID " + OPENID_TOKEN},
                )
                self.assertEqual(authed[0], 405, method)
                unauth = _http(method, fx.base + "/v1/moments/m_1")
                self.assertEqual(unauth[0], 401, method)
                self.assertEqual(unauth[1]["code"], "auth.missing", method)

    def test_post_put_auth_then_404(self):
        with self.server() as fx:
            for method in ("POST", "PUT"):
                authed = _http(
                    method, fx.base + "/v1/moments",
                    headers={"Authorization": "Bearer " + MATRIX_TOKEN},
                )
                self.assertEqual(authed[0], 404, method)
                unauth = _http(method, fx.base + "/v1/moments")
                self.assertEqual(unauth[0], 401, method)

    def test_non_moments_routes_keep_caller_auth(self):
        """其它接口仍走 §1.2.2：无凭据 401 auth.missing，不触发用户校验。"""
        with self.server(auth_token=GATEWAY_TOKEN, dev_mode=False) as fx:
            status, body = _http("GET", fx.base + "/v1/limits")
            self.assertEqual(status, 401)
            self.assertEqual(body["code"], "auth.missing")
            self.assertEqual(self.hs.requests, [])
            # 有效网关 token 走 caller/legacy 鉴权仍然可用。
            ok_status, _ = _http(
                "GET", fx.base + "/v1/limits",
                headers={"Authorization": "Bearer " + GATEWAY_TOKEN},
            )
            self.assertEqual(ok_status, 200)
            # 且全程没有触发 homeserver 用户校验。
            self.assertEqual(self.hs.requests, [])
