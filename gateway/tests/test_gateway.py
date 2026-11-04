# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""端到端测试：超限拦截、静默延后、正常投递 + 幂等 + 密钥扫描。

运行：python3 -m unittest discover -s gateway/tests -v
依赖：仅 Python 标准库（zoneinfo + datetime，Python 3.9+）。
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
import threading
import time
import unittest
import urllib.request
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

GATEWAY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 让 `python3 -m unittest discover -s gateway/tests` 无需安装即可导入包。
_SRC = os.path.join(GATEWAY_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from botville_gateway.config import Config, ConfigError, load_config  # noqa: E402
from botville_gateway.http_server import MAX_BODY_BYTES, build_server  # noqa: E402
from botville_gateway.matrix import DeliveryError, InMemoryMatrixTransport  # noqa: E402
from botville_gateway.matrix_appservice import AppServiceMatrixTransport  # noqa: E402
from botville_gateway.service import (  # noqa: E402
    GatewayService,
    IdempotencyConflict,
    ValidationError,
)
from botville_gateway.store import EventKind, UsageEvent, UsageStore  # noqa: E402
from botville_gateway.policy import PolicyEngine, estimate_tokens  # noqa: E402


def _cfg(**overrides):
    base = dict(
        dev_mode=True,  # 单元测试不打 token；见 TestAuthHardening 的 fail-closed 测试
        rate_limit_messages=3,
        rate_limit_window_seconds=3600,
        silent_hours_enabled=True,
        silent_hours_start="23:00",
        silent_hours_end="07:30",
        silent_hours_timezone="Asia/Shanghai",
        max_message_chars=8000,
    )
    base.update(overrides)
    return Config(**base)


class _FixedClock:
    """UTC 时钟，测试手动推进，避免依赖 wall-clock。"""

    def __init__(self, start_iso: str = "2026-11-03T12:00:00Z"):
        self._now = datetime.datetime.strptime(start_iso, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )

    def __call__(self) -> datetime.datetime:
        return self._now

    def advance(self, **delta) -> None:
        self._now = self._now + timedelta(**delta)


def _make_service(config=None, *, now=None) -> tuple:
    config = config or _cfg()
    now = now or _FixedClock()
    transport = InMemoryMatrixTransport()
    store = UsageStore()
    svc = GatewayService(config, transport, store, now_fn=now)
    return svc, transport, store, now


# ---------------------------------------------------------------------------
# 1. 超限 → outcome=blocked，retryable=false，有记录，transport 未发送
# ---------------------------------------------------------------------------
class TestRateLimitBlocked(unittest.TestCase):
    def test_blocks_after_limit_reached(self):
        cfg = _cfg(rate_limit_messages=2)
        svc, transport, store, now = _make_service(cfg)
        payload = {
            "sender_persona_id": "persona_demo",
            "content": {"type": "text", "text": "hello 0"},
        }
        # 前两条投递
        r1 = svc.send_ai_message("conv_demo", payload)
        self.assertEqual(r1["outcome"], "delivered")
        r2 = svc.send_ai_message("conv_demo", {
            "sender_persona_id": "persona_demo",
            "content": {"type": "text", "text": "hello 1"},
        })
        self.assertEqual(r2["outcome"], "delivered")
        self.assertEqual(len(transport.sent), 2)

        # 第三条超限
        r3 = svc.send_ai_message("conv_demo", {
            "sender_persona_id": "persona_demo",
            "content": {"type": "text", "text": "hello 2"},
        })
        self.assertEqual(r3["outcome"], "blocked")
        self.assertFalse(r3["retryable"])
        self.assertIsNone(r3["retry_after_seconds"])
        self.assertEqual(r3["decision"]["decision"], "block")
        self.assertFalse(r3["decision"]["allowed"])
        self.assertTrue(r3["same_content_allowed"])
        # 额度会恢复，因此 may_clear_at 给出窗口重置时刻（契约把该字段放在回执上，
        # 不在 PolicyDecision 里，所以这里只断言回执层）
        self.assertIsNotNone(r3["may_clear_at"])
        self.assertIn("messages.window", [c["key"] for c in r3["decision"]["checks"]])

        # transport 未被调用：仍只有前两条
        self.assertEqual(len(transport.sent), 2)

        # 有 blocked 审计记录
        blocked = store.blocked_events(persona_id="persona_demo")
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0].kind, EventKind.BLOCKED)
        self.assertEqual(blocked[0].reason_code, "messages.window.exceeded")

    def test_limit_zero_means_unlimited(self):
        cfg = _cfg(rate_limit_messages=0)
        svc, transport, store, now = _make_service(cfg)
        for i in range(5):
            r = svc.send_ai_message("c", {
                "sender_persona_id": "p",
                "content": {"type": "text", "text": f"msg {i}"},
            })
            self.assertEqual(r["outcome"], "delivered")
        self.assertEqual(len(transport.sent), 5)


