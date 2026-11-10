# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""#124 PR-D 验收测试：调用方身份（GATEWAY_CALLER_TOKENS / CallerAuthenticator）。

运行：PYTHONPATH=gateway/src python3 -m unittest gateway.tests.test_124_caller_identity -v
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

GATEWAY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(GATEWAY_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from botville_gateway.caller_identity import (  # noqa: E402
    AUTH_INVALID,
    AUTH_MISSING,
    AUTH_OK,
    AUTH_PASS_THROUGH,
    CallerAuthenticator,
    log_startup_warnings,
)
from botville_gateway.config import (  # noqa: E402
    CallerToken,
    Config,
    ConfigError,
    parse_caller_tokens,
)
from botville_gateway.http_server import build_server  # noqa: E402
from botville_gateway.isolation import PersonaRecord  # noqa: E402

# ── 假凭据（所有值均 ≥32 字节，不含真实密钥） ─────────────────────────────
TOKEN_A = "A" * 32          # 32 bytes，恰好在强度下限
TOKEN_B = "B" * 32
TOKEN_LEGACY = "L" * 32
_HMAC_HEX = "ab" * 32       # 64 hex chars = 32 bytes dev hmac_secret
_HMAC_BYTES = bytes.fromhex(_HMAC_HEX)


def _base_cfg(**kw) -> Config:
    """测试用 Config 基础构造（端口 0、dev 模式）。"""
    base = dict(
        host="127.0.0.1",
        port=0,
        dev_mode=True,
        rate_limit_messages=60,
        silent_hours_enabled=False,
        auth_token=None,
        hmac_secret=_HMAC_BYTES,
    )
    base.update(kw)
    return Config(**base)


# ── HTTP helper ───────────────────────────────────────────────────────────────

def _http(method, url, body=None, headers=None, timeout=5):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, json.loads(raw) if raw else None


class _ServerFixture:
    """后台线程里起 GatewayServer（端口 0 = 随机空闲端口）。"""

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


# ═══════════════════════════════════════════════════════════════════════
# 1. parse_caller_tokens 解析
# ═══════════════════════════════════════════════════════════════════════

class TestParseCallerTokens(unittest.TestCase):

    def test_semicolon_and_newline_mixed(self):
        raw = f"callerA:{TOKEN_A}:ownerA\ncallerB:{TOKEN_B}:ownerB;callerC:{'C'*32}:ownerC"
        result = parse_caller_tokens(raw)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0].caller_id, "callerA")
        self.assertEqual(result[1].caller_id, "callerB")
        self.assertEqual(result[2].caller_id, "callerC")

    def test_whitespace_trimmed(self):
        raw = f"  callerA  :  {TOKEN_A}  :  ownerA  "
        result = parse_caller_tokens(raw)
        self.assertEqual(result[0].caller_id, "callerA")
        self.assertEqual(result[0].owner_id, "ownerA")

    def test_empty_entries_ignored(self):
        raw = f"callerA:{TOKEN_A}:ownerA;;\n\n  ;callerB:{TOKEN_B}:ownerB"
        result = parse_caller_tokens(raw)
        self.assertEqual([e.caller_id for e in result], ["callerA", "callerB"])

    def test_missing_field_two_segments_raises(self):
        with self.assertRaises(ConfigError) as cm:
            parse_caller_tokens(f"callerA:{TOKEN_A}")
        self.assertIn("3", str(cm.exception))

    def test_four_segments_raises(self):
        with self.assertRaises(ConfigError):
            parse_caller_tokens(f"callerA:{TOKEN_A}:ownerA:extra")

    def test_empty_field_raises(self):
        with self.assertRaises(ConfigError):
            parse_caller_tokens(f"callerA::{TOKEN_A}")

    def test_field_containing_colon_raises(self):
        # token 内含 ':' → 切出 4 段
        with self.assertRaises(ConfigError):
            parse_caller_tokens(f"callerA:tok:part:ownerA")

    def test_field_containing_newline_splits_entries(self):
        # 换行是条目分隔符，字段里藏换行 → 会被切开，必然字段数不对
        with self.assertRaises(ConfigError):
            parse_caller_tokens(f"callerA:{TOKEN_A}\nownerA:callerB:{TOKEN_B}:ownerB")

    def test_empty_raw_returns_empty(self):
        self.assertEqual(parse_caller_tokens(""), ())


# ═══════════════════════════════════════════════════════════════════════
# 2. Config 校验（强度 / 重复 / 与 legacy 冲突 / legacy 关闭）
# ═══════════════════════════════════════════════════════════════════════

def _tokens(*entries) -> tuple:
    return tuple(
        CallerToken(caller_id=c, token=t, owner_id=o) for c, t, o in entries
    )


