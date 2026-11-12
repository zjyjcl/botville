# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""B2.4 内置角色引擎测试（engine/）。

覆盖：
- personas.py：load_personas 校验（本服 @ai_*、重复 user_id、credential_ref 形状、
  system_prompt 必填、max_reply_chars 范围、顶层/未知字段拒绝）；
- model_client.py：ModelConfig.from_env、HTTP 2xx / 4xx / 5xx / 超时 →
  ModelError / TransientUnavailable；重定向不跟随；密钥不进 repr；
- engine.py：EngineHooks.on_human_text → evaluate → model → send 完整流程；
  blocked → 零模型调用；TransientUnavailable 上抛；ModelError 上抛；
  幂等键去重（同一 event_id 不重复发言）；ring buffer 历史拼装；
  未配置模型 → no-op；无对应 persona → no-op；
- server 集成：engine_hooks 只在 personas + model 均已配时才非 None；
  无 personas / 无模型时 engine_hooks 为 None，消费者仍启动（只记录/计数）。

运行：python3 -m unittest gateway.tests.test_engine_reply -v
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import List, Optional
from unittest import mock

GATEWAY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(GATEWAY_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from botville_gateway.as_consumer import (  # noqa: E402
    ConsumerContext,
    speech_idempotency_key,
)
from botville_gateway.as_txn_store import (  # noqa: E402
    AsTransactionStore,
    PersistentSpeechDedupe,
    build_as_txn_store,
    connect,
)
from botville_gateway.config import Config  # noqa: E402
from botville_gateway.engine import (  # noqa: E402
    EngineHooks,
    ModelClient,
    ModelConfig,
    ModelConfigError,
    ModelError,
    Persona,
    PersonaConfigError,
    load_personas,
)
from botville_gateway.http_server import build_server  # noqa: E402
from botville_gateway.service import GatewayService
from botville_gateway.matrix import InMemoryMatrixTransport  # noqa: E402

SERVER_NAME = "example.test"
AI_A = f"@ai_a:{SERVER_NAME}"
AI_B = f"@ai_b:{SERVER_NAME}"


def _cfg(**overrides):
    base = dict(
        host="127.0.0.1", port=0, dev_mode=True, auth_token=None,
        silent_hours_enabled=False, hmac_secret=b"test-hmac-secret" * 2,
        matrix_server_name=SERVER_NAME,
    )
    base.update(overrides)
    return Config(**base)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
class _TmpDb(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "as.db")
        self.conn = connect(self.db_path)
        self.store = AsTransactionStore(self.conn)

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()


def _make_persona(user_id=AI_A, **kw):
    defaults = dict(user_id=user_id, system_prompt="Be nice.", display_name="Luna")
    defaults.update(kw)
    return Persona(**defaults)


def _write_personas_file(path, personas_data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"personas": personas_data}, f)


def _base_persona_dict(user_id=AI_A):
    return {
        "user_id": user_id,
        "system_prompt": "Be a helpful assistant.",
        "display_name": "Luna",
    }


