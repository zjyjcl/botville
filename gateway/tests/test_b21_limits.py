# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""B2.1 三层限额与 token 预算的单元/集成测试（Issue #17）。

覆盖：
- `LimitStore`：规则校验、逐项取最严、平台底线只读、token 预算；
- `PolicyEngine`：消息/token/间隔/静默/主动发言/小镇 token 的 checks[] 与 winner_layer；
- `GatewayService`：GET/PUT /limits 形态、GET /usage、GET /usage/events、散漏字段；
- #92 剩余项：限制窗口开区间、释放时刻向上取整、文本不入库、容量上限与淘汰。
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from datetime import timedelta, timezone

GATEWAY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(GATEWAY_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from botville_gateway.config import Config  # noqa: E402
from botville_gateway.errors import LimitError, ValidationError  # noqa: E402
from botville_gateway.http_server import MAX_BODY_BYTES, build_server  # noqa: E402
from botville_gateway.limits import (  # noqa: E402
    CODE_OUT_OF_RANGE,
    LimitStore,
    validate_rules,
)
from botville_gateway.matrix import InMemoryMatrixTransport  # noqa: E402
from botville_gateway.policy import PolicyEngine, estimate_tokens  # noqa: E402
from botville_gateway.service import GatewayService  # noqa: E402
from botville_gateway.store import EventKind, UsageEvent, UsageStore  # noqa: E402


class FixedClock:
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
        rate_limit_messages=5,
        rate_limit_window_seconds=3600,
        silent_hours_enabled=False,
        max_message_chars=8000,
        platform_token_budget=1000,
        platform_proactive_per_day=3,
    )
    base.update(overrides)
    return Config(**base)


def make_service(config: Config = None, *, start: str = None):
    config = config or make_config()
    clock = FixedClock(start) if start else FixedClock()
    transport = InMemoryMatrixTransport()
    store = UsageStore(max_events=0)
    limits = LimitStore(config)
    svc = GatewayService(config, transport, store, limits, now_fn=clock)
    return svc, transport, store, limits, clock


# ---------------------------------------------------------------------------
# 1. LimitStore：规则校验
# ---------------------------------------------------------------------------
class TestValidateRules(unittest.TestCase):
    def test_accepts_all_documented_fields(self):
        rules = validate_rules({
            "window_seconds": 600,
            "messages_per_window": 2,
            "tokens_per_window": 100,
            "proactive_per_day": 1,
            "town_tokens_per_day": 50,
            "max_message_chars": 100,
            "min_seconds_between_messages": 30,
            "silent_hours": {
                "enabled": True,
                "start": "23:00",
                "end": "07:30",
                "timezone": "Asia/Shanghai",
            },
            "anti_spam": {"max_consecutive_ai_messages": 3, "cooldown_seconds": 10},
            "ai_loop": {"max_ai_messages_per_hour": 5, "on_break": "notify"},
        })
        self.assertEqual(rules["messages_per_window"], 2)
        self.assertEqual(rules["silent_hours"]["timezone"], "Asia/Shanghai")
        self.assertEqual(rules["ai_loop"]["on_break"], "notify")

    def test_zero_is_a_real_limit_not_unlimited(self):
        self.assertEqual(validate_rules({"messages_per_window": 0})["messages_per_window"], 0)

    def test_omitted_fields_are_absent(self):
        self.assertNotIn("messages_per_window", validate_rules({}))

    def test_rejects_unknown_field(self):
        with self.assertRaises(ValidationError):
            validate_rules({"max_message_per_window": 3})

    def test_rejects_bool_as_int(self):
        with self.assertRaises(ValidationError):
            validate_rules({"messages_per_window": True})

    def test_out_of_range_raises_limit_error(self):
        with self.assertRaises(LimitError) as ctx:
            validate_rules({"window_seconds": 10})
        self.assertEqual(ctx.exception.code, CODE_OUT_OF_RANGE)
        self.assertEqual(ctx.exception.errors[0]["field"], "rules.window_seconds")

    def test_rejects_bad_hhmm(self):
        with self.assertRaises(LimitError):
            validate_rules({
                "silent_hours": {
                    "enabled": True, "start": "25:00", "end": "07:00",
                    "timezone": "UTC",
                }
            })

    def test_rejects_unknown_timezone(self):
        with self.assertRaises(LimitError):
            validate_rules({
                "silent_hours": {
                    "enabled": True, "start": "23:00", "end": "07:00",
                    "timezone": "Not/A_Zone",
                }
            })

    def test_silent_hours_enabled_defaults_true(self):
        rules = validate_rules({
            "silent_hours": {"start": "23:00", "end": "07:00", "timezone": "UTC"}
        })
        self.assertTrue(rules["silent_hours"]["enabled"])

    def test_rejects_bad_on_break(self):
        with self.assertRaises(LimitError):
            validate_rules({"ai_loop": {"on_break": "explode"}})


