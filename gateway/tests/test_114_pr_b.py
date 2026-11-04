# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""#114 PR-b 卫生项回归测试。

每条修复配一个回归用例（旧实现下会失败）。
"""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from botville_gateway.config import Config, ConfigError, load_config, HMAC_SECRET_MIN_HEX_CHARS
from botville_gateway.limits import LimitStore, validate_silent_hours, _silent_is_effective
from botville_gateway.matrix import InMemoryMatrixTransport
from botville_gateway.policy import PolicyEngine, ReservationBook
from botville_gateway.service import GatewayService
from botville_gateway.store import EventKind, UsageEvent, UsageStore

# 测试密钥：明显的测试值，无任何真实密钥
SECRET = b"ab" * 32
HEX_SECRET = "ab" * 32  # 64 hex chars

_BASE_ENV = {"GATEWAY_AUTH_TOKEN": "test-token", "GATEWAY_HMAC_SECRET": HEX_SECRET}


def make_cfg(**kw) -> Config:
    base = dict(dev_mode=True, rate_limit_messages=100, silent_hours_enabled=False,
                rate_limit_window_seconds=3600, hmac_secret=SECRET)
    base.update(kw)
    return Config(**base)


def make_svc(cfg=None):
    cfg = cfg or make_cfg()
    transport = InMemoryMatrixTransport()
    store = UsageStore(max_events=1000, secret=cfg.hmac_secret)
    svc = GatewayService(cfg, transport, store, LimitStore(cfg))
    return svc, transport, store


def send(svc, key=None, text="hello", conv="conv"):
    return svc.send_ai_message(
        conv,
        {"sender_persona_id": "p", "content": {"type": "text", "text": text}},
        idempotency_key=key,
    )


class FixedClock:
    def __init__(self, ts="2026-11-05T12:00:00Z"):
        self._t = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

    def __call__(self):
        return self._t

    def advance(self, seconds):
        self._t += timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# 7.3 平台零长度静默窗口不压制 owner 的真实窗口
# ---------------------------------------------------------------------------
class TestPlatformZeroLengthSilentWindow(unittest.TestCase):
    def _make_engine(self, platform_silent, owner_silent):
        """platform_silent 和 owner_silent 都是 silent_hours dict。"""
        from botville_gateway.config import DEFAULT_TIMEZONE
        platform_cfg = make_cfg(
            silent_hours_enabled=platform_silent["enabled"],
            silent_hours_start=platform_silent.get("start", "00:00"),
            silent_hours_end=platform_silent.get("end", "00:00"),
        )
        store = UsageStore(max_events=0)
        limit_store = LimitStore(platform_cfg)
        if owner_silent:
            limit_store.put({
                "layer": "owner", "scope": "persona", "scope_id": "p",
                "rules": {"silent_hours": owner_silent},
            }, now=datetime.now(timezone.utc))
        clock = FixedClock("2026-11-05T02:00:00Z")  # 02:00 UTC，在 23:00-07:30 内
        return PolicyEngine(platform_cfg, store, limit_store, now_fn=clock)

    def test_platform_zero_length_does_not_suppress_owner_silent(self):
        """平台 start==end（零长度） + owner 真实窗口 → 静默来自 owner 应生效。

        旧实现中：`effective()` 用 `silent.get("enabled")` 判定，零长度平台层
        也会占据「第一个 enabled 层」的位置，导致 owner 真实静默被忽略。
        """
        platform_silent = {"enabled": True, "start": "23:00", "end": "23:00"}  # 零长度
        owner_silent = {"enabled": True, "start": "23:00", "end": "07:30", "timezone": "UTC"}
        engine = self._make_engine(platform_silent, owner_silent)
        decision = engine.evaluate(persona_id="p", conversation_id="c",
                                   now=datetime(2026, 11, 5, 2, 0, tzinfo=timezone.utc))
        # 02:00 UTC 在 23:00-07:30 内，owner 层应生效 → defer
        self.assertNotEqual(decision.decision, "allow",
                            "owner silent_hours should have blocked/deferred when platform is zero-length")

    def test_zero_length_silent_is_not_effective(self):
        """_silent_is_effective 对 start==end 返回 False。"""
        self.assertFalse(_silent_is_effective({"enabled": True, "start": "23:00", "end": "23:00"}))
        self.assertTrue(_silent_is_effective({"enabled": True, "start": "23:00", "end": "07:30"}))
        self.assertFalse(_silent_is_effective({"enabled": False, "start": "23:00", "end": "07:30"}))
        self.assertFalse(_silent_is_effective(None))


# ---------------------------------------------------------------------------
# 7.6 coverage_check.py 测试失败时退出码非 0
# ---------------------------------------------------------------------------
class TestCoverageCheckExitOnFailure(unittest.TestCase):
    @staticmethod
    def _load_module():
        """按文件路径加载 coverage_check（`gateway` 目录本身不是 Python 包）。"""
        import importlib.util

        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts", "coverage_check.py",
        )
        spec = importlib.util.spec_from_file_location("_cc_under_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_main_returns_1_when_tests_fail(self):
        """旧实现 main() 里 `_ok` 被丢弃 → 测试失败时仍返回覆盖率退出码。"""
        import contextlib
        import io

        cc = self._load_module()
        fake_result = {"fake": {"executable": {1, 2}, "hit": {1, 2}}}

        with patch.object(cc, "run_tests", return_value=(fake_result, False)), \
                contextlib.redirect_stdout(io.StringIO()):
            ret = cc.main(["--threshold", "0"])
        self.assertEqual(ret, 1, "tests_ok=False must cause exit code 1")

    def test_main_returns_0_when_tests_pass_and_coverage_ok(self):
        import contextlib
        import io

        cc = self._load_module()
        fake_result = {"fake": {"executable": {1, 2}, "hit": {1, 2}}}

        with patch.object(cc, "run_tests", return_value=(fake_result, True)), \
                contextlib.redirect_stdout(io.StringIO()):
            ret = cc.main(["--threshold", "0"])
        self.assertEqual(ret, 0)


# ---------------------------------------------------------------------------
# 7.7 在途预留 TTL 自动回收 + commit 顺序
# ---------------------------------------------------------------------------
class TestReservationTTL(unittest.TestCase):
    def test_expired_reservation_purged_on_next_call(self):
        """TTL 过期的预留在下一次 reserve()/release()/for_persona() 时被回收。

        旧实现无 TTL 机制，幽灵预留会永久占据名额。
        """
        book = ReservationBook(ttl_seconds=0.05)
        r = book.reserve(
            persona_id="p", tokens=10,
            at=datetime.now(timezone.utc),
        )
        self.assertEqual(len(book), 1)
        time.sleep(0.08)
        # 下次访问时触发回收
        self.assertEqual(len(book), 0, "expired reservation must be purged")
        # release 一个已过期（被自动回收）的预留返回 False
        result = book.release(r)
        self.assertFalse(result, "release of auto-purged reservation should return False")

    def test_unexpired_reservation_not_purged(self):
        book = ReservationBook(ttl_seconds=60.0)
        book.reserve(persona_id="p", tokens=10, at=datetime.now(timezone.utc))
        self.assertEqual(len(book), 1)


class TestCommitOrderAppendBeforeRelease(unittest.TestCase):
    def test_store_has_event_after_commit_send(self):
        """_commit_send 后 store 里必须有 SENT 事件（append 先于 release）。

        旧顺序是 release→append，若两步之间有中断，名额已释放但事件未记，
        导致后续请求计数偏低。
        """
        svc, transport, store = make_svc()
        r = send(svc, key="k1")
        self.assertEqual(r["outcome"], "delivered")
        events = store.events_in_window(persona_id="p", kind=EventKind.SENT)
        self.assertEqual(len(events), 1, "SENT event must be recorded after commit")


# ---------------------------------------------------------------------------
# 7.8a validate_silent_hours 错误字段指向真正出错的字段
# ---------------------------------------------------------------------------
class TestValidateSilentHoursEndField(unittest.TestCase):
    def test_bad_end_reports_end_field_not_start(self):
        """旧实现 start/end 共用同一 except，错误字段永远报 .start。"""
        from botville_gateway.errors import LimitError
        try:
            validate_silent_hours({
                "enabled": True,
                "start": "23:00",
                "end": "25:99",   # 非法
                "timezone": "UTC",
            })
            self.fail("Should have raised")
        except Exception as exc:
            errors = getattr(exc, "errors", None) or []
            fields = [e.get("field", "") for e in errors]
            self.assertTrue(
                any(".end" in f for f in fields),
                f"Error field should contain '.end', got {fields}",
            )

    def test_bad_start_still_reports_start(self):
        try:
            validate_silent_hours({
                "enabled": True,
                "start": "99:00",
                "end": "07:30",
                "timezone": "UTC",
            })
            self.fail("Should have raised")
        except Exception as exc:
            errors = getattr(exc, "errors", None) or []
            fields = [e.get("field", "") for e in errors]
            self.assertTrue(
                any(".start" in f for f in fields),
                f"Error field should contain '.start', got {fields}",
            )


# ---------------------------------------------------------------------------
# 7.8d RATE_LIMIT_WINDOW_SECONDS minimum 1→60
# ---------------------------------------------------------------------------
class TestRateLimitWindowMinimum(unittest.TestCase):
    def test_window_below_60_rejected_via_env(self):
        """旧 minimum=1，现在 <60 应报 ConfigError。"""
        for val in ("1", "30", "59"):
            with self.assertRaises(ConfigError, msg=f"window {val} should be rejected"):
                load_config({**_BASE_ENV, "RATE_LIMIT_WINDOW_SECONDS": val})

    def test_window_60_accepted(self):
        cfg = load_config({**_BASE_ENV, "RATE_LIMIT_WINDOW_SECONDS": "60"})
        self.assertEqual(cfg.rate_limit_window_seconds, 60)

    def test_window_0_still_rejected_via_constructor(self):
        with self.assertRaises(ConfigError):
            Config(dev_mode=True, rate_limit_window_seconds=0)


# ---------------------------------------------------------------------------
# 7.8e policy.py 没有 import math
# ---------------------------------------------------------------------------
class TestNoDeadImportMath(unittest.TestCase):
    def test_math_not_imported_by_policy(self):
        """旧实现有一行无用的 `import math`（没有任何调用点）。"""
        import importlib
        import botville_gateway.policy as pol
        self.assertFalse(
            hasattr(pol, "math"),
            "policy.py must not import math (dead code removed in #114-7.8e)",
        )


# ---------------------------------------------------------------------------
# 7.8f .env.example 含 GATEWAY_HMAC_SECRET 占位
# ---------------------------------------------------------------------------
class TestEnvExampleHasHmacSecret(unittest.TestCase):
    def _read_env_example(self, path: str) -> str:
        full = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            path,
        )
        try:
            with open(full, encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def test_root_env_example_has_hmac_secret(self):
        content = self._read_env_example(".env.example")
        self.assertIn("GATEWAY_HMAC_SECRET", content,
                      ".env.example must contain GATEWAY_HMAC_SECRET placeholder")

    def test_gateway_readme_mentions_hmac_secret_in_appservice_required(self):
        content = self._read_env_example("gateway/README.md")
        self.assertIn("GATEWAY_HMAC_SECRET", content,
                      "gateway/README.md must document GATEWAY_HMAC_SECRET")


# ---------------------------------------------------------------------------
# N3 账本键为 (conversation_id, sender, txnId)
# ---------------------------------------------------------------------------
class TestLedgerKeyTriple(unittest.TestCase):
    def test_ledger_key_is_triple(self):
        """旧实现账本键是裸 txnId，N3 改为三元组避免跨会话覆盖。"""
        svc, _, _ = make_svc()
        send(svc, key="mykey", conv="room1")
        keys = list(svc._txn_events.keys())
        self.assertEqual(len(keys), 1)
        key = keys[0]
        self.assertIsInstance(key, tuple, "ledger key must be a tuple")
        self.assertEqual(len(key), 3, "ledger key must be (conv_id, sender, txnId)")
        conv_id, sender, txn_id = key
        self.assertEqual(conv_id, "room1")
        self.assertEqual(sender, "p")
        self.assertRegex(txn_id, r"^[0-9a-f]{64}$")

    def test_ledger_key_includes_conversation_and_sender(self):
        """账本按 (conversation_id, sender, txnId) 归档（N3）。

        旧实现键是裸 txnId 字符串：不同会话/发送者的条目无法区分，跨会话
        复用同一幂等键（缓存淘汰后）会覆盖对方的 event_id、导致多计一次 SENT。
        """
        svc, _, _ = make_svc(make_cfg(max_idempotency_entries=10))
        send(svc, key="K", conv="room1", text="one")
        send(svc, key="L", conv="room2", text="two")
        keys = list(svc._txn_events)
        self.assertEqual(len(keys), 2)
        for key in keys:
            self.assertIsInstance(key, tuple, "ledger key must be a tuple")
            self.assertEqual(len(key), 3, "ledger key must be (conv_id, sender, txnId)")
        self.assertEqual(sorted(k[0] for k in keys), ["room1", "room2"])
        self.assertEqual({k[1] for k in keys}, {"p"})
        self.assertEqual(
            sorted(k[2] for k in keys),
            sorted([svc._txn_id_for("K"), svc._txn_id_for("L")]),
        )

    def test_same_txn_in_two_conversations_do_not_collide(self):
        """账本层：同 txnId + 不同 conversation 是两条独立记录（N3 核心）。"""
        svc, _, _ = make_svc(make_cfg(max_idempotency_entries=10))
        txn = svc._txn_id_for("K")
        svc._txn_events_put(("room1", "p", txn), "$evt_1")
        svc._txn_events_put(("room2", "p", txn), "$evt_2")
        self.assertEqual(len(svc._txn_events), 2)
        self.assertEqual(svc._txn_events_get(("room1", "p", txn)), "$evt_1")
        self.assertEqual(svc._txn_events_get(("room2", "p", txn)), "$evt_2")


# ---------------------------------------------------------------------------
# N8 GatewayService：store 无 secret 而 config 非 dev 时报错
# ---------------------------------------------------------------------------
class TestN8StoreSecretConsistency(unittest.TestCase):
    def test_store_without_secret_raises_when_config_has_secret_non_dev(self):
        """旧实现静默退回裸 SHA-256（无告警 fail-open）。"""
        cfg = Config(
            auth_token="token",
            dev_mode=False,
            silent_hours_enabled=False,
            hmac_secret=SECRET,
        )
        store_no_secret = UsageStore(max_events=0, secret=None)
        transport = InMemoryMatrixTransport()
        with self.assertRaises(RuntimeError, msg="Should raise when store has no secret but config does"):
            GatewayService(cfg, transport, store_no_secret, LimitStore(cfg))

    def test_store_with_secret_fine(self):
        cfg = Config(
            auth_token="token",
            dev_mode=False,
            silent_hours_enabled=False,
            hmac_secret=SECRET,
        )
        store = UsageStore(max_events=0, secret=SECRET)
        transport = InMemoryMatrixTransport()
        svc = GatewayService(cfg, transport, store, LimitStore(cfg))
        self.assertIsNotNone(svc)

    def test_dev_mode_store_without_secret_ok(self):
        """dev 模式下注入无 secret 的 store 不报错（测试兼容性）。"""
        cfg = make_cfg(dev_mode=True)
        store = UsageStore(max_events=0, secret=None)
        svc = GatewayService(cfg, InMemoryMatrixTransport(), store, LimitStore(cfg))
        self.assertIsNotNone(svc)


# ---------------------------------------------------------------------------
# N10 GATEWAY_HMAC_SECRET 只接受 hex，≥64 hex 字符，拒绝全零
# ---------------------------------------------------------------------------
class TestN10HmacSecretHexOnly(unittest.TestCase):
    BASE = {"GATEWAY_AUTH_TOKEN": "strong-token"}

    def test_non_hex_rejected(self):
        """旧实现把非 hex 字符串按 UTF-8 接受（奇数 hex 长度绕过）。"""
        non_hex = "z" * 64  # 不是有效 hex
        with self.assertRaises(ConfigError):
            load_config({**self.BASE, "GATEWAY_HMAC_SECRET": non_hex})

    def test_63_hex_chars_rejected_as_odd_length(self):
        """63 hex 字符 → 奇数，不能整字节解码，应被拒绝。"""
        with self.assertRaises(ConfigError):
            load_config({**self.BASE, "GATEWAY_HMAC_SECRET": "a" * 63})

    def test_62_hex_chars_too_short(self):
        """62 hex 字符 = 31 字节 < 32 字节最小要求。"""
        with self.assertRaises(ConfigError):
            load_config({**self.BASE, "GATEWAY_HMAC_SECRET": "ab" * 31})

    def test_all_zeros_64_chars_rejected(self):
        """全零（0*64）虽然长度达标，但熵为零，应被拒绝。"""
        with self.assertRaises(ConfigError):
            load_config({**self.BASE, "GATEWAY_HMAC_SECRET": "0" * 64})

    def test_valid_64_hex_chars_accepted(self):
        cfg = load_config({**self.BASE, "GATEWAY_HMAC_SECRET": "ab" * 32})
        self.assertEqual(cfg.hmac_secret, bytes.fromhex("ab" * 32))

    def test_all_zeros_via_constructor_rejected(self):
        with self.assertRaises(ConfigError):
            Config(dev_mode=False, auth_token="t", silent_hours_enabled=False,
                   hmac_secret=b"\x00" * 32)

    def test_error_message_includes_hint(self):
        try:
            load_config({**self.BASE, "GATEWAY_HMAC_SECRET": "nothex"})
            self.fail("Should have raised")
        except ConfigError as exc:
            self.assertIn("secrets.token_hex", str(exc),
                          "error message must include generation command hint")


# ---------------------------------------------------------------------------
# N11 InMemoryMatrixTransport.clear() 同时清 _by_txn
# ---------------------------------------------------------------------------
class TestN11ClearAlsoByTxn(unittest.TestCase):
    def test_clear_resets_by_txn(self):
        """旧实现 clear() 只清 sent，同 txnId 仍返回旧事件（测试假通过）。"""
        t = InMemoryMatrixTransport()
        e1 = t.send_text(room_id="r", sender_id="s", text="x", txn_id="t1")
        self.assertEqual(len(t.sent), 1)
        t.clear()
        self.assertEqual(len(t.sent), 0)
        # 关键：clear 后同 txnId 必须产生新事件，不能返回旧事件
        e2 = t.send_text(room_id="r", sender_id="s", text="x", txn_id="t1")
        self.assertNotEqual(e1.event_id, e2.event_id,
                            "clear() must also reset _by_txn; same txnId after clear must produce a new event")
        self.assertEqual(len(t.sent), 1)

    def test_clear_then_messages_for_is_empty(self):
        t = InMemoryMatrixTransport()
        t.send_text(room_id="r", sender_id="s", text="x")
        t.clear()
        self.assertEqual(t.messages_for("r"), [])


# ---------------------------------------------------------------------------
# N12 /health single-flight try/finally 包住 leader 标记
# ---------------------------------------------------------------------------
class TestN12HealthSingleFlightFinally(unittest.TestCase):
    def test_inflight_cleared_even_if_request_raises_base_exception(self):
        """旧实现：leader 置位 `_health_inflight` 后若 BaseException 打断，
        Event 永不 set，后续所有探针都等满 timeout。"""
        from botville_gateway.matrix_appservice import AppServiceMatrixTransport

        t = AppServiceMatrixTransport(
            homeserver_url="http://127.0.0.1:1",
            as_token="t" * 32,
            server_name="example.invalid",
            health_cache_seconds=0.0,
            health_timeout=1.0,
        )

        call_count = [0]

        def _bad_request(method, path, **kw):
            call_count[0] += 1
            raise MemoryError("simulated BaseException in whoami")

        t._request = _bad_request

        with self.assertRaises(MemoryError):
            t.healthy()

        # 关键：即使 MemoryError，_health_inflight 必须被清空
        self.assertIsNone(t._health_inflight,
                          "_health_inflight must be cleared even on BaseException")

        # 第二次调用应能重新发起请求（不永久等待）
        # 此处我们替换为返回 False 的 _request，确保可以再次调用
        def _ok_request(method, path, **kw):
            call_count[0] += 1
            return {"user_id": "@bot:example.invalid"}

        t._request = _ok_request
        result = t.healthy()
        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
