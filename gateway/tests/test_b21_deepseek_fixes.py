# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""DeepSeek B1–B4 与 #99 加固的回归测试。

B1: window_seconds 与计数成对比较（速率对），不独立取 min。
B2: peer_id 进入实发路径，三层全部生效。
B3: token_estimate:0 不可绕过 token 预算。
#99-1: txnId 由 Idempotency-Key 推导（重试同 txnId）。
#99-2: healthy() 短超时 + 结果缓存。
#99-3: ensure_user 读写加锁（double-check）。
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import threading
import time
import unittest
import urllib.parse
from datetime import timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

GATEWAY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(GATEWAY_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from botville_gateway.config import Config  # noqa: E402
from botville_gateway.errors import LimitError  # noqa: E402
from botville_gateway.limits import LimitStore, rate_is_looser  # noqa: E402
from botville_gateway.matrix import DeliveryError, InMemoryMatrixTransport  # noqa: E402
from botville_gateway.matrix_appservice import AppServiceMatrixTransport  # noqa: E402
from botville_gateway.policy import PolicyEngine, estimate_tokens  # noqa: E402
from botville_gateway.service import GatewayService  # noqa: E402
from botville_gateway.store import EventKind, UsageEvent, UsageStore  # noqa: E402


# ------------------------------------------------------------------ helpers
class FixedClock:
    def __init__(self, start: str = "2026-11-03T12:00:00Z") -> None:
        self._now = datetime.datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )

    def __call__(self) -> datetime.datetime:
        return self._now

    def advance(self, **delta) -> None:
        self._now = self._now + timedelta(**delta)


