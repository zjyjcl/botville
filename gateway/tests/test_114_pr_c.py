# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""#114 PR-C 回归测试：N7（平台层 anti_spam / ai_loop）、N8（ai_loop may_clear_at）、
#117 N5（USAGE_RETENTION_SECONDS 硬校验）。

每条用例在旧实现下应失败（N7 的「平台三项为 0 行为不变」除外——那是回归保护）。
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from typing import Optional

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from botville_gateway.config import Config, ConfigError, load_config
from botville_gateway.errors import LimitError
from botville_gateway.limits import LimitStore
from botville_gateway.matrix import InMemoryMatrixTransport
from botville_gateway.policy import (
    CODE_AI_LOOP_TRIPPED,
    CODE_ANTI_SPAM_CONSECUTIVE,
    PolicyEngine,
    ReservationBook,
)
from botville_gateway.service import GatewayService
from botville_gateway.store import EventKind, UsageEvent, UsageStore

# 测试密钥：明显的测试值，无任何真实密钥
SECRET = b"ab" * 32
HEX_SECRET = "ab" * 32

_ISO = "%Y-%m-%dT%H:%M:%SZ"


def parse_iso(value: str) -> datetime:
    return datetime.strptime(value, _ISO).replace(tzinfo=timezone.utc)


def make_cfg(**kw) -> Config:
    base = dict(
        dev_mode=True,
        rate_limit_messages=100,
        rate_limit_window_seconds=3600,
        silent_hours_enabled=False,
        platform_token_budget=0,
        platform_proactive_per_day=0,
        hmac_secret=SECRET,
    )
    base.update(kw)
    return Config(**base)


class FixedClock:
    def __init__(self, ts: str = "2026-11-05T12:00:00Z") -> None:
        self._t = parse_iso(ts)

    def __call__(self) -> datetime:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += timedelta(seconds=seconds)


def put_rules(limits: LimitStore, persona: str, rules: dict) -> None:
    limits.put(
        {
            "layer": "owner",
            "scope": "persona",
            "scope_id": persona,
            "rules": rules,
        },
        now=datetime.now(timezone.utc),
    )


def make_engine(cfg: Config, clock: FixedClock, *, reservations=None, limits=None):
    store = UsageStore(max_events=0)
    limits = limits or LimitStore(cfg)
    engine = PolicyEngine(
        cfg, store, limits, now_fn=clock, reservations=reservations
    )
    return engine, store, limits


def add_events(
    store: UsageStore, conv: str, at_times, kind=EventKind.SENT, persona="bot1"
) -> None:
    for at in at_times:
        store.append(
            UsageEvent.make(
                kind, persona_id=persona, conversation_id=conv, text="hi", at=at
            )
        )


