# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""B2.2 防刷屏与 AI 互聊熔断单元/集成测试。

覆盖：
- anti_spam：连续达 max → blocked(consecutive)；冷却期 blocked(cooldown) 且 may_clear_at 不变；
  冷却结束后计数清零可再发 max 条；blocked 事件不计入连续数。
- ai_loop：两 AI 交替发言到 max_ai_messages_per_hour → blocked(tripped)；
  on_break=notify 同样 blocked；may_clear_at 取第 used−limit+1 早的计入项出窗时刻，
  最终下界 max(计算值, ceil(now)+1s)（N8，契约 §6.3）。
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

    def test_unregistered_conv_with_peer_id_ok(self):
        """会话未注册 + 带 peer_id → B2.1 行为，原样采信（#117 B1 退回 B2.1）。"""
        r = send(self.svc, "bot1", "conv_unregistered", peer_id="bot2")
        self.assertEqual(r["outcome"], "delivered")

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
        config = make_config(auth_token=None, dev_mode=True, port=0)
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
        self.server.server_close()

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


# ---------------------------------------------------------------------------
# 11. TestB22ReviewFixes — #117 审查回归
# ---------------------------------------------------------------------------

def _put_peer_rule(limits: LimitStore, peer_id: str, **rules) -> None:
    """为 peer 层写入规则（scope_id = peer_id）。"""
    limits.put(
        {
            "layer": "peer",
            "scope": "peer",
            "scope_id": peer_id,
            "rules": rules,
        },
        now=datetime.datetime.now(timezone.utc),
    )