# ---------------------------------------------------------------------------
# 2. 静默时段 → deferred（带 queue_id），静默结束后可投
# ---------------------------------------------------------------------------
class TestSilentHoursDeferred(unittest.TestCase):
    def test_deferred_with_queue_id(self):
        cfg = _cfg(silent_hours_start="23:00", silent_hours_end="07:30")
        now = _FixedClock("2026-11-03T15:00:00Z")  # 23:00 Asia/Shanghai
        svc, transport, store, now_ = _make_service(cfg, now=now)

        r = svc.send_ai_message("conv_demo", {
            "sender_persona_id": "persona_demo",
            "content": {"type": "text", "text": "晚上好"},
        })
        self.assertEqual(r["outcome"], "deferred")
        self.assertFalse(r["retryable"])
        self.assertIsNotNone(r["queue_id"])
        self.assertTrue(r["queue_id"].startswith("queue_"))
        self.assertEqual(r["decision"]["decision"], "defer")
        # retry_after_seconds 是到静默结束的等待秒数（仅供参考，约 8.5h）
        self.assertGreater(r["retry_after_seconds"], 0)
        self.assertIsNotNone(r["may_clear_at"])

        # transport 没有发送
        self.assertEqual(len(transport.sent), 0)

        # deferred 也进 store
        deferred = [e for e in store.all_events() if e.kind == EventKind.DEFERRED]
        self.assertEqual(len(deferred), 1)
        self.assertEqual(deferred[0].reason_code, "silent_hours.active")

        # 排程项存在
        pending = svc.pending_queue()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].queue_id, r["queue_id"])

    def test_allow_deferred_false_blocks(self):
        cfg = _cfg(silent_hours_start="23:00", silent_hours_end="07:30")
        now = _FixedClock("2026-11-03T15:00:00Z")
        svc, transport, store, now_ = _make_service(cfg, now=now)

        r = svc.send_ai_message("conv_demo", {
            "sender_persona_id": "persona_demo",
            "content": {"type": "text", "text": "晚上好"},
            "allow_deferred": False,
        })
        self.assertEqual(r["outcome"], "blocked")
        self.assertIsNone(r["queue_id"])
        self.assertFalse(r["retryable"])
        # blocked 是静默导致，不是额度，may_clear_at 是静默结束时刻
        self.assertIsNotNone(r["may_clear_at"])
        self.assertEqual(len(transport.sent), 0)

    def test_outside_silent_hours_delivers(self):
        cfg = _cfg(silent_hours_start="23:00", silent_hours_end="07:30")
        now = _FixedClock("2026-11-03T02:00:00Z")  # 10:00 Asia/Shanghai，不在静默
        svc, transport, store, now_ = _make_service(cfg, now=now)
        r = svc.send_ai_message("conv_demo", {
            "sender_persona_id": "persona_demo",
            "content": {"type": "text", "text": "早上好"},
        })
        self.assertEqual(r["outcome"], "delivered")
        self.assertEqual(len(transport.sent), 1)

    def test_disabled_silent_hours_never_defer(self):
        cfg = _cfg(silent_hours_enabled=False)
        now = _FixedClock("2026-11-03T15:00:00Z")  # 本来是静默
        svc, transport, store, now_ = _make_service(cfg, now=now)
        r = svc.send_ai_message("conv_demo", {
            "sender_persona_id": "persona_demo",
            "content": {"type": "text", "text": "晚上好"},
        })
        self.assertEqual(r["outcome"], "delivered")
        self.assertEqual(len(transport.sent), 1)

    def test_cross_midnight_window(self):
        # 02:00 local 落在 [23:00, 07:30) 跨午夜窗口内
        cfg = _cfg(silent_hours_start="23:00", silent_hours_end="07:30")
        # 2026-11-04T18:00:00Z = 02:00 next-day Asia/Shanghai
        now = _FixedClock("2026-11-04T18:00:00Z")
        svc, transport, store, now_ = _make_service(cfg, now=now)
        r = svc.send_ai_message("c", {
            "sender_persona_id": "p",
            "content": {"type": "text", "text": "凌晨好"},
        })
        self.assertEqual(r["outcome"], "deferred")

    def test_zero_length_window_not_silent(self):
        cfg = _cfg(silent_hours_start="12:00", silent_hours_end="12:00")
        # 任意时刻都不静默
        for t in ("2026-11-03T04:00:00Z", "2026-11-03T12:00:00Z", "2026-11-03T20:00:00Z"):
            now = _FixedClock(t)
            svc, transport, store, now_ = _make_service(cfg, now=now)
            r = svc.send_ai_message("c", {
                "sender_persona_id": "p",
                "content": {"type": "text", "text": "x"},
            })
            self.assertEqual(r["outcome"], "delivered", f"unexpected at {t}")


# ---------------------------------------------------------------------------
# 3. 正常时段未超限 → delivered，transport 有消息
# ---------------------------------------------------------------------------
class TestDelivered(unittest.TestCase):
    def test_basic_delivery(self):
        cfg = _cfg(rate_limit_messages=60)
        svc, transport, store, now = _make_service(cfg)
        r = svc.send_ai_message("conv_demo", {
            "sender_persona_id": "persona_demo",
            "content": {"type": "text", "text": "你好"},
        })
        self.assertEqual(r["outcome"], "delivered")
        self.assertFalse(r["retryable"])
        self.assertIsNone(r["queue_id"])
        self.assertIsNotNone(r["message"])
        self.assertEqual(r["message"]["content"]["text"], "你好")
        self.assertEqual(r["message"]["conversation_id"], "conv_demo")
        self.assertEqual(r["message"]["sender"]["kind"], "ai")
        self.assertEqual(r["message"]["trust"], "trusted")
        self.assertEqual(r["message"]["provenance"]["origin"], "ai")
        self.assertFalse(r["message"]["redacted"])
        self.assertIsNotNone(r["delivery"])
        self.assertEqual(r["delivery"]["matrix_event_id"], r["message"]["matrix_event_id"])
        self.assertIn("usage_delta", r)
        self.assertEqual(r["usage_delta"]["town_tokens"], 0)
        self.assertEqual(r["decision"]["decision"], "allow")
        self.assertTrue(r["decision"]["allowed"])
        self.assertEqual(len(transport.sent), 1)
        self.assertEqual(transport.sent[0].room_id, "conv_demo")
        self.assertEqual(transport.sent[0].text, "你好")
        self.assertEqual(transport.sent[0].sender_id, "persona_demo")

    def test_rate_limit_check_reflects_usage(self):
        cfg = _cfg(rate_limit_messages=60)
        svc, transport, store, now = _make_service(cfg)
        svc.send_ai_message("c", {
            "sender_persona_id": "p", "content": {"type": "text", "text": "1"},
        })
        r = svc.send_ai_message("c", {
            "sender_persona_id": "p", "content": {"type": "text", "text": "2"},
        })
        checks = {c["key"]: c for c in r["decision"]["checks"]}
        self.assertIn("messages.window", checks)
        # `used` reflects how many SENT events existed at evaluation time (before
        # this message is recorded): message "1" was recorded, so used=1.
        self.assertEqual(checks["messages.window"]["used"], 1)
        self.assertEqual(checks["messages.window"]["remaining"], 59)
        self.assertTrue(checks["messages.window"]["allowed"])
        # After both sends the store has 2 events.
        self.assertEqual(store.sent_count_since(
            persona_id="p",
            since=now() - datetime.timedelta(hours=2),
        ), 2)

    def test_sliding_window_expires(self):
        """窗口 60s，发满 3 条后前进 61s，窗口滑出旧事件，第 4 条可以投递。"""
        cfg = _cfg(rate_limit_messages=3, rate_limit_window_seconds=60)
        now = _FixedClock("2026-11-03T12:00:00Z")
        svc, transport, store, now_ = _make_service(cfg, now=now)
        for i in range(3):
            svc.send_ai_message("c", {
                "sender_persona_id": "p",
                "content": {"type": "text", "text": f"m{i}"},
            })
        # 第 4 条应被拦截（used=3, limit=3）
        r = svc.send_ai_message("c", {
            "sender_persona_id": "p",
            "content": {"type": "text", "text": "m3"},
        })
        self.assertEqual(r["outcome"], "blocked")
        # 前进 61 秒：3 条旧事件（都在 T+0）滑出 [T+1, T+61] 窗口
        now_.advance(seconds=61)
        r2 = svc.send_ai_message("c", {
            "sender_persona_id": "p",
            "content": {"type": "text", "text": "m4"},
        })
        self.assertEqual(r2["outcome"], "delivered")
        checks = {c["key"]: c for c in r2["decision"]["checks"]}
        # 评估 m4 时，旧 3 条在窗口外 → used=0，恰好又允许 3 条
        self.assertEqual(checks["messages.window"]["used"], 0)
        self.assertEqual(checks["messages.window"]["remaining"], 3)