# ---------------------------------------------------------------------------
# N7：平台层 anti_spam / ai_loop 配置
# ---------------------------------------------------------------------------
class TestPlatformConfigParsing(unittest.TestCase):
    def test_defaults_are_unset(self):
        cfg = make_cfg()
        self.assertEqual(cfg.platform_max_consecutive_ai_messages, 0)
        self.assertEqual(cfg.platform_anti_spam_cooldown_seconds, 0)
        self.assertEqual(cfg.platform_max_ai_messages_per_hour, 0)
        self.assertEqual(cfg.platform_ai_loop_on_break, "stop")

    def test_load_config_reads_platform_env(self):
        env = {
            "GATEWAY_DEV_MODE": "true",
            "PLATFORM_MAX_CONSECUTIVE_AI_MESSAGES": "3",
            "PLATFORM_ANTI_SPAM_COOLDOWN_SECONDS": "60",
            "PLATFORM_MAX_AI_MESSAGES_PER_HOUR": "20",
            "PLATFORM_AI_LOOP_ON_BREAK": "notify",
        }
        cfg = load_config(env)
        self.assertEqual(cfg.platform_max_consecutive_ai_messages, 3)
        self.assertEqual(cfg.platform_anti_spam_cooldown_seconds, 60)
        self.assertEqual(cfg.platform_max_ai_messages_per_hour, 20)
        self.assertEqual(cfg.platform_ai_loop_on_break, "notify")

    def test_platform_env_accepts_zero_as_unset(self):
        cfg = load_config(
            {
                "GATEWAY_DEV_MODE": "true",
                "PLATFORM_MAX_CONSECUTIVE_AI_MESSAGES": "0",
                "PLATFORM_ANTI_SPAM_COOLDOWN_SECONDS": "0",
                "PLATFORM_MAX_AI_MESSAGES_PER_HOUR": "0",
            }
        )
        self.assertEqual(cfg.platform_max_consecutive_ai_messages, 0)
        self.assertEqual(cfg.platform_anti_spam_cooldown_seconds, 0)
        self.assertEqual(cfg.platform_max_ai_messages_per_hour, 0)

    def test_non_integer_platform_value_rejected(self):
        with self.assertRaises(ConfigError):
            make_cfg(platform_max_consecutive_ai_messages="2")  # type: ignore[arg-type]

    def test_negative_platform_value_rejected(self):
        with self.assertRaises(ConfigError):
            make_cfg(platform_max_ai_messages_per_hour=-1)

    def test_out_of_range_nonzero_values_rejected(self):
        # 非 0 时必须落在与 API 相同的范围内。
        with self.assertRaises(ConfigError):
            make_cfg(platform_max_consecutive_ai_messages=51)
        with self.assertRaises(ConfigError):
            make_cfg(platform_anti_spam_cooldown_seconds=86_401)
        with self.assertRaises(ConfigError):
            make_cfg(platform_max_ai_messages_per_hour=1_001)

    def test_range_boundaries_accepted(self):
        cfg = make_cfg(
            platform_max_consecutive_ai_messages=50,
            platform_anti_spam_cooldown_seconds=86_400,
            platform_max_ai_messages_per_hour=1_000,
        )
        self.assertEqual(cfg.platform_max_consecutive_ai_messages, 50)
        cfg = make_cfg(
            platform_max_consecutive_ai_messages=1,
            platform_anti_spam_cooldown_seconds=1,
            platform_max_ai_messages_per_hour=1,
        )
        self.assertEqual(cfg.platform_max_consecutive_ai_messages, 1)

    def test_invalid_on_break_rejected(self):
        with self.assertRaises(ConfigError):
            make_cfg(platform_ai_loop_on_break="explode")
        with self.assertRaises(ConfigError):
            load_config(
                {"GATEWAY_DEV_MODE": "true", "PLATFORM_AI_LOOP_ON_BREAK": "explode"}
            )


class TestPlatformLayerRules(unittest.TestCase):
    def test_zero_platform_values_omit_keys(self):
        """平台三项为 0 → 平台层省略对应键（与 RATE_LIMIT_MESSAGES=0 一致）。"""
        store = LimitStore(make_cfg())
        rules = store.platform.rules
        self.assertNotIn("anti_spam", rules)
        self.assertEqual(rules["ai_loop"], {"on_break": "stop"})

    def test_nonzero_platform_values_written(self):
        store = LimitStore(
            make_cfg(
                platform_max_consecutive_ai_messages=4,
                platform_anti_spam_cooldown_seconds=120,
                platform_max_ai_messages_per_hour=20,
                platform_ai_loop_on_break="notify",
            )
        )
        rules = store.platform.rules
        self.assertEqual(
            rules["anti_spam"],
            {"max_consecutive_ai_messages": 4, "cooldown_seconds": 120},
        )
        self.assertEqual(
            rules["ai_loop"],
            {"max_ai_messages_per_hour": 20, "on_break": "notify"},
        )

    def test_effective_sees_platform_layer_keys(self):
        cfg = make_cfg(
            platform_max_consecutive_ai_messages=4,
            platform_max_ai_messages_per_hour=20,
        )
        store = LimitStore(cfg)
        eff = store.effective(persona_id="p", now=datetime.now(timezone.utc))
        anti_spam = eff.get("anti_spam.max_consecutive_ai_messages")
        ai_loop = eff.get("ai_loop.max_ai_messages_per_hour")
        self.assertIsNotNone(anti_spam)
        self.assertIsNotNone(ai_loop)
        self.assertEqual(anti_spam.value, 4)
        self.assertEqual(anti_spam.source_layer, "platform")
        self.assertEqual(ai_loop.value, 20)
        self.assertEqual(ai_loop.source_layer, "platform")

    def test_owner_tighter_wins_with_owner_layer(self):
        cfg = make_cfg(platform_max_ai_messages_per_hour=20)
        store = LimitStore(cfg)
        put_rules(store, "p", {"ai_loop": {"max_ai_messages_per_hour": 5}})
        eff = store.effective(persona_id="p", now=datetime.now(timezone.utc))
        self.assertEqual(eff.get("ai_loop.max_ai_messages_per_hour").value, 5)
        self.assertEqual(
            eff.get("ai_loop.max_ai_messages_per_hour").source_layer, "owner"
        )


