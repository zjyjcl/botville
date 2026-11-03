# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""B2.1 锁外投递：预留-提交/回滚 与并发测试。

要证明的三件事：

1. **并发不超发**：多线程同时发送、限额为 N 时恰好 N 条 delivered，其余 blocked；
   在途预留（锁外投递中的名额）计入消息数与 token 预算。
2. **send_text 在锁外执行**：假 transport 会 sleep，多个请求的投递时间必须重叠，
   总耗时接近「一次 sleep」而不是「N 次 sleep 串行」。
3. **投递失败回滚**：失败后不写 UsageEvent、不计费、名额可立刻再用；
   预留账本回到 0，异常仍映射为 `GatewayDeliveryError`。

另加一条回归：同 `Idempotency-Key` 的并发请求仍然只投递一次（重构前由全局锁
保证，重构后由按 key 拆分的互斥闸门保证）。
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
from botville_gateway.errors import GatewayDeliveryError  # noqa: E402
from botville_gateway.http_server import build_server  # noqa: E402
from botville_gateway.limits import LimitStore  # noqa: E402
from botville_gateway.matrix import DeliveryError, SentEvent  # noqa: E402
from botville_gateway.service import GatewayService  # noqa: E402
from botville_gateway.store import EventKind, UsageStore  # noqa: E402


class FixedClock:
    """冻结时钟：并发测试里所有请求看到同一个 now，窗口判断不靠运气。"""

    def __init__(self, start_iso: str = "2026-11-03T12:00:00Z") -> None:
        self._now = datetime.datetime.strptime(start_iso, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )

    def __call__(self) -> datetime.datetime:
        return self._now

    def advance(self, **delta) -> None:
        self._now = self._now + timedelta(**delta)


@dataclass
class SlowTransport:
    """会 sleep 的假 transport，同时统计并发度与投递耗时。

    - `sleep_seconds`：每次 `send_text` 的耗时（模拟 Matrix 网络往返）；
    - `fail_times`：前 N 次投递抛 `DeliveryError`（模拟传输失败）；
    - `gate`：非 None 时 `send_text` 在 gate.set() 之前挂住，用于「在途」断言。
    """

    sleep_seconds: float = 0.0
    fail_times: int = 0
    gate: Optional[threading.Event] = None
    sent: List[SentEvent] = field(default_factory=list)
    calls: int = 0
    max_concurrent: int = 0
    _concurrent: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def send_text(
        self,
        *,
        room_id: str,
        sender_id: str,
        text: str,
        reply_to: Optional[str] = None,
        content_type: str = "text",
    ) -> SentEvent:
        with self._lock:
            self.calls += 1
            self._concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self._concurrent)
            call_no = self.calls
        try:
            if self.gate is not None:
                self.gate.wait(timeout=5)
            if self.sleep_seconds:
                time.sleep(self.sleep_seconds)
            if call_no <= self.fail_times:
                raise DeliveryError("matrix.unavailable", "simulated transport failure")
            event = SentEvent(
                event_id=f"$slow_{call_no}",
                room_id=room_id,
                sender_id=sender_id,
                text=text,
                sent_at=datetime.datetime.now(timezone.utc),
                reply_to=reply_to,
                content_type=content_type,
            )
            with self._lock:
                self.sent.append(event)
            return event
        finally:
            with self._lock:
                self._concurrent -= 1

    def healthy(self) -> bool:
        return True


def make_config(**overrides) -> Config:
    base = dict(
        dev_mode=True,
        rate_limit_messages=5,
        rate_limit_window_seconds=3600,
        silent_hours_enabled=False,
        max_message_chars=8000,
        platform_token_budget=10_000,
        platform_proactive_per_day=100,
    )
    base.update(overrides)
    return Config(**base)


def make_service(config: Config = None, *, transport=None, start: str = None):
    config = config or make_config()
    clock = FixedClock(start) if start else FixedClock()
    transport = transport if transport is not None else SlowTransport()
    store = UsageStore(max_events=0)
    limits = LimitStore(config)
    svc = GatewayService(config, transport, store, limits, now_fn=clock)
    return svc, transport, store, limits, clock