# ---------------------------------------------------------------------------
# 4. 幂等 / 校验
# ---------------------------------------------------------------------------
class TestIdempotency(unittest.TestCase):
    def test_same_key_same_body_returns_cached(self):
        svc, transport, store, now = _make_service()
        payload = {
            "sender_persona_id": "p",
            "content": {"type": "text", "text": "hello"},
        }
        r1 = svc.send_ai_message("c", payload, idempotency_key="key-1234")
        r2 = svc.send_ai_message("c", payload, idempotency_key="key-1234")
        self.assertEqual(r1, r2)
        self.assertEqual(len(transport.sent), 1)

    def test_same_key_different_body_conflicts(self):
        svc, transport, store, now = _make_service()
        svc.send_ai_message("c", {
            "sender_persona_id": "p",
            "content": {"type": "text", "text": "a"},
        }, idempotency_key="key-5678")
        with self.assertRaises(IdempotencyConflict):
            svc.send_ai_message("c", {
                "sender_persona_id": "p",
                "content": {"type": "text", "text": "b"},
            }, idempotency_key="key-5678")

    def test_validation_missing_persona(self):
        svc, transport, store, now = _make_service()
        with self.assertRaises(ValidationError):
            svc.send_ai_message("c", {"content": {"type": "text", "text": "x"}})

    def test_validation_oversized_text(self):
        cfg = _cfg(max_message_chars=10)
        svc, transport, store, now = _make_service(cfg)
        with self.assertRaises(ValidationError):
            svc.send_ai_message("c", {
                "sender_persona_id": "p",
                "content": {"type": "text", "text": "x" * 11},
            })

    def test_mentions_passed_through(self):
        """B5: content.mentions 原样进入回执 message.content。"""
        svc, transport, store, now = _make_service()
        r = svc.send_ai_message("c", {
            "sender_persona_id": "p",
            "content": {"type": "text", "text": "hi", "mentions": ["@a:example.invalid"]},
        })
        self.assertEqual(r["outcome"], "delivered")
        self.assertEqual(r["message"]["content"]["mentions"], ["@a:example.invalid"])

    def test_reply_to_must_be_string(self):
        """B5: reply_to 非 string → ValidationError（403 前先卡住）。"""
        svc, transport, store, now = _make_service()
        with self.assertRaises(ValidationError):
            svc.send_ai_message("c", {
                "sender_persona_id": "p",
                "content": {"type": "text", "text": "hi"},
                "reply_to": {"evil": 1},
            })

    def test_reply_to_passed_through(self):
        svc, transport, store, now = _make_service()
        r = svc.send_ai_message("c", {
            "sender_persona_id": "p",
            "content": {"type": "text", "text": "hi"},
            "reply_to": "msg_prev",
        })
        self.assertEqual(r["message"]["reply_to"], "msg_prev")
        self.assertEqual(transport.sent[0].reply_to, "msg_prev")

    def test_unknown_send_field_rejected_at_service(self):
        """B5: additionalProperties: false。"""
        svc, transport, store, now = _make_service()
        with self.assertRaises(ValidationError):
            svc.send_ai_message("c", {
                "sender_persona_id": "p",
                "content": {"type": "text", "text": "hi"},
                "bogus": True,
            })

    def test_markdown_content_type_accepted(self):
        svc, transport, store, now = _make_service()
        r = svc.send_ai_message("c", {
            "sender_persona_id": "p",
            "content": {"type": "markdown", "text": "**hi**"},
        })
        self.assertEqual(r["outcome"], "delivered")

    def test_markdown_content_type_preserved(self):
        """#92: content.type=markdown 必须原样到 transport 与回执，不得降级为 text。"""
        svc, transport, store, now = _make_service()
        r = svc.send_ai_message("c", {
            "sender_persona_id": "p",
            "content": {"type": "markdown", "text": "**bold** and `code`"},
            "metadata": {"source": "test"},
        })
        self.assertEqual(r["outcome"], "delivered")
        # 回执：content.type 保持 markdown
        self.assertEqual(r["message"]["content"]["type"], "markdown")
        self.assertEqual(r["message"]["content"]["text"], "**bold** and `code`")
        # transport：类型原样透传
        self.assertEqual(transport.sent[0].content_type, "markdown")
        self.assertEqual(transport.sent[0].text, "**bold** and `code`")

    def test_plain_text_content_type_preserved(self):
        """对照组：#92 的修复不能反过来把 text 改成 markdown。"""
        svc, transport, store, now = _make_service()
        r = svc.send_ai_message("c", {
            "sender_persona_id": "p",
            "content": {"type": "text", "text": "plain"},
        })
        self.assertEqual(r["message"]["content"]["type"], "text")
        self.assertEqual(transport.sent[0].content_type, "text")

    def test_evaluate_policy_rejects_bad_kind(self):
        """B5: PolicyEvaluateRequest.kind enum 校验。"""
        svc, transport, store, now = _make_service()
        with self.assertRaises(ValidationError):
            svc.evaluate_policy({"persona_id": "p", "kind": "bogus"})

    def test_evaluate_policy_rejects_unknown_field(self):
        svc, transport, store, now = _make_service()
        with self.assertRaises(ValidationError):
            svc.evaluate_policy({"persona_id": "p", "bogus": 1})