def make_config(**overrides) -> Config:
    base = dict(
        dev_mode=True,
        rate_limit_messages=2,
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


def send(svc, text="hi", *, peer_id=None, token_estimate=None):
    payload: Dict[str, Any] = {
        "sender_persona_id": "p",
        "content": {"type": "text", "text": text},
    }
    if peer_id is not None:
        payload["peer_id"] = peer_id
    if token_estimate is not None:
        payload["token_estimate"] = token_estimate
    return svc.send_ai_message("conv", payload)


# ============================================================
# B1: 速率对 — window_seconds 与计数成对比较
# ============================================================
class TestB1RatePair(unittest.TestCase):
    """平台 2/3600；若 window_seconds 被单独取 min，owner {window:60} 就变成 2/60。"""

    def setUp(self):
        self.cfg = make_config(rate_limit_messages=2, rate_limit_window_seconds=3600)
        self.store = LimitStore(self.cfg)
        self.now = datetime.datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)

    def put(self, rules):
        return self.store.put(
            {"layer": "owner", "scope": "persona", "scope_id": "p", "rules": rules},
            now=self.now,
        )

    def test_window_only_shorter_is_rejected_422(self):
        """只缩短窗口（计数不变）= 速率 × N 倍放宽 → 必须 422。"""
        with self.assertRaises(LimitError) as ctx:
            self.put({"window_seconds": 60})
        self.assertEqual(ctx.exception.code, "limit.out_of_range")

    def test_platform_2_3600_plus_owner_window_60_is_422(self):
        """平台 2/3600，owner 显式写 messages:2 window:60 → 速率放宽 → 422。"""
        with self.assertRaises(LimitError):
            self.put({"messages_per_window": 2, "window_seconds": 60})

    def test_owner_messages_1_is_accepted(self):
        """messages:1 不带窗口 → 用平台窗口比较 → 1/3600 < 2/3600 → 接受。"""
        layer = self.put({"messages_per_window": 1})
        self.assertEqual(layer.rules["messages_per_window"], 1)

    def test_token_window_only_shorter_is_rejected(self):
        """平台有 tokens_per_window，只缩短窗口 → 速率放宽 → 422。"""
        with self.assertRaises(LimitError):
            self.put({"tokens_per_window": 1000, "window_seconds": 60})

    def test_effective_uses_winner_layer_window(self):
        """owner 设 messages:1（用平台窗口 3600），生效值里 messages.window_seconds == 3600。"""
        self.put({"messages_per_window": 1})
        eff = self.store.effective(persona_id="p", now=self.now)
        win_item = eff.get("messages.window_seconds")
        self.assertIsNotNone(win_item)
        self.assertEqual(win_item.value, 3600)
        self.assertEqual(win_item.source_layer, "owner")

    def test_send_5_messages_within_platform_window_respects_2_limit(self):
        """实发 5 条散布在 5 分钟内，平台 2/3600；限额不被放大。"""
        svc, transport, *_ , clock = make_service(make_config(
            rate_limit_messages=2, rate_limit_window_seconds=3600
        ))
        # 每 60 秒发一条，总共覆盖 4 分钟（远小于平台 3600s 窗口）
        outcomes = []
        for i in range(5):
            outcomes.append(send(svc, f"m{i}")["outcome"])
            clock.advance(seconds=60)
        self.assertEqual(outcomes.count("delivered"), 2)
        self.assertEqual(outcomes.count("blocked"), 3)
        # 若 window 被单独取 min 成 60s，每 60 秒就会腾出一个名额 → 5 条全 delivered。
        self.assertEqual(len(transport.sent), 2)

    def test_shortened_window_cannot_be_applied(self):
        """尝试只写 window_seconds 缩小窗口 → 422，实际发送仍按平台窗口。"""
        svc, transport, store, limits, clock = make_service(make_config(
            rate_limit_messages=2, rate_limit_window_seconds=3600
        ))
        with self.assertRaises(LimitError):
            svc.put_limits({
                "layer": "owner", "scope": "persona", "scope_id": "p",
                "rules": {"window_seconds": 60},
            })
        for i in range(3):
            send(svc, f"m{i}")
            clock.advance(seconds=60)
        self.assertEqual(len(transport.sent), 2)

    def test_rate_is_looser_helper(self):
        """rate_is_looser: 2/60 比 2/3600 宽松（交叉相乘验证）。"""
        # 2/60 vs 2/3600: 2*3600 > 2*60 → looser
        self.assertTrue(rate_is_looser(2, 60, 2, 3600))
        # 1/3600 vs 2/3600: 1*3600 <= 2*3600 → not looser
        self.assertFalse(rate_is_looser(1, 3600, 2, 3600))
        # 相同速率
        self.assertFalse(rate_is_looser(2, 3600, 2, 3600))

    def test_effective_keys_are_unique(self):
        """复审 B1-残：effective[] 每个 key 只出现一次，source_layer 不自相矛盾。

        平台 2@3600 + owner {messages:2, window:7200}（速率更严）时，
        旧实现会写出两条 messages_per_window（owner 与 platform 各一）。
        """
        self.put({"messages_per_window": 2, "window_seconds": 7200})
        eff = self.store.effective(persona_id="p", now=self.now)
        body = eff.to_dict()
        keys = [item["key"] for item in body["effective"]]
        self.assertEqual(len(keys), len(set(keys)), f"duplicate keys: {keys}")
        # get() / dict 取值 / 列表三者一致（都指向 owner 速率对）
        by_key = {item["key"]: item for item in body["effective"]}
        msg = by_key["messages_per_window"]
        win = by_key["messages.window_seconds"]
        self.assertEqual(msg["value"], 2)
        self.assertEqual(msg["source_layer"], "owner")
        self.assertEqual(win["value"], 7200)
        self.assertEqual(win["source_layer"], "owner")
        self.assertEqual(eff.get("messages_per_window").source_layer, "owner")
        self.assertEqual(eff.get("messages.window_seconds").value, 7200)

    def test_higher_count_with_longer_window_accepted_when_rate_stricter(self):
        """复审 B1-残：{count↑, window↑} 且 rate 不放宽时应被接受（不被标量规则 422）。

        平台 2/3600；owner {messages:3, window:7200} → 3/7200 < 2/3600 → 更严。
        """
        layer = self.put({"messages_per_window": 3, "window_seconds": 7200})
        self.assertEqual(layer.rules["messages_per_window"], 3)
        self.assertEqual(layer.rules["window_seconds"], 7200)
        eff = self.store.effective(persona_id="p", now=self.now)
        self.assertEqual(eff.get("messages_per_window").value, 3)
        self.assertEqual(eff.get("messages_per_window").source_layer, "owner")
        self.assertEqual(eff.get("messages.window_seconds").value, 7200)
        # dict 取值与 get() 一致（无重复 key 覆盖）
        by_key = {i["key"]: i for i in eff.to_dict()["effective"]}
        self.assertEqual(by_key["messages_per_window"]["source_layer"], "owner")
        self.assertEqual(by_key["messages_per_window"]["value"], 3)