# ---------------------------------------------------------------------------
# 2. LimitStore：三层取最严 / 平台底线
# ---------------------------------------------------------------------------
class TestLimitStore(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config()
        self.store = LimitStore(self.cfg)
        self.now = datetime.datetime(2026, 11, 3, 4, 0, tzinfo=timezone.utc)

    def put(self, layer, scope, scope_id, rules):
        return self.store.put(
            {"layer": layer, "scope": scope, "scope_id": scope_id, "rules": rules},
            now=self.now,
        )

    def test_platform_layer_read_only(self):
        with self.assertRaises(ValidationError):
            self.put("platform", "peer", "platform_default", {"messages_per_window": 1})

    def test_platform_defaults_present(self):
        eff = self.store.effective(persona_id="p", now=self.now)
        self.assertEqual(eff.value("messages_per_window"), 5)
        self.assertEqual(eff.get("messages_per_window").source_layer, "platform")
        self.assertEqual(eff.value("tokens_per_window"), 1000)

    def test_zero_config_means_unset_for_platform(self):
        cfg = make_config(rate_limit_messages=0, platform_token_budget=0,
                          platform_proactive_per_day=0)
        store = LimitStore(cfg)
        eff = store.effective(persona_id="p", now=self.now)
        self.assertIsNone(eff.get("messages_per_window"))
        self.assertIsNone(eff.get("tokens_per_window"))
        self.assertIsNone(eff.get("proactive_per_day"))

    def test_owner_tightens_and_wins_per_item(self):
        self.put("owner", "persona", "p", {"messages_per_window": 2})
        eff = self.store.effective(persona_id="p", now=self.now)
        messages = eff.get("messages_per_window")
        tokens = eff.get("tokens_per_window")
        self.assertEqual(messages.value, 2)
        self.assertEqual(messages.source_layer, "owner")
        self.assertEqual(messages.source_id, "p")
        # token 未被 owner 收紧 → 仍归 platform
        self.assertEqual(tokens.value, 1000)
        self.assertEqual(tokens.source_layer, "platform")

    def test_equal_value_keeps_platform(self):
        self.put("owner", "persona", "p", {"messages_per_window": 5})
        eff = self.store.effective(persona_id="p", now=self.now)
        self.assertEqual(eff.get("messages_per_window").source_layer, "platform")

    def test_peer_layer_is_stricter_than_owner(self):
        self.put("owner", "persona", "p", {"messages_per_window": 4})
        self.put("peer", "peer", "peer_persona", {"messages_per_window": 2})
        eff = self.store.effective(
            persona_id="p", peer_id="peer_persona", now=self.now
        )
        winner = eff.get("messages_per_window")
        self.assertEqual(winner.value, 2)
        self.assertEqual(winner.source_layer, "peer")
        self.assertEqual(winner.source_id, "peer_persona")

    def test_loosening_above_platform_is_rejected(self):
        with self.assertRaises(LimitError) as ctx:
            self.put("owner", "persona", "p", {"messages_per_window": 6})
        self.assertEqual(ctx.exception.code, "limit.out_of_range")
        self.assertEqual(ctx.exception.errors[0]["field"], "rules.messages_per_window")
        # B1：消息数是速率对，错误信息按 `count/window` 给出
        self.assertIn("looser than the platform floor 5/3600s",
                      ctx.exception.errors[0]["message"])

    def test_loosening_nested_rules_is_rejected(self):
        cfg = make_config()
        cfg = Config(**{**cfg.__dict__, "platform_token_budget": 1000})
        store = LimitStore(cfg)
        platform = store.platform.rules
        platform["anti_spam"] = {"max_consecutive_ai_messages": 3, "cooldown_seconds": 60}
        with self.assertRaises(LimitError) as ctx:
            store.put(
                {"layer": "owner", "scope": "persona", "scope_id": "p",
                 "rules": {"anti_spam": {"max_consecutive_ai_messages": 5}}},
                now=self.now,
            )
        self.assertEqual(
            ctx.exception.errors[0]["field"],
            "rules.anti_spam.max_consecutive_ai_messages",
        )

    def test_cannot_disable_platform_silent_hours(self):
        with self.assertRaises(LimitError) as ctx:
            cfg = make_config(silent_hours_enabled=True)
            store = LimitStore(cfg)
            self.store = store
            self.put("owner", "persona", "p", {
                "silent_hours": {
                    "enabled": False, "start": "23:00", "end": "07:00", "timezone": "UTC",
                }
            })
        self.assertEqual(ctx.exception.errors[0]["field"], "rules.silent_hours.enabled")

    def test_owner_can_tighten_silent_window(self):
        cfg = make_config(silent_hours_enabled=True)
        store = LimitStore(cfg)
        self.store = store
        layer = self.put("owner", "persona", "p", {
            "silent_hours": {
                "enabled": True, "start": "22:00", "end": "06:00", "timezone": "Asia/Shanghai",
            }
        })
        self.assertEqual(layer.rules["silent_hours"]["start"], "22:00")

    def test_min_interval_direction_is_max(self):
        # 平台没设 min_seconds_between_messages → owner 可以设任意值
        self.put("owner", "persona", "p", {"min_seconds_between_messages": 30})
        eff = self.store.effective(persona_id="p", now=self.now)
        self.assertEqual(eff.get("min_seconds_between_messages").value, 30)

    def test_peer_scope_mismatch_rejected(self):
        with self.assertRaises(LimitError):
            self.put("peer", "persona", "p", {"proactive_per_day": 1})
        with self.assertRaises(LimitError):
            self.put("owner", "peer", "x", {"proactive_per_day": 1})

    def test_missing_required_field(self):
        with self.assertRaises(ValidationError):
            self.store.put({"layer": "owner"}, now=self.now)

    def test_non_object_body(self):
        with self.assertRaises(ValidationError):
            self.store.put([], now=self.now)

    def test_include_platform_false_hides_it(self):
        eff = self.store.effective(persona_id="p", now=self.now, include_platform=False)
        self.assertEqual([layer.layer for layer in eff.layers], [])

    def test_conversation_layer_participates(self):
        self.put("owner", "conversation", "conv_a", {"messages_per_window": 1})
        eff = self.store.effective(conversation_id="conv_a", now=self.now)
        self.assertEqual(eff.get("messages_per_window").value, 1)
        self.assertEqual(eff.get("messages_per_window").source_id, "conv_a")
        self.assertEqual(eff.scope, "conversation")
        self.assertEqual(eff.scope_id, "conv_a")

    def test_rules_are_copied_not_aliased(self):
        self.put("owner", "persona", "p", {"messages_per_window": 2})
        eff = self.store.effective(persona_id="p", now=self.now)
        body = eff.to_dict()
        body["layers"][1]["rules"]["messages_per_window"] = 99
        again = self.store.effective(persona_id="p", now=self.now).to_dict()
        self.assertEqual(again["layers"][1]["rules"]["messages_per_window"], 2)

    def test_antispam_and_ai_loop_effective_values(self):
        platform = self.store.platform.rules
        platform["anti_spam"] = {"max_consecutive_ai_messages": 4, "cooldown_seconds": 60}
        platform["ai_loop"] = {"max_ai_messages_per_hour": 20}
        self.put("owner", "persona", "p", {
            "anti_spam": {"max_consecutive_ai_messages": 2},
            "ai_loop": {"max_ai_messages_per_hour": 10},
        })
        eff = self.store.effective(persona_id="p", now=self.now)
        self.assertEqual(eff.value("anti_spam.max_consecutive_ai_messages"), 2)
        self.assertEqual(eff.value("ai_loop.max_ai_messages_per_hour"), 10)
        # cooldown 越大越严，只有平台设了 → 归平台
        self.assertEqual(eff.value("anti_spam.cooldown_seconds"), 60)
        self.assertEqual(
            eff.get("anti_spam.cooldown_seconds").source_layer, "platform"
        )

    def test_silent_window_helper_returns_owner_window(self):
        cfg = make_config(silent_hours_enabled=False)
        store = LimitStore(cfg)
        store.put(
            {"layer": "owner", "scope": "persona", "scope_id": "p",
             "rules": {"silent_hours": {
                 "enabled": True, "start": "22:00", "end": "06:00",
                 "timezone": "Asia/Shanghai"}}},
            now=self.now,
        )
        eff = store.effective(persona_id="p", now=self.now)
        window = store.silent_window(eff)
        self.assertTrue(window["enabled"])
        self.assertEqual(window["start"], "22:00")
        self.assertEqual(window["timezone"], "Asia/Shanghai")

    def test_silent_window_disabled_default(self):
        cfg = make_config(silent_hours_enabled=False)
        store = LimitStore(cfg)
        eff = store.effective(persona_id="p", now=self.now)
        window = store.silent_window(eff)
        self.assertFalse(window["enabled"])


# ---------------------------------------------------------------------------
# 3. PolicyEngine：三层 checks[] / winner_layer
# ---------------------------------------------------------------------------
class TestPolicyThreeLayers(unittest.TestCase):
    def build(self, config=None, start="2026-11-03T04:00:00Z"):
        cfg = config or make_config()
        clock = FixedClock(start)
        store = UsageStore(max_events=0)
        limits = LimitStore(cfg)
        return PolicyEngine(cfg, store, limits, now_fn=clock), store, limits, clock

    def test_owner_message_limit_wins_and_blocks(self):
        engine, store, limits, clock = self.build()
        limits.put(
            {"layer": "owner", "scope": "persona", "scope_id": "p",
             "rules": {"messages_per_window": 1}},
            now=clock(),
        )
        store.append(UsageEvent.make(EventKind.SENT, persona_id="p", at=clock(),
                                     tokens_out=1))
        decision = engine.evaluate(persona_id="p", now=clock())
        self.assertEqual(decision.decision, "block")
        self.assertEqual(decision.winner_layer, "owner")
        check = {c.key: c for c in decision.checks}["messages.window"]
        self.assertEqual(check.layer, "owner")
        self.assertEqual(check.limit, 1)
        self.assertEqual(check.used, 1)
        self.assertEqual(check.code, "messages.window.exceeded")

    def test_platform_wins_when_owner_did_not_set_the_item(self):
        """逐层比较：owner 只收紧消息数，token 仍由平台胜出。"""
        engine, store, limits, clock = self.build()
        limits.put(
            {"layer": "owner", "scope": "persona", "scope_id": "p",
             "rules": {"messages_per_window": 4}},
            now=clock(),
        )
        for _ in range(5):
            store.append(UsageEvent.make(EventKind.SENT, persona_id="p", at=clock(),
                                         tokens_out=1))
        decision = engine.evaluate(persona_id="p", now=clock())
        checks = {c.key: c for c in decision.checks}
        # 消息数：owner 4 < 平台 5 → owner 胜出
        self.assertEqual(checks["messages.window"].layer, "owner")
        self.assertEqual(checks["messages.window"].limit, 4)
        # token：owner 未设置 → platform 胜出
        self.assertEqual(checks["tokens.window"].layer, "platform")
        self.assertEqual(checks["tokens.window"].limit, 1000)

    def test_token_budget_precheck_blocks(self):
        engine, store, limits, clock = self.build()
        store.append(UsageEvent.make(EventKind.SENT, persona_id="p", at=clock(),
                                     tokens_out=990))
        decision = engine.evaluate(persona_id="p", text="x", token_estimate=20,
                                   now=clock())
        self.assertEqual(decision.decision, "block")
        self.assertEqual(decision.winner_layer, "platform")
        token = {c.key: c for c in decision.checks}["tokens.window"]
        self.assertFalse(token.allowed)
        self.assertEqual(token.limit, 1000)
        self.assertEqual(token.used, 990)
        self.assertEqual(token.code, "tokens.window.exceeded")
        self.assertIsNotNone(token.reset_at)

    def test_token_budget_exact_fit_allowed(self):
        engine, store, limits, clock = self.build()
        store.append(UsageEvent.make(EventKind.SENT, persona_id="p", at=clock(),
                                     tokens_out=980))
        decision = engine.evaluate(persona_id="p", token_estimate=20, now=clock())
        token = {c.key: c for c in decision.checks}["tokens.window"]
        self.assertTrue(token.allowed)
        self.assertEqual(token.remaining, 20)

    def test_owner_token_limit_wins(self):
        engine, store, limits, clock = self.build()
        limits.put(
            {"layer": "owner", "scope": "persona", "scope_id": "p",
             "rules": {"tokens_per_window": 100}},
            now=clock(),
        )
        decision = engine.evaluate(persona_id="p", token_estimate=150, now=clock())
        token = {c.key: c for c in decision.checks}["tokens.window"]
        self.assertEqual(token.limit, 100)
        self.assertEqual(token.layer, "owner")
        self.assertEqual(decision.winner_layer, "owner")
        # 单次请求就超过总预算 → 等多久都不会恢复，reset_at 省略
        self.assertIsNone(token.reset_at)

    def test_oversized_request_with_no_events_has_no_reset(self):
        engine, store, limits, clock = self.build()
        decision = engine.evaluate(persona_id="p", token_estimate=5000, now=clock())
        self.assertEqual(decision.decision, "block")
        self.assertIsNone(decision.may_clear_at)

    def test_min_interval_blocks(self):
        engine, store, limits, clock = self.build()
        limits.put(
            {"layer": "owner", "scope": "persona", "scope_id": "p",
             "rules": {"min_seconds_between_messages": 30}},
            now=clock(),
        )
        store.append(UsageEvent.make(EventKind.SENT, persona_id="p", at=clock(),
                                     tokens_out=1))
        decision = engine.evaluate(persona_id="p", now=clock())
        check = {c.key: c for c in decision.checks}["min_interval"]
        self.assertFalse(check.allowed)
        self.assertEqual(check.limit, 30)
        self.assertEqual(check.code, "min_interval.too_soon")
        self.assertEqual(decision.decision, "block")
        self.assertEqual(decision.winner_layer, "owner")

        clock.advance(seconds=31)
        decision2 = engine.evaluate(persona_id="p", now=clock())
        self.assertNotEqual(decision2.decision, "block")

    def test_min_interval_zero_is_disabled(self):
        engine, store, limits, clock = self.build()
        limits.put(
            {"layer": "owner", "scope": "persona", "scope_id": "p",
             "rules": {"min_seconds_between_messages": 0}},
            now=clock(),
        )
        store.append(UsageEvent.make(EventKind.SENT, persona_id="p", at=clock(),
                                     tokens_out=1))
        decision = engine.evaluate(persona_id="p", now=clock())
        check = {c.key: c for c in decision.checks}["min_interval"]
        self.assertTrue(check.allowed)

    def test_proactive_per_day_only_for_proactive_kind(self):
        engine, store, limits, clock = self.build()
        for _ in range(3):
            store.append(UsageEvent.make(EventKind.PROACTIVE, persona_id="p",
                                         at=clock()))
        normal = engine.evaluate(persona_id="p", kind="message", now=clock())
        self.assertNotIn("proactive.per_day", [c.key for c in normal.checks])

        proactive = engine.evaluate(persona_id="p", kind="proactive", now=clock())
        check = {c.key: c for c in proactive.checks}["proactive.per_day"]
        self.assertFalse(check.allowed)
        self.assertEqual(check.limit, 3)
        self.assertEqual(check.used, 3)
        self.assertEqual(proactive.decision, "block")

    def test_town_tokens_per_day_only_for_town_action(self):
        engine, store, limits, clock = self.build()
        limits.put(
            {"layer": "owner", "scope": "persona", "scope_id": "p",
             "rules": {"town_tokens_per_day": 100}},
            now=clock(),
        )
        store.append(UsageEvent.make(EventKind.TOWN_ACTION, persona_id="p",
                                     at=clock(), town_tokens=90))
        decision = engine.evaluate(persona_id="p", kind="town_action",
                                   token_estimate=20, now=clock())
        check = {c.key: c for c in decision.checks}["town.tokens_per_day"]
        self.assertFalse(check.allowed)
        self.assertEqual(check.used, 90)
        self.assertEqual(decision.winner_layer, "owner")

    def test_silent_hours_from_owner_layer(self):
        cfg = make_config(silent_hours_enabled=False)
        engine, store, limits, clock = self.build(cfg, start="2026-11-03T15:00:00Z")
        limits.put(
            {"layer": "owner", "scope": "persona", "scope_id": "p",
             "rules": {"silent_hours": {
                 "enabled": True, "start": "23:00", "end": "07:30",
                 "timezone": "Asia/Shanghai"}}},
            now=clock(),
        )
        decision = engine.evaluate(persona_id="p", now=clock())
        self.assertEqual(decision.decision, "defer")
        self.assertEqual(decision.winner_layer, "owner")
        check = {c.key: c for c in decision.checks}["silent_hours"]
        self.assertEqual(check.layer, "owner")
        self.assertEqual(check.source_id, "p")

    def test_platform_silent_hours_beats_owner_disabled(self):
        cfg = make_config(silent_hours_enabled=True)
        engine, store, limits, clock = self.build(cfg, start="2026-11-03T15:00:00Z")
        decision = engine.evaluate(persona_id="p", now=clock())
        self.assertEqual(decision.decision, "defer")
        self.assertEqual(decision.winner_layer, "platform")

    def test_block_beats_defer(self):
        cfg = make_config(silent_hours_enabled=True)
        engine, store, limits, clock = self.build(cfg, start="2026-11-03T15:00:00Z")
        for _ in range(5):
            store.append(UsageEvent.make(EventKind.SENT, persona_id="p", at=clock(),
                                         tokens_out=1))
        decision = engine.evaluate(persona_id="p", now=clock())
        self.assertEqual(decision.decision, "block")
        keys = [c.key for c in decision.checks]
        self.assertIn("silent_hours", keys)
        self.assertIn("messages.window", keys)

    def test_all_checks_are_strictest_per_item(self):
        """一次评估里不同项可以来自不同层（逐项取最严）。"""
        engine, store, limits, clock = self.build()
        limits.put(
            {"layer": "owner", "scope": "persona", "scope_id": "p",
             "rules": {"messages_per_window": 4}},
            now=clock(),
        )
        limits.put(
            {"layer": "owner", "scope": "persona", "scope_id": "p",
             "rules": {"messages_per_window": 4, "tokens_per_window": 500}},
            now=clock(),
        )
        limits.put(
            {"layer": "peer", "scope": "peer", "scope_id": "peer_x",
             "rules": {"tokens_per_window": 200}},
            now=clock(),
        )
        decision = engine.evaluate(
            persona_id="p", peer_id="peer_x", token_estimate=1, now=clock()
        )
        checks = {c.key: c for c in decision.checks}
        self.assertEqual(checks["messages.window"].layer, "owner")
        self.assertEqual(checks["messages.window"].limit, 4)
        self.assertEqual(checks["tokens.window"].layer, "peer")
        self.assertEqual(checks["tokens.window"].limit, 200)
        self.assertEqual(checks["tokens.window"].source_id, "peer_x")

    def test_estimate_tokens(self):
        self.assertEqual(estimate_tokens(""), 0)
        self.assertGreaterEqual(estimate_tokens("hello world"), 1)


# ---------------------------------------------------------------------------
# 4. #92：限速窗口开区间 + 释放时刻向上取整
# ---------------------------------------------------------------------------
class TestWindowBoundaries(unittest.TestCase):
    def build(self, limit=1, window=60, **overrides):
        cfg = make_config(rate_limit_messages=limit,
                          rate_limit_window_seconds=window, **overrides)
        clock = FixedClock("2026-11-03T12:00:00Z")
        store = UsageStore(max_events=0)
        limits = LimitStore(cfg)
        return PolicyEngine(cfg, store, limits, now_fn=clock), store, clock

    def test_event_exactly_at_window_boundary_is_expired(self):
        """`now - window` 恰好等于事件时刻 → 已过期（左开区间）。"""
        engine, store, clock = self.build(window=60)
        start = clock()
        store.append(UsageEvent.make(EventKind.SENT, persona_id="p", at=start))
        clock.advance(seconds=60)
        decision = engine.evaluate(persona_id="p", now=clock())
        check = {c.key: c for c in decision.checks}["messages.window"]
        self.assertEqual(check.used, 0)
        self.assertTrue(check.allowed,
                        "第 60 秒时该事件应已滑出窗口（reset_at 到了就该放行）")

    def test_event_one_second_inside_window_still_counts(self):
        engine, store, clock = self.build(window=60)
        store.append(UsageEvent.make(EventKind.SENT, persona_id="p", at=clock()))
        clock.advance(seconds=59)
        decision = engine.evaluate(persona_id="p", now=clock())
        check = {c.key: c for c in decision.checks}["messages.window"]
        self.assertEqual(check.used, 1)
        self.assertFalse(check.allowed)

    def test_reset_at_rounds_up_to_second(self):
        """23:59:59.5 的释放时刻报 00:00:00，而不是 23:59:59。"""
        cfg = make_config(rate_limit_messages=1, rate_limit_window_seconds=60)
        base = datetime.datetime(2026, 11, 3, 11, 58, 59, 0, tzinfo=timezone.utc)
        store = UsageStore(max_events=0)
        limits = LimitStore(cfg)
        engine = PolicyEngine(cfg, store, limits, now_fn=lambda: base)
        # 事件在 11:58:59.500Z；窗口 60s → 释放 11:59:59.500Z → 向上取整 12:00:00Z
        event_at = base + timedelta(milliseconds=500)
        store.append(UsageEvent.make(EventKind.SENT, persona_id="p", at=event_at))
        decision = engine.evaluate(persona_id="p", now=event_at)
        check = {c.key: c for c in decision.checks}["messages.window"]
        self.assertEqual(check.reset_at, "2026-11-03T12:00:00Z")

    def test_reset_at_with_used_greater_than_limit(self):
        """used > limit 时取第 used−limit+1 早的事件的过期时刻。"""
        cfg = make_config(rate_limit_messages=1, rate_limit_window_seconds=60)
        clock = FixedClock("2026-11-03T12:00:00Z")
        store = UsageStore(max_events=0)
        limits = LimitStore(cfg)
        engine = PolicyEngine(cfg, store, limits, now_fn=clock)
        # limit=1，写 3 条事件（正常路径不可能，但历史数据/并发后再计算会这样）
        for offset in (0, 10, 20):
            store.append(UsageEvent.make(
                EventKind.SENT, persona_id="p",
                at=clock() + timedelta(seconds=offset),
            ))
        # now = 12:00:20；used=3, limit=1 → 释放索引 = 3-1 = 2 → 第 3 早（12:00:20）+60s
        decision = engine.evaluate(persona_id="p", now=clock() + timedelta(seconds=20))
        check = {c.key: c for c in decision.checks}["messages.window"]
        self.assertEqual(check.used, 3)
        self.assertEqual(check.reset_at, "2026-11-03T12:01:20Z")

    def test_token_reset_at_is_when_budget_recovers(self):
        cfg = make_config(platform_token_budget=100, rate_limit_window_seconds=60)
        clock = FixedClock("2026-11-03T12:00:00Z")
        store = UsageStore(max_events=0)
        limits = LimitStore(cfg)
        engine = PolicyEngine(cfg, store, limits, now_fn=clock)
        store.append(UsageEvent.make(EventKind.SENT, persona_id="p", at=clock(),
                                     tokens_out=80))
        store.append(UsageEvent.make(EventKind.SENT, persona_id="p",
                                     at=clock() + timedelta(seconds=10),
                                     tokens_out=15))
        # used=95, 请求 10 → 需要至少放出 5 token → 第一条过期（12:01:00）即够
        decision = engine.evaluate(persona_id="p", token_estimate=10,
                                   now=clock() + timedelta(seconds=20))
        token = {c.key: c for c in decision.checks}["tokens.window"]
        self.assertFalse(token.allowed)
        self.assertEqual(token.reset_at, "2026-11-03T12:01:00Z")


# ---------------------------------------------------------------------------
# 5. 存储：不存正文 + 容量上限
# ---------------------------------------------------------------------------
class TestUsageStoreBounds(unittest.TestCase):
    def test_event_does_not_store_text(self):
        event = UsageEvent.make(EventKind.SENT, persona_id="p", text="机密内容")
        self.assertFalse(hasattr(event, "text_preview"))
        self.assertEqual(event.text_length, 4)
        self.assertEqual(len(event.text_sha256), 64)
        # 同内容 → 同摘要
        other = UsageEvent.make(EventKind.SENT, persona_id="p", text="机密内容")
        self.assertEqual(event.text_sha256, other.text_sha256)
        # 不同内容 → 不同摘要
        third = UsageEvent.make(EventKind.SENT, persona_id="p", text="别的内容")
        self.assertNotEqual(event.text_sha256, third.text_sha256)

    def test_empty_text_has_no_hash(self):
        event = UsageEvent.make(EventKind.SENT, persona_id="p", text="")
        self.assertEqual(event.text_length, 0)
        self.assertEqual(event.text_sha256, "")

    def test_capacity_evicts_oldest(self):
        store = UsageStore(max_events=10)
        for i in range(25):
            store.append(UsageEvent.make(EventKind.SENT, persona_id=f"p{i % 3}"))
        self.assertLessEqual(len(store), 10)
        self.assertGreater(len(store), 0)

    def test_zero_capacity_means_unbounded(self):
        store = UsageStore(max_events=0)
        for _ in range(50):
            store.append(UsageEvent.make(EventKind.SENT, persona_id="p"))
        self.assertEqual(len(store), 50)

    def test_negative_capacity_rejected(self):
        with self.assertRaises(ValueError):
            UsageStore(max_events=-1)

    def test_events_in_window_left_open_right_closed(self):
        store = UsageStore(max_events=0)
        base = datetime.datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
        store.append(UsageEvent.make(EventKind.SENT, persona_id="p", at=base))
        included = store.events_in_window(
            persona_id="p",
            since=base - timedelta(seconds=1),
            until=base,
        )
        self.assertEqual(len(included), 1)
        excluded = store.events_in_window(
            persona_id="p",
            since=base,
            until=base + timedelta(seconds=1),
        )
        self.assertEqual(len(excluded), 0)

    def test_contract_kind_mapping(self):
        self.assertEqual(
            UsageEvent.make(EventKind.BLOCKED, persona_id="p").contract_kind, "blocked"
        )
        self.assertEqual(
            UsageEvent.make(EventKind.SENT, persona_id="p").contract_kind, "message_sent"
        )
        self.assertEqual(
            UsageEvent.make(EventKind.DEFERRED, persona_id="p").contract_kind,
            "message_deferred",
        )


# ---------------------------------------------------------------------------
# 6. GatewayService：limits / usage / usage events（不经 HTTP）
# ---------------------------------------------------------------------------
class TestServiceLimitsAndUsage(unittest.TestCase):
    def test_get_limits_shape(self):
        svc, *_ = make_service()
        body = svc.get_limits(persona_id="p")
        self.assertEqual(body["scope"], "persona")
        self.assertEqual(body["scope_id"], "p")
        self.assertIn("generated_at", body)
        self.assertTrue(body["effective"])
        self.assertEqual(body["layers"][0]["layer"], "platform")
        for item in body["effective"]:
            for field in ("key", "value", "unit", "source_layer", "source_id"):
                self.assertIn(field, item)

    def test_get_limits_without_scope_uses_platform(self):
        svc, *_ = make_service()
        body = svc.get_limits()
        self.assertEqual(body["scope"], "peer")
        self.assertEqual(body["scope_id"], "platform_default")

    def test_put_limits_returns_recomputed_effective(self):
        svc, transport, store, limits, clock = make_service()
        body = svc.put_limits({
            "layer": "owner", "scope": "persona", "scope_id": "p",
            "rules": {"messages_per_window": 2},
        })
        effective = {item["key"]: item for item in body["effective"]}
        self.assertEqual(effective["messages_per_window"]["value"], 2)
        self.assertEqual(effective["messages_per_window"]["source_layer"], "owner")

    def test_put_limits_out_of_range_raises_limit_error(self):
        svc, *_ = make_service()
        with self.assertRaises(LimitError):
            svc.put_limits({
                "layer": "owner", "scope": "persona", "scope_id": "p",
                "rules": {"messages_per_window": 999},
            })

    def test_owner_limit_actually_blocks_send(self):
        svc, transport, store, limits, clock = make_service()
        svc.put_limits({
            "layer": "owner", "scope": "persona", "scope_id": "p",
            "rules": {"messages_per_window": 1},
        })
        payload = {"sender_persona_id": "p", "content": {"type": "text", "text": "a"}}
        first = svc.send_ai_message("c", payload)
        second = svc.send_ai_message(
            "c", {"sender_persona_id": "p", "content": {"type": "text", "text": "b"}}
        )
        self.assertEqual(first["outcome"], "delivered")
        self.assertEqual(second["outcome"], "blocked")
        self.assertEqual(second["decision"]["winner_layer"], "owner")
        self.assertEqual(len(transport.sent), 1)

    def test_sent_event_records_tokens_out(self):
        svc, transport, store, limits, clock = make_service()
        svc.send_ai_message("c", {
            "sender_persona_id": "p",
            "content": {"type": "text", "text": "hello"},
            "token_estimate": 42,
        })
        sent = [e for e in store.all_events() if e.kind == EventKind.SENT][0]
        self.assertEqual(sent.tokens_out, 42)
        self.assertEqual(sent.text_length, 5)
        self.assertTrue(sent.text_sha256)

    def test_token_budget_blocks_after_use(self):
        cfg = make_config(platform_token_budget=100)
        svc, transport, store, limits, clock = make_service(cfg)
        svc.send_ai_message("c", {
            "sender_persona_id": "p",
            "content": {"type": "text", "text": "x"},
            "token_estimate": 90,
        })
        blocked = svc.send_ai_message("c", {
            "sender_persona_id": "p",
            "content": {"type": "text", "text": "y"},
            "token_estimate": 20,
        })
        self.assertEqual(blocked["outcome"], "blocked")
        self.assertEqual(blocked["decision"]["winner_layer"], "platform")
        codes = [r["code"] for r in blocked["decision"]["reasons"]]
        self.assertIn("tokens.window.exceeded", codes)

    def test_get_usage_totals_and_breakdown(self):
        svc, transport, store, limits, clock = make_service()
        svc.send_ai_message("conv_a", {
            "sender_persona_id": "p",
            "content": {"type": "text", "text": "one"},
            "token_estimate": 10,
        })
        svc.send_ai_message("conv_b", {
            "sender_persona_id": "p",
            "content": {"type": "text", "text": "two"},
            "token_estimate": 20,
        })
        usage = svc.get_usage(persona_id="p", group_by="conversation")
        self.assertEqual(usage["totals"]["messages_sent"], 2)
        self.assertEqual(usage["totals"]["tokens_out"], 30)
        self.assertEqual(usage["totals"]["tokens_total"], 30)
        buckets = {b["key"]: b for b in usage["breakdown"]}
        self.assertEqual(buckets["conv_a"]["messages_sent"], 1)
        self.assertEqual(buckets["conv_b"]["tokens_total"], 20)
        self.assertEqual(usage["budget"]["source_layer"], "platform")
        self.assertIsNotNone(usage["budget"]["reset_at"])
        self.assertEqual(usage["timezone"], "UTC")

    def test_get_usage_group_by_day_and_timezone(self):
        svc, transport, store, limits, clock = make_service(
            make_config(), start="2026-11-03T15:30:00Z"
        )
        svc.send_ai_message("c", {
            "sender_persona_id": "p",
            "content": {"type": "text", "text": "x"},
            "token_estimate": 5,
        })
        usage = svc.get_usage(persona_id="p", group_by="day", timezone_name="Asia/Shanghai")
        # 2026-11-03T15:30Z = 11-03 23:30 Asia/Shanghai
        self.assertIn("2026-11-03", [b["key"] for b in usage["breakdown"]])
        self.assertEqual(usage["timezone"], "Asia/Shanghai")

    def test_get_usage_rejects_bad_group_by(self):
        svc, *_ = make_service()
        with self.assertRaises(ValidationError):
            svc.get_usage(persona_id="p", group_by="persona")

    def test_get_usage_rejects_unknown_timezone(self):
        svc, *_ = make_service()
        with self.assertRaises(ValidationError):
            svc.get_usage(persona_id="p", timezone_name="Not/A_Zone")

    def test_get_usage_rejects_reversed_range(self):
        svc, *_ = make_service()
        with self.assertRaises(ValidationError):
            svc.get_usage(from_="2026-11-04T00:00:00Z", to="2026-11-03T00:00:00Z")

    def test_get_usage_budget_null_when_unset(self):
        cfg = make_config(platform_token_budget=0)
        svc, *_ = make_service(cfg)
        usage = svc.get_usage(persona_id="p")
        self.assertIsNone(usage["budget"]["limit"])
        self.assertIsNone(usage["budget"]["remaining"])

    def test_usage_events_do_not_include_text(self):
        svc, transport, store, limits, clock = make_service()
        svc.send_ai_message("c", {
            "sender_persona_id": "p",
            "content": {"type": "text", "text": "不该出现在账单里的正文"},
        })
        page = svc.list_usage_events(persona_id="p")
        self.assertEqual(len(page["items"]), 1)
        item = page["items"][0]
        self.assertEqual(item["kind"], "message_sent")
        self.assertEqual(item["text_length"], len("不该出现在账单里的正文"))
        self.assertIn("text_sha256", item)
        raw = json.dumps(page, ensure_ascii=False)
        self.assertNotIn("不该出现在账单里的正文", raw)
        self.assertNotIn("text_preview", raw)

    def test_usage_events_pagination(self):
        svc, transport, store, limits, clock = make_service(
            make_config(rate_limit_messages=100)
        )
        for i in range(5):
            svc.send_ai_message("c", {
                "sender_persona_id": "p",
                "content": {"type": "text", "text": f"m{i}"},
            })
            clock.advance(seconds=1)
        first = svc.list_usage_events(persona_id="p", limit=2)
        self.assertEqual(len(first["items"]), 2)
        self.assertTrue(first["has_more"])
        second = svc.list_usage_events(
            persona_id="p", limit=2, cursor=first["next_cursor"]
        )
        self.assertEqual(len(second["items"]), 2)
        # 倒序：first 的第一条比 second 的第一条晚
        self.assertGreater(first["items"][0]["occurred_at"],
                           second["items"][0]["occurred_at"])

    def test_usage_events_kind_filter_accepts_legacy_alias(self):
        svc, transport, store, limits, clock = make_service(
            make_config(rate_limit_messages=1)
        )
        svc.send_ai_message("c", {
            "sender_persona_id": "p", "content": {"type": "text", "text": "a"}})
        svc.send_ai_message("c", {
            "sender_persona_id": "p", "content": {"type": "text", "text": "b"}})
        for kind in ("blocked", "message_blocked"):
            page = svc.list_usage_events(persona_id="p", kind=kind)
            self.assertEqual(len(page["items"]), 1, kind)
            self.assertEqual(page["items"][0]["kind"], "blocked")

    def test_usage_events_rejects_bad_kind_and_limit(self):
        svc, *_ = make_service()
        with self.assertRaises(ValidationError):
            svc.list_usage_events(kind="not_a_kind")
        with self.assertRaises(ValidationError):
            svc.list_usage_events(limit=0)
        with self.assertRaises(ValidationError):
            svc.list_usage_events(limit=101)
        with self.assertRaises(ValidationError):
            svc.list_usage_events(cursor="bogus")

    def test_town_usage_recorded(self):
        svc, transport, store, limits, clock = make_service()
        svc.record_town_usage(persona_id="p", tokens=15)
        totals = svc.get_usage(persona_id="p")["totals"]
        self.assertEqual(totals["town_tokens"], 15)
        page = svc.list_usage_events(persona_id="p")
        self.assertEqual(page["items"][0]["kind"], "town_action")

    def test_metadata_passed_through(self):
        svc, transport, store, limits, clock = make_service()
        r = svc.send_ai_message("c", {
            "sender_persona_id": "p",
            "content": {"type": "text", "text": "hi"},
            "metadata": {"origin": "reply"},
        })
        self.assertEqual(r["message"]["metadata"], {"origin": "reply"})

    def test_metadata_rejects_too_many_properties(self):
        svc, *_ = make_service()
        with self.assertRaises(ValidationError):
            svc.send_ai_message("c", {
                "sender_persona_id": "p",
                "content": {"type": "text", "text": "hi"},
                "metadata": {f"k{i}": "v" for i in range(9)},
            })

    def test_metadata_rejects_non_string_values(self):
        svc, *_ = make_service()
        with self.assertRaises(ValidationError):
            svc.send_ai_message("c", {
                "sender_persona_id": "p",
                "content": {"type": "text", "text": "hi"},
                "metadata": {"k": 1},
            })

    def test_evaluate_policy_accepts_peer_id(self):
        svc, transport, store, limits, clock = make_service()
        limits.put(
            {"layer": "peer", "scope": "peer", "scope_id": "peer_x",
             "rules": {"tokens_per_window": 10}},
            now=clock(),
        )
        body = svc.evaluate_policy({
            "persona_id": "p", "peer_id": "peer_x", "token_estimate": 20,
        })
        token = {c["key"]: c for c in body["checks"]}["tokens.window"]
        self.assertEqual(token["layer"], "peer")
        self.assertFalse(token["allowed"])

    def test_evaluate_policy_rejects_bad_peer_id(self):
        svc, *_ = make_service()
        with self.assertRaises(ValidationError):
            svc.evaluate_policy({"persona_id": "p", "peer_id": 1})

    def test_idempotency_cache_is_bounded(self):
        cfg = make_config(rate_limit_messages=1000, max_idempotency_entries=3)
        svc, transport, store, limits, clock = make_service(cfg)
        payload = {"sender_persona_id": "p", "content": {"type": "text", "text": "a"}}
        for i in range(6):
            svc.send_ai_message("c", payload, idempotency_key=f"key-000{i}")
        self.assertLessEqual(len(svc._idempotency), 3)

    def test_idempotency_cache_expires(self):
        cfg = make_config(rate_limit_messages=100, idempotency_ttl_seconds=10)
        svc, transport, store, limits, clock = make_service(cfg)
        payload = {"sender_persona_id": "p", "content": {"type": "text", "text": "a"}}
        r1 = svc.send_ai_message("c", payload, idempotency_key="key-expire")
        clock.advance(seconds=11)
        r2 = svc.send_ai_message("c", payload, idempotency_key="key-expire")
        # 缓存过期后会重新投递；HS（内存传输同语义）按 txnId 返回同一事件（#114 PR-a）。
        self.assertEqual(len(transport.sent), 1)
        self.assertEqual(r1["delivery"]["matrix_event_id"], r2["delivery"]["matrix_event_id"])

    def test_queue_is_bounded(self):
        cfg = make_config(max_queue_entries=2, silent_hours_enabled=True,
                          silent_hours_start="23:00", silent_hours_end="07:30")
        svc, transport, store, limits, clock = make_service(
            cfg, start="2026-11-03T15:00:00Z"
        )
        for i in range(5):
            r = svc.send_ai_message("c", {
                "sender_persona_id": "p",
                "content": {"type": "text", "text": f"m{i}"},
            })
            self.assertEqual(r["outcome"], "deferred")
        self.assertLessEqual(len(svc.pending_queue()), 2)


# ---------------------------------------------------------------------------
# 7. HTTP：1 MiB 上限 / 新端点 / 422
# ---------------------------------------------------------------------------
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


class _Fixture:
    def __init__(self, config):
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
    def base(self):
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"


def http_config(**overrides):
    base = dict(
        host="127.0.0.1", port=0, dev_mode=True, rate_limit_messages=60,
        silent_hours_enabled=False, auth_token=None,
        hmac_secret=b"test-hmac-secret" * 2,
    )
    base.update(overrides)
    return Config(**base)


class TestHttpLimitsAndUsage(unittest.TestCase):
    def test_get_limits_over_http(self):
        with _Fixture(http_config()) as fixture:
            status, body = _http(
                "GET", f"{fixture.base}/v1/limits?persona_id=p"
            )
        self.assertEqual(status, 200)
        self.assertEqual(body["scope"], "persona")
        self.assertTrue(body["effective"])

    def test_get_limits_include_platform_false(self):
        with _Fixture(http_config()) as fixture:
            status, body = _http(
                "GET",
                f"{fixture.base}/v1/limits?persona_id=p&include_platform=false",
            )
        self.assertEqual(status, 200)
        self.assertEqual([layer["layer"] for layer in body["layers"]], [])

    def test_get_limits_rejects_bad_include_platform(self):
        with _Fixture(http_config()) as fixture:
            status, body = _http(
                "GET", f"{fixture.base}/v1/limits?persona_id=p&include_platform=maybe"
            )
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "request.invalid")

    def test_put_limits_over_http(self):
        with _Fixture(http_config()) as fixture:
            status, body = _http(
                "PUT", f"{fixture.base}/v1/limits",
                {"layer": "owner", "scope": "persona", "scope_id": "p",
                 "rules": {"messages_per_window": 3}},
            )
        self.assertEqual(status, 200)
        effective = {item["key"]: item for item in body["effective"]}
        self.assertEqual(effective["messages_per_window"]["value"], 3)
        self.assertEqual(effective["messages_per_window"]["source_layer"], "owner")

    def test_put_limits_out_of_range_is_422(self):
        with _Fixture(http_config()) as fixture:
            status, body = _http(
                "PUT", f"{fixture.base}/v1/limits",
                {"layer": "owner", "scope": "persona", "scope_id": "p",
                 "rules": {"messages_per_window": 9999}},
            )
        self.assertEqual(status, 422)
        self.assertEqual(body["code"], "limit.out_of_range")
        self.assertEqual(body["errors"][0]["field"], "rules.messages_per_window")
        self.assertEqual(body["type"], "urn:botville:error:limit.out_of_range")

    def test_put_limits_bad_body_is_400(self):
        with _Fixture(http_config()) as fixture:
            status, body = _http(
                "PUT", f"{fixture.base}/v1/limits",
                {"layer": "owner", "scope": "persona", "scope_id": "p",
                 "rules": {"bogus_field": 1}},
            )
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "request.invalid")

    def test_put_limits_platform_readonly_is_400(self):
        with _Fixture(http_config()) as fixture:
            status, body = _http(
                "PUT", f"{fixture.base}/v1/limits",
                {"layer": "platform", "scope": "peer", "scope_id": "x", "rules": {}},
            )
        self.assertEqual(status, 400)

    def test_put_on_other_path_is_405(self):
        with _Fixture(http_config()) as fixture:
            status, body = _http("PUT", f"{fixture.base}/v1/usage", {})
        self.assertEqual(status, 405)
        self.assertEqual(body["code"], "request.invalid")

    def test_get_usage_over_http(self):
        with _Fixture(http_config()) as fixture:
            _http("POST", f"{fixture.base}/v1/conversations/c/messages", {
                "sender_persona_id": "p", "content": {"type": "text", "text": "x"},
                "token_estimate": 7,
            })
            status, body = _http("GET", f"{fixture.base}/v1/usage?persona_id=p")
        self.assertEqual(status, 200)
        self.assertEqual(body["totals"]["messages_sent"], 1)
        self.assertEqual(body["totals"]["tokens_total"], 7)
        self.assertEqual(body["budget"]["source_layer"], "platform")

    def test_get_usage_events_over_http(self):
        with _Fixture(http_config()) as fixture:
            _http("POST", f"{fixture.base}/v1/conversations/c/messages", {
                "sender_persona_id": "p", "content": {"type": "text", "text": "正文"},
            })
            status, body = _http(
                "GET", f"{fixture.base}/v1/usage/events?persona_id=p"
            )
        self.assertEqual(status, 200)
        self.assertEqual(len(body["items"]), 1)
        self.assertIn("text_length", body["items"][0])
        self.assertNotIn("text_preview", json.dumps(body))

    def test_usage_endpoints_require_auth(self):
        with _Fixture(http_config(dev_mode=False, auth_token="strong-token-123")) as fixture:
            for path in ("/v1/limits", "/v1/usage", "/v1/usage/events"):
                status, body = _http("GET", f"{fixture.base}{path}")
                self.assertEqual(status, 401, path)
                self.assertEqual(body["code"], "auth.missing")

    def test_get_unknown_path_is_404_after_auth(self):
        with _Fixture(http_config()) as fixture:
            status, _ = _http("GET", f"{fixture.base}/v1/no-such-thing")
        self.assertEqual(status, 404)

    def test_oversized_body_has_too_large_error_code(self):
        with _Fixture(http_config()) as fixture:
            url = f"{fixture.base}/v1/conversations/c/messages"
            req = urllib.request.Request(url, data=b'{"x":"y"}', method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("Content-Length", str(MAX_BODY_BYTES + 1))
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    status, raw = resp.status, resp.read()
            except urllib.error.HTTPError as exc:
                status, raw = exc.code, exc.read()
        body = json.loads(raw)
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "request.invalid")
        self.assertEqual(body["errors"][0]["code"], "too_large")

    def test_body_at_limit_is_accepted(self):
        """恰好 1 MiB 的合法请求体不应被误拒（边界）。"""
        filler = "a" * (MAX_BODY_BYTES - 200)
        body = {
            "sender_persona_id": "p",
            "content": {"type": "text", "text": "short"},
            "metadata": {"pad": filler},
        }
        raw = json.dumps(body).encode("utf-8")
        self.assertLessEqual(len(raw), MAX_BODY_BYTES)
        with _Fixture(http_config(max_message_chars=8000)) as fixture:
            req = urllib.request.Request(
                f"{fixture.base}/v1/conversations/c/messages", data=raw, method="POST"
            )
            req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    status, payload = resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                status, payload = exc.code, json.loads(exc.read())
        # metadata 值长度合法（string），但只有 1 个属性 → 应通过校验
        self.assertEqual(status, 200)
        self.assertEqual(payload["outcome"], "delivered")

    def test_limits_configured_from_config_appear(self):
        with _Fixture(http_config(rate_limit_messages=12, platform_token_budget=345)) as fixture:
            status, body = _http("GET", f"{fixture.base}/v1/limits?persona_id=p")
        self.assertEqual(status, 200)
        effective = {item["key"]: item for item in body["effective"]}
        self.assertEqual(effective["messages_per_window"]["value"], 12)
        self.assertEqual(effective["tokens_per_window"]["value"], 345)


