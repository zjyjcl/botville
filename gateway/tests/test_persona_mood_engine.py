# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""PR4b engine mood boundaries and storage migration regressions."""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest

GATEWAY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(GATEWAY_ROOT, "src") not in sys.path:
    sys.path.insert(0, os.path.join(GATEWAY_ROOT, "src"))

from botville_gateway.as_consumer import ConsumerContext, speech_idempotency_key
from botville_gateway.config import Config
from botville_gateway.engine import EngineHooks, ModelClient, ModelConfig, Persona
from botville_gateway.matrix import InMemoryMatrixTransport
from botville_gateway.memory.store import MemoryStore, SCHEMA_VERSION
from botville_gateway.service import GatewayService

SERVER = "example.test"
AI_A = "@ai_a:" + SERVER
AI_B = "@ai_b:" + SERVER


class _Mood:
    def __init__(self, label="happy", fail=False):
        self.label, self.fail = label, fail
        self.calls = []

    def get(self, persona_id):
        self.calls.append(("get", persona_id))
        if self.fail:
            raise RuntimeError("database fault")
        return {"label": self.label, "mood_public": True}

    def record_reply(self, persona_id, *, human=True):
        self.calls.append(("reply", persona_id, human))
        if self.fail:
            raise RuntimeError("database fault")
        return {"label": self.label}

    def record_block(self, persona_id):
        self.calls.append(("block", persona_id))
        if self.fail:
            raise RuntimeError("database fault")
        return {"label": "sad"}


def _config():
    return Config(host="127.0.0.1", port=0, dev_mode=True,
                  matrix_server_name=SERVER, silent_hours_enabled=False,
                  hmac_secret=b"m" * 32)


def _client(payloads):
    client = ModelClient(ModelConfig(base_url="http://fake.invalid", model="m"))
    def post(payload, persona):
        payloads.append(payload)
        return {"choices": [{"message": {"content": "ok"}}]}
    client._post = post  # type: ignore[assignment]
    return client


def _context(target=AI_A, event_id="$mood"):
    return ConsumerContext(event_type="message_text", event_id=event_id, room_id="!r:" + SERVER,
        sender="@human:" + SERVER, trust="trusted", text="hello", should_reply=True,
        persona_user_id=target, target_user_ids=(target,),
        idempotency_key=speech_idempotency_key(target, event_id))


class MoodEngineTests(unittest.TestCase):
    def _hooks(self, mood, payloads, service=None):
        service = service or GatewayService(_config(), InMemoryMatrixTransport())
        return EngineHooks(personas={AI_A: Persona(user_id=AI_A, system_prompt="trusted system")},
            client=_client(payloads), service=service, mood_service=mood), service

    def test_only_target_persona_mood_is_untrusted_not_system(self):
        mood, payloads = _Mood("excited"), []
        hooks, _ = self._hooks(mood, payloads)
        hooks.on_human_text(_context())
        self.assertEqual(len(payloads), 1)
        self.assertNotIn("excited", payloads[0]["messages"][0]["content"])
        self.assertTrue(any("mood: excited" in row["content"] for row in payloads[0]["messages"][1:]))
        self.assertIn(("reply", AI_A, True), mood.calls)
        self.assertFalse(any(AI_B in call for call in mood.calls))

    def test_block_defer_and_send_block_event_rules(self):
        mood, payloads = _Mood(), []
        service = GatewayService(_config(), InMemoryMatrixTransport())
        service.evaluate_policy = lambda request: {"decision": "block"}
        hooks, _ = self._hooks(mood, payloads, service)
        hooks.on_human_text(_context())
        self.assertIn(("block", AI_A), mood.calls)

        mood, payloads = _Mood(), []
        service = GatewayService(_config(), InMemoryMatrixTransport())
        service.evaluate_policy = lambda request: {"decision": "defer", "retry_at": "2099-01-01T00:00:00Z"}
        hooks, _ = self._hooks(mood, payloads, service)
        from botville_gateway.as_consumer import DeferredUntil
        with self.assertRaises(DeferredUntil):
            hooks.on_human_text(_context(event_id="$defer"))
        self.assertNotIn(("block", AI_A), mood.calls)

        mood, payloads = _Mood(), []
        service = GatewayService(_config(), InMemoryMatrixTransport())
        service.send_ai_message = lambda **kwargs: {"outcome": "blocked", "decision": {"decision": "block"}}
        hooks, _ = self._hooks(mood, payloads, service)
        hooks.on_human_text(_context(event_id="$send-block"))
        self.assertIn(("block", AI_A), mood.calls)

    def test_mood_failure_does_not_prevent_delivery(self):
        mood, payloads = _Mood(fail=True), []
        transport = InMemoryMatrixTransport()
        hooks = EngineHooks(personas={AI_A: Persona(user_id=AI_A, system_prompt="trusted")},
            client=_client(payloads), service=GatewayService(_config(), transport), mood_service=mood)
        with self.assertLogs("botville_gateway.engine.engine", level="WARNING") as logs:
            hooks.on_human_text(_context())
        self.assertNotIn("database fault", "\n".join(logs.output))
        self.assertEqual(len(transport.sent), 1)


class MoodMigrationTests(unittest.TestCase):
    def test_version_eight_database_upgrades_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "memory.sqlite")
            store = MemoryStore(path, hmac_secret=b"x" * 32)
            store.close()
            db = sqlite3.connect(path)
            db.execute("DROP TABLE persona_state")
            db.execute("UPDATE memory_schema SET version=8 WHERE id=1")
            db.commit()
            db.close()
            upgraded = MemoryStore(path, hmac_secret=b"x" * 32)
            self.assertEqual(upgraded._db.execute("SELECT version FROM memory_schema WHERE id=1").fetchone()[0], SCHEMA_VERSION)
            self.assertIsNotNone(upgraded._db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='persona_state'").fetchone())
            upgraded.close()
            repeat = MemoryStore(path, hmac_secret=b"x" * 32)
            self.assertEqual(repeat._db.execute("SELECT count(*) FROM persona_state").fetchone()[0], 0)
            repeat.close()
