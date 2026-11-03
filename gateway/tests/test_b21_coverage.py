# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""覆盖率补强测试：补 `__main__`、HTTP 边界、config/store/service 的分支。

主测试集已覆盖核心行为；这里专门跑那些被标记 `# pragma: no cover` 之外
但仍然欠覆盖的分支：入口点、畸形请求体、未知 cursor、env 解析的非法值、
store 的辅助方法等。保持零运行时依赖，只用标准库。
"""

from __future__ import annotations

import datetime
import io
import json
import os
import subprocess
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

from botville_gateway import __main__ as gateway_main  # noqa: E402
from botville_gateway.config import Config, ConfigError, load_config  # noqa: E402
from botville_gateway.http_server import MAX_BODY_BYTES, build_server  # noqa: E402
from botville_gateway.limits import LimitStore, validate_rules  # noqa: E402
from botville_gateway.matrix import DeliveryError, InMemoryMatrixTransport  # noqa: E402
from botville_gateway.policy import PolicyEngine  # noqa: E402
from botville_gateway.service import GatewayService  # noqa: E402
from botville_gateway.store import EventKind, UsageEvent, UsageStore  # noqa: E402
from botville_gateway.timeutil import iso_utc, seconds_until  # noqa: E402


# ---------------------------------------------------------------------------
# 1. __main__.main() 入口
# ---------------------------------------------------------------------------
class TestMainEntryPoint(unittest.TestCase):
    def _run_main(self, env):
        proc = subprocess.run(
            [sys.executable, "-m", "botville_gateway"],
            env={**os.environ, "PYTHONPATH": _SRC, **env},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def test_main_starts_and_shuts_down_on_keyboard_interrupt(self):
        """进程内直调 `main()`，用假 server 让 `serve_forever()` 抛 Ctrl-C。

        比拉子进程稳定：不需要信号竞争，也不会因为启动时序偶发失败。
        """
        import contextlib

        class _FakeServer:
            def serve_forever(self):
                raise KeyboardInterrupt

            def server_close(self):
                self.closed = True

        fake = _FakeServer()
        stdout, stderr = io.StringIO(), io.StringIO()
        old = gateway_main.build_server
        gateway_main.build_server = lambda cfg, transport: fake
        # `main()` 读真实环境变量：显式给 dev 模式，否则 load_config 直接拒绝启动。
        saved = {k: os.environ.get(k) for k in ("GATEWAY_DEV_MODE", "GATEWAY_AUTH_TOKEN")}
        os.environ["GATEWAY_DEV_MODE"] = "true"
        os.environ.pop("GATEWAY_AUTH_TOKEN", None)
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = gateway_main.main()
        finally:
            gateway_main.build_server = old
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.assertEqual(code, 0)
        self.assertTrue(getattr(fake, "closed", False))
        self.assertIn("listening", stdout.getvalue())
        self.assertIn("shutting down", stdout.getvalue())
        # dev 模式（无 token）必须打 WARNING
        self.assertIn("WARNING", stderr.getvalue())

    def test_config_error_exits_2(self):
        code, _out, err = self._run_main({"GATEWAY_DEV_MODE": "true", "GATEWAY_PORT": "not-a-num"})
        self.assertEqual(code, 2)
        self.assertIn(b"config error", err.lower())

    def test_token_required_exits_2(self):
        code, _out, err = self._run_main({})
        self.assertEqual(code, 2)
        self.assertIn(b"config error", err.lower())


# ---------------------------------------------------------------------------
# 2. config.py：env 解析的边界
# ---------------------------------------------------------------------------
class TestConfigEdge(unittest.TestCase):
    def test_env_int_whitespace(self):
        cfg = load_config({"GATEWAY_DEV_MODE": "true", "RATE_LIMIT_MESSAGES": "  12  "})
        self.assertEqual(cfg.rate_limit_messages, 12)

    def test_env_int_bad_raises(self):
        with self.assertRaises(ConfigError):
            load_config({"GATEWAY_DEV_MODE": "true", "RATE_LIMIT_MESSAGES": "abc"})

    def test_env_int_below_min_raises(self):
        with self.assertRaises(ConfigError):
            load_config({"GATEWAY_DEV_MODE": "true",
                         "RATE_LIMIT_WINDOW_SECONDS": "0"})

    def test_env_bool_bad_raises(self):
        with self.assertRaises(ConfigError):
            load_config({"GATEWAY_DEV_MODE": "maybe"})

    def test_env_bool_truthy_values(self):
        for val in ("1", "true", "yes", "on"):
            cfg = load_config({"GATEWAY_DEV_MODE": val})
            self.assertTrue(cfg.dev_mode, val)

    def test_env_bool_falsey_values(self):
        for val in ("0", "false", "no", "off"):
            cfg = load_config({"GATEWAY_DEV_MODE": val, "GATEWAY_AUTH_TOKEN": "t"})
            self.assertFalse(cfg.dev_mode, val)

    def test_silent_start_bad_format_raises(self):
        with self.assertRaises(ConfigError):
            load_config({"GATEWAY_AUTH_TOKEN": "t", "SILENT_HOURS_START": "25:99"})

    def test_rate_limit_negative_raises(self):
        with self.assertRaises(ConfigError):
            Config(dev_mode=True, rate_limit_messages=-1)

    def test_window_too_small_raises(self):
        with self.assertRaises(ConfigError):
            Config(dev_mode=True, rate_limit_window_seconds=0)

    def test_port_zero_rejected_via_env(self):
        with self.assertRaises(ConfigError):
            load_config({"GATEWAY_AUTH_TOKEN": "t", "GATEWAY_PORT": "0"})

    def test_port_via_constructor_zero_ok(self):
        cfg = Config(dev_mode=True, port=0)
        self.assertEqual(cfg.port, 0)

    def test_silent_minute_properties(self):
        cfg = Config(dev_mode=True, silent_hours_start="22:30", silent_hours_end="06:15")
        self.assertEqual(cfg.silent_start_minute, 22 * 60 + 30)
        self.assertEqual(cfg.silent_end_minute, 6 * 60 + 15)

    def test_default_token_budget_constants(self):
        from botville_gateway.config import (
            DEFAULT_TOKEN_BUDGET, DEFAULT_PROACTIVE_PER_DAY,
            DEFAULT_MAX_USAGE_EVENTS, DEFAULT_MAX_IDEMPOTENCY_ENTRIES,
            DEFAULT_IDEMPOTENCY_TTL, DEFAULT_MAX_QUEUE_ENTRIES,
        )
        self.assertGreater(DEFAULT_TOKEN_BUDGET, 0)
        self.assertGreater(DEFAULT_PROACTIVE_PER_DAY, 0)
        self.assertGreater(DEFAULT_MAX_USAGE_EVENTS, 0)
        self.assertGreater(DEFAULT_MAX_IDEMPOTENCY_ENTRIES, 0)
        self.assertGreaterEqual(DEFAULT_IDEMPOTENCY_TTL, 0)
        self.assertGreater(DEFAULT_MAX_QUEUE_ENTRIES, 0)


# ---------------------------------------------------------------------------
# 3. store.py：辅助方法与容量
# ---------------------------------------------------------------------------
class TestStoreHelpers(unittest.TestCase):
    def test_sent_count_since_counts_only_sent(self):
        store = UsageStore(max_events=0)
        base = datetime.datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
        # 左开区间：`since` 本身不算，故从 base-1s 起算
        store.append(UsageEvent.make(EventKind.SENT, persona_id="p", at=base))
        store.append(UsageEvent.make(EventKind.BLOCKED, persona_id="p", at=base))
        self.assertEqual(
            store.sent_count_since(persona_id="p", since=base - timedelta(seconds=1)),
            1,
        )

    def test_all_events_returns_copy(self):
        store = UsageStore(max_events=0)
        store.append(UsageEvent.make(EventKind.SENT, persona_id="p"))
        events = store.all_events()
        events.clear()
        self.assertEqual(len(store), 1)

    def test_clear(self):
        store = UsageStore(max_events=0)
        store.append(UsageEvent.make(EventKind.SENT, persona_id="p"))
        store.clear()
        self.assertEqual(len(store), 0)

    def test_events_in_window_filter_by_conversation(self):
        store = UsageStore(max_events=0)
        base = datetime.datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
        store.append(UsageEvent.make(EventKind.SENT, persona_id="p",
                                    conversation_id="a", at=base))
        store.append(UsageEvent.make(EventKind.SENT, persona_id="p",
                                    conversation_id="b", at=base))
        self.assertEqual(len(store.events_in_window(conversation_id="a")), 1)

    def test_events_in_window_kind_filter_none(self):
        store = UsageStore(max_events=0)
        store.append(UsageEvent.make(EventKind.SENT, persona_id="p"))
        self.assertEqual(len(store.events_in_window(persona_id="p")), 1)
        self.assertEqual(len(store.events_in_window(persona_id="p",
                                                    kind=EventKind.SENT)), 1)

    def test_eviction_keeps_most_recent(self):
        store = UsageStore(max_events=5)
        base = datetime.datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
        for i in range(20):
            store.append(UsageEvent.make(EventKind.SENT, persona_id=f"p{i}",
                                         at=base + timedelta(seconds=i)))
        events = store.all_events()
        self.assertLessEqual(len(events), 5)
        # 最旧的被淘汰，最新的事件仍在
        ids = {e.persona_id for e in events}
        self.assertIn("p19", ids)
        self.assertNotIn("p0", ids)

    def test_usage_event_tokens_total(self):
        event = UsageEvent.make(EventKind.SENT, persona_id="p",
                                tokens_in=10, tokens_out=20)
        self.assertEqual(event.tokens_total, 30)

    def test_usage_event_make_default_now(self):
        event = UsageEvent.make(EventKind.SENT, persona_id="p")
        self.assertIsNotNone(event.recorded_at)


# ---------------------------------------------------------------------------
# 4. HTTP 边界：畸形请求 / 错误码 / 405 / 404
# ---------------------------------------------------------------------------
def _raw_request(url, method, data=None, headers=None, timeout=5):
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


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


def _cfg(**overrides):
    base = dict(host="127.0.0.1", port=0, dev_mode=True, rate_limit_messages=60,
                silent_hours_enabled=False, auth_token=None)
    base.update(overrides)
    return Config(**base)


class TestHttpEdgeCases(unittest.TestCase):
    def test_health_head_allowed(self):
        with _Fixture(_cfg()) as fixture:
            status, raw = _raw_request(f"{fixture.base}/health", "HEAD")
        self.assertEqual(status, 200)

    def test_patch_not_allowed(self):
        with _Fixture(_cfg()) as fixture:
            status, body = _raw_request(f"{fixture.base}/v1/limits", "PATCH", b"{}")
        self.assertEqual(status, 405)
        self.assertEqual(json.loads(body)["code"], "request.invalid")

    def test_delete_not_allowed(self):
        with _Fixture(_cfg()) as fixture:
            status, _ = _raw_request(f"{fixture.base}/v1/usage", "DELETE")
        self.assertEqual(status, 405)

    def test_options_not_allowed(self):
        with _Fixture(_cfg()) as fixture:
            status, _ = _raw_request(f"{fixture.base}/v1/limits", "OPTIONS")
        self.assertEqual(status, 405)

    def test_trace_not_allowed(self):
        with _Fixture(_cfg()) as fixture:
            status, _ = _raw_request(f"{fixture.base}/v1/limits", "TRACE")
        self.assertEqual(status, 405)

    def test_chunked_transfer_encoding_rejected(self):
        with _Fixture(_cfg()) as fixture:
            req = urllib.request.Request(
                f"{fixture.base}/v1/conversations/c/messages", data=b"{}", method="POST"
            )
            req.add_header("Content-Type", "application/json")
            req.add_header("Transfer-Encoding", "chunked")
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    status, body = resp.status, resp.read()
            except urllib.error.HTTPError as exc:
                status, body = exc.code, exc.read()
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["code"], "request.invalid")

    def test_negative_content_length_rejected(self):
        with _Fixture(_cfg()) as fixture:
            req = urllib.request.Request(
                f"{fixture.base}/v1/conversations/c/messages", data=b"{}", method="POST"
            )
            req.add_header("Content-Type", "application/json")
            req.add_header("Content-Length", "-5")
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    status, body = resp.status, resp.read()
            except urllib.error.HTTPError as exc:
                status, body = exc.code, exc.read()
        self.assertEqual(status, 400)

    def test_non_integer_content_length_rejected(self):
        with _Fixture(_cfg()) as fixture:
            req = urllib.request.Request(
                f"{fixture.base}/v1/conversations/c/messages", data=b"{}", method="POST"
            )
            req.add_header("Content-Type", "application/json")
            req.add_header("Content-Length", "abc")
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    status, body = resp.status, resp.read()
            except urllib.error.HTTPError as exc:
                status, body = exc.code, exc.read()
        self.assertEqual(status, 400)

    def test_invalid_json_rejected(self):
        with _Fixture(_cfg()) as fixture:
            req = urllib.request.Request(
                f"{fixture.base}/v1/conversations/c/messages", data=b"{bad", method="POST"
            )
            req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    status, body = resp.status, resp.read()
            except urllib.error.HTTPError as exc:
                status, body = exc.code, exc.read()
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["code"], "request.invalid")

    def test_json_array_rejected(self):
        with _Fixture(_cfg()) as fixture:
            req = urllib.request.Request(
                f"{fixture.base}/v1/conversations/c/messages", data=b"[1,2]", method="POST"
            )
            req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    status, body = resp.status, resp.read()
            except urllib.error.HTTPError as exc:
                status, body = exc.code, exc.read()
        self.assertEqual(status, 400)

    def test_text_content_type_rejected(self):
        with _Fixture(_cfg()) as fixture:
            status, body = _raw_request(
                f"{fixture.base}/v1/conversations/c/messages", "POST", b"{}",
                headers={"Content-Type": "text/plain"}
            )
        self.assertEqual(status, 400)

    def test_empty_body_returns_default_object(self):
        """length=0 → `{}`，后续校验会发现缺字段。"""
        with _Fixture(_cfg()) as fixture:
            req = urllib.request.Request(
                f"{fixture.base}/v1/conversations/c/messages", data=b"", method="POST"
            )
            req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    status, body = resp.status, resp.read()
            except urllib.error.HTTPError as exc:
                status, body = exc.code, exc.read()
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["code"], "request.invalid")

    def test_x_request_id_pass_through(self):
        with _Fixture(_cfg()) as fixture:
            req = urllib.request.Request(f"{fixture.base}/health")
            req.add_header("X-Request-Id", "custom-id-1234")
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.headers["X-Request-Id"], "custom-id-1234")

    def test_invalid_x_request_id_replaced(self):
        with _Fixture(_cfg()) as fixture:
            req = urllib.request.Request(f"{fixture.base}/health")
            req.add_header("X-Request-Id", "bad value with spaces")
            with urllib.request.urlopen(req, timeout=5) as resp:
                rid = resp.headers["X-Request-Id"]
                self.assertTrue(rid.startswith("req_"))

    def test_oversized_idempotency_key_over_http(self):
        with _Fixture(_cfg()) as fixture:
            req = urllib.request.Request(
                f"{fixture.base}/v1/conversations/c/messages",
                data=json.dumps({
                    "sender_persona_id": "p",
                    "content": {"type": "text", "text": "x"},
                }).encode(),
                method="POST",
            )
            req.add_header("Content-Type", "application/json")
            req.add_header("Idempotency-Key", "k" * 200)
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    status, body = resp.status, resp.read()
            except urllib.error.HTTPError as exc:
                status, body = exc.code, exc.read()
        self.assertEqual(status, 400)

    def test_policy_evaluate_unknown_field_rejected_over_http(self):
        with _Fixture(_cfg()) as fixture:
            req = urllib.request.Request(
                f"{fixture.base}/v1/policy/evaluate",
                data=json.dumps({"persona_id": "p", "bogus": 1}).encode(),
                method="POST",
            )
            req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    status, body = resp.status, resp.read()
            except urllib.error.HTTPError as exc:
                status, body = exc.code, exc.read()
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["code"], "request.invalid")

    def test_policy_evaluate_bad_kind_over_http(self):
        with _Fixture(_cfg()) as fixture:
            req = urllib.request.Request(
                f"{fixture.base}/v1/policy/evaluate",
                data=json.dumps({"persona_id": "p", "kind": "bogus"}).encode(),
                method="POST",
            )
            req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    status, body = resp.status, resp.read()
            except urllib.error.HTTPError as exc:
                status, body = exc.code, exc.read()
        self.assertEqual(status, 400)

    def test_limits_bad_persona_type_over_http(self):
        with _Fixture(_cfg()) as fixture:
            status, body = _raw_request(
                f"{fixture.base}/v1/limits?persona_id=", "GET"
            )
        # 空 query value → 解析为 None → 不报错
        self.assertIn(status, (200, 400))

    def test_put_limits_with_validation_error_over_http(self):
        with _Fixture(_cfg()) as fixture:
            req = urllib.request.Request(
                f"{fixture.base}/v1/limits",
                data=json.dumps({"layer": "owner", "scope": "persona",
                                 "scope_id": "p", "rules": "not-an-object"}).encode(),
                method="PUT",
            )
            req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    status, body = resp.status, resp.read()
            except urllib.error.HTTPError as exc:
                status, body = exc.code, exc.read()
        self.assertEqual(status, 400)

    def test_put_limits_missing_layer_over_http(self):
        with _Fixture(_cfg()) as fixture:
            req = urllib.request.Request(
                f"{fixture.base}/v1/limits",
                data=json.dumps({"scope": "persona", "scope_id": "p",
                                 "rules": {}}).encode(),
                method="PUT",
            )
            req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    status, body = resp.status, resp.read()
            except urllib.error.HTTPError as exc:
                status, body = exc.code, exc.read()
        self.assertEqual(status, 400)

    def test_usage_bad_from_date_over_http(self):
        with _Fixture(_cfg()) as fixture:
            status, body = _raw_request(
                f"{fixture.base}/v1/usage?from=not-a-date", "GET"
            )
        self.assertEqual(status, 400)

    def test_usage_events_bad_kind_over_http(self):
        with _Fixture(_cfg()) as fixture:
            status, body = _raw_request(
                f"{fixture.base}/v1/usage/events?kind=bogus", "GET"
            )
        self.assertEqual(status, 400)

    def test_usage_events_bad_limit_over_http(self):
        with _Fixture(_cfg()) as fixture:
            status, _ = _raw_request(
                f"{fixture.base}/v1/usage/events?limit=999", "GET"
            )
        self.assertEqual(status, 400)

    def test_usage_events_bad_cursor_over_http(self):
        with _Fixture(_cfg()) as fixture:
            status, _ = _raw_request(
                f"{fixture.base}/v1/usage/events?cursor=bogus", "GET"
            )
        self.assertEqual(status, 400)

    def test_transport_failure_over_http(self):
        with _Fixture(_cfg()) as fixture:
            fixture.server.transport.fail = True
            req = urllib.request.Request(
                f"{fixture.base}/v1/conversations/c/messages",
                data=json.dumps({
                    "sender_persona_id": "p",
                    "content": {"type": "text", "text": "x"},
                }).encode(),
                method="POST",
            )
            req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    status, body = resp.status, resp.read()
            except urllib.error.HTTPError as exc:
                status, body = exc.code, exc.read()
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body)["code"], "internal.error")

    def test_send_message_validation_errors_over_http(self):
        cases = [
            {"content": {"type": "text", "text": "x"}},  # 缺 sender
            {"sender_persona_id": "p", "content": {"type": "text"}},  # 缺 text
            {"sender_persona_id": "p", "content": {"type": "card", "text": "x"}},  # bad type
            {"sender_persona_id": "p", "content": {"type": "text", "text": "x"},
             "reply_to": 123},  # bad reply_to
            {"sender_persona_id": "p", "content": {"type": "text", "text": "x"},
             "allow_deferred": "yes"},  # bad bool
            {"sender_persona_id": "p", "content": {"type": "text", "text": "x"},
             "token_estimate": "five"},  # bad token
            {"sender_persona_id": "p", "content": {"type": "text", "text": "x"},
             "metadata": "notobj"},  # bad metadata
        ]
        with _Fixture(_cfg()) as fixture:
            for payload in cases:
                req = urllib.request.Request(
                    f"{fixture.base}/v1/conversations/c/messages",
                    data=json.dumps(payload).encode(), method="POST",
                )
                req.add_header("Content-Type", "application/json")
                try:
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        status = resp.status
                except urllib.error.HTTPError as exc:
                    status = exc.code
                self.assertEqual(status, 400, payload)

    def test_mentions_bad_type_over_http(self):
        with _Fixture(_cfg()) as fixture:
            req = urllib.request.Request(
                f"{fixture.base}/v1/conversations/c/messages",
                data=json.dumps({
                    "sender_persona_id": "p",
                    "content": {"type": "text", "text": "x", "mentions": [1, 2]},
                }).encode(),
                method="POST",
            )
            req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    status = resp.status
            except urllib.error.HTTPError as exc:
                status = exc.code
        self.assertEqual(status, 400)

    def test_oversized_text_over_http(self):
        with _Fixture(_cfg(max_message_chars=10)) as fixture:
            req = urllib.request.Request(
                f"{fixture.base}/v1/conversations/c/messages",
                data=json.dumps({
                    "sender_persona_id": "p",
                    "content": {"type": "text", "text": "x" * 20},
                }).encode(),
                method="POST",
            )
            req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    status = resp.status
            except urllib.error.HTTPError as exc:
                status = exc.code
        self.assertEqual(status, 400)

    def test_policy_evaluate_bad_content_type_over_http(self):
        with _Fixture(_cfg()) as fixture:
            req = urllib.request.Request(
                f"{fixture.base}/v1/policy/evaluate",
                data=json.dumps({"persona_id": "p"}).encode(), method="POST",
            )
            req.add_header("Content-Type", "text/plain")
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    status = resp.status
            except urllib.error.HTTPError as exc:
                status = exc.code
        self.assertEqual(status, 400)


# ---------------------------------------------------------------------------
# 5. matrix.py：DeliveryError / healthy / messages_for
# ---------------------------------------------------------------------------
class TestMatrixHelpers(unittest.TestCase):
    def test_delivery_error_str(self):
        err = DeliveryError("matrix.unavailable", "boom")
        self.assertIn("matrix.unavailable", str(err))

    def test_healthy_flag(self):
        transport = InMemoryMatrixTransport()
        self.assertTrue(transport.healthy())
        transport.fail = True
        self.assertFalse(transport.healthy())

    def test_messages_for_room(self):
        transport = InMemoryMatrixTransport()
        transport.send_text(room_id="r1", sender_id="p", text="a")
        transport.send_text(room_id="r2", sender_id="p", text="b")
        self.assertEqual(len(transport.messages_for("r1")), 1)
        self.assertEqual(transport.messages_for("r1")[0].text, "a")

    def test_clear(self):
        transport = InMemoryMatrixTransport()
        transport.send_text(room_id="r", sender_id="p", text="x")
        transport.clear()
        self.assertEqual(len(transport.sent), 0)

    def test_send_with_reply_to(self):
        transport = InMemoryMatrixTransport()
        event = transport.send_text(room_id="r", sender_id="p", text="x",
                                     reply_to="msg_prev")
        self.assertEqual(event.reply_to, "msg_prev")


# ---------------------------------------------------------------------------
# 6. timeutil.py：seconds_until 边界
# ---------------------------------------------------------------------------
class TestTimeutil(unittest.TestCase):
    def test_seconds_until_negative_clamps_to_zero(self):
        now = datetime.datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
        past = now - timedelta(seconds=10)
        self.assertEqual(seconds_until(past, now=now), 0)

    def test_seconds_until_rounds_up(self):
        now = datetime.datetime(2026, 11, 3, 12, 0, 0, tzinfo=timezone.utc)
        target = now + timedelta(seconds=1, milliseconds=1)
        self.assertEqual(seconds_until(target, now=now), 2)

    def test_iso_utc_non_utc_input(self):
        from zoneinfo import ZoneInfo
        sh = datetime.datetime(2026, 11, 3, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(iso_utc(sh), "2026-11-03T12:00:00Z")


# ---------------------------------------------------------------------------
# 7. validate_rules 剩余分支
# ---------------------------------------------------------------------------
class TestValidateRulesBranches(unittest.TestCase):
    def test_anti_spam_non_object_rejected(self):
        from botville_gateway.errors import ValidationError
        with self.assertRaises(ValidationError):
            validate_rules({"anti_spam": "bad"})

    def test_ai_loop_non_object_rejected(self):
        from botville_gateway.errors import ValidationError
        with self.assertRaises(ValidationError):
            validate_rules({"ai_loop": "bad"})

    def test_anti_spam_unknown_field_rejected(self):
        from botville_gateway.errors import ValidationError
        with self.assertRaises(ValidationError):
            validate_rules({"anti_spam": {"bogus": 1}})

    def test_ai_loop_unknown_field_rejected(self):
        from botville_gateway.errors import ValidationError
        with self.assertRaises(ValidationError):
            validate_rules({"ai_loop": {"bogus": 1}})

    def test_anti_spam_out_of_range(self):
        from botville_gateway.errors import LimitError
        with self.assertRaises(LimitError):
            validate_rules({"anti_spam": {"max_consecutive_ai_messages": 0}})

    def test_ai_loop_out_of_range(self):
        from botville_gateway.errors import LimitError
        with self.assertRaises(LimitError):
            validate_rules({"ai_loop": {"max_ai_messages_per_hour": -1}})

    def test_silent_hours_non_object_rejected(self):
        from botville_gateway.errors import ValidationError
        with self.assertRaises(ValidationError):
            validate_rules({"silent_hours": "bad"})

    def test_silent_hours_missing_field_rejected(self):
        from botville_gateway.errors import ValidationError
        with self.assertRaises(ValidationError):
            validate_rules({"silent_hours": {"enabled": True}})

    def test_silent_hours_bad_type_rejected(self):
        from botville_gateway.errors import ValidationError
        with self.assertRaises(ValidationError):
            validate_rules({"silent_hours": {
                "enabled": True, "start": 123, "end": "07:00", "timezone": "UTC"}})

    def test_silent_hours_bad_enabled_rejected(self):
        from botville_gateway.errors import ValidationError
        with self.assertRaises(ValidationError):
            validate_rules({"silent_hours": {
                "enabled": "yes", "start": "23:00", "end": "07:00", "timezone": "UTC"}})

    def test_unknown_top_field_rejected(self):
        from botville_gateway.errors import ValidationError
        with self.assertRaises(ValidationError):
            validate_rules({"max_tokens": 100})

    def test_rules_non_object_rejected(self):
        from botville_gateway.errors import ValidationError
        with self.assertRaises(ValidationError):
            validate_rules("bad")


# ---------------------------------------------------------------------------
# 8. LimitStore 剩余分支
# ---------------------------------------------------------------------------
class TestLimitStoreBranches(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(dev_mode=True, rate_limit_messages=5,
                          platform_token_budget=100, silent_hours_enabled=False)
        self.store = LimitStore(self.cfg)
        self.now = datetime.datetime(2026, 11, 3, 4, 0, tzinfo=timezone.utc)

    def test_get_layer_for_platform(self):
        self.assertIs(self.store.get_layer("platform", "peer", "x"),
                      self.store.platform)

    def test_get_layer_missing_returns_none(self):
        self.assertIsNone(self.store.get_layer("owner", "persona", "nope"))

    def test_layers_for_with_all_three(self):
        self.store.put(
            {"layer": "owner", "scope": "persona", "scope_id": "p",
             "rules": {"messages_per_window": 3}}, now=self.now)
        self.store.put(
            {"layer": "owner", "scope": "conversation", "scope_id": "c",
             "rules": {"messages_per_window": 2}}, now=self.now)
        self.store.put(
            {"layer": "peer", "scope": "peer", "scope_id": "peer_x",
             "rules": {"messages_per_window": 1}}, now=self.now)
        layers = self.store.layers_for(persona_id="p", conversation_id="c",
                                       peer_id="peer_x")
        self.assertEqual(len(layers), 4)  # platform + 3

    def test_bad_layer_value_rejected(self):
        with self.assertRaises(Exception):
            self.store.put(
                {"layer": "unknown", "scope": "persona", "scope_id": "p",
                 "rules": {}}, now=self.now)

    def test_bad_scope_value_rejected(self):
        with self.assertRaises(Exception):
            self.store.put(
                {"layer": "owner", "scope": "bogus", "scope_id": "p",
                 "rules": {}}, now=self.now)

    def test_empty_scope_id_rejected(self):
        with self.assertRaises(Exception):
            self.store.put(
                {"layer": "owner", "scope": "persona", "scope_id": "",
                 "rules": {}}, now=self.now)

    def test_unknown_field_in_payload_rejected(self):
        with self.assertRaises(Exception):
            self.store.put(
                {"layer": "owner", "scope": "persona", "scope_id": "p",
                 "rules": {}, "extra": 1}, now=self.now)

    def test_effective_no_scope_falls_back_to_platform(self):
        eff = self.store.effective(now=self.now)
        self.assertEqual(eff.scope, "peer")
        self.assertEqual(eff.scope_id, "platform_default")


# ---------------------------------------------------------------------------
# 9. PolicyEngine 无 LimitStore 回退路径（测试 Config-only 路径）
# ---------------------------------------------------------------------------
class TestPolicyWithoutLimitStore(unittest.TestCase):
    def test_config_only_rate_limit(self):
        cfg = Config(dev_mode=True, rate_limit_messages=2, silent_hours_enabled=False)
        clock = lambda: datetime.datetime(2026, 11, 3, 4, 0, tzinfo=timezone.utc)
        store = UsageStore(max_events=0)
        engine = PolicyEngine(cfg, store, limit_store=None, now_fn=clock)
        for _ in range(2):
            store.append(UsageEvent.make(EventKind.SENT, persona_id="p", at=clock()))
        decision = engine.evaluate(persona_id="p", now=clock())
        self.assertEqual(decision.decision, "block")
        check = {c.key: c for c in decision.checks}["messages.window"]
        self.assertEqual(check.layer, "platform")

    def test_config_only_silent_hours(self):
        cfg = Config(dev_mode=True, silent_hours_enabled=True,
                     silent_hours_start="23:00", silent_hours_end="07:30")
        clock = lambda: datetime.datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)
        store = UsageStore(max_events=0)
        engine = PolicyEngine(cfg, store, limit_store=None, now_fn=clock)
        decision = engine.evaluate(persona_id="p", now=clock())
        self.assertEqual(decision.decision, "defer")

    def test_config_only_zero_messages_means_disabled(self):
        cfg = Config(dev_mode=True, rate_limit_messages=0, silent_hours_enabled=False)
        clock = lambda: datetime.datetime(2026, 11, 3, 4, 0, tzinfo=timezone.utc)
        store = UsageStore(max_events=0)
        engine = PolicyEngine(cfg, store, limit_store=None, now_fn=clock)
        decision = engine.evaluate(persona_id="p", now=clock())
        check = {c.key: c for c in decision.checks}["messages.window"]
        self.assertTrue(check.allowed)


if __name__ == "__main__":
    unittest.main()