def payload(index: int, *, token_estimate: int = None) -> dict:
    body = {
        "sender_persona_id": "p",
        "content": {"type": "text", "text": f"m{index}"},
    }
    if token_estimate is not None:
        body["token_estimate"] = token_estimate
    return body


# ---------------------------------------------------------------------------
# 1. 并发限额：N 条成功，其余 blocked
# ---------------------------------------------------------------------------
class TestConcurrentLimit(unittest.TestCase):
    def _run_concurrently(self, svc, count, barrier):
        results = [None] * count
        errors = []

        def worker(i):
            try:
                barrier.wait(timeout=5)
                results[i] = svc.send_ai_message("conv", payload(i))
            except BaseException as exc:  # pragma: no cover - 失败时打印
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(errors, [])
        return results

    def test_exactly_limit_messages_delivered_under_concurrency(self):
        cfg = make_config(rate_limit_messages=3)
        transport = SlowTransport(sleep_seconds=0.02)
        svc, transport, store, limits, clock = make_service(cfg, transport=transport)

        results = self._run_concurrently(svc, 8, threading.Barrier(8))

        delivered = [r for r in results if r["outcome"] == "delivered"]
        blocked = [r for r in results if r["outcome"] == "blocked"]
        self.assertEqual(len(delivered), 3)
        self.assertEqual(len(blocked), 5)
        self.assertEqual(len(transport.sent), 3)
        self.assertEqual(
            len([e for e in store.all_events() if e.kind == EventKind.SENT]), 3
        )
        # 被拦截的请求不占名额、不投递，但记 blocked 事件
        self.assertEqual(
            len([e for e in store.all_events() if e.kind == EventKind.BLOCKED]), 5
        )
        # 全部结束后没有残留预留
        self.assertEqual(svc.pending_reservations, 0)
        for receipt in blocked:
            self.assertFalse(receipt["retryable"])
            codes = [c["code"] for c in receipt["decision"]["reasons"]]
            self.assertIn("messages.window.exceeded", codes)

    def test_token_budget_is_reserved_not_oversubscribed(self):
        """token 预扣并发安全：budget=100、每条 60 token → 恰好 1 条成功。"""
        cfg = make_config(rate_limit_messages=100, platform_token_budget=100)
        transport = SlowTransport(sleep_seconds=0.02)
        svc, transport, store, limits, clock = make_service(cfg, transport=transport)

        barrier = threading.Barrier(6)
        outcomes = [None] * 6
        errors = []

        def worker(i):
            try:
                barrier.wait(timeout=5)
                outcomes[i] = svc.send_ai_message("conv", payload(i, token_estimate=60))
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(errors, [])
        delivered = [r for r in outcomes if r["outcome"] == "delivered"]
        self.assertEqual(len(delivered), 1)
        self.assertEqual(len(transport.sent), 1)
        sent_events = [e for e in store.all_events() if e.kind == EventKind.SENT]
        self.assertEqual(sum(e.tokens_out for e in sent_events), 60)
        self.assertEqual(svc.pending_reservations, 0)

    def test_inflight_reservation_blocks_a_second_sender(self):
        """一条消息还在投递途中（未提交）时，第二个请求看到名额已被占用。"""
        cfg = make_config(rate_limit_messages=1)
        gate = threading.Event()
        transport = SlowTransport(gate=gate)
        svc, transport, store, limits, clock = make_service(cfg, transport=transport)

        first_result = {}

        def first():
            first_result["receipt"] = svc.send_ai_message("conv", payload(0))

        thread = threading.Thread(target=first)
        thread.start()

        # 等第一条真正进入 transport（说明它已预留、锁已释放）
        deadline = time.time() + 5
        while transport.calls == 0 and time.time() < deadline:
            time.sleep(0.005)
        self.assertEqual(transport.calls, 1)
        self.assertEqual(len(transport.sent), 0)

        # 在途预留占住唯一名额：第二个请求被拦截，且不会被 transport 阻塞
        second = svc.send_ai_message("conv", payload(1))
        self.assertEqual(second["outcome"], "blocked")
        self.assertEqual(transport.calls, 1)

        gate.set()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(first_result["receipt"]["outcome"], "delivered")
        self.assertEqual(len(transport.sent), 1)
        self.assertEqual(svc.pending_reservations, 0)

        # 提交后名额确实被这次投递消耗掉
        third = svc.send_ai_message("conv", payload(2))
        self.assertEqual(third["outcome"], "blocked")