class TestPlatformFloorRejectsLooserOwner(unittest.TestCase):
    def _put(self, store: LimitStore, rules: dict):
        return store.put(
            {"layer": "owner", "scope": "persona", "scope_id": "p", "rules": rules},
            now=datetime.now(timezone.utc),
        )

    def test_looser_max_consecutive_rejected(self):
        store = LimitStore(make_cfg(platform_max_consecutive_ai_messages=3))
        with self.assertRaises(LimitError) as ctx:
            self._put(store, {"anti_spam": {"max_consecutive_ai_messages": 5}})
        self.assertEqual(ctx.exception.code, "limit.out_of_range")
        self.assertEqual(
            ctx.exception.errors[0]["field"],
            "rules.anti_spam.max_consecutive_ai_messages",
        )

    def test_shorter_cooldown_rejected(self):
        store = LimitStore(make_cfg(platform_anti_spam_cooldown_seconds=60))
        with self.assertRaises(LimitError) as ctx:
            self._put(store, {"anti_spam": {"cooldown_seconds": 30}})
        self.assertEqual(ctx.exception.code, "limit.out_of_range")
        self.assertEqual(
            ctx.exception.errors[0]["field"], "rules.anti_spam.cooldown_seconds"
        )

    def test_looser_max_ai_messages_per_hour_rejected(self):
        store = LimitStore(make_cfg(platform_max_ai_messages_per_hour=2))
        with self.assertRaises(LimitError) as ctx:
            self._put(store, {"ai_loop": {"max_ai_messages_per_hour": 10}})
        self.assertEqual(ctx.exception.code, "limit.out_of_range")
        self.assertEqual(
            ctx.exception.errors[0]["field"],
            "rules.ai_loop.max_ai_messages_per_hour",
        )

    def test_tighter_owner_accepted(self):
        store = LimitStore(
            make_cfg(
                platform_max_consecutive_ai_messages=3,
                platform_anti_spam_cooldown_seconds=60,
                platform_max_ai_messages_per_hour=20,
            )
        )
        layer = self._put(
            store,
            {
                "anti_spam": {
                    "max_consecutive_ai_messages": 2,
                    "cooldown_seconds": 120,
                },
                "ai_loop": {"max_ai_messages_per_hour": 10},
            },
        )
        self.assertEqual(layer.rules["anti_spam"]["max_consecutive_ai_messages"], 2)

    def test_owner_notify_not_rejected_when_platform_stop(self):
        """on_break 不进宽松校验：平台 stop、owner notify 不报 422（契约 §6.3）。"""
        store = LimitStore(make_cfg(platform_ai_loop_on_break="stop"))
        layer = self._put(store, {"ai_loop": {"on_break": "notify"}})
        self.assertIsNotNone(layer)
        self.assertEqual(layer.rules["ai_loop"]["on_break"], "notify")


