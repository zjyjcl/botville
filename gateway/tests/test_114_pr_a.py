# SPDX-License-Identifier: Apache-2.0
"""#114 PR-a：txnId / text_sha256 改 HMAC、淘汰后去重计费、/health single-flight。"""

from __future__ import annotations

import hashlib
import os
import sys
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from botville_gateway.config import Config, ConfigError, load_config
from botville_gateway.limits import LimitStore
from botville_gateway.matrix import DeliveryError, InMemoryMatrixTransport
from botville_gateway.matrix_appservice import AppServiceMatrixTransport
from botville_gateway.service import GatewayService
from botville_gateway.store import EventKind, UsageStore, text_digest

SECRET_A = b"A" * 32
SECRET_B = b"B" * 32
HEX_SECRET = "0123456789abcdef" * 4


def make_cfg(**kw) -> Config:
    base = dict(dev_mode=True, rate_limit_messages=100, silent_hours_enabled=False,
                hmac_secret=SECRET_A)
    base.update(kw)
    return Config(**base)


def make_svc(cfg: Config, clock=None):
    transport = InMemoryMatrixTransport()
    store = UsageStore(1000, secret=cfg.hmac_secret)
    kw = {"now_fn": clock} if clock is not None else {}
    svc = GatewayService(cfg, transport, store, LimitStore(cfg), **kw)
    return svc, transport, store


def send(svc, key, text="hi"):
    return svc.send_ai_message(
        "conv",
        {"sender_persona_id": "p", "content": {"type": "text", "text": text}},
        idempotency_key=key,
    )


class FixedClock:
    """可控时钟（N1 需要跨过账本 TTL 才会暴露问题）。"""

    def __init__(self, start: str = "2026-11-03T12:00:00Z") -> None:
        self._now = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: int) -> None:
        self._now += timedelta(seconds=seconds)


def sent_count(store, text: str) -> int:
    """只统计某个正文对应的 SENT 条数（`text_sha256` 可区分不同正文）。"""
    digest = text_digest(text, SECRET_A)
    return sum(
        1
        for ev in store.events_in_window(persona_id="p", kind=EventKind.SENT)
        if ev.text_sha256 == digest
    )


class TestTxnIdHmac(unittest.TestCase):
    def test_stable_and_secret_dependent(self):
        a, _, _ = make_svc(make_cfg())
        a2, _, _ = make_svc(make_cfg())
        b, _, _ = make_svc(make_cfg(hmac_secret=SECRET_B))
        self.assertEqual(a._txn_id_for("k1"), a2._txn_id_for("k1"))
        self.assertNotEqual(a._txn_id_for("k1"), b._txn_id_for("k1"))
        self.assertNotEqual(a._txn_id_for("k1"), a._txn_id_for("k2"))
        tid = a._txn_id_for("k1")
        self.assertRegex(tid, r"^[0-9a-f]{1,64}$")
        self.assertNotEqual(tid, hashlib.sha256(b"k1").hexdigest()[:64])


class TestSecretConfig(unittest.TestCase):
    BASE = {"GATEWAY_AUTH_TOKEN": "a-strong-random-token"}

    def test_non_dev_requires_secret(self):
        for val in (None, "", "<change-me>"):
            env = dict(self.BASE)
            if val is not None:
                env["GATEWAY_HMAC_SECRET"] = val
            with self.assertRaises(ConfigError):
                load_config(env)

    def test_short_secret_rejected(self):
        with self.assertRaises(ConfigError):
            load_config({**self.BASE, "GATEWAY_HMAC_SECRET": "abcd"})
        with self.assertRaises(ConfigError):
            Config(dev_mode=True, hmac_secret=b"x" * 31)

    def test_hex_secret_decoded(self):
        cfg = load_config({**self.BASE, "GATEWAY_HMAC_SECRET": HEX_SECRET})
        self.assertEqual(cfg.hmac_secret, bytes.fromhex(HEX_SECRET))
        self.assertNotIn(HEX_SECRET, repr(cfg))

    def test_dev_mode_generates_secret(self):
        cfg = load_config({"GATEWAY_DEV_MODE": "true"})
        self.assertIsNotNone(cfg.hmac_secret)
        self.assertGreaterEqual(len(cfg.hmac_secret), 32)


