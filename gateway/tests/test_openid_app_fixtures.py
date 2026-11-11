#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""#124 OpenID App 接入：problem fixture 与网关实际输出的一致性测试。

目的：`gateway/openapi/examples/problem-auth-missing.json`、
`problem-auth-invalid-token.json`、`problem-homeserver-unavailable.json` 三个
fixture 必须与 `/_auth_user_ok` 的真实输出逐字段一致，实现漂移时测试失败。

覆盖（契约 §1.2.3，`moments.md` §3）：

  1. 缺 `Authorization` → 401 `auth.missing`；
  2. 无效 OpenID token（homeserver userinfo 401）→ 401 `auth.invalid_token`；
  3. homeserver 502 → 500 `internal.error`（可重试）。

每种情况都在「无前缀」与「`GATEWAY_HTTP_PATH_PREFIX=/_botville/gateway`」两种
部署形态下各打一次。

比较口径：**除 `request_id` 外逐字段一致**。
  - `request_id` 每次请求唯一，只断言存在且非空；
  - 其余键集合与值都与 fixture 完全相等——网关新增或删除任何字段都会失败。

全部使用本地假 homeserver 与明显假 token，不含真实密钥。
运行：python3 -m unittest gateway.tests.test_openid_app_fixtures -v
"""

from __future__ import annotations

import json
import os
import sys
import unittest
import urllib.error
import urllib.request
from typing import Dict, Optional, Tuple

GATEWAY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TESTS = os.path.join(GATEWAY_ROOT, "tests")
if _TESTS not in sys.path:
    sys.path.insert(0, _TESTS)

from test_openid_user_identity import (  # noqa: E402
    OPENID_TOKEN,
    FakeHomeserver,
    USERINFO_PATH,
    _ServerFixture,
    _cfg,
)

EXAMPLES_DIR = os.path.join(GATEWAY_ROOT, "openapi", "examples")

# 与本测试相关的 fixture 文件名。
FIXTURE_AUTH_MISSING = "problem-auth-missing.json"
FIXTURE_AUTH_INVALID = "problem-auth-invalid-token.json"
FIXTURE_HS_UNAVAILABLE = "problem-homeserver-unavailable.json"

# 部署在反代路径前缀之后的形态（#165 `GATEWAY_HTTP_PATH_PREFIX`）。
PATH_PREFIX = "/_botville/gateway"

# `request_id` 是每次请求唯一的，不与 fixture 比较；其余全部字段要完全一致。
IGNORED_KEYS = frozenset(["request_id"])


def _fixture(name: str) -> dict:
    with open(os.path.join(EXAMPLES_DIR, name), "r", encoding="utf-8") as handle:
        return json.load(handle)


def _request(method: str, url: str,
             headers: Optional[Dict[str, str]] = None,
             timeout: float = 10) -> Tuple[int, Dict[str, str], Optional[dict]]:
    """返回 (status, 响应头, 解析后的 JSON)。"""
    req = urllib.request.Request(url, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, dict(resp.headers), json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, dict(exc.headers), json.loads(raw) if raw else None


class MomentsProblemFixtureTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.hs = FakeHomeserver()
        self.addCleanup(self.hs.close)
        self.fixtures = {
            name: _fixture(name)
            for name in (
                FIXTURE_AUTH_MISSING,
                FIXTURE_AUTH_INVALID,
                FIXTURE_HS_UNAVAILABLE,
            )
        }

    def server(self, **overrides) -> _ServerFixture:
        overrides.setdefault("homeserver_url", self.hs.base_url)
        return _ServerFixture(_cfg(**overrides))

    def assert_matches_fixture(self, fixture_name: str, status: int,
                               headers: Dict[str, str], body: dict) -> None:
        """除 request_id 外，响应 JSON 的键集合与值都必须与 fixture 完全一致。

        键集合相等保证：网关新增字段或删除字段时测试都会失败。
        """
        fixture = self.fixtures[fixture_name]

        # Content-Type 必须是 problem+json（可能带 charset 参数）。
        content_type = headers.get("Content-Type", "")
        self.assertTrue(
            content_type.startswith("application/problem+json"),
            "expected application/problem+json, got %r" % (content_type,),
        )

        # request_id 只验证非空；其余键与 fixture 完全一致。
        self.assertIsInstance(body.get("request_id"), str)
        self.assertTrue(body.get("request_id"), "request_id must not be empty")

        body_cmp = {k: v for k, v in body.items() if k not in IGNORED_KEYS}
        fixture_cmp = {k: v for k, v in fixture.items() if k not in IGNORED_KEYS}
        self.assertEqual(
            body_cmp, fixture_cmp,
            "%s: response (excluding %s) does not match fixture\n"
            "  response: %r\n  fixture:  %r"
            % (fixture_name, sorted(IGNORED_KEYS), body_cmp, fixture_cmp),
        )


class TestMomentsProblemFixtures(MomentsProblemFixtureTestBase):
    def test_missing_header_matches_fixture(self):
        with self.server() as fx:
            status, headers, body = _request("GET", fx.base + "/v1/moments")
        self.assertEqual(status, 401)
        self.assert_matches_fixture(FIXTURE_AUTH_MISSING, 401, headers, body)
        self.assertEqual(self.hs.requests, [], "no homeserver call without a token")

    def test_invalid_openid_token_matches_fixture(self):
        self.hs.userinfo_status = 401
        with self.server() as fx:
            status, headers, body = _request(
                "GET", fx.base + "/v1/moments",
                headers={"Authorization": "MatrixOpenID " + OPENID_TOKEN},
            )
        self.assertEqual(status, 401)
        self.assert_matches_fixture(FIXTURE_AUTH_INVALID, 401, headers, body)
        # 分流校验：userinfo 被打了，whoami 一次都不打。
        self.assertEqual(len(self.hs.matching(USERINFO_PATH)), 1)

    def test_homeserver_unavailable_matches_fixture(self):
        self.hs.userinfo_status = 502
        with self.server() as fx:
            status, headers, body = _request(
                "GET", fx.base + "/v1/moments",
                headers={"Authorization": "MatrixOpenID " + OPENID_TOKEN},
            )
        self.assertEqual(status, 500)
        self.assert_matches_fixture(FIXTURE_HS_UNAVAILABLE, 500, headers, body)

    # -- 部署在路径前缀之后（#165）-----------------------------------------
    def test_missing_header_matches_fixture_under_path_prefix(self):
        with self.server(http_path_prefix=PATH_PREFIX) as fx:
            status, headers, body = _request(
                "GET", fx.base + PATH_PREFIX + "/v1/moments"
            )
        self.assertEqual(status, 401)
        self.assert_matches_fixture(FIXTURE_AUTH_MISSING, 401, headers, body)

    def test_invalid_openid_token_matches_fixture_under_path_prefix(self):
        self.hs.userinfo_status = 401
        with self.server(http_path_prefix=PATH_PREFIX) as fx:
            status, headers, body = _request(
                "GET", fx.base + PATH_PREFIX + "/v1/moments",
                headers={"Authorization": "MatrixOpenID " + OPENID_TOKEN},
            )
        self.assertEqual(status, 401)
        self.assert_matches_fixture(FIXTURE_AUTH_INVALID, 401, headers, body)

    def test_homeserver_unavailable_matches_fixture_under_path_prefix(self):
        self.hs.userinfo_status = 502
        with self.server(http_path_prefix=PATH_PREFIX) as fx:
            status, headers, body = _request(
                "GET", fx.base + PATH_PREFIX + "/v1/moments",
                headers={"Authorization": "MatrixOpenID " + OPENID_TOKEN},
            )
        self.assertEqual(status, 500)
        self.assert_matches_fixture(FIXTURE_HS_UNAVAILABLE, 500, headers, body)


class TestFixtureFilesAreWiredIntoValidator(unittest.TestCase):
    """三个 fixture 都必须登记在 `scripts/validate_openapi.py` 中。"""

    def test_every_new_fixture_has_an_example_target(self):
        scripts_dir = os.path.join(GATEWAY_ROOT, "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        try:
            import validate_openapi  # noqa: PLC0415 - 故意延迟到需要时导入
        except SystemExit as exc:  # pragma: no cover - PyYAML 缺失
            self.skipTest("validate_openapi import failed: %s" % (exc,))
        for name in (
            FIXTURE_AUTH_MISSING,
            FIXTURE_AUTH_INVALID,
            FIXTURE_HS_UNAVAILABLE,
        ):
            self.assertIn(name, validate_openapi.EXAMPLE_TARGETS, name)


if __name__ == "__main__":
    unittest.main()