# ---------------------------------------------------------------------------
# 2. send_text 在锁外执行（投递并行）
# ---------------------------------------------------------------------------
class TestDeliveryOutsideLock(unittest.TestCase):
    def test_sends_overlap_instead_of_serialising(self):
        cfg = make_config(rate_limit_messages=4)
        transport = SlowTransport(sleep_seconds=0.25)
        svc, transport, store, limits, clock = make_service(cfg, transport=transport)

        barrier = threading.Barrier(4)
        results = [None] * 4
        errors = []

        def worker(i):
            try:
                barrier.wait(timeout=5)
                results[i] = svc.send_ai_message("conv", payload(i))
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        started = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        elapsed = time.monotonic() - started

        self.assertEqual(errors, [])
        self.assertEqual([r["outcome"] for r in results], ["delivered"] * 4)
        self.assertEqual(len(transport.sent), 4)
        # 串行执行至少 4×0.25s = 1.0s；并行执行接近一次 sleep。
        self.assertLess(elapsed, 0.85, f"delivery looks serialised ({elapsed:.2f}s)")
        self.assertGreaterEqual(
            transport.max_concurrent, 2,
            "send_text never overlapped, so it is probably still called under the lock",
        )
        # 投递期间锁未被持有：其它请求（例如 usage 查询）可以立刻得到响应。
        self.assertEqual(svc.pending_reservations, 0)

    def test_usage_query_not_blocked_during_delivery(self):
        cfg = make_config(rate_limit_messages=4)
        gate = threading.Event()
        transport = SlowTransport(gate=gate)
        svc, transport, store, limits, clock = make_service(cfg, transport=transport)

        thread = threading.Thread(target=lambda: svc.send_ai_message("conv", payload(0)))
        thread.start()
        deadline = time.time() + 5
        while transport.calls == 0 and time.time() < deadline:
            time.sleep(0.005)

        started = time.monotonic()
        usage = svc.get_usage(persona_id="p")
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0, "get_usage blocked behind an in-flight delivery")
        self.assertEqual(usage["totals"]["messages_sent"], 0)

        gate.set()
        thread.join(timeout=5)
        self.assertEqual(svc.get_usage(persona_id="p")["totals"]["messages_sent"], 1)