# ============================================================
# B2: peer 层进实发
# ============================================================
class TestB2PeerInSend(unittest.TestCase):

    def test_peer_tokens_block_send(self):
        """peer 层 tokens:10；实发估算 20 → 应被 block（而不是只预检才 block）。"""
        svc, transport, store, limits, clock = make_service(
            make_config(rate_limit_messages=100, platform_token_budget=10000)
        )
        limits.put(
            {"layer": "peer", "scope": "peer", "scope_id": "peer_x",
             "rules": {"tokens_per_window": 10}},
            now=clock(),
        )
        # 未注册会话按 B2.1 采信 peer_id（#117 B1：不做成员校验）。
        result = send(svc, "hello world", peer_id="peer_x", token_estimate=20)
        self.assertEqual(result["outcome"], "blocked")
        codes = [r["code"] for r in result["decision"]["reasons"]]
        self.assertIn("tokens.window.exceeded", codes)
        # 真实 transport 不应收到消息
        self.assertEqual(len(transport.sent), 0)

    def test_peer_messages_block_second_send(self):
        """peer 层 messages:1；第 2 条实发被 block，不只是预检 block。"""
        svc, transport, store, limits, clock = make_service(
            make_config(rate_limit_messages=100, platform_token_budget=10000)
        )
        limits.put(
            {"layer": "peer", "scope": "peer", "scope_id": "peer_x",
             "rules": {"messages_per_window": 1}},
            now=clock(),
        )
        # 未注册会话按 B2.1 采信 peer_id（#117 B1：不做成员校验）。
        r1 = send(svc, "first", peer_id="peer_x")
        r2 = send(svc, "second", peer_id="peer_x")
        self.assertEqual(r1["outcome"], "delivered")
        self.assertEqual(r2["outcome"], "blocked")
        self.assertEqual(len(transport.sent), 1)

    def test_send_without_peer_id_ignores_peer_layer(self):
        """不传 peer_id 时 peer 层不生效（原有行为不变）。"""
        svc, transport, store, limits, clock = make_service(
            make_config(rate_limit_messages=100, platform_token_budget=10000)
        )
        limits.put(
            {"layer": "peer", "scope": "peer", "scope_id": "peer_x",
             "rules": {"tokens_per_window": 10}},
            now=clock(),
        )
        result = send(svc, "hello world")  # 无 peer_id
        self.assertEqual(result["outcome"], "delivered")

    def test_evaluate_and_send_agree_on_peer_block(self):
        """预检与实发结论一致：同时传 peer_id。"""
        svc, transport, store, limits, clock = make_service(
            make_config(rate_limit_messages=100, platform_token_budget=10000)
        )
        limits.put(
            {"layer": "peer", "scope": "peer", "scope_id": "peer_x",
             "rules": {"messages_per_window": 1}},
            now=clock(),
        )
        # 未注册会话按 B2.1 采信 peer_id（#117 B1：不做成员校验）。
        # 第 1 条 evaluate → allow
        ev1 = svc.evaluate_policy({
            "persona_id": "p", "peer_id": "peer_x", "content": "x",
        })
        self.assertEqual(ev1["decision"], "allow")
        # 发出去，占满名额
        send(svc, "x", peer_id="peer_x")
        # 第 2 条 evaluate → block
        ev2 = svc.evaluate_policy({
            "persona_id": "p", "peer_id": "peer_x", "content": "y",
        })
        self.assertEqual(ev2["decision"], "block")
        # 实发也 block
        r2 = send(svc, "y", peer_id="peer_x")
        self.assertEqual(r2["outcome"], "blocked")