class TestPlatformEnforcementAtRuntime(unittest.TestCase):
    def _svc(self, cfg: Config, clock: FixedClock) -> GatewayService:
        transport = InMemoryMatrixTransport()
        store = UsageStore(max_events=0, secret=cfg.hmac_secret)
        svc = GatewayService(cfg, transport, store, LimitStore(cfg), now_fn=clock)
        return svc

    def _send(self, svc: GatewayService, persona: str, conv: str) -> dict:
        return svc.send_ai_message(
            conv,
            {"sender_persona_id": persona, "content": {"type": "text", "text": "hi"}},
        )

    def test_platform_stricter_ai_loop_blocks(self):
        """平台 max_ai_messages_per_hour=2 → 达到后按平台层拦截。"""
        cfg = make_cfg(platform_max_ai_messages_per_hour=2)
        clock = FixedClock()
        svc = self._svc(cfg, clock)
        for _ in range(2):
            self.assertEqual(self._send(svc, "bot1", "conv1")["outcome"], "delivered")
        r = self._send(svc, "bot1", "conv1")
        self.assertEqual(r["outcome"], "blocked")
        checks = {c["key"]: c for c in r["decision"]["checks"]}
        self.assertEqual(checks["ai_loop.per_hour"]["layer"], "platform")
        self.assertEqual(checks["ai_loop.per_hour"]["limit"], 2)
        self.assertEqual(r["decision"]["winner_layer"], "platform")
        reasons = [x for x in r["decision"]["reasons"] if x["code"] == CODE_AI_LOOP_TRIPPED]
        self.assertTrue(reasons)
        self.assertEqual(reasons[0]["layer"], "platform")

    def test_owner_stricter_ai_loop_blocks_with_owner_layer(self):
        cfg = make_cfg(platform_max_ai_messages_per_hour=20)
        clock = FixedClock()
        svc = self._svc(cfg, clock)
        put_rules(svc._limits, "bot1", {"ai_loop": {"max_ai_messages_per_hour": 2}})
        for _ in range(2):
            self.assertEqual(self._send(svc, "bot1", "conv1")["outcome"], "delivered")
        r = self._send(svc, "bot1", "conv1")
        self.assertEqual(r["outcome"], "blocked")
        checks = {c["key"]: c for c in r["decision"]["checks"]}
        self.assertEqual(checks["ai_loop.per_hour"]["layer"], "owner")
        self.assertEqual(r["decision"]["winner_layer"], "owner")

    def test_platform_stricter_anti_spam_blocks(self):
        cfg = make_cfg(platform_max_consecutive_ai_messages=2)
        clock = FixedClock()
        svc = self._svc(cfg, clock)
        for _ in range(2):
            self.assertEqual(self._send(svc, "bot1", "conv1")["outcome"], "delivered")
        r = self._send(svc, "bot1", "conv1")
        self.assertEqual(r["outcome"], "blocked")
        checks = {c["key"]: c for c in r["decision"]["checks"]}
        self.assertEqual(checks["anti_spam.consecutive"]["layer"], "platform")
        codes = [x["code"] for x in r["decision"]["reasons"]]
        self.assertIn(CODE_ANTI_SPAM_CONSECUTIVE, codes)

    def test_zero_platform_config_behaves_as_before(self):
        """平台三项为 0：行为与改前一致（仅 owner 层生效）。"""
        cfg = make_cfg()
        clock = FixedClock()
        svc = self._svc(cfg, clock)
        put_rules(svc._limits, "bot1", {"ai_loop": {"max_ai_messages_per_hour": 2}})
        for _ in range(2):
            self.assertEqual(self._send(svc, "bot1", "conv1")["outcome"], "delivered")
        r = self._send(svc, "bot1", "conv1")
        self.assertEqual(r["outcome"], "blocked")
        checks = {c["key"]: c for c in r["decision"]["checks"]}
        self.assertEqual(checks["ai_loop.per_hour"]["layer"], "owner")