class TestConfigCallerTokenValidation(unittest.TestCase):

    def test_short_token_non_dev_rejected(self):
        with self.assertRaises(ConfigError) as cm:
            _base_cfg(
                dev_mode=False,
                auth_token=TOKEN_LEGACY,
                caller_tokens=_tokens(("callerA", "short-token", "ownerA")),
            )
        self.assertIn("32", str(cm.exception))
        # 错误信息不含 token 明文
        self.assertNotIn("short-token", str(cm.exception))

    def test_short_token_dev_warns_only(self):
        with self.assertLogs("botville_gateway.config", level="WARNING") as logs:
            _base_cfg(caller_tokens=_tokens(("callerA", "short-token", "ownerA")))
        joined = "\n".join(logs.output)
        self.assertIn("shorter than 32 bytes", joined)
        self.assertNotIn("short-token", joined)

    def test_duplicate_token_non_dev_rejected(self):
        with self.assertRaises(ConfigError) as cm:
            _base_cfg(
                dev_mode=False,
                auth_token=TOKEN_LEGACY,
                caller_tokens=_tokens(
                    ("callerA", TOKEN_A, "ownerA"),
                    ("callerB", TOKEN_A, "ownerB"),
                ),
            )
        self.assertIn("reuses the token", str(cm.exception))
        self.assertNotIn(TOKEN_A, str(cm.exception))

    def test_duplicate_token_dev_warns_only(self):
        with self.assertLogs("botville_gateway.config", level="WARNING") as logs:
            _base_cfg(
                caller_tokens=_tokens(
                    ("callerA", TOKEN_A, "ownerA"),
                    ("callerB", TOKEN_A, "ownerB"),
                )
            )
        self.assertIn("reuses the token", "\n".join(logs.output))

    def test_duplicate_caller_id_rejected_even_in_dev(self):
        with self.assertRaises(ConfigError):
            _base_cfg(
                caller_tokens=_tokens(
                    ("callerA", TOKEN_A, "ownerA"),
                    ("callerA", TOKEN_B, "ownerB"),
                )
            )

    def test_caller_token_equal_to_legacy_rejected_even_in_dev(self):
        """§1.2.2：同一值不能既是 caller token 又是站点级 token（dev 也拒绝）。"""
        with self.assertRaises(ConfigError) as cm:
            _base_cfg(
                dev_mode=True,
                auth_token=TOKEN_A,
                caller_tokens=_tokens(("callerA", TOKEN_A, "ownerA")),
            )
        self.assertIn("same token value as GATEWAY_AUTH_TOKEN", str(cm.exception))
        self.assertNotIn(TOKEN_A, str(cm.exception))

    def test_legacy_disabled_does_not_require_auth_token(self):
        """legacy 关闭时不要求 GATEWAY_AUTH_TOKEN（非 dev 也不拒绝）。"""
        cfg = _base_cfg(
            dev_mode=False,
            auth_token=None,
            legacy_token_enabled=False,
            caller_tokens=_tokens(("callerA", TOKEN_A, "ownerA")),
        )
        self.assertFalse(cfg.legacy_token_enabled)
        self.assertIsNone(cfg.auth_token)


# ═══════════════════════════════════════════════════════════════════════
# 3. 启动 WARNING（assertLogs；文案不含 token）
# ═══════════════════════════════════════════════════════════════════════

class TestStartupWarnings(unittest.TestCase):

    def test_warning_when_callers_and_legacy_both_enabled(self):
        cfg = _base_cfg(
            auth_token=TOKEN_LEGACY,
            legacy_token_enabled=True,
            caller_tokens=_tokens(("callerA", TOKEN_A, "ownerA")),
        )
        with self.assertLogs("botville_gateway.caller_identity", level="WARNING") as logs:
            log_startup_warnings(cfg)
        joined = "\n".join(logs.output)
        self.assertIn("GATEWAY_LEGACY_TOKEN_ENABLED is still true", joined)
        # 任何 token 明文都不得出现在日志里
        for secret in (TOKEN_A, TOKEN_LEGACY):
            self.assertNotIn(secret, joined)

    def test_warning_when_legacy_disabled_and_no_callers(self):
        cfg = _base_cfg(
            auth_token=None,
            legacy_token_enabled=False,
            caller_tokens=(),
        )
        with self.assertLogs("botville_gateway.caller_identity", level="WARNING") as logs:
            log_startup_warnings(cfg)
        self.assertIn("no credential is configured", "\n".join(logs.output))

    def test_no_warning_when_caller_only(self):
        """只配 caller token 且关掉 legacy：没有任何 WARNING。"""
        import logging
        cfg = _base_cfg(
            auth_token=None,
            legacy_token_enabled=False,
            caller_tokens=_tokens(("callerA", TOKEN_A, "ownerA")),
        )
        logger = logging.getLogger("botville_gateway.caller_identity")
        old_level = logger.level
        logger.setLevel(logging.WARNING)
        handler = logging.handlers.MemoryHandler(capacity=100, flushLevel=logging.CRITICAL)
        logger.addHandler(handler)
        try:
            log_startup_warnings(cfg)
            warnings = [r for r in handler.buffer if r.levelno >= logging.WARNING]
            self.assertEqual(warnings, [], "Expected no WARNINGs but got: " +
                             ", ".join(r.getMessage() for r in warnings))
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)