# ============================================================
# B3: token_estimate:0 不可绕过
# ============================================================
class TestB3TokenEstimateZero(unittest.TestCase):

    def test_zero_estimate_uses_server_estimate(self):
        """estimate:0 + 非空正文 → 服务端估算 > 0 → 不被 token:0 挡住时正常发。"""
        cfg = make_config(platform_token_budget=1000)
        svc, transport, *_ = make_service(cfg)
        r = send(svc, "hello world", token_estimate=0)
        self.assertEqual(r["outcome"], "delivered")
        # estimated_tokens 应 > 0（服务端算了）
        self.assertGreater(r["decision"]["estimated_tokens"], 0)

    def test_tokens_per_window_zero_blocks_non_empty_text(self):
        """tokens_per_window:0 + 非空正文 → 必须 block（token_estimate:0 不能绕过）。"""
        cfg = make_config(rate_limit_messages=100, platform_token_budget=0)
        svc, transport, store, limits, clock = make_service(cfg)
        limits.put(
            {"layer": "owner", "scope": "persona", "scope_id": "p",
             "rules": {"tokens_per_window": 0}},
            now=clock(),
        )
        r = send(svc, "hello", token_estimate=0)
        self.assertEqual(r["outcome"], "blocked")
        codes = [x["code"] for x in r["decision"]["reasons"]]
        self.assertIn("tokens.window.exceeded", codes)
        self.assertEqual(len(transport.sent), 0)

    def test_zero_estimate_cannot_exhaust_budget_for_free(self):
        """带 token_estimate:0 的消息按服务端估算计费，预算用尽后被 block。"""
        cfg = make_config(rate_limit_messages=0, platform_token_budget=5)
        svc, transport, store, limits, clock = make_service(cfg)
        results = []
        for i in range(10):
            r = send(svc, "hi", token_estimate=0)  # 服务端估算 1 token/条
            results.append(r["outcome"])
        delivered = results.count("delivered")
        # 预算 5 → 最多 5 条；客户端给 0 不能让它变成无限
        self.assertEqual(delivered, 5)
        self.assertEqual(results.count("blocked"), 5)
        sent_tokens = sum(e.tokens_out for e in store.all_events()
                          if e.kind == EventKind.SENT)
        self.assertEqual(sent_tokens, 5)

    def test_estimate_tokens_nonzero_for_nonempty(self):
        """estimate_tokens 对非空字符串始终返回 > 0。"""
        self.assertGreater(estimate_tokens("x"), 0)
        self.assertGreater(estimate_tokens("hello world"), 0)
        self.assertGreater(estimate_tokens("中文字符"), 0)
        self.assertEqual(estimate_tokens(""), 0)