class TestDedupeBillingAfterEviction(unittest.TestCase):
    def test_retry_after_cache_eviction_not_billed_twice(self):
        svc, transport, store = make_svc(make_cfg(max_idempotency_entries=1))
        r1 = send(svc, "A")
        send(svc, "B")  # 挤掉 A 的幂等缓存
        r3 = send(svc, "A")  # 重新投递，HS（内存传输）按 txnId 返回同一事件
        sent = store.events_in_window(persona_id="p", kind=EventKind.SENT)
        self.assertEqual(len(sent), 2)
        self.assertEqual(len(transport.sent), 2)
        self.assertEqual(
            r1["delivery"]["matrix_event_id"], r3["delivery"]["matrix_event_id"]
        )

    def test_memory_transport_same_txn_same_event(self):
        t = InMemoryMatrixTransport()
        e1 = t.send_text(room_id="r", sender_id="s", text="x", txn_id="t1")
        e2 = t.send_text(room_id="r", sender_id="s", text="x", txn_id="t1")
        e3 = t.send_text(room_id="r", sender_id="s", text="x", txn_id="t2")
        self.assertEqual(e1.event_id, e2.event_id)
        self.assertNotEqual(e1.event_id, e3.event_id)
        self.assertEqual(len(t.sent), 2)


class _NoEchoTransport:
    """HS 语义但**不回填** `SentEvent.txn_id` 的传输（N2 的隐式契约探针）。"""

    def __init__(self) -> None:
        self._inner = InMemoryMatrixTransport()

    @property
    def sent(self):
        return self._inner.sent

    def send_text(self, **kw):
        event = self._inner.send_text(**kw)
        return replace(event, txn_id=None)

    def healthy(self) -> bool:
        return True


class TestLedgerKeyedByServiceTxn(unittest.TestCase):
    """B1 / N2：账本键取 service 自己算出的 txnId，且无 key 时不写账本。"""

    def test_no_key_sends_do_not_grow_ledger(self):
        svc, transport, store = make_svc(make_cfg())
        for i in range(50):
            r = svc.send_ai_message("conv", {
                "sender_persona_id": "p",
                "content": {"type": "text", "text": f"n{i}"},
            })
            self.assertEqual(r["outcome"], "delivered")
        # 无 key → 随机 txnId，没有重试语义，不该污染账本（B1）。
        self.assertEqual(len(svc._txn_events), 0)
        # 计费照常：每个无 key 发送都是一条 SENT。
        self.assertEqual(len(transport.sent), 50)
        self.assertEqual(
            len(store.events_in_window(persona_id="p", kind=EventKind.SENT)), 50
        )

    def test_no_key_noise_does_not_evict_ledger_entry(self):
        svc, transport, store = make_svc(make_cfg(max_idempotency_entries=1))
        r1 = send(svc, "K", text="keep")
        send(svc, "L", text="other")  # 挤掉 K 的幂等缓存，账本容量 2 仍持有 K
        for i in range(10):  # 大量无 key 噪声：不写账本，也就挤不掉 K
            send(svc, None, text=f"noise{i}")
        r2 = send(svc, "K", text="keep")  # 重新投递，HS 返回同一事件
        self.assertEqual(sent_count(store, "keep"), 1)
        self.assertEqual(
            r1["delivery"]["matrix_event_id"], r2["delivery"]["matrix_event_id"]
        )
        self.assertEqual(len(transport.sent), 12)
        self.assertEqual(sorted(svc._txn_events), sorted([
            svc._txn_id_for("K"), svc._txn_id_for("L"),
        ]))

    def test_ledger_survives_transport_not_echoing_txn_id(self):
        """N2：传输不回填 `SentEvent.txn_id` 时去重仍生效。"""
        svc, transport, store = make_svc(make_cfg(max_idempotency_entries=1))
        svc._transport = transport = _NoEchoTransport()
        r1 = send(svc, "A", text="alpha")
        send(svc, "B", text="beta")
        r3 = send(svc, "A", text="alpha")
        self.assertEqual(sent_count(store, "alpha"), 1)
        self.assertEqual(
            r1["delivery"]["matrix_event_id"], r3["delivery"]["matrix_event_id"]
        )
        self.assertEqual(len(transport.sent), 2)


