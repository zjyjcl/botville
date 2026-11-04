# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""B2.2 防刷屏与 AI 互聊熔断单元/集成测试。

覆盖：
- anti_spam：连续达 max → blocked(consecutive)；冷却期 blocked(cooldown) 且 may_clear_at 不变；
  冷却结束后计数清零可再发 max 条；blocked 事件不计入连续数。
- ai_loop：两 AI 交替发言到 max_ai_messages_per_hour → blocked(tripped)；
  on_break=notify 同样 blocked；may_clear_at = 最早事件 + 3600。
- record_human_message：重置 anti_spam / ai_loop 计数，提前解除冷却。
- 三层取最严：owner/peer 更严生效；放宽超过平台 → 422；window_seconds 单写 → 422。
- peer_id 422：非成员、等于自身、未注册带 peer_id、有 peer 规则时缺 peer_id；
  无 peer 规则时缺省同 B2.1；evaluate 与 send 一致；HTTP 422。
- peer 计数：peer 层按 (persona, peer_id) 计数；旧事件无 peer_id 不计入。
- max_message_chars 分层：owner/peer 更严 → 200 blocked + message.length；
  超平台硬上限 → 400 request.invalid（不走策略路径）。
- 在途预留：reset_at 非 null 保守值；并发场景恰好 N 条 delivered。
- 保留期淘汰：UsageStore 按 retention_seconds 淘汰旧事件。
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from typing import List, Optional

GATEWAY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(GATEWAY_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from botville_gateway.config import Config  # noqa: E402
from botville_gateway.errors import LimitError, PeerValidationError, ValidationError  # noqa: E402
from botville_gateway.http_server import build_server  # noqa: E402
from botville_gateway.limits import LimitStore, validate_rules  # noqa: E402
from botville_gateway.matrix import DeliveryError, InMemoryMatrixTransport, SentEvent  # noqa: E402
from botville_gateway.policy import (  # noqa: E402
    CODE_AI_LOOP_TRIPPED,
    CODE_ANTI_SPAM_CONSECUTIVE,
    CODE_ANTI_SPAM_COOLDOWN,
    CODE_MESSAGE_LENGTH,
    PolicyEngine,
    ReservationBook,
)
from botville_gateway.service import GatewayService  # noqa: E402
from botville_gateway.store import EventKind, UsageEvent, UsageStore  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class FixedClock:
    """冻结时钟：注入不 sleep 的可控时间。"""

    def __init__(self, start_iso: str = "2026-11-03T12:00:00Z") -> None:
        self._now = datetime.datetime.strptime(start_iso, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )

    def __call__(self) -> datetime.datetime:
        return self._now

    def set(self, iso: str) -> None:
        self._now = datetime.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )

    def advance(self, **delta) -> None:
        self._now = self._now + timedelta(**delta)


def make_config(**overrides) -> Config:
    base = dict(
        dev_mode=True,
        rate_limit_messages=100,
        rate_limit_window_seconds=3600,
        silent_hours_enabled=False,
        max_message_chars=8000,
        platform_token_budget=0,
        platform_proactive_per_day=0,
    )
    base.update(overrides)
    return Config(**base)


def make_service(config: Config = None, **config_overrides):
    if config_overrides:
        config = make_config(**config_overrides)
    config = config or make_config()
    clock = FixedClock()
    transport = InMemoryMatrixTransport()
    store = UsageStore(max_events=0)
    limits = LimitStore(config)
    svc = GatewayService(config, transport, store, limits, now_fn=clock)
    return svc, transport, store, limits, clock


def send(svc: GatewayService, persona: str, conv: str, text: str = "hello", peer_id: Optional[str] = None) -> dict:
    payload: dict = {
        "sender_persona_id": persona,
        "content": {"type": "text", "text": text},
    }
    if peer_id is not None:
        payload["peer_id"] = peer_id
    return svc.send_ai_message(conv, payload)