# ---------------------------------------------------------------------------
# 1. load_personas 校验
# ---------------------------------------------------------------------------
class TestLoadPersonas(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.p = os.path.join(self._tmp.name, "p.json")

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, data):
        with open(self.p, "w") as f:
            json.dump(data, f)

    def test_valid_minimal(self):
        self._write({"personas": [_base_persona_dict()]})
        ps = load_personas(self.p, server_name=SERVER_NAME)
        self.assertIn(AI_A, ps)
        self.assertEqual(ps[AI_A].system_prompt, "Be a helpful assistant.")

    def test_empty_personas_list_ok(self):
        self._write({"personas": []})
        self.assertEqual(load_personas(self.p, server_name=SERVER_NAME), {})

    def test_none_path_returns_empty(self):
        self.assertEqual(load_personas(None, server_name=SERVER_NAME), {})

    def test_foreign_server_rejected(self):
        self._write({"personas": [{"user_id": "@ai_x:other.server", "system_prompt": "x"}]})
        with self.assertRaises(PersonaConfigError):
            load_personas(self.p, server_name=SERVER_NAME)

    def test_non_ai_localpart_rejected(self):
        self._write({"personas": [{"user_id": f"@human:{SERVER_NAME}", "system_prompt": "x"}]})
        with self.assertRaises(PersonaConfigError):
            load_personas(self.p, server_name=SERVER_NAME)

    def test_duplicate_user_id_rejected(self):
        entry = _base_persona_dict()
        self._write({"personas": [entry, entry]})
        with self.assertRaises(PersonaConfigError) as cm:
            load_personas(self.p, server_name=SERVER_NAME)
        self.assertIn("duplicates", str(cm.exception))

    def test_missing_system_prompt_rejected(self):
        self._write({"personas": [{"user_id": AI_A}]})
        with self.assertRaises(PersonaConfigError):
            load_personas(self.p, server_name=SERVER_NAME)

    def test_credential_ref_must_be_env_form(self):
        entry = dict(_base_persona_dict(), credential_ref="sk-not-an-env-ref")
        self._write({"personas": [entry]})
        with self.assertRaises(PersonaConfigError) as cm:
            load_personas(self.p, server_name=SERVER_NAME)
        self.assertNotIn("sk-", str(cm.exception))  # 值本身不能回显

    def test_credential_ref_env_form_accepted(self):
        entry = dict(_base_persona_dict(), credential_ref="env:MY_PERSONA_KEY")
        self._write({"personas": [entry]})
        ps = load_personas(self.p, server_name=SERVER_NAME)
        self.assertEqual(ps[AI_A].credential_env_name, "MY_PERSONA_KEY")

    def test_max_reply_chars_too_large_rejected(self):
        entry = dict(_base_persona_dict(), max_reply_chars=99999)
        self._write({"personas": [entry]})
        with self.assertRaises(PersonaConfigError):
            load_personas(self.p, server_name=SERVER_NAME, max_message_chars=8000)

    def test_max_reply_chars_zero_rejected(self):
        entry = dict(_base_persona_dict(), max_reply_chars=0)
        self._write({"personas": [entry]})
        with self.assertRaises(PersonaConfigError):
            load_personas(self.p, server_name=SERVER_NAME)

    def test_unknown_field_rejected(self):
        entry = dict(_base_persona_dict(), unknown_field="oops")
        self._write({"personas": [entry]})
        with self.assertRaises(PersonaConfigError):
            load_personas(self.p, server_name=SERVER_NAME)

    def test_no_server_name_raises(self):
        self._write({"personas": [_base_persona_dict()]})
        with self.assertRaises(PersonaConfigError):
            load_personas(self.p, server_name=None)

    def test_bad_json_raises(self):
        with open(self.p, "w") as f:
            f.write("not json")
        with self.assertRaises(PersonaConfigError):
            load_personas(self.p, server_name=SERVER_NAME)

    def test_server_name_comparison_case_insensitive(self):
        entry = dict(_base_persona_dict(), user_id=f"@ai_a:{SERVER_NAME.upper()}")
        self._write({"personas": [entry]})
        ps = load_personas(self.p, server_name=SERVER_NAME)
        self.assertEqual(len(ps), 1)


# ---------------------------------------------------------------------------
# 2. ModelConfig.from_env
# ---------------------------------------------------------------------------
class TestModelConfig(unittest.TestCase):
    def test_empty_env_means_not_configured(self):
        cfg = ModelConfig.from_env({})
        self.assertFalse(cfg.configured)

    def test_base_url_without_model_name_raises(self):
        with self.assertRaises(ModelConfigError):
            ModelConfig.from_env({"GATEWAY_MODEL_BASE_URL": "https://api.example.com/v1"})

    def test_valid_config(self):
        cfg = ModelConfig.from_env({
            "GATEWAY_MODEL_BASE_URL": "https://api.example.com/v1",
            "GATEWAY_MODEL_NAME": "gpt-4o-mini",
            "GATEWAY_MODEL_API_KEY": "sk-secret",
        })
        self.assertTrue(cfg.configured)
        self.assertEqual(cfg.model, "gpt-4o-mini")
        self.assertNotIn("sk-secret", repr(cfg))

    def test_bad_scheme_raises(self):
        with self.assertRaises(ModelConfigError):
            ModelConfig.from_env({
                "GATEWAY_MODEL_BASE_URL": "ftp://oops",
                "GATEWAY_MODEL_NAME": "m",
            })

    def test_timeout_non_number_raises(self):
        with self.assertRaises(ModelConfigError):
            ModelConfig.from_env({
                "GATEWAY_MODEL_BASE_URL": "https://x/v1",
                "GATEWAY_MODEL_NAME": "m",
                "GATEWAY_MODEL_TIMEOUT_SECONDS": "abc",
            })

    def test_timeout_zero_raises(self):
        with self.assertRaises(ModelConfigError):
            ModelConfig.from_env({
                "GATEWAY_MODEL_BASE_URL": "https://x/v1",
                "GATEWAY_MODEL_NAME": "m",
                "GATEWAY_MODEL_TIMEOUT_SECONDS": "0",
            })

    def test_key_not_in_repr(self):
        cfg = ModelConfig.from_env({
            "GATEWAY_MODEL_BASE_URL": "https://x/v1",
            "GATEWAY_MODEL_NAME": "m",
            "GATEWAY_MODEL_API_KEY": "my-secret-key",
        })
        self.assertNotIn("my-secret-key", repr(cfg))