# ---------------------------------------------------------------------------
# 5. PolicyEngine 直接测试（不经过 transport）
# ---------------------------------------------------------------------------
class TestPolicyEngine(unittest.TestCase):
    def test_block_when_rate_exceeded(self):
        cfg = _cfg(rate_limit_messages=1)
        now = _FixedClock()
        store = UsageStore()
        engine = PolicyEngine(cfg, store, now_fn=now)
        store.append(UsageEvent.make(EventKind.SENT, persona_id="p", text="x"))
        d = engine.evaluate(persona_id="p", text="y", now=now())
        self.assertEqual(d.decision, "block")
        self.assertEqual(d.winner_layer, "platform")
        self.assertTrue(d.same_content_allowed)

    def test_defer_in_silent_window(self):
        cfg = _cfg()
        now = _FixedClock("2026-11-03T15:00:00Z")
        store = UsageStore()
        engine = PolicyEngine(cfg, store, now_fn=now)
        d = engine.evaluate(persona_id="p", text="y", now=now())
        self.assertEqual(d.decision, "defer")
        self.assertGreater(d.retry_after_seconds, 0)

    def test_allow_outside_silent(self):
        cfg = _cfg()
        now = _FixedClock("2026-11-03T02:00:00Z")
        store = UsageStore()
        engine = PolicyEngine(cfg, store, now_fn=now)
        d = engine.evaluate(persona_id="p", text="y", now=now())
        self.assertEqual(d.decision, "allow")
        self.assertTrue(d.allowed)

    def test_rate_block_takes_precedence_over_silent_defer(self):
        """限速超限且处于静默 → block（不是 defer）。"""
        cfg = _cfg(rate_limit_messages=1)
        now = _FixedClock("2026-11-03T15:00:00Z")  # 同时静默
        store = UsageStore()
        engine = PolicyEngine(cfg, store, now_fn=now)
        store.append(UsageEvent.make(EventKind.SENT, persona_id="p", text="x"))
        d = engine.evaluate(persona_id="p", text="y", now=now())
        self.assertEqual(d.decision, "block")
        # 同时报出两个检查项
        codes = [r.code for r in d.reasons]
        self.assertIn("messages.window.exceeded", codes)
        self.assertIn("silent_hours.active", codes)


