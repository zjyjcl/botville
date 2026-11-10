# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""部署路径前缀（GATEWAY_HTTP_PATH_PREFIX）测试。

覆盖：
- 空前缀：行为与现状完全一致（`/health` 200、`/_botville/gateway/health` 404）；
- 配置前缀：带前缀的 `/health`、`/v1/limits` 可用；不带前缀的 `/health` 仍可用；
- 整段边界匹配：`/_botville/gatewayX/health` 不算带前缀；
- 带前缀时未鉴权仍 401（状态码/错误码不变）；
- 配置归一与校验：尾 `/` 归一、`/` 视为空、非法前缀 ConfigError。

运行：PYTHONPATH=gateway/src python3 -m unittest gateway.tests.test_path_prefix -v
"""

from __future__ import annotations

import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request

GATEWAY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(GATEWAY_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from botville_gateway.config import (  # noqa: E402
    Config,
    ConfigError,
    load_config,
    normalize_http_path_prefix,
)
from botville_gateway.http_server import build_server, strip_path_prefix  # noqa: E402

PREFIX = "/_botville/gateway"
_TOKEN = "test-token-path-prefix-0123456789"


def _cfg(**overrides):
    base = dict(
        host="127.0.0.1",
        port=0,
        dev_mode=True,
        auth_token=None,
        rate_limit_messages=60,
        silent_hours_enabled=False,  # 不受当前真实时刻影响
        hmac_secret=b"test-hmac-secret" * 2,
    )
    base.update(overrides)
    return Config(**base)


class _ServerFixture:
    """在后台线程里起 GatewayServer（端口 0 = 随机空闲端口）。"""

    def __init__(self, config: Config):
        self.server = build_server(config)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @property
    def base(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"


def _http(method, url, body=None, headers=None, timeout=5):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, json.loads(raw) if raw else None


# ---------------------------------------------------------------------------
# 1. strip_path_prefix 纯函数：空前缀 / 整段边界 / 不带前缀
# ---------------------------------------------------------------------------
class TestStripPathPrefix(unittest.TestCase):
    def test_empty_prefix_is_identity(self):
        for path in ("/health", "/v1/limits", PREFIX + "/health", "/"):
            self.assertEqual(strip_path_prefix(path, ""), path)

    def test_strips_exact_prefix_and_segment_boundary(self):
        self.assertEqual(strip_path_prefix(PREFIX + "/health", PREFIX), "/health")
        self.assertEqual(strip_path_prefix(PREFIX + "/v1/limits", PREFIX), "/v1/limits")
        # 前缀本身 → 根路径
        self.assertEqual(strip_path_prefix(PREFIX, PREFIX), "/")
        # 整段边界：多一个字符不算带前缀
        self.assertEqual(
            strip_path_prefix(PREFIX + "X/health", PREFIX), PREFIX + "X/health"
        )
        self.assertEqual(strip_path_prefix("/_botville/gatewayX", PREFIX),
                         "/_botville/gatewayX")

    def test_unprefixed_request_passes_through(self):
        # 兼容已去前缀的 edge / 入口反代
        self.assertEqual(strip_path_prefix("/health", PREFIX), "/health")
        self.assertEqual(strip_path_prefix("/v1/limits", PREFIX), "/v1/limits")

    def test_prefix_longer_than_path(self):
        self.assertEqual(strip_path_prefix("/a", "/a/b/c"), "/a")


# ---------------------------------------------------------------------------
# 2. 配置归一与校验
# ---------------------------------------------------------------------------
class TestPathPrefixConfig(unittest.TestCase):
    def test_default_is_empty(self):
        self.assertEqual(_cfg().http_path_prefix, "")
        self.assertEqual(_cfg(http_path_prefix="/").http_path_prefix, "")

    def test_trailing_slash_normalized(self):
        self.assertEqual(
            _cfg(http_path_prefix="/_botville/gateway/").http_path_prefix,
            PREFIX,
        )
        self.assertEqual(normalize_http_path_prefix(PREFIX + "///"), PREFIX)

    def test_load_config_reads_env_and_defaults_empty(self):
        env = {"GATEWAY_DEV_MODE": "true"}
        self.assertEqual(load_config(env).http_path_prefix, "")
        env = {"GATEWAY_DEV_MODE": "true", "GATEWAY_HTTP_PATH_PREFIX": PREFIX + "/"}
        self.assertEqual(load_config(env).http_path_prefix, PREFIX)

    def test_invalid_prefixes_raise_config_error(self):
        bad = [
            "botville",          # 不以 / 开头
            "/a//b",             # 连续斜杠
            "/a/../b",           # 路径穿越
            "/a?b",              # 查询串
            "/a#b",              # 片段
            "/a%2fb",            # 百分号编码
            "/a b",              # 空白
            "/a\tb",             # 控制字符
            "/a\x00b",
            "/a\x7fb",
        ]
        for value in bad:
            with self.subTest(value=value):
                with self.assertRaises(ConfigError):
                    _cfg(http_path_prefix=value)

    def test_load_config_rejects_invalid_prefix(self):
        with self.assertRaises(ConfigError):
            load_config(
                {"GATEWAY_DEV_MODE": "true", "GATEWAY_HTTP_PATH_PREFIX": "botville"}
            )


# ---------------------------------------------------------------------------
# 3. HTTP 层端到端
# ---------------------------------------------------------------------------
class TestPathPrefixHttp(unittest.TestCase):
    def test_empty_prefix_keeps_current_routing(self):
        with _ServerFixture(_cfg()) as server:
            ok, _ = _http("GET", f"{server.base}/health")
            missing, _ = _http("GET", f"{server.base}{PREFIX}/health")
        self.assertEqual(ok, 200)
        # 默认配置下前缀路径不存在：404 与现状一致
        self.assertEqual(missing, 404)

    def test_prefixed_requests_route(self):
        cfg = _cfg(auth_token=_TOKEN, http_path_prefix=PREFIX)
        headers = {"Authorization": f"Bearer {_TOKEN}"}
        with _ServerFixture(cfg) as server:
            health, _ = _http("GET", f"{server.base}{PREFIX}/health")
            limits, _ = _http("GET", f"{server.base}{PREFIX}/v1/limits", headers=headers)
            # 不带前缀的请求仍被接受
            plain_health, _ = _http("GET", f"{server.base}/health")
            # 整段边界：`gatewayX` 不是前缀（带合法 token，确认是 404 而不是 401）
            boundary, _ = _http("GET", f"{server.base}{PREFIX}X/health", headers=headers)
            boundary_limits, _ = _http(
                "GET", f"{server.base}{PREFIX}X/v1/limits", headers=headers
            )
        self.assertEqual(health, 200)
        self.assertEqual(limits, 200)
        self.assertEqual(plain_health, 200)
        self.assertEqual(boundary, 404)
        self.assertEqual(boundary_limits, 404)

    def test_prefixed_query_string_unaffected(self):
        headers = {"Authorization": f"Bearer {_TOKEN}"}
        cfg = _cfg(auth_token=_TOKEN, http_path_prefix=PREFIX)
        with _ServerFixture(cfg) as server:
            status, body = _http(
                "GET",
                f"{server.base}{PREFIX}/v1/limits?persona_id=p_demo",
                headers=headers,
            )
        self.assertEqual(status, 200)
        self.assertIn("effective", body)

    def test_prefixed_request_without_auth_is_401(self):
        cfg = _cfg(auth_token=_TOKEN, http_path_prefix=PREFIX)
        with _ServerFixture(cfg) as server:
            status, body = _http("GET", f"{server.base}{PREFIX}/v1/limits")
        self.assertEqual(status, 401)
        self.assertEqual(body["code"], "auth.missing")

    def test_prefixed_post_routes(self):
        cfg = _cfg(auth_token=_TOKEN, http_path_prefix=PREFIX)
        headers = {"Authorization": f"Bearer {_TOKEN}"}
        with _ServerFixture(cfg) as server:
            status, body = _http(
                "POST",
                f"{server.base}{PREFIX}/v1/policy/evaluate",
                {"persona_id": "p", "conversation_id": "c", "content": "hi"},
                headers=headers,
            )
        self.assertEqual(status, 200)
        self.assertIn(body["decision"], ("allow", "defer", "block"))


if __name__ == "__main__":
    unittest.main()