# ═══════════════════════════════════════════════════════════════════════
# 4. CallerAuthenticator：结果码 + 「先 HMAC 再全量 compare_digest」
# ═══════════════════════════════════════════════════════════════════════

class TestCallerAuthenticator(unittest.TestCase):

    def _auth(self, **kw) -> CallerAuthenticator:
        return CallerAuthenticator(_base_cfg(**kw))

    def test_caller_token_resolves_owner(self):
        auth = self._auth(caller_tokens=_tokens(("callerA", TOKEN_A, "ownerA")))
        outcome, ident = auth.authenticate(f"Bearer {TOKEN_A}")
        self.assertEqual(outcome, AUTH_OK)
        self.assertEqual(ident.caller_id, "callerA")
        self.assertEqual(ident.owner_id, "ownerA")
        self.assertFalse(ident.legacy)

    def test_wrong_token_invalid(self):
        auth = self._auth(caller_tokens=_tokens(("callerA", TOKEN_A, "ownerA")))
        outcome, ident = auth.authenticate(f"Bearer {'Z' * 32}")
        self.assertEqual(outcome, AUTH_INVALID)
        self.assertIsNone(ident.owner_id)

    def test_missing_header(self):
        auth = self._auth(caller_tokens=_tokens(("callerA", TOKEN_A, "ownerA")))
        self.assertEqual(auth.authenticate(None)[0], AUTH_MISSING)
        self.assertEqual(auth.authenticate("")[0], AUTH_MISSING)
        self.assertEqual(auth.authenticate("Basic xyz")[0], AUTH_MISSING)

    def test_legacy_token_gives_no_owner(self):
        auth = self._auth(auth_token=TOKEN_LEGACY, legacy_token_enabled=True)
        outcome, ident = auth.authenticate(f"Bearer {TOKEN_LEGACY}")
        self.assertEqual(outcome, AUTH_OK)
        self.assertTrue(ident.legacy)
        self.assertIsNone(ident.owner_id)

    def test_legacy_disabled_rejects_legacy_token(self):
        auth = self._auth(
            auth_token=TOKEN_LEGACY,
            legacy_token_enabled=False,
            caller_tokens=_tokens(("callerA", TOKEN_A, "ownerA")),
        )
        self.assertEqual(auth.authenticate(f"Bearer {TOKEN_LEGACY}")[0], AUTH_INVALID)

    def test_dev_pass_through_when_nothing_configured(self):
        auth = self._auth(dev_mode=True, auth_token=None, legacy_token_enabled=True)
        self.assertEqual(auth.authenticate(None)[0], AUTH_PASS_THROUGH)

    def test_compare_digest_before_length_and_full_sweep(self):
        """每次比较的两个参数都是等长 32 字节摘要；调用次数 = caller 数 + legacy(1)。

        证明：比较的是 HMAC 后的等长摘要（不泄漏 token 长度），且遍历全部条目
        不提前返回（命中位置不影响耗时/次数）。
        """
        import botville_gateway.caller_identity as ci

        real = ci.hmac.compare_digest
        lengths = []

        def spy(a, b):
            lengths.append((len(a), len(b)))
            return real(a, b)

        caller_tokens = _tokens(
            ("callerA", TOKEN_A, "ownerA"),
            ("callerB", TOKEN_B, "ownerB"),
        )
        auth = self._auth(
            auth_token=TOKEN_LEGACY,
            legacy_token_enabled=True,
            caller_tokens=caller_tokens,
        )
        with patch.object(ci.hmac, "compare_digest", side_effect=spy):
            outcome, ident = auth.authenticate(f"Bearer {TOKEN_B}")

        self.assertEqual(outcome, AUTH_OK)
        self.assertEqual(ident.caller_id, "callerB")
        # 2 caller + 1 legacy = 3 次，即使第一条就命中也不提前返回
        self.assertEqual(len(lengths), len(caller_tokens) + 1)
        for got in lengths:
            self.assertEqual(got, (32, 32))

    def test_compare_digest_count_without_legacy(self):
        import botville_gateway.caller_identity as ci

        real = ci.hmac.compare_digest
        calls = []

        def spy(a, b):
            calls.append(1)
            return real(a, b)

        caller_tokens = _tokens(
            ("callerA", TOKEN_A, "ownerA"),
            ("callerB", TOKEN_B, "ownerB"),
        )
        auth = self._auth(
            dev_mode=False,
            auth_token=None,
            legacy_token_enabled=False,
            caller_tokens=caller_tokens,
        )
        with patch.object(ci.hmac, "compare_digest", side_effect=spy):
            auth.authenticate(f"Bearer {TOKEN_A}")
        self.assertEqual(len(calls), len(caller_tokens))