# ---------------------------------------------------------------------------
# N8：ai_loop may_clear_at
# ---------------------------------------------------------------------------
class TestAiLoopMayClearAt(unittest.TestCase):
    def setUp(self):
        self.clock = FixedClock("2026-11-05T12:00:00Z")
        self.cfg = make_cfg()
        self.engine, self.store, self.limits = make_engine(self.cfg, self.clock)

    def _evaluate(self, max_per_hour: int):
        put_rules(
            self.limits, "bot1", {"ai_loop": {"max_ai_messages_per_hour": max_per_hour}}
        )
        return self.engine.evaluate(
            persona_id="bot1",
            conversation_id="conv1",
            text="hello",
            now=self.clock(),
        )

    def _ai_loop_check(self, decision):
        return next(c for c in decision.checks if c.key == "ai_loop.per_hour")

    def test_used_greater_than_limit_picks_correct_event(self):
        """limit=3、窗口内 5 条事件（limit 被下调）→ 取第 3 早事件的 t+3600。"""
        base = self.clock() - timedelta(seconds=100)
        times = [base + timedelta(seconds=i * 10) for i in range(5)]
        add_events(self.store, "conv1", times)
        decision = self._evaluate(3)
        check = self._ai_loop_check(decision)
        self.assertFalse(check.allowed)
        self.assertEqual(check.used, 5)
        self.assertEqual(check.limit, 3)
        # used - limit + 1 = 3 → 第 3 早（索引 2）
        expected = times[2] + timedelta(seconds=3600)
        self.assertEqual(check.reset_at, expected.strftime(_ISO))
        self.assertEqual(decision.may_clear_at, check.reset_at)

    def test_used_equals_limit_uses_earliest(self):
        base = self.clock() - timedelta(seconds=100)
        times = [base + timedelta(seconds=i * 10) for i in range(3)]
        add_events(self.store, "conv1", times)
        decision = self._evaluate(3)
        check = self._ai_loop_check(decision)
        self.assertFalse(check.allowed)
        self.assertEqual(check.reset_at, (times[0] + timedelta(seconds=3600)).strftime(_ISO))

    def test_events_and_reservations_share_one_sort(self):
        """预留按 at+3600 参与同一索引。"""
        reservations = ReservationBook()
        engine, store, limits = make_engine(
            self.cfg, self.clock, reservations=reservations, limits=self.limits
        )
        base = self.clock() - timedelta(seconds=200)
        # t0 事件、t1 事件、预留 t2、预留 t3
        t0 = base
        t1 = base + timedelta(seconds=10)
        t2 = base + timedelta(seconds=20)
        t3 = base + timedelta(seconds=30)
        add_events(store, "conv1", [t0, t1])
        reservations.reserve(persona_id="bot1", tokens=1, at=t2, conversation_id="conv1")
        reservations.reserve(persona_id="bot2", tokens=1, at=t3, conversation_id="conv1")
        put_rules(limits, "bot1", {"ai_loop": {"max_ai_messages_per_hour": 2}})
        decision = engine.evaluate(
            persona_id="bot1", conversation_id="conv1", text="hello", now=self.clock()
        )
        check = self._ai_loop_check(decision)
        self.assertFalse(check.allowed)
        self.assertEqual(check.used, 4)
        # used - limit + 1 = 3 → 第 3 早 = 预留 t2
        self.assertEqual(check.reset_at, (t2 + timedelta(seconds=3600)).strftime(_ISO))

    def test_expired_reservation_not_counted_nor_sorted(self):
        """超过 TTL 的预留先回收：不计入 used、不参与排序。"""
        reservations = ReservationBook(ttl_seconds=0.01)
        engine, store, limits = make_engine(
            self.cfg, self.clock, reservations=reservations, limits=self.limits
        )
        base = self.clock() - timedelta(seconds=100)
        t0 = base
        add_events(store, "conv1", [t0])
        old = reservations.reserve(
            persona_id="bot1", tokens=1, at=base - timedelta(seconds=50),
            conversation_id="conv1",
        )
        import time as _time

        _time.sleep(0.03)
        # 该预留应被回收：release 返回 False 说明它已不在账上
        self.assertFalse(reservations.release(old))
        put_rules(limits, "bot1", {"ai_loop": {"max_ai_messages_per_hour": 1}})
        decision = engine.evaluate(
            persona_id="bot1", conversation_id="conv1", text="hello", now=self.clock()
        )
        check = self._ai_loop_check(decision)
        self.assertFalse(check.allowed)
        self.assertEqual(check.used, 1, "超 TTL 预留不得计入 used")
        self.assertEqual(check.reset_at, (t0 + timedelta(seconds=3600)).strftime(_ISO))

    def test_limit_zero_blocks_with_null_may_clear_at(self):
        """limit=0（一条都不允许）→ blocked 且 may_clear_at / reset_at 为 null。"""
        add_events(self.store, "conv1", [self.clock() - timedelta(seconds=30)])
        decision = self._evaluate(0)
        check = self._ai_loop_check(decision)
        self.assertFalse(check.allowed)
        self.assertIsNone(check.reset_at)
        self.assertIsNone(decision.may_clear_at)

    def test_limit_zero_blocks_even_with_no_events(self):
        decision = self._evaluate(0)
        check = self._ai_loop_check(decision)
        self.assertFalse(check.allowed)
        self.assertIsNone(check.reset_at)
        self.assertIsNone(decision.may_clear_at)

    def test_computed_value_in_past_is_floored_to_future(self):
        """计算出的出窗时刻已过去/等于 now → 最终值 >= ceil(now)+1s。"""
        # 事件在窗口内（> now-3600），但 t+3600 早于 now。
        events_at = self.clock() - timedelta(seconds=3550)
        add_events(self.store, "conv1", [events_at])
        decision = self._evaluate(1)
        check = self._ai_loop_check(decision)
        self.assertFalse(check.allowed)
        now = self.clock()
        reset_at = parse_iso(check.reset_at)
        # ceil(now) 本身就是整秒；下界是 floor(now)+1s（now 为整秒时即 now+1s）
        ceil_now = now if now.microsecond == 0 else now.replace(
            microsecond=0
        ) + timedelta(seconds=1)
        self.assertGreaterEqual(reset_at, ceil_now + timedelta(seconds=1))
        self.assertGreater(reset_at, now)
        self.assertEqual(decision.may_clear_at, check.reset_at)

    def test_allowed_case_has_no_reset(self):
        add_events(self.store, "conv1", [self.clock() - timedelta(seconds=30)])
        decision = self._evaluate(5)
        check = self._ai_loop_check(decision)
        self.assertTrue(check.allowed)
        self.assertIsNone(check.reset_at)

    def test_human_message_does_not_release_reservations(self):
        """预留不受人类消息重置：人类消息只重置事件计数。"""
        reservations = ReservationBook()
        engine, store, limits = make_engine(
            self.cfg, self.clock, reservations=reservations, limits=self.limits
        )
        reservations.reserve(
            persona_id="bot1",
            tokens=1,
            at=self.clock() - timedelta(seconds=10),
            conversation_id="conv1",
        )
        store.append(
            UsageEvent.make(
                EventKind.HUMAN,
                persona_id="human",
                conversation_id="conv1",
                at=self.clock() - timedelta(seconds=5),
            )
        )
        put_rules(limits, "bot1", {"ai_loop": {"max_ai_messages_per_hour": 1}})
        decision = engine.evaluate(
            persona_id="bot1", conversation_id="conv1", text="hello", now=self.clock()
        )
        check = self._ai_loop_check(decision)
        self.assertFalse(check.allowed, "在途预留不应被人类消息清空")
        self.assertEqual(check.used, 1)