# ============================================================
# #99-1: txnId 由 Idempotency-Key 推导
# ============================================================
class TestTxnIdFromIdempotencyKey(unittest.TestCase):

    def test_same_key_same_txn_id(self):
        from botville_gateway.service import GatewayService
        tid1 = GatewayService._txn_id_for("key-abc")
        tid2 = GatewayService._txn_id_for("key-abc")
        self.assertEqual(tid1, tid2)

    def test_different_keys_different_txn_ids(self):
        from botville_gateway.service import GatewayService
        self.assertNotEqual(
            GatewayService._txn_id_for("key-1"),
            GatewayService._txn_id_for("key-2"),
        )

    def test_none_key_is_random(self):
        from botville_gateway.service import GatewayService
        t1 = GatewayService._txn_id_for(None)
        t2 = GatewayService._txn_id_for(None)
        self.assertNotEqual(t1, t2)

    def test_txn_id_is_safe_matrix_chars(self):
        """txnId 只含 [0-9a-f]（hexdigest 字符集），Matrix 路径无需转义。"""
        import re
        from botville_gateway.service import GatewayService
        tid = GatewayService._txn_id_for("idempotency-key-xyz")
        self.assertRegex(tid, r'^[0-9a-f]{1,64}$')

    def test_retry_with_same_idempotency_key_returns_cached_not_resent(self):
        """同一 Idempotency-Key 重试：幂等缓存命中，transport 只收到一次。"""
        svc, transport, *_ = make_service(make_config(rate_limit_messages=100))
        payload = {
            "sender_persona_id": "p",
            "content": {"type": "text", "text": "idempotent"},
        }
        r1 = svc.send_ai_message("conv", payload, idempotency_key="idem-key-1")
        r2 = svc.send_ai_message("conv", payload, idempotency_key="idem-key-1")
        self.assertEqual(r1["outcome"], "delivered")
        self.assertEqual(r2["outcome"], "delivered")
        self.assertEqual(len(transport.sent), 1)  # 只发了一次

    def test_transport_receives_derived_txn_id(self):
        """service 真的把推导出的 txnId 交给了 transport，而不是让实现自己生成。"""
        from botville_gateway.service import GatewayService
        svc, transport, *_ = make_service(make_config(rate_limit_messages=100))
        svc.send_ai_message(
            "conv",
            {"sender_persona_id": "p", "content": {"type": "text", "text": "x"}},
            idempotency_key="stable-key",
        )
        self.assertEqual(
            transport.sent[0].txn_id, GatewayService._txn_id_for("stable-key")
        )

    def test_transport_gets_random_txn_id_without_key(self):
        svc, transport, *_ = make_service(make_config(rate_limit_messages=100))
        svc.send_ai_message(
            "conv",
            {"sender_persona_id": "p", "content": {"type": "text", "text": "x"}},
        )
        self.assertEqual(len(transport.sent[0].txn_id), 32)  # uuid4().hex

    def test_invalid_txn_id_rejected_by_transport(self):
        """传输层不盲信外部 txnId：非法字符直接拒绝（路径安全）。"""
        transport = InMemoryMatrixTransport()
        with self.assertRaises(DeliveryError):
            transport.send_text(
                room_id="r", sender_id="p", text="x", txn_id="bad/id"
            )


# ============================================================
# #99-2: healthy() 超时 + 缓存
# ============================================================
class _ThreadingHSServer(ThreadingHTTPServer):
    """listen backlog 放宽：并发 ensure_user 测试会同时打开多个连接。"""

    request_queue_size = 64
    daemon_threads = True