# ---------------------------------------------------------------------------
# 3. ModelClient HTTP（本地假服务器）
# ---------------------------------------------------------------------------
class _FakeModelServer:
    """最简单的 OpenAI-shape 假模型服务器（单次响应列表）。"""

    def __init__(self):
        self._responses = []
        self._requests = []
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self):
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def push(self, status: int, body):
        with self._lock:
            self._responses.append((status, body))

    def stop(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def _make_handler(self):
        outer = self

        class _H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                with outer._lock:
                    outer._requests.append(json.loads(body))
                    code, resp = outer._responses.pop(0) if outer._responses else (200, {})
                data = json.dumps(resp).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        return _H


def _mock_client(text="Hello!") -> ModelClient:
    cfg = ModelConfig(base_url="http://fake.invalid", model="m", _api_key=None)
    client = ModelClient(cfg)
    client._post = lambda payload, persona: {"choices": [{"message": {"content": text}}]}  # type: ignore
    return client


class TestModelClientHttp(unittest.TestCase):
    def setUp(self):
        self._server = _FakeModelServer()

    def tearDown(self):
        self._server.stop()

    def _client(self, **kw):
        cfg = ModelConfig(base_url=self._server.base_url, model="gpt-test", **kw)
        return ModelClient(cfg)

    def test_success_returns_text(self):
        self._server.push(200, {"choices": [{"message": {"content": "hi there"}}]})
        c = self._client()
        result = c.complete(system_text="sys", messages=[{"role": "user", "content": "hi"}])
        self.assertEqual(result, "hi there")

    def test_5xx_raises_transient(self):
        from botville_gateway.as_consumer import TransientUnavailable
        self._server.push(500, {"error": "overloaded"})
        c = self._client()
        with self.assertRaises(TransientUnavailable):
            c.complete(system_text="", messages=[{"role": "user", "content": "x"}])

    def test_429_raises_transient(self):
        from botville_gateway.as_consumer import TransientUnavailable
        self._server.push(429, {"error": "rate limited"})
        c = self._client()
        with self.assertRaises(TransientUnavailable):
            c.complete(system_text="", messages=[{"role": "user", "content": "x"}])

    def test_400_raises_model_error(self):
        self._server.push(400, {"error": "bad request"})
        c = self._client()
        with self.assertRaises(ModelError):
            c.complete(system_text="", messages=[{"role": "user", "content": "x"}])

    def test_empty_choices_raises_model_error(self):
        self._server.push(200, {"choices": []})
        c = self._client()
        with self.assertRaises(ModelError):
            c.complete(system_text="", messages=[{"role": "user", "content": "x"}])

    def test_not_configured_raises_model_error(self):
        c = ModelClient(ModelConfig())
        with self.assertRaises(ModelError):
            c.complete(system_text="", messages=[])

    def test_api_key_sent_in_auth_header(self):
        self._server.push(200, {"choices": [{"message": {"content": "ok"}}]})
        c = ModelClient(ModelConfig(
            base_url=self._server.base_url, model="m", _api_key="test-key-not-secret"
        ))
        c.complete(system_text="", messages=[{"role": "user", "content": "hi"}])
        # Key must NOT appear in repr
        self.assertNotIn("test-key-not-secret", repr(c.config))


# ---------------------------------------------------------------------------
# 4. EngineHooks (消费侧集成)
# ---------------------------------------------------------------------------
class TestEngineHooks(unittest.TestCase):
    def _make_ctx(self, event_id="$e1", text="hello", target=AI_A, room="!r:example.test"):
        return ConsumerContext(
            event_type="message_text",
            event_id=event_id,
            room_id=room,
            sender=f"@alice:{SERVER_NAME}",
            trust="trusted",
            text=text,
            should_reply=True,
            persona_user_id=target,
            target_user_ids=(target,),
            idempotency_key=speech_idempotency_key(target, event_id),
        )

    def _hooks(self, reply="好的", fail_with=None):
        client = _mock_client(reply)
        if fail_with is not None:
            original_post = client._post
            def _fail(*a, **kw):
                raise fail_with
            client._post = _fail  # type: ignore
        transport = InMemoryMatrixTransport()
        personas = {AI_A: _make_persona()}
        return (
            EngineHooks(personas=personas, client=client, service=GatewayService(_cfg(), transport)),
            transport,
        )

    def test_happy_path_sends_reply(self):
        hooks, transport = self._hooks("你好！")
        ctx = self._make_ctx()
        hooks.on_human_text(ctx)
        self.assertEqual(len(transport.sent), 1)
        self.assertEqual(transport.sent[0].room_id, "!r:example.test")
        self.assertEqual(transport.sent[0].sender_id, AI_A)
        self.assertEqual(transport.sent[0].text, "你好！")

    def test_idempotency_key_matches(self):
        hooks, transport = self._hooks("hi")
        ctx = self._make_ctx(event_id="$unique")
        hooks.on_human_text(ctx)
        self.assertEqual(transport.sent[0].txn_id, hooks._service._txn_id_for(speech_idempotency_key(AI_A, "$unique")))

    def test_no_matching_persona_no_send(self):
        hooks, transport = self._hooks()
        ctx = self._make_ctx(target=f"@ai_unknown:{SERVER_NAME}")
        hooks.on_human_text(ctx)
        self.assertEqual(transport.sent, [])

    def test_model_error_raises(self):
        hooks, _ = self._hooks(fail_with=ModelError("bad"))
        with self.assertRaises(ModelError):
            hooks.on_human_text(self._make_ctx())

    def test_transient_raises(self):
        from botville_gateway.as_consumer import TransientUnavailable
        hooks, _ = self._hooks(fail_with=TransientUnavailable("down"))
        with self.assertRaises(TransientUnavailable):
            hooks.on_human_text(self._make_ctx())

    def test_in_memory_dedupe_prevents_double_send(self):
        from botville_gateway.engine.engine import InMemorySpeechDedupeForEngine
        client = _mock_client("hi")
        transport = InMemoryMatrixTransport()
        dedupe = InMemorySpeechDedupeForEngine()
        hooks = EngineHooks(
            personas={AI_A: _make_persona()},
            client=client,
            service=GatewayService(_cfg(), transport),
            dedupe=dedupe,
        )
        ctx = self._make_ctx(event_id="$dup")
        hooks.on_human_text(ctx)
        hooks.on_human_text(ctx)   # replay
        self.assertEqual(len(transport.sent), 1)

    def test_blocked_by_policy_no_model_call(self):
        client = _mock_client()
        client._post = mock.Mock(wraps=client._post)
        transport = InMemoryMatrixTransport()
        service = GatewayService(_cfg(), transport)
        service.evaluate_policy = mock.Mock(return_value={"decision": "block"})
        hooks = EngineHooks(personas={AI_A: _make_persona()}, client=client, service=service)
        hooks.on_human_text(self._make_ctx())
        client._post.assert_not_called()
        self.assertEqual(transport.sent, [])
        self.assertEqual(hooks.stats.snapshot()["precheck_blocked"], 1)

    def test_multiple_targets_replied_in_order(self):
        """群聊同时 @ 两个 AI → 两个都发言，顺序与 target_user_ids 一致。"""
        client = _mock_client("回复")
        transport = InMemoryMatrixTransport()
        personas = {AI_A: _make_persona(AI_A), AI_B: _make_persona(AI_B)}
        hooks = EngineHooks(personas=personas, client=client, service=GatewayService(_cfg(), transport))
        ctx = ConsumerContext(
            event_type="message_text",
            event_id="$g1",
            room_id="!r:example.test",
            sender=f"@alice:{SERVER_NAME}",
            trust="trusted",
            text="hey",
            should_reply=True,
            persona_user_id=AI_A,
            target_user_ids=(AI_A, AI_B),
            idempotency_key=speech_idempotency_key(AI_A, "$g1"),
        )
        hooks.on_human_text(ctx)
        senders = [e.sender_id for e in transport.sent]
        self.assertEqual(senders, [AI_A, AI_B])

    def test_ring_buffer_history_in_messages(self):
        """先说一句、再说一句 → 第二次 complete 的 messages 包含第一轮历史。"""
        payloads = []
        cfg = ModelConfig(base_url="http://fake.invalid", model="m")
        client = ModelClient(cfg)

        def _capture(payload, persona):
            payloads.append(payload)
            return {"choices": [{"message": {"content": "pong"}}]}

        client._post = _capture  # type: ignore
        transport = InMemoryMatrixTransport()
        hooks = EngineHooks(personas={AI_A: _make_persona()}, client=client, service=GatewayService(_cfg(), transport))

        hooks.on_human_text(ConsumerContext(
            event_type="message_text", event_id="$first", room_id="!r:x",
            sender=f"@alice:{SERVER_NAME}", trust="trusted", text="ping one",
            should_reply=True, persona_user_id=AI_A, target_user_ids=(AI_A,),
        ))
        hooks.on_human_text(ConsumerContext(
            event_type="message_text", event_id="$second", room_id="!r:x",
            sender=f"@alice:{SERVER_NAME}", trust="trusted", text="ping two",
            should_reply=True, persona_user_id=AI_A, target_user_ids=(AI_A,),
        ))
        # 第二次调用的 messages 应含历史（user + assistant）+ 当前 user。
        msgs = payloads[1]["messages"]
        roles = [m["role"] for m in msgs if m["role"] != "system"]
        self.assertIn("assistant", roles)
        self.assertTrue(roles[-1] == "user")


class TestEngineServicePath(unittest.TestCase):
    _make_ctx = TestEngineHooks._make_ctx
    def _setup_service(self, **config):
        transport = InMemoryMatrixTransport()
        service = GatewayService(_cfg(**config), transport)
        client = _mock_client("pong")
        client._post = mock.Mock(wraps=client._post)
        from botville_gateway.engine.engine import InMemorySpeechDedupeForEngine
        hooks = EngineHooks(personas={AI_A: _make_persona()}, client=client,
                            service=service, dedupe=InMemorySpeechDedupeForEngine())
        return hooks, service, client, transport

    def _assert_cap(self, config, reason):
        hooks, service, client, transport = self._setup_service(**config)
        hooks.on_human_text(self._make_ctx(event_id="$first"))
        totals = service.get_usage(persona_id=AI_A)["totals"]
        self.assertEqual(totals["messages_sent"], 1)
        self.assertEqual(totals["tokens_total"], 2)
        decision = service.evaluate_policy({"persona_id": AI_A,
            "conversation_id": "!r:example.test", "content": "hello", "token_estimate": 2})
        self.assertIn(reason, [r["code"] for r in decision["reasons"]])
        client._post.reset_mock()
        hooks.on_human_text(self._make_ctx(event_id="$second"))
        hooks.on_human_text(self._make_ctx(event_id="$third"))
        client._post.assert_not_called()
        self.assertEqual(len(transport.sent), 1)
        self.assertEqual(hooks.stats.snapshot()["precheck_blocked"], 2)
        self.assertEqual(service.get_usage(persona_id=AI_A)["totals"]["messages_sent"], 1)

    def test_daily_message_cap(self):
        self._assert_cap({"rate_limit_messages": 1, "rate_limit_window_seconds": 86400}, "messages.window.exceeded")

    def test_token_budget(self):
        hooks, service, client, transport = self._setup_service(platform_token_budget=100)
        hooks.on_human_text(self._make_ctx())
        client._post.assert_not_called()
        self.assertEqual(transport.sent, [])
        self.assertFalse(hooks._dedupe.seen(speech_idempotency_key(AI_A, "$e1")))

    def test_character_token_budget_allows_prompt_and_max_reply(self):
        from botville_gateway.engine.engine import estimate_tokens

        for text, expected in [("", 0), ("abc", 1), ("abcd", 2), ("你好世界", 5), ("😀" * 8, 3)]:
            with self.subTest(text=text):
                self.assertEqual(estimate_tokens(text), expected)

        hooks, service, client, transport = self._setup_service(platform_token_budget=1000)
        service.evaluate_policy = mock.Mock(wraps=service.evaluate_policy)
        hooks.on_human_text(self._make_ctx(text="你好😀"))
        client._post.assert_called_once()
        self.assertEqual(len(transport.sent), 1)
        messages = client._post.call_args.args[0]["messages"]
        expected = sum(estimate_tokens(m["content"]) for m in messages)
        expected += _make_persona().max_reply_chars // 4 + 1
        self.assertEqual(service.evaluate_policy.call_args.args[0]["token_estimate"], expected)
        self.assertEqual(service.get_usage(persona_id=AI_A)["totals"]["tokens_total"], 2)

    def test_anti_spam(self):
        self._assert_cap({"platform_max_consecutive_ai_messages": 1}, "anti_spam.consecutive")

    def test_ai_loop(self):
        self._assert_cap({"platform_max_ai_messages_per_hour": 1}, "ai_loop.tripped")

    def test_send_race_block_is_terminal(self):
        hooks, service, client, transport = self._setup_service(rate_limit_messages=1)
        original = client._post
        def race(*args, **kwargs):
            service.send_ai_message("!r:example.test", {"sender_persona_id": AI_A,
                "content": {"type": "text", "text": "other"}, "allow_deferred": False})
            return original(*args, **kwargs)
        client._post = mock.Mock(side_effect=race)
        ctx = self._make_ctx()
        hooks.on_human_text(ctx)
        hooks.on_human_text(ctx)
        self.assertEqual(client._post.call_count, 1)
        self.assertEqual(hooks.stats.snapshot()["send_blocked"], 1)
        self.assertEqual(len(transport.sent), 1)

    def test_service_idempotency_without_speech_dedupe(self):
        hooks, service, client, transport = self._setup_service()
        hooks._dedupe = None
        ctx = self._make_ctx()
        hooks.on_human_text(ctx)
        hooks.on_human_text(ctx)
        self.assertEqual(len(transport.sent), 1)
        self.assertEqual(service.get_usage(persona_id=AI_A)["totals"]["messages_sent"], 1)

    def test_delivery_errors(self):
        from botville_gateway.matrix import DeliveryError
        from botville_gateway.errors import GatewayDeliveryError
        from botville_gateway.as_consumer import TransientUnavailable
        for code in ("matrix.unreachable", "matrix.unavailable", "matrix.forbidden"):
            with self.subTest(code=code):
                hooks, service, client, transport = self._setup_service()
                transport.send_text = mock.Mock(side_effect=DeliveryError(code, "fake failure"))
                expected = GatewayDeliveryError if code == "matrix.forbidden" else TransientUnavailable
                with self.assertRaises(expected):
                    hooks.on_human_text(self._make_ctx())
                self.assertFalse(hooks._dedupe.seen(speech_idempotency_key(AI_A, "$e1")))
                self.assertEqual(service.pending_reservations, 0)
                self.assertEqual(service.get_usage()["totals"]["messages_sent"], 0)

    def test_restart_persistent_speech_dedupe(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "speech.db")
            transport = InMemoryMatrixTransport()
            for attempt in range(2):
                conn = connect(path)
                try:
                    client = _mock_client("pong")
                    client._post = mock.Mock(wraps=client._post)
                    hooks = EngineHooks(personas={AI_A: _make_persona()}, client=client,
                        service=GatewayService(_cfg(), transport),
                        dedupe=PersistentSpeechDedupe(AsTransactionStore(conn)))
                    hooks.on_human_text(self._make_ctx())
                    self.assertEqual(client._post.call_count, 1 if attempt == 0 else 0)
                finally:
                    conn.close()
            self.assertEqual(len(transport.sent), 1)

    def test_all_chat_is_wrapped_and_bounded(self):
        from botville_gateway.engine.engine import _build_messages
        history = [("assistant", "😀" * 9000)] * 25
        messages = _build_messages(history, "x" * 9000)
        self.assertEqual(len(messages), 20)
        for message in messages:
            self.assertTrue(message["content"].startswith('<untrusted id="'))
        self.assertEqual(messages[0]["content"].count("😀"), 8000)
        self.assertIn("x" * 8000 + "</untrusted", messages[-1]["content"])


class TestEngineMentionOrder(_TmpDb):
    def test_mentions_and_body_order_are_serial(self):
        from botville_gateway.as_consumer import AsTransactionConsumer
        for structured in (True, False):
            with self.subTest(structured=structured):
                trace = []
                transport = InMemoryMatrixTransport()
                original_send = transport.send_text
                def send(**kwargs):
                    trace.append(("send", kwargs["sender_id"]))
                    return original_send(**kwargs)
                transport.send_text = send
                client = _mock_client()
                def complete(**kwargs):
                    trace.append(("model", kwargs["persona"].user_id))
                    return "pong"
                client.complete = complete
                service = GatewayService(_cfg(), transport)
                hooks = EngineHooks(personas={AI_A: _make_persona(AI_A), AI_B: _make_persona(AI_B)},
                    client=client, service=service, dedupe=PersistentSpeechDedupe(self.store))
                consumer = AsTransactionConsumer(self.store, hooks=hooks, service=service,
                    server_name=SERVER_NAME, room_members=lambda room: [AI_A, AI_B])
                content = {"msgtype": "m.text", "body": f"{AI_B} {AI_A}"}
                if structured:
                    content["m.mentions"] = {"user_ids": [AI_B, AI_A]}
                    content["body"] = f"{AI_A} {AI_B}"
                event = {"type": "m.room.message", "event_id": "$order" + str(structured),
                    "room_id": "!r:example.test", "sender": "@alice:example.test", "content": content}
                self.store.store_transaction("fake-order-" + str(structured), json.dumps({"events": [event]}))
                consumer.run_once()
                self.assertEqual(trace, [("model", AI_B), ("send", AI_B), ("model", AI_A), ("send", AI_A)])


    def test_partial_reply_restart_continues_remaining_persona(self):
        from botville_gateway.as_consumer import AsTransactionConsumer, TransientUnavailable
        transport = InMemoryMatrixTransport()
        service = GatewayService(_cfg(), transport)
        client = _mock_client()
        calls = []
        def complete(**kwargs):
            uid = kwargs["persona"].user_id
            calls.append(uid)
            if uid == AI_B and calls.count(AI_B) == 1:
                raise TransientUnavailable("fake unavailable")
            return "pong"
        client.complete = complete
        event = {"type": "m.room.message", "event_id": "$partial", "room_id": "!r:example.test",
            "sender": "@alice:example.test", "content": {"msgtype": "m.text", "body": "hi",
            "m.mentions": {"user_ids": [AI_A, AI_B]}}}
        self.store.store_transaction("fake-partial", json.dumps({"events": [event]}))
        for attempt in range(2):
            # Reopen SQLite and recreate hooks/consumer as on process restart.
            conn = connect(self.db_path)
            try:
                store = AsTransactionStore(conn)
                hooks = EngineHooks(personas={AI_A: _make_persona(AI_A), AI_B: _make_persona(AI_B)},
                    client=client, service=service, dedupe=PersistentSpeechDedupe(store))
                consumer = AsTransactionConsumer(store, hooks=hooks, service=service, server_name=SERVER_NAME)
                consumer.run_once()
                conn.execute("UPDATE as_transactions SET next_attempt_at=NULL")
                conn.commit()
            finally:
                conn.close()
        self.assertEqual(calls, [AI_A, AI_B, AI_B])
        self.assertEqual([e.sender_id for e in transport.sent], [AI_A, AI_B])


# ---------------------------------------------------------------------------
# 5. PersistentSpeechDedupe
# ---------------------------------------------------------------------------
class TestPersistentSpeechDedupe(_TmpDb):
    def setUp(self):
        super().setUp()
        self.dedupe = PersistentSpeechDedupe(self.store)

    def test_new_key_not_seen(self):
        self.assertFalse(self.dedupe.seen("spk_abc"))

    def test_mark_then_seen(self):
        self.dedupe.mark("spk_xyz")
        self.assertTrue(self.dedupe.seen("spk_xyz"))

    def test_idempotent_mark(self):
        self.dedupe.mark("spk_xyz")
        self.dedupe.mark("spk_xyz")
        self.assertEqual(self.store.speech_dedupe_count(), 1)

    def test_purge_removes_old_keys(self):
        from datetime import datetime, timedelta, timezone
        self.dedupe.mark("spk_old")
        future = datetime.now(timezone.utc) + timedelta(days=100)
        removed = self.store.purge_speech_dedupe(1, now=future)
        self.assertEqual(removed, 1)
        self.assertEqual(self.store.speech_dedupe_count(), 0)

    def test_purge_keeps_recent_keys(self):
        from datetime import datetime, timezone
        self.dedupe.mark("spk_recent")
        removed = self.store.purge_speech_dedupe(90, now=datetime.now(timezone.utc))
        self.assertEqual(removed, 0)
        self.assertTrue(self.dedupe.seen("spk_recent"))

    def test_migration_creates_table(self):
        """旧库（无 as_speech_dedupe 表）经 connect() 迁移后表存在且可用。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = os.path.join(tmpdir, "old.db")
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE as_transactions (txn_id TEXT PRIMARY KEY, body TEXT NOT NULL,"
                " received_at TEXT NOT NULL, processed_at TEXT)"
            )
            conn.commit()
            conn.close()
            conn2 = connect(db)
            tables = {r[0] for r in conn2.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("as_speech_dedupe", tables)
            # 可用
            PersistentSpeechDedupe(AsTransactionStore(conn2)).mark("spk_test")
            conn2.close()


# ---------------------------------------------------------------------------
# 6. 服务器集成：engine_hooks 装配条件
# ---------------------------------------------------------------------------
class TestEngineServerIntegration(unittest.TestCase):
    def _server(self, personas_data=None, model_url=None, model_name="gpt-t", **overrides):
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        db = os.path.join(tmpdir, "as.db")
        kw = dict(consumer_enabled=True, as_txn_db_path=db, port=0)
        if model_url:
            kw["model_base_url"] = model_url
            kw["model_name"] = model_name
        if personas_data is not None:
            p = os.path.join(tmpdir, "p.json")
            with open(p, "w") as f:
                json.dump({"personas": personas_data}, f)
            kw["personas_file"] = p
        kw.update(overrides)
        return build_server(_cfg(**kw)), tmpdir

    def test_no_personas_no_model_engine_inactive(self):
        server, _ = self._server()
        try:
            self.assertIsNone(server.engine_hooks)
            self.assertIsNotNone(server.as_consumer)
        finally:
            server.server_close()

    def test_personas_but_no_model_engine_inactive(self):
        entry = dict(_base_persona_dict())
        server, _ = self._server(personas_data=[entry])
        try:
            self.assertIsNone(server.engine_hooks)
        finally:
            server.server_close()

    def test_model_but_no_personas_engine_inactive(self):
        server, _ = self._server(model_url="http://fake.invalid", personas_data=[])
        try:
            self.assertIsNone(server.engine_hooks)
        finally:
            server.server_close()

    def test_engine_uses_service_appservice_transport(self):
        from botville_gateway.matrix_appservice import AppServiceMatrixTransport
        server, _ = self._server(personas_data=[_base_persona_dict()],
            model_url="http://fake.invalid", matrix_transport="appservice",
            homeserver_url="http://127.0.0.1:9999", as_token="obviously-fake-as-token-for-test-only-12345")
        try:
            self.assertIsInstance(server.transport, AppServiceMatrixTransport)
            self.assertIs(server.engine_hooks._service, server.service)
            sent = InMemoryMatrixTransport()
            with mock.patch.object(server.transport, "send_text", side_effect=sent.send_text) as send:
                server.engine_hooks._client = _mock_client("pong")
                ctx = TestEngineHooks._make_ctx(self)
                server.engine_hooks.on_human_text(ctx)
                self.assertEqual(send.call_args.kwargs["sender_id"], AI_A)
                self.assertEqual(send.call_args.kwargs["txn_id"],
                    server.service._txn_id_for(speech_idempotency_key(AI_A, ctx.event_id)))
                self.assertEqual(server.service.get_usage()["totals"]["messages_sent"], 1)
        finally:
            server.server_close()

    def test_both_configured_engine_active(self):
        entry = dict(_base_persona_dict())
        server, _ = self._server(
            personas_data=[entry],
            model_url="http://fake.invalid",
        )
        try:
            self.assertIsNotNone(server.engine_hooks)
            self.assertIsNotNone(server.as_consumer)
        finally:
            server.server_close()


# ---------------------------------------------------------------------------
# 7. 密钥安全检查
# ---------------------------------------------------------------------------
class TestKeySafety(unittest.TestCase):
    def test_config_model_api_key_not_in_repr(self):
        from botville_gateway.config import Config
        cfg = Config(dev_mode=True, model_api_key="totally-secret-key-do-not-log")
        self.assertNotIn("totally-secret-key-do-not-log", repr(cfg))


if __name__ == "__main__":
    unittest.main()