# ═══════════════════════════════════════════════════════════════════════
# 5. HTTP 端到端：caller token → owner_id → persona 区（§1.2.2 / §7.2）
# ═══════════════════════════════════════════════════════════════════════

PERSONA = "persona_124"
OWNER_A = "owner_124_a"
OWNER_B = "owner_124_b"
DISPLAY_NAME = "Alice-from-owner-A"


def _assemble_body(persona_id: str = PERSONA) -> dict:
    return {
        "persona_id": persona_id,
        "items": [{"kind": "external", "text": "hi"}],
    }


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestCallerIdentityOverHttp(unittest.TestCase):

    def _cfg(self, **kw) -> Config:
        base = dict(
            auth_token=TOKEN_LEGACY,
            legacy_token_enabled=False,
            caller_tokens=_tokens(
                ("callerA", TOKEN_A, OWNER_A),
                ("callerB", TOKEN_B, OWNER_B),
            ),
        )
        base.update(kw)
        return _base_cfg(**base)

    def test_owner_gets_persona_zone_other_owner_does_not(self):
        with _ServerFixture(self._cfg()) as fx:
            fx.server.service.persona_store.register(
                PersonaRecord(
                    persona_id=PERSONA,
                    owner_id=OWNER_A,
                    display_name=DISPLAY_NAME,
                    bio="bio",
                    personality="friendly",
                )
            )
            url = f"{fx.base}/v1/context/assemble"

            # owner A：caller token 推出 owner → persona 区填充
            s_a, body_a = _http("POST", url, _assemble_body(), _bearer(TOKEN_A))
            # owner B：同 persona 但 owner 不符 → 区为空，状态码仍 200
            s_b, body_b = _http("POST", url, _assemble_body(), _bearer(TOKEN_B))

        self.assertEqual(s_a, 200)
        self.assertIn(DISPLAY_NAME, body_a["zones"]["persona"])
        self.assertEqual(s_b, 200)
        self.assertEqual(body_b["zones"]["persona"], "")
        self.assertEqual(body_a["outcome"], "allowed")

    def test_legacy_token_persona_empty_then_rejected_after_disable(self):
        """legacy 开启时通过但推不出 owner（区为空）；关闭后同一 token → 401。"""
        cfg_legacy_on = self._cfg(auth_token=TOKEN_LEGACY, legacy_token_enabled=True)
        with _ServerFixture(cfg_legacy_on) as fx:
            fx.server.service.persona_store.register(
                PersonaRecord(persona_id=PERSONA, owner_id=OWNER_A, display_name=DISPLAY_NAME)
            )
            url = f"{fx.base}/v1/context/assemble"
            status, body = _http("POST", url, _assemble_body(), _bearer(TOKEN_LEGACY))
        self.assertEqual(status, 200)
        self.assertEqual(body["zones"]["persona"], "")

        cfg_legacy_off = self._cfg(auth_token=TOKEN_LEGACY, legacy_token_enabled=False)
        with _ServerFixture(cfg_legacy_off) as fx:
            status, body = _http(
                "POST", f"{fx.base}/v1/context/assemble",
                _assemble_body(), _bearer(TOKEN_LEGACY),
            )
        self.assertEqual(status, 401)
        self.assertEqual(body["code"], "auth.invalid_token")

    def test_wrong_and_missing_token_rejected(self):
        with _ServerFixture(self._cfg()) as fx:
            url = f"{fx.base}/v1/context/assemble"
            s_wrong, b_wrong = _http("POST", url, _assemble_body(), _bearer("Z" * 32))
            s_none, b_none = _http("POST", url, _assemble_body())
        self.assertEqual(s_wrong, 401)
        self.assertEqual(b_wrong["code"], "auth.invalid_token")
        self.assertEqual(s_none, 401)
        self.assertEqual(b_none["code"], "auth.missing")