class TestLedgerTtlRefresh(unittest.TestCase):
    """N1：去重命中也刷新账本时间戳，账本不会先于幂等缓存过期。"""

    def test_dedupe_hit_refreshes_ledger_timestamp(self):
        clock = FixedClock()
        cfg = make_cfg(idempotency_ttl_seconds=100, max_idempotency_entries=1)
        svc, transport, store = make_svc(cfg, clock=clock)
        send(svc, "A", text="alpha")            # t=0   账本 A@0，TTL 200
        send(svc, "B", text="beta")             # 挤出 A 的幂等缓存
        clock.advance(150)
        r3 = send(svc, "A", text="alpha")       # t=150 去重命中，账本续到 350
        self.assertEqual(sent_count(store, "alpha"), 1)
        clock.advance(110)
        # t=260：幂等缓存（TTL 100，150 起算）已过期，账本若未续期（200）也已过期。
        r4 = send(svc, "A", text="alpha")
        self.assertEqual(sent_count(store, "alpha"), 1)
        self.assertEqual(r3["delivery"]["matrix_event_id"],
                         r4["delivery"]["matrix_event_id"])
        self.assertEqual(len(transport.sent), 2)


class _StubAS(AppServiceMatrixTransport):
    def __init__(self, delay=0.0, fail=False, **kw):
        super().__init__(homeserver_url="http://127.0.0.1:1", as_token="t" * 32,
                         server_name="example.invalid", **kw)
        self.calls = []
        self.delay = delay
        self.fail = fail
        self._calls_lock = threading.Lock()

    def _request(self, method, path, body=None, query=None, timeout=None):
        with self._calls_lock:
            self.calls.append((method, path))
        if self.delay:
            time.sleep(self.delay)
        if "whoami" in path:
            if self.fail:
                raise DeliveryError("matrix.unavailable", "down")
            return {"user_id": "@bot:example.invalid"}
        if "register" in path:
            return {"user_id": "@ai_p:example.invalid"}
        if "/send/" in path:
            return {"event_id": "$evt_" + path.rsplit("/", 1)[-1][:8]}
        return {}


class TestAppServiceTxnId(unittest.TestCase):
    def test_sent_event_carries_txn_id(self):
        t = _StubAS()
        cfg = make_cfg()
        svc, _, _ = make_svc(cfg)
        txn = svc._txn_id_for("same-key")
        e1 = t.send_text(room_id="!r:example.invalid", sender_id="@ai_p:example.invalid",
                         text="x", txn_id=txn)
        e2 = t.send_text(room_id="!r:example.invalid", sender_id="@ai_p:example.invalid",
                         text="x", txn_id=txn)
        self.assertEqual(e1.txn_id, txn)
        self.assertEqual(e1.txn_id, e2.txn_id)

    def test_no_uuid_import(self):
        import botville_gateway.matrix_appservice as m
        self.assertFalse(hasattr(m, "uuid"))


class TestHealthSingleFlight(unittest.TestCase):
    def _burst(self, t, n=10):
        results = []
        lock = threading.Lock()
        barrier = threading.Barrier(n)

        def run():
            barrier.wait()
            r = t.healthy()
            with lock:
                results.append(r)

        threads = [threading.Thread(target=run) for _ in range(n)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(5)
        return results

    def test_concurrent_probes_one_whoami(self):
        t = _StubAS(delay=0.2)
        results = self._burst(t)
        self.assertEqual(len(t.calls), 1)
        self.assertEqual(results, [True] * 10)

    def test_failure_also_single_flight_and_recovers(self):
        t = _StubAS(delay=0.2, fail=True, health_cache_seconds=0.0)
        results = self._burst(t)
        self.assertEqual(len(t.calls), 1)
        self.assertEqual(results, [False] * 10)
        t.fail = False
        t.delay = 0.0
        self.assertTrue(t.healthy())
        self.assertEqual(len(t.calls), 2)


class TestTextDigestHmac(unittest.TestCase):
    def test_hmac_digest(self):
        self.assertEqual(text_digest("hello", SECRET_A), text_digest("hello", SECRET_A))
        self.assertNotEqual(text_digest("hello", SECRET_A), text_digest("hello", SECRET_B))
        self.assertNotEqual(text_digest("hello", SECRET_A),
                            hashlib.sha256(b"hello").hexdigest())
        self.assertEqual(text_digest("", SECRET_A), "")

    def test_store_events_use_secret(self):
        svc, _, store = make_svc(make_cfg())
        send(svc, "k", text="hello")
        ev = store.events_in_window(persona_id="p", kind=EventKind.SENT)[0]
        self.assertEqual(ev.text_sha256, text_digest("hello", SECRET_A))


if __name__ == "__main__":
    unittest.main()