# ---------------------------------------------------------------------------
# 3. 投递失败：回滚预留、不计费、名额可再用
# ---------------------------------------------------------------------------
class TestDeliveryFailureRollback(unittest.TestCase):
    def test_failed_delivery_does_not_charge_or_consume_a_slot(self):
        cfg = make_config(rate_limit_messages=1, platform_token_budget=100)
        transport = SlowTransport(fail_times=1)
        svc, transport, store, limits, clock = make_service(cfg, transport=transport)

        with self.assertRaises(GatewayDeliveryError) as ctx:
            svc.send_ai_message("conv", payload(0, token_estimate=90))
        self.assertEqual(ctx.exception.code, "matrix.unavailable")

        # 失败：没有 SENT 事件、没有计费、没有残留预留
        self.assertEqual([e for e in store.all_events() if e.kind == EventKind.SENT], [])
        self.assertEqual(svc.pending_reservations, 0)
        usage = svc.get_usage(persona_id="p")
        self.assertEqual(usage["totals"]["messages_sent"], 0)
        self.assertEqual(usage["totals"]["tokens_total"], 0)
        self.assertEqual(usage["budget"]["used"], 0)
        self.assertEqual(usage["budget"]["remaining"], 100)

        # 名额与 token 预算都可立刻再用
        retry = svc.send_ai_message("conv", payload(1, token_estimate=90))
        self.assertEqual(retry["outcome"], "delivered")
        self.assertEqual(len(transport.sent), 1)
        self.assertEqual(svc.get_usage(persona_id="p")["totals"]["tokens_total"], 90)

    def test_every_failed_attempt_rolls_back(self):
        """连续失败多次也不会把名额吃光：limit=1 时可无限重试。"""
        cfg = make_config(rate_limit_messages=1)
        transport = SlowTransport(fail_times=3)
        svc, transport, store, limits, clock = make_service(cfg, transport=transport)

        for i in range(3):
            with self.assertRaises(GatewayDeliveryError):
                svc.send_ai_message("conv", payload(i))
            self.assertEqual(svc.pending_reservations, 0)
            self.assertEqual(len(transport.sent), 0)

        ok = svc.send_ai_message("conv", payload(99))
        self.assertEqual(ok["outcome"], "delivered")
        self.assertEqual(transport.calls, 4)

    def test_unexpected_transport_exception_also_rolls_back(self):
        class ExplodingTransport(SlowTransport):
            def send_text(self, **kwargs):
                raise RuntimeError("boom")

        cfg = make_config(rate_limit_messages=1)
        transport = ExplodingTransport()
        svc, transport, store, limits, clock = make_service(cfg, transport=transport)

        with self.assertRaises(RuntimeError):
            svc.send_ai_message("conv", payload(0))
        self.assertEqual(svc.pending_reservations, 0)
        self.assertEqual(len([e for e in store.all_events() if e.kind == EventKind.SENT]), 0)

    def test_failure_over_http_is_500_and_not_recorded(self):
        cfg = make_config(rate_limit_messages=1)
        transport = SlowTransport(fail_times=1)
        server = build_server(cfg, transport=transport)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address[:2]
        base = f"http://{host}:{port}"
        try:
            status, body = _post(f"{base}/v1/conversations/c/messages", payload(0))
            self.assertEqual(status, 500)
            self.assertEqual(body["code"], "internal.error")
            self.assertEqual(server.service.pending_reservations, 0)
            self.assertEqual(len(server.service.store), 0)

            status, body = _post(f"{base}/v1/conversations/c/messages", payload(1))
            self.assertEqual(status, 200)
            self.assertEqual(body["outcome"], "delivered")
            self.assertEqual(len(server.service.store), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_build_server_accepts_optional_transport(self):
        transport = SlowTransport()
        cfg = make_config()
        server = build_server(cfg, transport=transport)
        try:
            self.assertIs(server.transport, transport)
            self.assertIs(server.service._transport, transport)
        finally:
            server.server_close()
        default = build_server(make_config())
        try:
            self.assertIsNot(default.transport, transport)
            self.assertEqual(default.transport.__class__.__name__, "InMemoryMatrixTransport")
        finally:
            default.server_close()


# ---------------------------------------------------------------------------
# 4. ReservationBook 直接单测：在途预留计入三类限额
# ---------------------------------------------------------------------------
class TestReservationBookAccounting(unittest.TestCase):
    def _engine(self, cfg):
        from botville_gateway.policy import PolicyEngine, ReservationBook

        clock = FixedClock()
        store = UsageStore(max_events=0)
        limits = LimitStore(cfg)
        book = ReservationBook()
        engine = PolicyEngine(
            cfg, store, limits, now_fn=clock, reservations=book
        )
        return engine, store, limits, book, clock

    def test_reservation_counts_toward_message_window(self):
        engine, store, limits, book, clock = self._engine(
            make_config(rate_limit_messages=2)
        )
        book.reserve(persona_id="p", tokens=1, at=clock())
        check = {c.key: c for c in engine.evaluate(persona_id="p").checks}[
            "messages.window"
        ]
        self.assertEqual(check.used, 1)
        self.assertEqual(check.remaining, 1)
        self.assertTrue(check.allowed)

        book.reserve(persona_id="p", tokens=1, at=clock())
        blocked = engine.evaluate(persona_id="p")
        check = {c.key: c for c in blocked.checks}["messages.window"]
        self.assertEqual(check.used, 2)
        self.assertFalse(check.allowed)
        self.assertEqual(blocked.decision, "block")

    def test_reservation_reserves_token_budget(self):
        engine, store, limits, book, clock = self._engine(
            make_config(platform_token_budget=100)
        )
        book.reserve(persona_id="p", tokens=70, at=clock())
        check = {c.key: c for c in engine.evaluate(
            persona_id="p", token_estimate=40
        ).checks}["tokens.window"]
        self.assertFalse(check.allowed)
        self.assertEqual(check.used, 70)

    def test_reservation_counts_for_min_interval(self):
        engine, store, limits, book, clock = self._engine(make_config())
        limits.put(
            {"layer": "owner", "scope": "persona", "scope_id": "p",
             "rules": {"min_seconds_between_messages": 30}},
            now=clock(),
        )
        book.reserve(persona_id="p", tokens=1, at=clock())
        check = {c.key: c for c in engine.evaluate(persona_id="p").checks}[
            "min_interval"
        ]
        self.assertFalse(check.allowed)
        self.assertEqual(check.limit, 30)
        self.assertEqual(check.code, "min_interval.too_soon")

    def test_release_frees_the_quota_again(self):
        engine, store, limits, book, clock = self._engine(
            make_config(rate_limit_messages=1)
        )
        reservation = book.reserve(persona_id="p", tokens=5, at=clock())
        self.assertFalse(
            {c.key: c for c in engine.evaluate(persona_id="p").checks}[
                "messages.window"
            ].allowed
        )
        self.assertTrue(book.release(reservation))
        self.assertFalse(book.release(reservation), "double release must be a no-op")
        self.assertTrue(
            {c.key: c for c in engine.evaluate(persona_id="p").checks}[
                "messages.window"
            ].allowed
        )
        self.assertEqual(len(book), 0)

    def test_reservations_are_scoped_per_persona(self):
        engine, store, limits, book, clock = self._engine(
            make_config(rate_limit_messages=1)
        )
        book.reserve(persona_id="other", tokens=5, at=clock())
        check = {c.key: c for c in engine.evaluate(persona_id="p").checks}[
            "messages.window"
        ]
        self.assertTrue(check.allowed)
        self.assertEqual(check.used, 0)


# ---------------------------------------------------------------------------
# 5. 回归：同 Idempotency-Key 并发仍只投递一次
# ---------------------------------------------------------------------------
class TestIdempotencyStillSerialised(unittest.TestCase):
    def test_same_key_concurrent_requests_deliver_once(self):
        cfg = make_config(rate_limit_messages=100)
        transport = SlowTransport(sleep_seconds=0.05)
        svc, transport, store, limits, clock = make_service(cfg, transport=transport)

        barrier = threading.Barrier(6)
        results = [None] * 6
        errors = []

        def worker(i):
            try:
                barrier.wait(timeout=5)
                results[i] = svc.send_ai_message(
                    "conv", payload(0), idempotency_key="same-key-0001"
                )
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(transport.calls, 1)
        self.assertEqual(len(transport.sent), 1)
        self.assertEqual(
            len([e for e in store.all_events() if e.kind == EventKind.SENT]), 1
        )
        # 六个回执是同一个投递结果
        delivery_ids = {r["delivery"]["matrix_event_id"] for r in results}
        self.assertEqual(len(delivery_ids), 1)
        self.assertEqual(svc.pending_reservations, 0)
        # 闸门用完即回收，不会无界增长
        self.assertEqual(svc._idempotency_guards, {})

    def test_different_keys_still_run_concurrently(self):
        cfg = make_config(rate_limit_messages=100)
        transport = SlowTransport(sleep_seconds=0.2)
        svc, transport, store, limits, clock = make_service(cfg, transport=transport)

        barrier = threading.Barrier(3)
        errors = []

        def worker(i):
            try:
                barrier.wait(timeout=5)
                svc.send_ai_message("conv", payload(i), idempotency_key=f"key-000{i}")
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
        started = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        elapsed = time.monotonic() - started

        self.assertEqual(errors, [])
        self.assertEqual(len(transport.sent), 3)
        self.assertLess(elapsed, 0.55, f"distinct keys were serialised ({elapsed:.2f}s)")
        self.assertGreaterEqual(transport.max_concurrent, 2)


def _post(url: str, body: dict):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST"
    )
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, json.loads(raw) if raw else None


if __name__ == "__main__":
    unittest.main()