def put_anti_spam(limits: LimitStore, persona: str, max_consecutive: int, cooldown_seconds: int) -> None:
    limits.put(
        {
            "layer": "owner",
            "scope": "persona",
            "scope_id": persona,
            "rules": {
                "anti_spam": {
                    "max_consecutive_ai_messages": max_consecutive,
                    "cooldown_seconds": cooldown_seconds,
                }
            },
        },
        now=datetime.datetime.now(timezone.utc),
    )


def put_ai_loop(limits: LimitStore, persona: str, max_per_hour: int, on_break: str = "stop") -> None:
    limits.put(
        {
            "layer": "owner",
            "scope": "persona",
            "scope_id": persona,
            "rules": {
                "ai_loop": {
                    "max_ai_messages_per_hour": max_per_hour,
                    "on_break": on_break,
                }
            },
        },
        now=datetime.datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# 1. anti_spam：连续计数与冷却
# ---------------------------------------------------------------------------

class TestAntiSpamConsecutive(unittest.TestCase):
    def setUp(self):
        self.svc, _, self.store, self.limits, self.clock = make_service()
        put_anti_spam(self.limits, "bot1", max_consecutive=3, cooldown_seconds=60)

    def test_first_max_messages_delivered(self):
        for _ in range(3):
            r = send(self.svc, "bot1", "conv1")
            self.assertEqual(r["outcome"], "delivered", r)

    def test_max_plus_one_blocked_consecutive(self):
        for _ in range(3):
            send(self.svc, "bot1", "conv1")
        r = send(self.svc, "bot1", "conv1")
        self.assertEqual(r["outcome"], "blocked")
        codes = [reason["code"] for reason in r["decision"]["reasons"]]
        self.assertIn(CODE_ANTI_SPAM_CONSECUTIVE, codes)

    def test_cooldown_period_returns_cooldown_code(self):
        for _ in range(3):
            send(self.svc, "bot1", "conv1")
        # trigger consecutive
        send(self.svc, "bot1", "conv1")
        # advance into cooldown but not past it
        self.clock.advance(seconds=30)
        r = send(self.svc, "bot1", "conv1")
        self.assertEqual(r["outcome"], "blocked")
        codes = [reason["code"] for reason in r["decision"]["reasons"]]
        self.assertIn(CODE_ANTI_SPAM_COOLDOWN, codes)

    def test_cooldown_may_clear_at_does_not_extend_on_retry(self):
        """冷却期内多次请求不延长冷却锚点。"""
        for _ in range(3):
            send(self.svc, "bot1", "conv1")
        # trigger
        r1 = send(self.svc, "bot1", "conv1")
        self.clock.advance(seconds=10)
        r2 = send(self.svc, "bot1", "conv1")
        self.clock.advance(seconds=10)
        r3 = send(self.svc, "bot1", "conv1")
        # All three blocked; may_clear_at must be the same (anchored at first trigger)
        self.assertEqual(r1["may_clear_at"], r2["may_clear_at"])
        self.assertEqual(r1["may_clear_at"], r3["may_clear_at"])

    def test_after_cooldown_can_send_max_again(self):
        for _ in range(3):
            send(self.svc, "bot1", "conv1")
        # trigger
        send(self.svc, "bot1", "conv1")
        # skip past cooldown
        self.clock.advance(seconds=61)
        for _ in range(3):
            r = send(self.svc, "bot1", "conv1")
            self.assertEqual(r["outcome"], "delivered", r)
        # max+1 after cooldown → blocked again
        r = send(self.svc, "bot1", "conv1")
        self.assertEqual(r["outcome"], "blocked")

    def test_blocked_events_do_not_count_toward_consecutive(self):
        """blocked 事件（kind=blocked）不计入连续数，不应累积计数。"""
        for _ in range(3):
            send(self.svc, "bot1", "conv1")
        # trigger consecutive: this is blocked, not counted
        r = send(self.svc, "bot1", "conv1")
        self.assertEqual(r["outcome"], "blocked")
        # advance past cooldown
        self.clock.advance(seconds=61)
        # counter should be reset; exactly 3 deliveries allowed
        for _ in range(3):
            r = send(self.svc, "bot1", "conv1")
            self.assertEqual(r["outcome"], "delivered")

    def test_anti_spam_check_fields_consecutive(self):
        """check 口径：consecutive 有 limit/used/remaining。"""
        for _ in range(3):
            send(self.svc, "bot1", "conv1")
        r = send(self.svc, "bot1", "conv1")
        checks = {c["key"]: c for c in r["decision"]["checks"]}
        c = checks.get("anti_spam.consecutive")
        self.assertIsNotNone(c)
        self.assertEqual(c["limit"], 3)
        self.assertFalse(c["allowed"])
        self.assertEqual(c["remaining"], 0)

    def test_anti_spam_check_fields_cooldown(self):
        """check 口径：cooldown 有 limit=cooldown_seconds/used/remaining/reset_at。"""
        for _ in range(3):
            send(self.svc, "bot1", "conv1")
        send(self.svc, "bot1", "conv1")  # trigger
        self.clock.advance(seconds=20)
        r = send(self.svc, "bot1", "conv1")
        checks = {c["key"]: c for c in r["decision"]["checks"]}
        c = checks.get("anti_spam.cooldown")
        self.assertIsNotNone(c)
        self.assertEqual(c["limit"], 60)
        self.assertGreater(c["used"], 0)
        self.assertGreater(c["remaining"], 0)
        self.assertIsNotNone(c["reset_at"])


# ---------------------------------------------------------------------------
# 2. ai_loop：互聊熔断
# ---------------------------------------------------------------------------

class TestAiLoop(unittest.TestCase):
    def setUp(self):
        self.svc, _, self.store, self.limits, self.clock = make_service()
        # Two AI personas in the same conversation
        put_ai_loop(self.limits, "bot1", max_per_hour=4)
        put_ai_loop(self.limits, "bot2", max_per_hour=4)

    def test_two_ai_alternating_reach_limit(self):
        """两 AI 交替发言达到 max_ai_messages_per_hour 后 blocked tripped。"""
        for i in range(4):
            persona = "bot1" if i % 2 == 0 else "bot2"
            r = send(self.svc, persona, "conv1")
            self.assertEqual(r["outcome"], "delivered", f"message {i+1}: {r}")
        # 5th AI message → tripped
        r = send(self.svc, "bot1", "conv1")
        self.assertEqual(r["outcome"], "blocked")
        codes = [reason["code"] for reason in r["decision"]["reasons"]]
        self.assertIn(CODE_AI_LOOP_TRIPPED, codes)

    def test_on_break_notify_treated_as_stop(self):
        """on_break=notify 当前版本按 stop 处理（fail-closed）。"""
        put_ai_loop(self.limits, "bot1", max_per_hour=2, on_break="notify")
        for _ in range(2):
            send(self.svc, "bot1", "conv1")
        r = send(self.svc, "bot1", "conv1")
        self.assertEqual(r["outcome"], "blocked")
        codes = [reason["code"] for reason in r["decision"]["reasons"]]
        self.assertIn(CODE_AI_LOOP_TRIPPED, codes)

    def test_ai_loop_may_clear_at_earliest_event_plus_3600(self):
        """may_clear_at = 被计入 count 的最早一条事件 + 3600s（向上取整到秒）。"""
        t0 = self.clock()
        for _ in range(4):
            send(self.svc, "bot1", "conv1")
            self.clock.advance(seconds=1)
        r = send(self.svc, "bot1", "conv1")
        self.assertEqual(r["outcome"], "blocked")
        expected_clear = t0 + timedelta(seconds=3600)
        # may_clear_at must be >= t0 + 3600
        actual = datetime.datetime.strptime(r["may_clear_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        self.assertGreaterEqual(actual, expected_clear)

    def test_ai_loop_may_clear_at_not_before_oldest_counted_event(self):
        """有早于人类消息的旧事件时，may_clear_at 不偏早（只计人类消息后的事件）。"""
        # Send some events BEFORE a human message
        for _ in range(2):
            send(self.svc, "bot1", "conv1")
        old_time = self.clock()
        self.clock.advance(seconds=10)
        # Record a human message; this should reset the count window
        self.svc.record_human_message("conv1", at=self.clock())
        self.clock.advance(seconds=1)
        t_after_human = self.clock()
        for _ in range(4):
            send(self.svc, "bot1", "conv1")
            self.clock.advance(seconds=1)
        r = send(self.svc, "bot1", "conv1")
        self.assertEqual(r["outcome"], "blocked")
        actual = datetime.datetime.strptime(r["may_clear_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        # may_clear_at must be >= t_after_human + 3600 (not from old pre-human events)
        self.assertGreaterEqual(actual, t_after_human + timedelta(seconds=3600))

    def test_ai_loop_check_fields(self):
        for _ in range(4):
            send(self.svc, "bot1", "conv1")
        r = send(self.svc, "bot1", "conv1")
        checks = {c["key"]: c for c in r["decision"]["checks"]}
        c = checks.get("ai_loop.per_hour")
        self.assertIsNotNone(c)
        self.assertEqual(c["limit"], 4)
        self.assertFalse(c["allowed"])
        self.assertEqual(c["remaining"], 0)
        self.assertIsNotNone(c["reset_at"])


# ---------------------------------------------------------------------------
# 3. record_human_message 重置
# ---------------------------------------------------------------------------

class TestHumanMessageReset(unittest.TestCase):
    def setUp(self):
        self.svc, _, self.store, self.limits, self.clock = make_service()
        put_anti_spam(self.limits, "bot1", max_consecutive=2, cooldown_seconds=30)
        put_ai_loop(self.limits, "bot1", max_per_hour=2)

    def test_human_message_resets_anti_spam_count(self):
        for _ in range(2):
            send(self.svc, "bot1", "conv1")
        # trigger consecutive
        r = send(self.svc, "bot1", "conv1")
        self.assertEqual(r["outcome"], "blocked")
        # human message arrives
        self.clock.advance(seconds=1)
        self.svc.record_human_message("conv1")
        self.clock.advance(seconds=1)
        # after reset, can send 2 again
        for _ in range(2):
            r = send(self.svc, "bot1", "conv1")
            self.assertEqual(r["outcome"], "delivered", r)

    def test_human_message_clears_cooldown_early(self):
        """冷却期内出现人类消息，提前解除冷却并重置计数。"""
        for _ in range(2):
            send(self.svc, "bot1", "conv1")
        send(self.svc, "bot1", "conv1")  # trigger
        # only 5s into cooldown (30s total)
        self.clock.advance(seconds=5)
        self.svc.record_human_message("conv1")
        self.clock.advance(seconds=1)
        # cooldown should be lifted; can send again
        for _ in range(2):
            r = send(self.svc, "bot1", "conv1")
            self.assertEqual(r["outcome"], "delivered", r)

    def test_human_message_resets_ai_loop_count(self):
        for _ in range(2):
            send(self.svc, "bot1", "conv1")
        r = send(self.svc, "bot1", "conv1")
        self.assertEqual(r["outcome"], "blocked")
        codes = [x["code"] for x in r["decision"]["reasons"]]
        self.assertIn(CODE_AI_LOOP_TRIPPED, codes)
        # human arrives
        self.clock.advance(seconds=1)
        self.svc.record_human_message("conv1")
        self.clock.advance(seconds=1)
        # can send 2 more
        for _ in range(2):
            r = send(self.svc, "bot1", "conv1")
            self.assertEqual(r["outcome"], "delivered", r)

    def test_human_events_not_in_usage_events(self):
        """EventKind.HUMAN 不出现在 GET /usage/events 响应中。"""
        self.svc.record_human_message("conv1")
        result = self.svc.list_usage_events()
        kinds = {item["kind"] for item in result["items"]}
        self.assertNotIn("human", kinds)
        self.assertNotIn("", kinds)

    def test_human_events_not_in_usage_totals(self):
        """HUMAN 事件不计入 /usage 消息计数。"""
        self.svc.record_human_message("conv1")
        result = self.svc.get_usage()
        # Should not appear in totals
        self.assertEqual(result["totals"]["messages_sent"], 0)


# ---------------------------------------------------------------------------
# 4. 三层取最严
# ---------------------------------------------------------------------------

class TestThreeLayerStrictness(unittest.TestCase):
    def setUp(self):
        self.svc, _, self.store, self.limits, self.clock = make_service()

    def test_owner_stricter_anti_spam_applies(self):
        put_anti_spam(self.limits, "bot1", max_consecutive=2, cooldown_seconds=10)
        for _ in range(2):
            send(self.svc, "bot1", "conv1")
        r = send(self.svc, "bot1", "conv1")
        self.assertEqual(r["outcome"], "blocked")

    def test_looser_than_platform_raises_422(self):
        """owner 试图把平台限额放宽 → 422 limit.out_of_range。"""
        with self.assertRaises(LimitError) as ctx:
            self.limits.put(
                {
                    "layer": "owner",
                    "scope": "persona",
                    "scope_id": "bot1",
                    "rules": {
                        "messages_per_window": 9999,  # looser than platform 100
                    },
                },
                now=self.clock(),
            )
        self.assertIn("out_of_range", ctx.exception.code)

    def test_window_seconds_alone_raises_422(self):
        """#114-4：单写 window_seconds 不成对 → 422 limit.out_of_range。"""
        from botville_gateway.limits import validate_rules
        from botville_gateway.errors import LimitError
        with self.assertRaises(LimitError):
            validate_rules({"window_seconds": 60})


# ---------------------------------------------------------------------------
# 5. peer_id 校验
# ---------------------------------------------------------------------------

class TestPeerIdValidation(unittest.TestCase):
    def setUp(self):
        self.svc, _, self.store, self.limits, self.clock = make_service()
        # Register a conversation with two members
        self.svc.register_conversation("conv_peer", ["bot1", "bot2"])
        # Give bot2 a peer-layer rule so peer_id becomes required
        self.limits.put(
            {
                "layer": "peer",
                "scope": "peer",
                "scope_id": "bot2",
                "rules": {"max_message_chars": 500},
            },
            now=self.clock(),
        )

    def test_missing_peer_id_when_peer_rule_exists_raises_422(self):
        with self.assertRaises(PeerValidationError) as ctx:
            send(self.svc, "bot1", "conv_peer")
        self.assertEqual(ctx.exception.http_status, 422)

    def test_non_member_peer_id_raises_422(self):
        with self.assertRaises(PeerValidationError):
            send(self.svc, "bot1", "conv_peer", peer_id="bot_stranger")

    def test_sender_equals_peer_id_raises_422(self):
        with self.assertRaises(PeerValidationError):
            send(self.svc, "bot1", "conv_peer", peer_id="bot1")

    def test_valid_peer_id_succeeds(self):
        r = send(self.svc, "bot1", "conv_peer", peer_id="bot2")
        self.assertEqual(r["outcome"], "delivered")

    def test_unregistered_conv_with_peer_id_raises_422(self):
        """会话未注册 + 带 peer_id → 422（fail-closed）。"""
        with self.assertRaises(PeerValidationError):
            send(self.svc, "bot1", "conv_unregistered", peer_id="bot2")

    def test_unregistered_conv_no_peer_id_ok(self):
        """会话未注册 + 无 peer_id → 与 B2.1 相同，pass。"""
        r = send(self.svc, "bot1", "conv_unregistered")
        self.assertEqual(r["outcome"], "delivered")

    def test_registered_conv_no_peer_rules_no_peer_id_ok(self):
        """会话已注册、成员无 peer 层规则、无 peer_id → 与 B2.1 相同。"""
        self.svc.register_conversation("conv_no_peer", ["bot3", "bot4"])
        # no peer-layer rules for bot3/bot4
        r = send(self.svc, "bot3", "conv_no_peer")
        self.assertEqual(r["outcome"], "delivered")

    def test_evaluate_policy_also_enforces_peer_id(self):
        """evaluate_policy 与 send_ai_message 对 peer_id 使用同一校验逻辑。"""
        with self.assertRaises(PeerValidationError):
            self.svc.evaluate_policy({
                "persona_id": "bot1",
                "conversation_id": "conv_peer",
                "content": "hello",
            })
        # With valid peer_id it should succeed
        result = self.svc.evaluate_policy({
            "persona_id": "bot1",
            "conversation_id": "conv_peer",
            "peer_id": "bot2",
            "content": "hello",
        })
        self.assertIn("decision", result)


class TestPeerIdValidationHTTP(unittest.TestCase):
    """HTTP 端点对 peer_id 校验返回 422。"""

    def setUp(self):
        config = make_config(auth_token=None, dev_mode=True)
        transport = InMemoryMatrixTransport()
        store = UsageStore(max_events=0)
        limits = LimitStore(config)
        clock = FixedClock()
        svc = GatewayService(config, transport, store, limits, now_fn=clock)
        svc.register_conversation("conv_http", ["bot1", "bot2"])
        limits.put(
            {
                "layer": "peer",
                "scope": "peer",
                "scope_id": "bot2",
                "rules": {"max_message_chars": 500},
            },
            now=clock(),
        )
        self.server = build_server(config, transport)
        self.server.service = svc
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}/v1"

    def tearDown(self):
        self.server.shutdown()

    def _post(self, path: str, body: dict) -> tuple:
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_send_without_peer_id_returns_422(self):
        status, body = self._post("/conversations/conv_http/messages", {
            "sender_persona_id": "bot1",
            "content": {"type": "text", "text": "hi"},
        })
        self.assertEqual(status, 422)
        self.assertEqual(body["code"], "request.invalid")
        errors = body.get("errors", [])
        self.assertTrue(any(e.get("field") == "peer_id" for e in errors))

    def test_evaluate_without_peer_id_returns_422(self):
        status, body = self._post("/policy/evaluate", {
            "persona_id": "bot1",
            "conversation_id": "conv_http",
            "content": "hi",
        })
        self.assertEqual(status, 422)
        self.assertEqual(body["code"], "request.invalid")


# ---------------------------------------------------------------------------
# 6. peer 计数
# ---------------------------------------------------------------------------

class TestPeerCounting(unittest.TestCase):
    def setUp(self):
        self.svc, _, self.store, self.limits, self.clock = make_service()
        self.svc.register_conversation("conv1", ["bot1", "bot2"])

    def test_peer_layer_counts_by_persona_and_peer(self):
        """peer 层按 (persona, peer_id) 计数；对另一 peer 不受限。"""
        # Add a per-peer message limit of 2 for bot2
        self.limits.put(
            {
                "layer": "peer",
                "scope": "peer",
                "scope_id": "bot2",
                "rules": {
                    "messages_per_window": 2,
                    "window_seconds": 3600,
                },
            },
            now=self.clock(),
        )
        # bot1 sends 2 to bot2 → OK
        for _ in range(2):
            r = send(self.svc, "bot1", "conv1", peer_id="bot2")
            self.assertEqual(r["outcome"], "delivered")
        # 3rd → blocked by peer layer
        r = send(self.svc, "bot1", "conv1", peer_id="bot2")
        self.assertEqual(r["outcome"], "blocked")

    def test_old_events_without_peer_id_not_counted_in_peer_layer(self):
        """旧事件（peer_id=None）不计入 peer 层计数。"""
        self.limits.put(
            {
                "layer": "peer",
                "scope": "peer",
                "scope_id": "bot2",
                "rules": {
                    "messages_per_window": 1,
                    "window_seconds": 3600,
                },
            },
            now=self.clock(),
        )
        # Inject an old event without peer_id directly
        self.store.append(
            UsageEvent.make(
                EventKind.SENT,
                persona_id="bot1",
                conversation_id="conv1",
                at=self.clock(),
                peer_id=None,  # old event, no peer_id
            )
        )
        self.clock.advance(seconds=1)
        # First send with peer_id should be allowed (old event doesn't count)
        r = send(self.svc, "bot1", "conv1", peer_id="bot2")
        self.assertEqual(r["outcome"], "delivered")
        # Second send → blocked by peer layer
        r = send(self.svc, "bot1", "conv1", peer_id="bot2")
        self.assertEqual(r["outcome"], "blocked")

    def test_peer_id_written_to_sent_event(self):
        """sent 事件写入 peer_id（B2.2 §6.4）。"""
        self.limits.put(
            {
                "layer": "peer",
                "scope": "peer",
                "scope_id": "bot2",
                "rules": {"max_message_chars": 500},
            },
            now=self.clock(),
        )
        send(self.svc, "bot1", "conv1", peer_id="bot2")
        sent_events = self.store.events_in_window(kind=EventKind.SENT)
        self.assertTrue(any(e.peer_id == "bot2" for e in sent_events))

    def test_peer_id_in_usage_events_output(self):
        """peer_id 出现在 GET /usage/events 的单条记录里。"""
        self.limits.put(
            {
                "layer": "peer",
                "scope": "peer",
                "scope_id": "bot2",
                "rules": {"max_message_chars": 500},
            },
            now=self.clock(),
        )
        send(self.svc, "bot1", "conv1", peer_id="bot2")
        result = self.svc.list_usage_events()
        sent_items = [i for i in result["items"] if i["kind"] == "message_sent"]
        self.assertTrue(any(i.get("peer_id") == "bot2" for i in sent_items))


# ---------------------------------------------------------------------------
# 7. max_message_chars 分层
# ---------------------------------------------------------------------------

class TestMessageLengthLayered(unittest.TestCase):
    def setUp(self):
        # platform max_message_chars = 400; owner/peer can set stricter
        self.svc, _, self.store, self.limits, self.clock = make_service(
            max_message_chars=400
        )

    def test_platform_hardlimit_returns_400(self):
        """超过平台硬上限 → 400 request.invalid（不走策略路径）。"""
        with self.assertRaises(ValidationError) as ctx:
            send(self.svc, "bot1", "conv1", text="x" * 401)
        self.assertEqual(ctx.exception.http_status, 400)

    def test_owner_stricter_limit_returns_200_blocked(self):
        """owner 层设 max_message_chars=10 → 200 blocked + message.length。"""
        self.limits.put(
            {
                "layer": "owner",
                "scope": "persona",
                "scope_id": "bot1",
                "rules": {"max_message_chars": 10},
            },
            now=self.clock(),
        )
        r = send(self.svc, "bot1", "conv1", text="x" * 20)
        self.assertEqual(r["outcome"], "blocked")
        codes = [reason["code"] for reason in r["decision"]["reasons"]]
        self.assertIn(CODE_MESSAGE_LENGTH, codes)

    def test_within_owner_limit_succeeds(self):
        self.limits.put(
            {
                "layer": "owner",
                "scope": "persona",
                "scope_id": "bot1",
                "rules": {"max_message_chars": 100},
            },
            now=self.clock(),
        )
        r = send(self.svc, "bot1", "conv1", text="x" * 50)
        self.assertEqual(r["outcome"], "delivered")

    def test_peer_stricter_limit_returns_200_blocked(self):
        self.svc.register_conversation("conv2", ["bot1", "bot2"])
        self.limits.put(
            {
                "layer": "peer",
                "scope": "peer",
                "scope_id": "bot2",
                "rules": {"max_message_chars": 5},
            },
            now=self.clock(),
        )
        r = send(self.svc, "bot1", "conv2", text="x" * 10, peer_id="bot2")
        self.assertEqual(r["outcome"], "blocked")
        codes = [reason["code"] for reason in r["decision"]["reasons"]]
        self.assertIn(CODE_MESSAGE_LENGTH, codes)


# ---------------------------------------------------------------------------
# 8. 在途预留 reset_at 保守值
# ---------------------------------------------------------------------------

class TestInflightReservationResetAt(unittest.TestCase):
    """在途预留阻塞时 messages/tokens 的 reset_at/may_clear_at 不得为 null/now。"""

    def test_reset_at_is_conservative_when_blocked_by_inflight(self):
        svc, _, store, limits, clock = make_service(
            rate_limit_messages=1, rate_limit_window_seconds=3600
        )
        # Send one to fill the quota
        r = send(svc, "bot1", "conv1")
        self.assertEqual(r["outcome"], "delivered")
        # Second should be blocked; reset_at should be a future time, not None
        r2 = send(svc, "bot1", "conv1")
        self.assertEqual(r2["outcome"], "blocked")
        checks = {c["key"]: c for c in r2["decision"]["checks"]}
        rate_check = checks.get("messages.window")
        self.assertIsNotNone(rate_check)
        self.assertIsNotNone(rate_check.get("reset_at"))
        reset_at = datetime.datetime.strptime(
            rate_check["reset_at"], "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc)
        self.assertGreater(reset_at, clock())


# ---------------------------------------------------------------------------
# 9. 保留期淘汰
# ---------------------------------------------------------------------------

class TestRetentionEviction(unittest.TestCase):
    def test_events_before_retention_evicted_first(self):
        store = UsageStore(max_events=5)
        base = datetime.datetime(2026, 11, 3, 12, 0, 0, tzinfo=timezone.utc)
        # Add 4 old events (60 seconds ago)
        for i in range(4):
            store.append(
                UsageEvent.make(
                    EventKind.SENT,
                    persona_id="bot1",
                    at=base - timedelta(seconds=60 - i),
                )
            )
        # Add 1 new event (now)
        store.append(
            UsageEvent.make(EventKind.SENT, persona_id="bot1", at=base)
        )
        self.assertEqual(len(store), 5)
        # Evict with retention_seconds=30: events older than 30s should be removed first
        new_event = UsageEvent.make(EventKind.SENT, persona_id="bot1", at=base)
        store.append(new_event, retention_seconds=30)
        # Only events within last 30s should remain + the new one
        remaining = store.all_events()
        for e in remaining:
            cutoff = base - timedelta(seconds=30)
            self.assertGreater(e.recorded_at, cutoff)


# ---------------------------------------------------------------------------
# 10. 并发：恰好 N 条 delivered
# ---------------------------------------------------------------------------

@dataclass
class SlowTransport:
    sleep_seconds: float = 0.05
    sent: List[SentEvent] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def send_text(self, *, room_id, sender_id, text, reply_to=None, content_type="text", txn_id=None) -> SentEvent:
        time.sleep(self.sleep_seconds)
        event = SentEvent(
            event_id=f"$evt_{threading.get_ident()}",
            room_id=room_id,
            sender_id=sender_id,
            text=text,
            sent_at=datetime.datetime.now(timezone.utc),
        )
        with self._lock:
            self.sent.append(event)
        return event

    def healthy(self) -> bool:
        return True


class TestConcurrentAntiSpam(unittest.TestCase):
    """并发：multi-thread 同时发送，anti_spam limit=N → 恰好 N 条 delivered。"""

    def test_concurrent_sends_exactly_n_delivered(self):
        config = make_config()
        clock = FixedClock()
        transport = SlowTransport(sleep_seconds=0.05)
        store = UsageStore(max_events=0)
        limits = LimitStore(config)
        svc = GatewayService(config, transport, store, limits, now_fn=clock)
        put_anti_spam(limits, "bot1", max_consecutive=3, cooldown_seconds=10)

        results = []
        errors = []
        barrier = threading.Barrier(6)

        def do_send():
            barrier.wait()
            try:
                r = send(svc, "bot1", "conv_concurrent")
                results.append(r["outcome"])
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=do_send) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertFalse(errors, errors)
        delivered = results.count("delivered")
        blocked = results.count("blocked")
        self.assertEqual(delivered, 3, f"outcomes: {results}")
        self.assertEqual(blocked, 3, f"outcomes: {results}")


if __name__ == "__main__":
    unittest.main()