# ---------------------------------------------------------------------------
# #117 N5：USAGE_RETENTION_SECONDS 硬校验
# ---------------------------------------------------------------------------
class TestUsageRetentionValidation(unittest.TestCase):
    def test_retention_shorter_than_rate_window_rejected(self):
        with self.assertRaises(ConfigError):
            make_cfg(usage_retention_seconds=1800, rate_limit_window_seconds=3600)

    def test_retention_shorter_than_platform_cooldown_rejected(self):
        with self.assertRaises(ConfigError):
            make_cfg(
                usage_retention_seconds=3600,
                rate_limit_window_seconds=3600,
                platform_anti_spam_cooldown_seconds=7200,
            )

    def test_retention_equal_to_max_passes(self):
        cfg = make_cfg(
            usage_retention_seconds=7200,
            rate_limit_window_seconds=3600,
            platform_anti_spam_cooldown_seconds=7200,
        )
        self.assertEqual(cfg.usage_retention_seconds, 7200)
        # 等于窗口本身也可通过
        cfg = make_cfg(
            usage_retention_seconds=3600,
            rate_limit_window_seconds=3600,
            platform_anti_spam_cooldown_seconds=0,
        )
        self.assertEqual(cfg.usage_retention_seconds, 3600)

    def test_zero_cooldown_does_not_constrain_retention(self):
        cfg = make_cfg(
            usage_retention_seconds=3600,
            rate_limit_window_seconds=3600,
            platform_anti_spam_cooldown_seconds=0,
        )
        self.assertEqual(cfg.platform_anti_spam_cooldown_seconds, 0)

    def test_default_config_constructs(self):
        # 默认 retention 30 天、窗口 3600、平台冷却 0
        cfg = Config(dev_mode=True)
        self.assertEqual(cfg.usage_retention_seconds, 2_592_000)


if __name__ == "__main__":
    unittest.main()