class FakeHS:
    """最小化假 HS，可编程控制 whoami 的响应与延迟。"""

    def __init__(self):
        outer = self
        self.requests: List[dict] = []
        self.delay: float = 0.0
        self.status: int = 200
        self.response: dict = {"user_id": "@botville-gateway:localhost"}

        class Handler(BaseHTTPRequestHandler):
            def _respond(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                outer.requests.append({"path": self.path})
                if outer.delay:
                    time.sleep(outer.delay)
                import json as _json
                body = _json.dumps(outer.response).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    # 超时测试里客户端先断开：正常现象，不要让假 HS 打栈。
                    pass

            do_GET = do_POST = do_PUT = _respond

            def log_message(self, *a):
                pass

        self.httpd = _ThreadingHSServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        h, p = self.httpd.server_address
        return f"http://{h}:{p}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def whoami_count(self) -> int:
        return len([r for r in self.requests
                    if r["path"].startswith("/_matrix/client/v3/account/whoami")])


def make_transport(hs: FakeHS, **kwargs) -> AppServiceMatrixTransport:
    return AppServiceMatrixTransport(
        homeserver_url=hs.base_url,
        as_token="test-token",
        server_name="localhost",
        **kwargs,
    )


class TestHealthyShortTimeout(unittest.TestCase):
    def setUp(self):
        self.hs = FakeHS()
        self.addCleanup(self.hs.close)

    def test_timeout_returns_false_quickly(self):
        """HS 响应比 health_timeout 慢 → healthy() 返回 False，不挂起。"""
        self.hs.delay = 5.0  # 5s 响应
        transport = make_transport(self.hs, health_timeout=0.3, health_cache_seconds=0)
        t0 = time.monotonic()
        result = transport.healthy()
        elapsed = time.monotonic() - t0
        self.assertFalse(result)
        self.assertLess(elapsed, 2.0, f"healthy() hung for {elapsed:.2f}s")

    def test_cache_prevents_duplicate_requests(self):
        """连续两次 healthy()：第 2 次命中缓存，HS 只收到一个请求。"""
        transport = make_transport(self.hs, health_timeout=2.0, health_cache_seconds=60)
        r1 = transport.healthy()
        r2 = transport.healthy()
        self.assertTrue(r1)
        self.assertTrue(r2)
        self.assertEqual(self.hs.whoami_count(), 1)

    def test_cache_expires_and_rerequests(self):
        """缓存过期后再次请求 HS。"""
        transport = make_transport(self.hs, health_timeout=2.0, health_cache_seconds=0.05)
        transport.healthy()
        count_before = self.hs.whoami_count()
        time.sleep(0.1)
        transport.healthy()
        count_after = self.hs.whoami_count()
        self.assertGreater(count_after, count_before)

    def test_unhealthy_when_hs_returns_error(self):
        """HS 返回 401 → healthy() 返回 False。"""
        self.hs.status = 401
        transport = make_transport(self.hs, health_timeout=2.0, health_cache_seconds=0)
        self.assertFalse(transport.healthy())


# ============================================================
# #99-3: ensure_user 读写加锁（double-check）
# ============================================================
class TestEnsureUserLock(unittest.TestCase):
    def setUp(self):
        self.hs = FakeHS()
        self.addCleanup(self.hs.close)
        # 让 register 接受多次请求（幂等 M_USER_IN_USE 路径）
        self.hs.response = {"user_id": "@ai_demo:localhost"}

    def make_transport(self) -> AppServiceMatrixTransport:
        return AppServiceMatrixTransport(
            homeserver_url=self.hs.base_url,
            as_token="test-token",
            server_name="localhost",
        )

    def test_ensure_user_idempotent_single_thread(self):
        """同一线程两次 ensure_user：只发一次注册请求。"""
        transport = self.make_transport()
        transport.ensure_user("@ai_demo:localhost")
        transport.ensure_user("@ai_demo:localhost")
        regs = [r for r in self.hs.requests if "/register" in r["path"]]
        self.assertEqual(len(regs), 1)

    def test_ensure_user_concurrent_same_user(self):
        """多线程并发首次 ensure_user 同一账号：不崩溃，最终缓存。"""
        transport = self.make_transport()
        errors = []
        barrier = threading.Barrier(8)

        def worker():
            try:
                barrier.wait(timeout=5)
                transport.ensure_user("@ai_demo:localhost")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertFalse(errors, f"errors in concurrent ensure_user: {errors}")
        # 最终缓存里有该 user
        with transport._lock:
            self.assertIn("@ai_demo:localhost", transport._ensured)

    def test_ensure_user_cached_prevents_second_request(self):
        """已缓存的账号不重复注册（锁外路径提前返回）。"""
        transport = self.make_transport()
        # 手动填入缓存，模拟「已确认」
        with transport._lock:
            transport._ensured.add("@ai_demo:localhost")
        transport.ensure_user("@ai_demo:localhost")
        regs = [r for r in self.hs.requests if "/register" in r["path"]]
        self.assertEqual(len(regs), 0)


if __name__ == "__main__":
    unittest.main()