# ---------------------------------------------------------------------------
# 6. 配置
# ---------------------------------------------------------------------------
class TestConfig(unittest.TestCase):
    def test_defaults(self):
        env = {"GATEWAY_DEV_MODE": "true"}
        cfg = load_config(env)
        self.assertEqual(cfg.rate_limit_messages, 60)
        self.assertEqual(cfg.silent_hours_start, "23:00")
        self.assertTrue(cfg.silent_hours_enabled)
        # 无 token 且非 dev 模式必须拒绝启动
        with self.assertRaises(ConfigError):
            load_config({})

    def test_env_override(self):
        env = {
            "RATE_LIMIT_MESSAGES": "10",
            "SILENT_HOURS_START": "22:00",
            "SILENT_HOURS_END": "08:00",
            "GATEWAY_PORT": "9000",
            "GATEWAY_HMAC_SECRET": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef", "GATEWAY_AUTH_TOKEN": "a-strong-random-token",
        }
        cfg = load_config(env)
        self.assertEqual(cfg.rate_limit_messages, 10)
        self.assertEqual(cfg.silent_hours_start, "22:00")
        self.assertEqual(cfg.port, 9000)
        self.assertEqual(cfg.auth_token, "a-strong-random-token")

    def test_invalid_port_raises(self):
        with self.assertRaises(Exception):
            load_config({"GATEWAY_PORT": "not-a-number"})

    def test_port_out_of_range_raises(self):
        # 建议 9: GATEWAY_PORT 0–65535 校验
        with self.assertRaises(ConfigError):
            load_config({"GATEWAY_PORT": "70000", "GATEWAY_HMAC_SECRET": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef", "GATEWAY_AUTH_TOKEN": "t"})
        with self.assertRaises(ConfigError):
            load_config({"GATEWAY_PORT": "0", "GATEWAY_HMAC_SECRET": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef", "GATEWAY_AUTH_TOKEN": "t"})

    def test_unknown_timezone_raises(self):
        with self.assertRaises(Exception):
            load_config({"SILENT_HOURS_TIMEZONE": "Not/A_Real_Zone", "GATEWAY_HMAC_SECRET": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef", "GATEWAY_AUTH_TOKEN": "t"})

    def test_change_me_token_rejected_without_dev_mode(self):
        """建议7: <change-me> 必须拒绝启动，即使显式设置也当未配置。"""
        with self.assertRaises(ConfigError):
            load_config({"GATEWAY_HMAC_SECRET": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef", "GATEWAY_AUTH_TOKEN": "<change-me>"})
        with self.assertRaises(ConfigError):
            load_config({"GATEWAY_HMAC_SECRET": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef", "GATEWAY_AUTH_TOKEN": ""})

    def test_dev_mode_allows_no_token(self):
        """建议7: 显式 dev 模式可免鉴权启动。"""
        cfg = load_config({"GATEWAY_DEV_MODE": "true"})
        self.assertIsNone(cfg.auth_token)
        self.assertTrue(cfg.dev_mode)

    def test_real_token_overrides_dev_mode(self):
        """设置真实 token 永远优先于 dev 模式。"""
        cfg = load_config({"GATEWAY_DEV_MODE": "true", "GATEWAY_HMAC_SECRET": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef", "GATEWAY_AUTH_TOKEN": "real-token-xyz"})
        self.assertEqual(cfg.auth_token, "real-token-xyz")
        self.assertTrue(cfg.dev_mode)

    def test_dev_mode_rejects_non_loopback_host(self):
        """#92: GATEWAY_DEV_MODE=true 且 GATEWAY_HOST 非 loopback → 拒绝启动。"""
        for host in ("0.0.0.0", "::", "192.168.1.10", "gateway.example.invalid"):
            with self.assertRaises(ConfigError, msg=f"host {host!r} should be rejected"):
                load_config({"GATEWAY_DEV_MODE": "true", "GATEWAY_HOST": host})

    def test_dev_mode_allows_loopback_hosts(self):
        """#92: 三种 loopback 写法都放行。"""
        for host in ("127.0.0.1", "::1", "localhost"):
            cfg = load_config({"GATEWAY_DEV_MODE": "true", "GATEWAY_HOST": host})
            self.assertTrue(cfg.dev_mode)
            self.assertEqual(cfg.host, host)

    def test_non_dev_mode_may_bind_any_host(self):
        """#92: 非 dev 模式（有真实 token）不受 loopback 限制——否则生产无法对外监听。"""
        cfg = load_config(
            {"GATEWAY_HOST": "0.0.0.0", "GATEWAY_HMAC_SECRET": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef", "GATEWAY_AUTH_TOKEN": "a-strong-random-token"}
        )
        self.assertFalse(cfg.dev_mode)
        self.assertEqual(cfg.host, "0.0.0.0")

    def test_dev_mode_non_loopback_exits_2(self):
        """#92: 该 ConfigError 走 `__main__` 的 ConfigError 分支，退出码为 2。"""
        code = subprocess.run(
            [sys.executable, "-m", "botville_gateway"],
            env={
                "PYTHONPATH": _SRC,
                "GATEWAY_DEV_MODE": "true",
                "GATEWAY_HOST": "0.0.0.0",
                "GATEWAY_PORT": "8080",
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        self.assertEqual(code.returncode, 2)
        self.assertIn("config error", code.stderr.decode("utf-8", "replace").lower())


# ---------------------------------------------------------------------------
# 7. 密钥扫描：仓库内无真实 token
# ---------------------------------------------------------------------------
class TestNoRealSecrets(unittest.TestCase):
    def test_no_real_secrets_in_gateway(self):
        # 真实凭据指纹（前缀），出现即失败。占位 <change-me> 与 example.invalid 放行。
        patterns = [
            re.compile(r"sk-[A-Za-z0-9]{20,}"),
            re.compile(r"AKIA[0-9A-Z]{16}"),
            re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
            re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
            re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
        ]
        allow = re.compile(r"<change-me>|example\.invalid|<placeholder>|<your-")
        hits = []
        for dirpath, _, files in os.walk(GATEWAY_ROOT):
            # 跳过 .git 与本测试文件本身
            if ".git" in dirpath:
                continue
            for name in files:
                if name.endswith((".pyc",)) or name == os.path.basename(__file__):
                    continue
                path = os.path.join(dirpath, name)
                try:
                    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                        for lineno, line in enumerate(fh, 1):
                            for pat in patterns:
                                if pat.search(line) and not allow.search(line):
                                    hits.append(f"{path}:{lineno}: {line.strip()[:120]}")
                except (OSError, UnicodeDecodeError):
                    pass
        self.assertFalse(hits, "疑似真实密钥:\n" + "\n".join(hits))


# ---------------------------------------------------------------------------
# 8. ISO UTC 格式（契约要求 date-time 以 Z 结尾）
# ---------------------------------------------------------------------------
class TestIsoFormat(unittest.TestCase):
    def test_iso_utc_ends_with_z(self):
        from botville_gateway.timeutil import iso_utc
        d = datetime.datetime(2026, 11, 3, 12, 30, 45, tzinfo=timezone.utc)
        self.assertEqual(iso_utc(d), "2026-11-03T12:30:45Z")
        # 非 UTC 输入也转 UTC
        d_sh = d.astimezone(ZoneInfo("Asia/Shanghai"))
        self.assertEqual(iso_utc(d_sh), "2026-11-03T12:30:45Z")


# ---------------------------------------------------------------------------
# 9. HTTP 层端到端：真起一个 stdlib 服务器，走 socket 请求
# ---------------------------------------------------------------------------
class _ServerFixture:
    """在后台线程里起 GatewayServer（端口 0 = 随机空闲端口）。"""

    def __init__(self, config: Config, transport=None):
        self.server = build_server(config, transport)
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


class _FailingTransport(InMemoryMatrixTransport):
    """按给定 code/detail 抛 DeliveryError 的内存传输（4xx/5xx 映射测试用）。"""

    def __init__(self, code: str, detail: str):
        super().__init__()
        self._code = code
        self._detail = detail

    def send_text(self, **kwargs):
        raise DeliveryError(self._code, self._detail)


def _http(method, url, body=None, headers=None, timeout=5):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
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


class TestHttpServer(unittest.TestCase):
    def _cfg(self, **overrides):
        base = dict(
            host="127.0.0.1",
            port=0,
            dev_mode=True,
            rate_limit_messages=60,
            silent_hours_enabled=False,  # 让 HTTP 测试不受当前真实时刻影响
            auth_token=None,
            hmac_secret=b"test-hmac-secret" * 2,
        )
        base.update(overrides)
        return Config(**base)

    def test_health_contract_shape(self):
        with _ServerFixture(self._cfg()) as server:
            status, body = _http("GET", f"{server.base}/health")
        self.assertEqual(status, 200)
        for field in ("status", "version", "contract_version", "uptime_seconds", "checks", "modules"):
            self.assertIn(field, body)
        self.assertEqual(body["status"], "ok")

    def test_send_and_block_over_http(self):
        with _ServerFixture(self._cfg(rate_limit_messages=1)) as fixture:
            url = f"{fixture.base}/v1/conversations/conv_demo/messages"
            s1, r1 = _http("POST", url, {
                "sender_persona_id": "p",
                "content": {"type": "text", "text": "first"},
            })
            s2, r2 = _http("POST", url, {
                "sender_persona_id": "p",
                "content": {"type": "text", "text": "second"},
            })
            transport_count = len(fixture.server.transport.sent)
        self.assertEqual(s1, 200)
        self.assertEqual(r1["outcome"], "delivered")
        self.assertEqual(transport_count, 1)
        self.assertEqual(s2, 200)
        self.assertEqual(r2["outcome"], "blocked")
        self.assertFalse(r2["retryable"])
        self.assertIsNone(r2["retry_after_seconds"])
        self.assertTrue(r2["same_content_allowed"])
        codes = [x["code"] for x in r2["decision"]["reasons"]]
        self.assertIn("messages.window.exceeded", codes)

    def test_policy_evaluate_over_http(self):
        with _ServerFixture(self._cfg()) as server:
            status, body = _http(
                "POST", f"{server.base}/v1/policy/evaluate",
                {"persona_id": "p", "conversation_id": "c", "content": "hi"},
            )
        self.assertEqual(status, 200)
        self.assertIn(body["decision"], ("allow", "defer", "block"))
        self.assertIn("checks", body)
        # 预检不投递
        self.assertNotIn("outcome", body)

    def test_validation_error_is_problem_json(self):
        with _ServerFixture(self._cfg()) as server:
            status, body = _http(
                "POST", f"{server.base}/v1/conversations/c/messages",
                {"content": {"type": "text", "text": "no sender"}},
            )
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "request.invalid")
        self.assertTrue(body["type"].startswith("urn:botville:error:"))
        self.assertIn("request_id", body)

    def test_idempotency_conflict_is_409(self):
        with _ServerFixture(self._cfg()) as server:
            url = f"{server.base}/v1/conversations/c/messages"
            headers = {"Idempotency-Key": "same-key-0001"}
            s1, _ = _http("POST", url, {
                "sender_persona_id": "p", "content": {"type": "text", "text": "a"},
            }, headers=headers)
            s2, body = _http("POST", url, {
                "sender_persona_id": "p", "content": {"type": "text", "text": "b"},
            }, headers=headers)
        self.assertEqual(s1, 200)
        self.assertEqual(s2, 409)
        self.assertEqual(body["code"], "idempotency.conflict")

    def test_transport_failure_is_500(self):
        """B6: 传输失败映射为 500 internal.error（非契约 502）。"""
        with _ServerFixture(self._cfg()) as fixture:
            fixture.server.transport.fail = True
            status, body = _http(
                "POST", f"{fixture.base}/v1/conversations/c/messages",
                {"sender_persona_id": "p", "content": {"type": "text", "text": "x"}},
            )
            # 投递失败不占名额
            store_count = len(fixture.server.service.store)
        self.assertEqual(status, 500)
        self.assertEqual(body["code"], "internal.error")
        self.assertEqual(store_count, 0)
        # X-Request-Id 在每个响应上（含 5xx）
        self.assertIn("request_id", body)

    def test_auth_enforced_when_token_set(self):
        """B2: 无效 token 返回 401 auth.invalid_token（非 403）；每个响应带 X-Request-Id。"""
        with _ServerFixture(self._cfg(dev_mode=False, auth_token="strongtoken12345")) as server:
            url = f"{server.base}/v1/conversations/c/messages"
            payload = {"sender_persona_id": "p", "content": {"type": "text", "text": "x"}}
            s_missing, b_missing = _http("POST", url, payload)
            s_wrong, b_wrong = _http("POST", url, payload, headers={"Authorization": "Bearer nope"})
            s_ok, body = _http(
                "POST", url, payload, headers={"Authorization": "Bearer strongtoken12345"}
            )
            # /health 免鉴权
            s_health, _ = _http("GET", f"{server.base}/health")
        # 缺 Authorization → 401 auth.missing
        self.assertEqual(s_missing, 401)
        self.assertEqual(b_missing["code"], "auth.missing")
        self.assertIn("request_id", b_missing)
        # 错误 token → 401 auth.invalid_token（不是 403）
        self.assertEqual(s_wrong, 401)
        self.assertEqual(b_wrong["code"], "auth.invalid_token")
        self.assertIn("request_id", b_wrong)
        # 正确 token → 200
        self.assertEqual(s_ok, 200)
        self.assertEqual(body["outcome"], "delivered")
        # /health 不需鉴权
        self.assertEqual(s_health, 200)


    def test_health_503_when_degraded(self):
        """B3: status != ok 时返回 503（结构相同），且不伪造 database: ok。"""
        with _ServerFixture(self._cfg()) as fixture:
            fixture.server.transport.fail = True
            status, body = _http("GET", f"{fixture.base}/health")
        self.assertEqual(status, 503)
        self.assertEqual(body["status"], "degraded")
        names = [c["name"] for c in body["checks"]]
        # 未接入的依赖不得报 ok
        self.assertNotIn("database", names)
        matrix = [c for c in body["checks"] if c["name"] == "matrix"][0]
        self.assertEqual(matrix["status"], "degraded")

    def test_health_has_x_request_id(self):
        """B2: 每个响应（含 /health）带 X-Request-Id。"""
        with _ServerFixture(self._cfg()) as server:
            import urllib.request as _ur
            req = _ur.Request(f"{server.base}/health")
            with _ur.urlopen(req, timeout=5) as resp:
                self.assertIn("X-Request-Id", resp.headers)
                self.assertTrue(resp.headers["X-Request-Id"])

    def test_non_object_body_rejected(self):
        """B4: JSON 顶层非对象（数组/字符串/数字）→ 400 problem+json。"""
        with _ServerFixture(self._cfg()) as server:
            url = f"{server.base}/v1/conversations/c/messages"
            for raw_body in (b"[]", b'"hi"', b"123"):
                data = raw_body
                req = urllib.request.Request(url, data=data, method="POST")
                req.add_header("Content-Type", "application/json")
                try:
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        status = resp.status
                        raw = resp.read()
                except urllib.error.HTTPError as exc:
                    status = exc.code
                    raw = exc.read()
                resp_body = json.loads(raw) if raw else None
                self.assertEqual(status, 400, f"body {raw_body!r} should be 400")
                self.assertEqual(resp_body["code"], "request.invalid")

    def test_oversized_body_rejected(self):
        """B4: Content-Length 超过 MAX_BODY_BYTES → 400（不读入全部内容）。"""
        with _ServerFixture(self._cfg()) as server:
            url = f"{server.base}/v1/conversations/c/messages"
            # 构造一个声称很大的请求（实际 body 就 10 字节，但 Content-Length 超限）
            fake_big = b'{"x":"y"}'
            req = urllib.request.Request(url, data=fake_big, method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("Content-Length", str(MAX_BODY_BYTES + 1))
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    status = resp.status
            except urllib.error.HTTPError as exc:
                status = exc.code
        self.assertEqual(status, 400)

    def test_unknown_field_rejected(self):
        """B5: OutgoingMessageRequest additionalProperties: false → 拒绝未知字段。"""
        with _ServerFixture(self._cfg()) as server:
            url = f"{server.base}/v1/conversations/c/messages"
            status, body = _http("POST", url, {
                "sender_persona_id": "p",
                "content": {"type": "text", "text": "hi"},
                "evil": 1,
            })
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "request.invalid")

    def test_invalid_content_type_rejected(self):
        """B5: content.type 只接受 text/markdown。"""
        with _ServerFixture(self._cfg()) as server:
            url = f"{server.base}/v1/conversations/c/messages"
            status, body = _http("POST", url, {
                "sender_persona_id": "p",
                "content": {"type": "card", "text": "hi"},
            })
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "request.invalid")

    def test_missing_content_type_rejected(self):
        """B4: Content-Type 非 application/json → 400。"""
        with _ServerFixture(self._cfg()) as server:
            url = f"{server.base}/v1/conversations/c/messages"
            data = json.dumps({"sender_persona_id": "p",
                               "content": {"type": "text", "text": "x"}}).encode()
            req = urllib.request.Request(url, data=data, method="POST")
            req.add_header("Content-Type", "text/plain")
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    status = resp.status
            except urllib.error.HTTPError as exc:
                status = exc.code
        self.assertEqual(status, 400)

    def test_method_not_allowed_is_problem_json(self):
        """建议8: 不支持的方法返回 405 problem+json（非 stdlib 501 HTML）。"""
        with _ServerFixture(self._cfg()) as server:
            status, body = _http("DELETE", f"{server.base}/v1/conversations/c/messages")
        self.assertEqual(status, 405)
        self.assertEqual(body["code"], "request.invalid")
        self.assertTrue(body["type"].startswith("urn:botville:error:"))

    def test_idempotency_key_too_long_rejected(self):
        """建议2: Idempotency-Key 超 128 字符 → 400。"""
        with _ServerFixture(self._cfg()) as server:
            url = f"{server.base}/v1/conversations/c/messages"
            headers = {"Idempotency-Key": "k" * 200}
            status, body = _http("POST", url, {
                "sender_persona_id": "p", "content": {"type": "text", "text": "x"},
            }, headers=headers)
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "request.invalid")

    def test_idempotency_key_too_short_rejected(self):
        """#92: Idempotency-Key < 8 字符不再静默忽略，而是 400 problem+json。"""
        with _ServerFixture(self._cfg()) as server:
            url = f"{server.base}/v1/conversations/c/messages"
            for key in ("", "short", "1234567"):
                status, body = _http("POST", url, {
                    "sender_persona_id": "p", "content": {"type": "text", "text": "x"},
                }, headers={"Idempotency-Key": key})
                self.assertEqual(status, 400, f"key {key!r} should be 400")
                self.assertEqual(body["code"], "request.invalid")
                self.assertTrue(body["type"].startswith("urn:botville:error:"))
                self.assertIn("request_id", body)
                self.assertIn("8–128", body.get("detail", ""))
            # 被拒绝的请求没有投递
            self.assertEqual(len(server.server.transport.sent), 0)

    def test_idempotency_key_boundary_lengths_accepted(self):
        """#92: 恰好 8 和 128 字符合法，不应被 400 误伤。"""
        with _ServerFixture(self._cfg()) as server:
            url = f"{server.base}/v1/conversations/c/messages"
            for key in ("k" * 8, "k" * 128):
                status, body = _http("POST", url, {
                    "sender_persona_id": "p", "content": {"type": "text", "text": "x"},
                }, headers={"Idempotency-Key": key})
                self.assertEqual(status, 200, f"key length {len(key)} should be accepted")
                self.assertEqual(body["outcome"], "delivered")

    def test_markdown_preserved_over_http(self):
        """#92: HTTP 端到端 content.type=markdown 原样保留到回执与 transport。"""
        with _ServerFixture(self._cfg()) as fixture:
            status, body = _http(
                "POST", f"{fixture.base}/v1/conversations/c/messages",
                {"sender_persona_id": "p", "content": {"type": "markdown", "text": "**hi**"}},
            )
            sent = fixture.server.transport.sent
        self.assertEqual(status, 200)
        self.assertEqual(body["message"]["content"]["type"], "markdown")
        self.assertEqual(sent[0].content_type, "markdown")

    # ---------------------- appservice 投递错误的 4xx 映射（PR #99 建议）
    def test_namespace_delivery_error_is_400(self):
        """appservice.namespace（非法 persona / 越权 sender）→ 400 request.invalid，
        不是 500：请求在客户端侧就能判定非法，且消息未投递、未记账。"""
        with _ServerFixture(
            self._cfg(), _FailingTransport("appservice.namespace", "sender outside namespace")
        ) as server:
            status, body = _http(
                "POST", f"{server.base}/v1/conversations/c/messages",
                {"sender_persona_id": "Bad Persona", "content": {"type": "text", "text": "x"}},
            )
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "request.invalid")
        errors = body.get("errors") or []
        self.assertTrue(any(e.get("field") == "sender_persona_id" for e in errors))

    def test_invalid_conversation_error_is_400(self):
        # 路径里不放非法字符（HTTP 请求行安全）：传输层无条件抛
        # conversation.invalid，映射只看错误码。
        with _ServerFixture(
            self._cfg(), _FailingTransport("conversation.invalid", "bad conversation id")
        ) as server:
            status, body = _http(
                "POST", f"{server.base}/v1/conversations/conv_bad/messages",
                {"sender_persona_id": "p", "content": {"type": "text", "text": "x"}},
            )
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "request.invalid")
        errors = body.get("errors") or []
        self.assertTrue(any(e.get("field") == "conversation_id" for e in errors))

    def test_conversation_not_found_is_404(self):
        """conversation.not_found（房间别名不存在）→ 404（契约错误码表已有）。"""
        with _ServerFixture(
            self._cfg(), _FailingTransport("conversation.not_found", "no such room alias")
        ) as server:
            status, body = _http(
                "POST", f"{server.base}/v1/conversations/conv_missing/messages",
                {"sender_persona_id": "p", "content": {"type": "text", "text": "x"}},
            )
        self.assertEqual(status, 404)
        self.assertEqual(body["code"], "conversation.not_found")
        self.assertIn("request_id", body)

    def test_other_delivery_errors_still_500(self):
        """其余传输失败仍统一 500 internal.error（契约无 502）。"""
        with _ServerFixture(
            self._cfg(), _FailingTransport("M_LIMIT_EXCEEDED", "slow down")
        ) as server:
            status, body = _http(
                "POST", f"{server.base}/v1/conversations/c/messages",
                {"sender_persona_id": "p", "content": {"type": "text", "text": "x"}},
            )
        self.assertEqual(status, 500)
        self.assertEqual(body["code"], "internal.error")


# ---------------------------------------------------------------------------
# 9b. build_server 传输工厂：transport 缺省时按 config 构建（PR #99 阻塞项 3）
# ---------------------------------------------------------------------------
class TestBuildServerTransportFactory(unittest.TestCase):
    @staticmethod
    def _appservice_cfg(**overrides):
        base = dict(
            host="127.0.0.1",
            port=0,
            dev_mode=True,
            silent_hours_enabled=False,
            matrix_transport="appservice",
            homeserver_url="http://127.0.0.1:8008",
            matrix_server_name="localhost",
            as_token="a" * 64,
        )
        base.update(overrides)
        return Config(**base)

    def test_appservice_config_does_not_fall_back_to_memory(self):
        """appservice 配置下 build_server 必须拿到真实传输（构造时不发请求）。"""
        server = build_server(self._appservice_cfg())
        try:
            self.assertIsInstance(server.transport, AppServiceMatrixTransport)
        finally:
            server.server_close()

    def test_appservice_transport_gets_room_cache_ttl(self):
        """#107：GATEWAY_ROOM_ALIAS_CACHE_TTL 经 build_transport 写入传输。"""
        server = build_server(
            self._appservice_cfg(room_alias_cache_ttl_seconds=7)
        )
        try:
            transport = server.transport
            self.assertIsInstance(transport, AppServiceMatrixTransport)
            self.assertEqual(transport._room_cache_ttl, 7)
        finally:
            server.server_close()

    def test_memory_config_keeps_in_memory_transport(self):
        server = build_server(_cfg())
        try:
            self.assertIsInstance(server.transport, InMemoryMatrixTransport)
        finally:
            server.server_close()

    def test_explicit_transport_wins(self):
        custom = InMemoryMatrixTransport()
        server = build_server(self._appservice_cfg(), custom)
        try:
            self.assertIs(server.transport, custom)
        finally:
            server.server_close()


# ---------------------------------------------------------------------------
# 10. B1 契约校验：interrupt 三态、无 default
# ---------------------------------------------------------------------------
class TestContractInterrupt(unittest.TestCase):
    """B1: OpenAPI 的 Card.interrupt 与 CreateCardRequest.interrupt 都去掉 default，
    描述明确三态语义。校验脚本会拒绝重复键，所以用文本搜索更稳。"""

    def setUp(self):
        try:
            import yaml  # noqa
        except ImportError:
            self.skipTest("pyyaml not installed")
        contract = os.path.join(GATEWAY_ROOT, "openapi", "gateway.v1.yaml")
        with open(contract, "r", encoding="utf-8") as fh:
            self.text = fh.read()

    def _interrupt_blocks(self):
        blocks = []
        idx = 0
        while True:
            pos = self.text.find("interrupt:", idx)
            if pos == -1:
                break
            blocks.append(self.text[pos:pos + 400])
            idx = pos + 1
        return blocks

    def test_card_and_createcard_interrupt_have_no_default(self):
        blocks = self._interrupt_blocks()
        # 至少两处 interrupt 字段（Card + CreateCardRequest）
        self.assertGreaterEqual(len(blocks), 2)
        for block in blocks:
            # 取每个 interrupt 块的前 250 字符，不应出现 default: false
            snippet = block[:250]
            self.assertNotIn("default: false", snippet,
                              f"interrupt 不应再有 default: false:\n{snippet}")

    def test_interrupt_mentions_three_state_semantics(self):
        joined = "\n".join(self._interrupt_blocks())
        # 描述要明确「省略时由 priority 决定」与「显式 false」
        self.assertIn("priority", joined)
        self.assertIn("urgent", joined)

    def test_contract_md_documents_three_state(self):
        contract_md = os.path.join(GATEWAY_ROOT, "docs", "contract.md")
        with open(contract_md, "r", encoding="utf-8") as fh:
            text = fh.read()
        # §4.8 描述三态且说明无默认值
        self.assertIn("无默认值", text)
        self.assertIn("三态", text)
        # changelog 记一笔（默认值变更 = minor + 公告）
        self.assertIn("移除", text)
        self.assertIn("minor", text)


if __name__ == "__main__":
    unittest.main()