class TestB22ReviewFixes(unittest.TestCase):
    """#117 审查修订回归：未注册会话、冷却独立、在途隔离、deferred 计数等。"""

    def setUp(self):
        self.svc, _, self.store, self.limits, self.clock = make_service()

    # ------------------------------------------------------------------
    # T1  未注册会话经 HTTP 带 peer_id → delivered；peer 层生效；usage 有 peer_id
    # ------------------------------------------------------------------
    def test_unregistered_conv_with_peer_id_delivered_over_http(self):
        config = make_config(auth_token=None, dev_mode=True, port=0)
        transport = InMemoryMatrixTransport()
        store = UsageStore(max_events=0)
        limits = LimitStore(config)
        clock = FixedClock()
        svc = GatewayService(config, transport, store, limits, now_fn=clock)
        _put_peer_rule(limits, "bot2", messages_per_window=5, window_seconds=3600)
        server = build_server(config, transport)
        server.service = svc
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = json.dumps({
                "sender_persona_id": "bot1",
                "peer_id": "bot2",
                "content": {"type": "text", "text": "hello"},
            }).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/v1/conversations/conv_unreg/messages",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req) as resp:
                status = resp.status
                receipt = json.loads(resp.read())
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(status, 200, receipt)
        self.assertEqual(receipt["outcome"], "delivered", receipt)
        # peer 层生效：usage 事件带 peer_id
        sent = [e for e in store.all_events() if e.peer_id == "bot2"]
        self.assertTrue(len(sent) >= 1, "usage event must carry peer_id")

    # ------------------------------------------------------------------
    # T2  未注册会话不带 peer_id → delivered（B2.1 行为，peer 层不生效）
    # ------------------------------------------------------------------
    def test_unregistered_conv_without_peer_id_not_blocked(self):
        _put_peer_rule(self.limits, "bot2", messages_per_window=5, window_seconds=3600)
        r = send(self.svc, "bot1", "conv_unreg")
        self.assertEqual(r["outcome"], "delivered", r)

    # ------------------------------------------------------------------
    # T3  convB 有在途预留时 convA 的 anti_spam 不被误伤
    # ------------------------------------------------------------------
    def test_inflight_in_other_conv_does_not_block_anti_spam(self):
        put_anti_spam(self.limits, "bot1", max_consecutive=2, cooldown_seconds=30)
        # 构造一条 convB 的在途预留
        self.svc._reservations.reserve(
            persona_id="bot1",
            tokens=10,
            at=self.clock(),
            conversation_id="conv_b",
        )
        # convA 发 2 条（应不被 convB 的在途计入 convA 的连续数）
        for _ in range(2):
            r = send(self.svc, "bot1", "conv_a")
            self.assertEqual(r["outcome"], "delivered", r)

    # ------------------------------------------------------------------
    # T4  UsageStore(max_events=10)：触发冷却后重试 20 次全 blocked，冷却后放行；
    #     冷却期 blocked 事件数 ≤ 2
    # ------------------------------------------------------------------
    def test_cooldown_independent_of_event_table_capacity(self):
        config = make_config(rate_limit_messages=100, rate_limit_window_seconds=3600)
        clock = FixedClock()
        transport = InMemoryMatrixTransport()
        store = UsageStore(max_events=10)   # 小容量：事件容易被淘汰
        limits = LimitStore(config)
        svc = GatewayService(config, transport, store, limits, now_fn=clock)
        put_anti_spam(limits, "bot1", max_consecutive=2, cooldown_seconds=60)

        for _ in range(2):
            r = send(svc, "bot1", "conv1")
            self.assertEqual(r["outcome"], "delivered", r)
        # 触发
        r = send(svc, "bot1", "conv1")
        self.assertEqual(r["outcome"], "blocked", r)
        codes = [x["code"] for x in r["decision"]["reasons"]]
        self.assertIn(CODE_ANTI_SPAM_CONSECUTIVE, codes)
        # 冷却期内 20 次重试全 blocked
        for _ in range(20):
            r = send(svc, "bot1", "conv1")
            self.assertEqual(r["outcome"], "blocked", r)
        # blocked 事件数 ≤ 2（冷却期内最多 1 条，加上首次触发 consecutive 1 条）
        blocked_events = [e for e in store.all_events() if e.kind == EventKind.BLOCKED]
        self.assertLessEqual(len(blocked_events), 2, blocked_events)
        # 冷却结束后放行
        clock.advance(seconds=61)
        r = send(svc, "bot1", "conv1")
        self.assertEqual(r["outcome"], "delivered", r)

    # ------------------------------------------------------------------
    # T5  平台 rate 2/3600 + anti_spam max=2 cooldown=60：
    #     第3条同时 rate+anti_spam 失败仍建立锚点；冷却结束后 anti_spam allowed
    # ------------------------------------------------------------------
    def test_dual_fail_still_anchors_cooldown_and_clears(self):
        # 平台 rate=2 (via LimitStore)
        self.limits.put(
            {
                "layer": "owner",
                "scope": "persona",
                "scope_id": "bot1",
                "rules": {"messages_per_window": 2, "window_seconds": 3600},
            },
            now=self.clock(),
        )
        put_anti_spam(self.limits, "bot1", max_consecutive=2, cooldown_seconds=60)
        for _ in range(2):
            r = send(self.svc, "bot1", "conv1")
            self.assertEqual(r["outcome"], "delivered", r)
        # 第3条：rate 被拦 + anti_spam 同时触发
        r = send(self.svc, "bot1", "conv1")
        self.assertEqual(r["outcome"], "blocked", r)
        codes = [x["code"] for x in r["decision"]["reasons"]]
        # 至少有一个是 anti_spam.consecutive（锚点应已建立）
        self.assertIn(CODE_ANTI_SPAM_CONSECUTIVE, codes)
        # 让时钟跳过 rate 窗口 + 冷却期
        self.clock.advance(seconds=3661)
        # anti_spam 冷却结束后 check 应为 allowed
        decision = self.svc._engine.evaluate(
            persona_id="bot1",
            conversation_id="conv1",
            now=self.clock(),
        )
        anti_spam_check = next(
            (c for c in decision.checks if c.key == "anti_spam.consecutive"), None
        )
        self.assertIsNotNone(anti_spam_check, "anti_spam.consecutive check missing")
        self.assertTrue(anti_spam_check.allowed, "anti_spam should be allowed after cooldown")

    # ------------------------------------------------------------------
    # T6  cooldown_seconds=0：触发后下一条 anti_spam 放行，且 reset_at ≠ now 活锁
    # ------------------------------------------------------------------
    def test_zero_cooldown_no_livelock(self):
        put_anti_spam(self.limits, "bot1", max_consecutive=1, cooldown_seconds=0)
        # 触发（第2条）
        send(self.svc, "bot1", "conv1")
        r = send(self.svc, "bot1", "conv1")
        self.assertEqual(r["outcome"], "blocked", r)
        codes = [x["code"] for x in r["decision"]["reasons"]]
        self.assertIn(CODE_ANTI_SPAM_CONSECUTIVE, codes)
        # 下一条（不 advance 时钟）应直接放行，不进入 cooldown 循环
        r2 = send(self.svc, "bot1", "conv1")
        self.assertEqual(r2["outcome"], "delivered", r2)

    # ------------------------------------------------------------------
    # T7  仅在途阻塞时 messages/tokens reset_at 非 None 且 > now
    # ------------------------------------------------------------------
    def test_inflight_only_reset_at_is_future(self):
        svc, _, store, limits, clock = make_service(
            rate_limit_messages=1, rate_limit_window_seconds=3600
        )
        r1 = send(svc, "bot1", "conv1")
        self.assertEqual(r1["outcome"], "delivered", r1)
        # 第2条被 rate 拦；在途预留 0（投递已完成），事件已提交
        r2 = send(svc, "bot1", "conv1")
        self.assertEqual(r2["outcome"], "blocked", r2)
        checks = {c["key"]: c for c in r2["decision"]["checks"]}
        mw = checks.get("messages.window")
        self.assertIsNotNone(mw)
        self.assertIsNotNone(mw.get("reset_at"), "reset_at must not be None")
        reset_dt = datetime.datetime.strptime(
            mw["reset_at"], "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc)
        self.assertGreater(reset_dt, clock())

    # ------------------------------------------------------------------
    # T8  peer 层 tokens_per_window：bot2 用尽不影响 bot3（按 (persona, peer_id)）
    # ------------------------------------------------------------------
    def test_peer_token_budget_isolated_by_peer_id(self):
        _put_peer_rule(self.limits, "bot2", tokens_per_window=4, window_seconds=3600)
        # 不注册会话，未注册走 B2.1：直接采信 peer_id
        # 用尽 bot2 peer 层 token 预算（每条文本 "hi" ≈ 1 token）
        for _ in range(4):
            r = send(self.svc, "bot1", "conv1", text="hi", peer_id="bot2")
            self.assertEqual(r["outcome"], "delivered", r)
        r = send(self.svc, "bot1", "conv1", text="hi", peer_id="bot2")
        self.assertEqual(r["outcome"], "blocked", r)
        # bot3 不受影响
        r3 = send(self.svc, "bot1", "conv1", text="hi", peer_id="bot3")
        self.assertEqual(r3["outcome"], "delivered", r3)

    def test_peer_min_interval_isolated_by_peer_id(self):
        """peer 层 min_seconds_between_messages 按 (persona, peer_id) 计：刚给 bot2 发过不影响给 bot3 发。"""
        _put_peer_rule(self.limits, "bot2", min_seconds_between_messages=600)
        _put_peer_rule(self.limits, "bot3", min_seconds_between_messages=600)
        r = send(self.svc, "bot1", "conv1", peer_id="bot2")
        self.assertEqual(r["outcome"], "delivered", r)
        r = send(self.svc, "bot1", "conv1", peer_id="bot2")
        self.assertEqual(r["outcome"], "blocked", r)
        r3 = send(self.svc, "bot1", "conv1", peer_id="bot3")
        self.assertEqual(r3["outcome"], "delivered", r3)

    # ------------------------------------------------------------------
    # T9  静默时段 deferred 计入连续数（anti_spam used 递增）
    # ------------------------------------------------------------------
    def test_deferred_counts_toward_anti_spam_consecutive(self):
        # 本用例单独开启静默时段（Asia/Shanghai 23:00-07:30）
        self.svc, _, self.store, self.limits, self.clock = make_service(
            silent_hours_enabled=True,
            silent_hours_start="23:00",
            silent_hours_end="07:30",
            silent_hours_timezone="Asia/Shanghai",
        )
        self.clock.set("2026-11-03T16:00:00Z")  # 00:00 Asia/Shanghai，落在默认静默 23:00-07:30
        put_anti_spam(self.limits, "bot1", max_consecutive=3, cooldown_seconds=60)
        # 静默时段内发言 → deferred
        r = send(self.svc, "bot1", "conv1")
        self.assertEqual(r["outcome"], "deferred", r)
        # deferred 应写入 UsageStore（kind=DEFERRED）
        events = self.store.all_events()
        deferred = [e for e in events if e.kind == EventKind.DEFERRED]
        self.assertTrue(len(deferred) >= 1, "deferred event must be recorded")
        # anti_spam 视角里 used 应包含此 deferred
        self.clock.set("2026-11-04T04:00:00Z")  # 12:00 Asia/Shanghai，白天
        decision = self.svc._engine.evaluate(
            persona_id="bot1",
            conversation_id="conv1",
            now=self.clock(),
        )
        anti_spam_check = next(
            (c for c in decision.checks if c.key == "anti_spam.consecutive"), None
        )
        self.assertIsNotNone(anti_spam_check)
        self.assertGreater(anti_spam_check.used, 0, "deferred should be counted")

    # ------------------------------------------------------------------
    # T10 保留期：早于 retention_seconds 的事件在 append 后被淘汰
    # ------------------------------------------------------------------
    def test_retention_evicts_old_events(self):
        base = datetime.datetime(2026, 11, 3, 12, 0, 0, tzinfo=timezone.utc)
        store = UsageStore(max_events=0, retention_seconds=3600)
        # 注入 5 条比 retention 更老的事件（2h 前）
        for i in range(5):
            store.append(
                UsageEvent.make(
                    EventKind.SENT,
                    persona_id="bot1",
                    at=base - timedelta(hours=2, seconds=i),
                )
            )
        self.assertEqual(len(store), 5)
        # append 一条 now 事件 → 旧事件被淘汰
        store.append(
            UsageEvent.make(EventKind.SENT, persona_id="bot1", at=base)
        )
        remaining = store.all_events()
        cutoff = base - timedelta(seconds=3600)
        for e in remaining:
            self.assertGreater(
                e.recorded_at, cutoff,
                f"event at {e.recorded_at} is older than retention cutoff {cutoff}"
            )
        self.assertLess(len(remaining), 6, "old events should have been evicted")

    # ------------------------------------------------------------------
    # T11 已注册会话，仅 sender 有 peer 规则，缺 peer_id → 不 422
    # ------------------------------------------------------------------
    def test_registered_conv_only_sender_has_peer_rule_no_422(self):
        self.svc.register_conversation("conv_reg", ["bot1", "bot2"])
        # bot1 (sender) 有 peer 规则；bot2 没有
        _put_peer_rule(self.limits, "bot1", messages_per_window=10, window_seconds=3600)
        # 发送方=bot1，无 peer_id → 对方 bot2 无规则，不应 422
        r = send(self.svc, "bot1", "conv_reg")
        self.assertEqual(r["outcome"], "delivered", r)

    # ------------------------------------------------------------------
    # T12 evaluate 带 peer_id 无 conversation_id：不 422，peer 层生效
    # ------------------------------------------------------------------
    def test_evaluate_with_peer_id_no_conv_id_not_422(self):
        _put_peer_rule(self.limits, "bot2", messages_per_window=5, window_seconds=3600)
        result = self.svc.evaluate_policy({
            "persona_id": "bot1",
            "peer_id": "bot2",
            "content": "hello",
        })
        # peer 层应生效（有 messages.window check）
        self.assertIsNotNone(result)
        self.assertEqual(result.get("decision"), "allow")

    # ------------------------------------------------------------------
    # T13 空串 peer_id → evaluate 与 send 都 400
    # ------------------------------------------------------------------
    def test_empty_peer_id_is_400_for_evaluate(self):
        with self.assertRaises(ValidationError) as ctx:
            self.svc.evaluate_policy({
                "persona_id": "bot1",
                "peer_id": "",
                "content": "hello",
            })
        self.assertEqual(ctx.exception.http_status, 400)

    def test_empty_peer_id_is_400_for_send(self):
        with self.assertRaises(ValidationError) as ctx:
            send(self.svc, "bot1", "conv1", peer_id="")
        self.assertEqual(ctx.exception.http_status, 400)

    # ------------------------------------------------------------------
    # T13 (#117 B6/N6a) ai_loop max=2、max_events=20：blocked 重试 30 次仍 blocked，
    #     冷却/限额不被重置，SENT 计数事件仍在
    # ------------------------------------------------------------------
    def test_blocked_flood_cannot_reset_ai_loop_counts(self):
        config = make_config()
        clock = FixedClock()
        transport = InMemoryMatrixTransport()
        store = UsageStore(max_events=20)
        limits = LimitStore(config)
        svc = GatewayService(config, transport, store, limits, now_fn=clock)
        put_ai_loop(limits, "bot1", max_per_hour=2)

        for _ in range(2):
            r = send(svc, "bot1", "conv1")
            self.assertEqual(r["outcome"], "delivered", r)
        # 第3条：熔断触发
        r = send(svc, "bot1", "conv1")
        self.assertEqual(r["outcome"], "blocked", r)
        codes = [x["code"] for x in r["decision"]["reasons"]]
        self.assertIn(CODE_AI_LOOP_TRIPPED, codes)

        # 30 次被拒重试：全部仍 blocked（计数事件未被挤掉）
        for _ in range(30):
            r = send(svc, "bot1", "conv1")
            self.assertEqual(r["outcome"], "blocked", r)

        sent = [e for e in store.all_events() if e.kind == EventKind.SENT]
        self.assertEqual(len(sent), 2, "SENT 计数事件不得被 blocked 洪泛挤掉")

        decision = svc._engine.evaluate(
            persona_id="bot1",
            conversation_id="conv1",
            now=clock(),
        )
        ai_loop = next(
            (c for c in decision.checks if c.key == "ai_loop.per_hour"), None
        )
        self.assertIsNotNone(ai_loop)
        self.assertFalse(ai_loop.allowed, "限额不得被重试重置")
        self.assertGreaterEqual(ai_loop.used, 2, "已用计数不得被重试清零")

    # ------------------------------------------------------------------
    # T14 (#117 B6/N6b) anti_spam max=50：发 10 条后连发 30 条超长
    #     （reason=message.length），anti_spam used 不得降为 0
    # ------------------------------------------------------------------
    def test_blocked_flood_message_length_does_not_reset_anti_spam(self):
        config = make_config(max_message_chars=8000)
        clock = FixedClock()
        transport = InMemoryMatrixTransport()
        store = UsageStore(max_events=20)  # 小容量：旧实现下 30 条 blocked 会挤掉 SENT
        limits = LimitStore(config)
        svc = GatewayService(config, transport, store, limits, now_fn=clock)
        limits.put(
            {
                "layer": "owner",
                "scope": "persona",
                "scope_id": "bot1",
                "rules": {
                    "max_message_chars": 20,
                    "anti_spam": {
                        "max_consecutive_ai_messages": 50,
                        "cooldown_seconds": 60,
                    },
                },
            },
            now=clock(),
        )

        for _ in range(10):
            r = send(svc, "bot1", "conv1")
            self.assertEqual(r["outcome"], "delivered", r)
        # 30 条超长：被 blocked（message.length），不是 anti_spam.cooldown
        for _ in range(30):
            r = send(svc, "bot1", "conv1", text="x" * 30)
            self.assertEqual(r["outcome"], "blocked", r)
            codes = [x["code"] for x in r["decision"]["reasons"]]
            self.assertIn(CODE_MESSAGE_LENGTH, codes)

        sent = [e for e in store.all_events() if e.kind == EventKind.SENT]
        self.assertEqual(len(sent), 10, "SENT 计数事件不得被 blocked 洪泛挤掉")

        decision = svc._engine.evaluate(
            persona_id="bot1",
            conversation_id="conv1",
            now=clock(),
        )
        anti_spam = next(
            (c for c in decision.checks if c.key == "anti_spam.consecutive"), None
        )
        self.assertIsNotNone(anti_spam)
        self.assertEqual(anti_spam.used, 10, "anti_spam used 不得被洪泛清零")
        self.assertTrue(anti_spam.allowed)

    # ------------------------------------------------------------------
    # T15 (#117 N6c) 真·仅在途：PolicyEngine + ReservationBook.reserve 后 evaluate，
    #     messages / tokens 的 reset_at 非 None 且 > now（不依赖已落库事件）
    # ------------------------------------------------------------------
    def test_inflight_only_blocks_with_future_reset_at(self):
        config = make_config(
            rate_limit_messages=1,
            platform_token_budget=100,
            rate_limit_window_seconds=3600,
            silent_hours_enabled=False,
        )
        clock = FixedClock()
        store = UsageStore(max_events=0)
        reservations = ReservationBook()
        engine = PolicyEngine(
            config, store, limit_store=None, now_fn=clock, reservations=reservations
        )
        now = clock()
        # 预留 tokens=100 占满平台 token 预算（budget=100）+ 1 个消息名额（limit=1）
        reservations.reserve(
            persona_id="bot1", tokens=100, at=now, conversation_id="conv1"
        )

        decision = engine.evaluate(persona_id="bot1", text="hello", now=now)
        checks = {c.key: c for c in decision.checks}
        for key in ("messages.window", "tokens.window"):
            check = checks.get(key)
            self.assertIsNotNone(check, f"{key} missing")
            self.assertFalse(check.allowed, f"{key} 应仅在途阻塞")
            self.assertIsNotNone(check.reset_at, f"{key}.reset_at must not be None")
            reset_dt = datetime.datetime.strptime(
                check.reset_at, "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc)
            self.assertGreater(reset_dt, now, f"{key}.reset_at must be > now")


if __name__ == "__main__":
    unittest.main()