# ---------------------------------------------------------------------------
# 8. 配置项
# ---------------------------------------------------------------------------
class TestNewConfig(unittest.TestCase):
    def test_new_defaults(self):
        cfg = Config(dev_mode=True)
        self.assertEqual(cfg.platform_token_budget, 200_000)
        self.assertEqual(cfg.platform_proactive_per_day, 6)
        self.assertGreaterEqual(cfg.max_usage_events, 1)
        self.assertGreaterEqual(cfg.max_idempotency_entries, 1)
        self.assertGreaterEqual(cfg.max_queue_entries, 1)
        self.assertGreaterEqual(cfg.idempotency_ttl_seconds, 0)

    def test_env_overrides(self):
        from botville_gateway.config import load_config

        cfg = load_config({
            "GATEWAY_DEV_MODE": "true",
            "PLATFORM_TOKEN_BUDGET": "5000",
            "PLATFORM_PROACTIVE_PER_DAY": "2",
            "MAX_USAGE_EVENTS": "50",
            "MAX_IDEMPOTENCY_ENTRIES": "7",
            "IDEMPOTENCY_TTL_SECONDS": "60",
            "MAX_QUEUE_ENTRIES": "9",
        })
        self.assertEqual(cfg.platform_token_budget, 5000)
        self.assertEqual(cfg.platform_proactive_per_day, 2)
        self.assertEqual(cfg.max_usage_events, 50)
        self.assertEqual(cfg.max_idempotency_entries, 7)
        self.assertEqual(cfg.idempotency_ttl_seconds, 60)
        self.assertEqual(cfg.max_queue_entries, 9)

    def test_invalid_capacities_rejected(self):
        from botville_gateway.config import ConfigError, load_config

        for env in (
            {"MAX_USAGE_EVENTS": "0"},
            {"MAX_IDEMPOTENCY_ENTRIES": "0"},
            {"MAX_QUEUE_ENTRIES": "0"},
            {"PLATFORM_TOKEN_BUDGET": "-1"},
            {"IDEMPOTENCY_TTL_SECONDS": "-5"},
        ):
            with self.assertRaises(ConfigError, msg=str(env)):
                load_config({"GATEWAY_DEV_MODE": "true", **env})


if __name__ == "__main__":
    unittest.main()
