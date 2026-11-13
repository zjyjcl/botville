# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""PR4c proactive scheduler: all effects stay in memory and use an injected clock."""
from __future__ import annotations

import os
import json
from dataclasses import replace
from unittest.mock import patch
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

from botville_gateway.config import Config, ConfigError
from botville_gateway.engine.personas import Persona
from botville_gateway.matrix import InMemoryMatrixTransport
from botville_gateway.memory.store import MemoryStore
from botville_gateway.proactive import ProactiveWorker
from botville_gateway.service import GatewayService


class Clock:
    def __init__(self, now): self.now = now
    def __call__(self): return self.now


class Client:
    def __init__(self): self.config = SimpleNamespace(configured=True); self.calls = []
    def complete(self, **kwargs): self.calls.append(kwargs); return "hello"


class ProactiveTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
        self.clock = Clock(self.now)
        self.store = MemoryStore(":memory:")
        self.addCleanup(self.store.close)
        self.client = Client()
        self.config = Config(dev_mode=True, matrix_server_name="example.test",
            proactive_enabled=True, proactive_tick_seconds=300,
            proactive_min_idle_minutes=0, proactive_min_interval_minutes=0,
            proactive_per_target_per_day=1, silent_hours_enabled=False,
            platform_token_budget=100000, platform_proactive_per_day=20, hmac_secret=b"x" * 32)
        self.transport = InMemoryMatrixTransport()
        self.service = GatewayService(self.config, self.transport, now_fn=self.clock)
        self.persona = Persona("@ai_p:example.test", "fixed instructions",
            owner_user_id="@owner:example.test", max_reply_chars=80,
            proactive={"enabled": True, "max_per_day": 6, "require_reply": False})
        self.members = {"!owner": (self.persona.user_id, "@owner:example.test")}
        self.worker = ProactiveWorker(self.store, self.service, {self.persona.user_id: self.persona},
            self.client, self.config, members=lambda room, _: self.members.get(room, ()), clock=self.clock)

    def contact(self, target="@owner:example.test", room="!owner", *, age=181, human=True):
        at = self.now - timedelta(minutes=age)
        self.store.add_item(persona_id=self.persona.user_id, scope_kind="dm", scope_key=target,
            room_id=room, counterpart=target, event_id="$" + str(len(self.store.items(self.persona.user_id))),
            sender=target, role="user" if human else "assistant", trust="trusted", text="private")
        with self.store._lock, self.store._db:
            self.store._db.execute("UPDATE memory_items SET created_at=? WHERE persona_id=? AND counterpart=?",
                (at.isoformat().replace("+00:00", "Z"), self.persona.user_id, target))

    def test_default_deployment_and_persona_are_off(self):
        self.contact()
        self.config = Config(dev_mode=True, matrix_server_name="example.test", proactive_enabled=False)
        worker = ProactiveWorker(self.store, self.service, {self.persona.user_id: self.persona}, self.client,
            self.config, members=lambda room, _: self.members.get(room, ()), clock=self.clock)
        self.assertEqual(worker.run_once(), 0)
        off = Persona(self.persona.user_id, "fixed", owner_user_id="@owner:example.test")
        self.worker.personas = {off.user_id: off}
        self.assertEqual(self.worker.run_once(), 0)

    def test_owner_only_and_confirmed_dm(self):
        self.contact("@other:example.test", "!other")
        self.members["!other"] = (self.persona.user_id, "@other:example.test")
        self.assertEqual(self.worker.run_once(), 0)
        self.contact()
        mood_worker = ProactiveWorker(self.store, self.service, {self.persona.user_id: self.persona},
            self.client, self.config, members=lambda room, _: self.members.get(room, ()), clock=self.clock,
            mood_label_provider=lambda _: "happy")
        self.assertEqual(mood_worker.run_once(), 1)
        self.assertEqual(len(self.client.calls), 1)

    def test_recent_contact_boundary_and_never_group_ai_or_foreign(self):
        self.config = Config(dev_mode=True, matrix_server_name="example.test", proactive_enabled=True,
            proactive_recent_contacts_enabled=True, proactive_min_idle_minutes=0,
            proactive_min_interval_minutes=0, proactive_per_target_per_day=1,
            platform_token_budget=100000, platform_proactive_per_day=20, hmac_secret=b"x" * 32)
        self.worker = ProactiveWorker(self.store, self.service, {self.persona.user_id: self.persona},
            self.client, self.config, members=lambda room, _: self.members.get(room, ()), clock=self.clock)
        self.contact("@edge:example.test", "!edge", age=7 * 24 * 60)
        self.members["!edge"] = (self.persona.user_id, "@edge:example.test")
        self.contact("@old:example.test", "!old", age=7 * 24 * 60 + 1)
        self.members["!old"] = (self.persona.user_id, "@old:example.test")
        self.contact("@ai_o:example.test", "!ai")
        self.members["!ai"] = (self.persona.user_id, "@ai_o:example.test")
        self.contact("@foreign:elsewhere", "!foreign")
        self.members["!foreign"] = (self.persona.user_id, "@foreign:elsewhere")
        self.contact("@group:example.test", "!group")
        self.members["!group"] = (self.persona.user_id, "@group:example.test", "@x:example.test")
        mood_worker = ProactiveWorker(self.store, self.service, {self.persona.user_id: self.persona},
            self.client, self.config, members=lambda room, _: self.members.get(room, ()), clock=self.clock,
            mood_label_provider=lambda _: "happy")
        self.assertEqual(mood_worker.run_once(), 1)
        self.assertEqual(len(self.client.calls), 1)

    def test_precheck_does_not_call_model_and_mood_is_untrusted(self):
        self.contact()
        blocked = Config(dev_mode=True, matrix_server_name="example.test", proactive_enabled=True,
            proactive_min_idle_minutes=0, proactive_min_interval_minutes=0, platform_token_budget=1,
            hmac_secret=b"x" * 32)
        worker = ProactiveWorker(self.store, GatewayService(blocked, self.transport, now_fn=self.clock),
            {self.persona.user_id: self.persona}, self.client, blocked,
            members=lambda room, _: self.members.get(room, ()), clock=self.clock,
            mood_label_provider=lambda _: "happy")
        self.assertEqual(worker.run_once(), 0); self.assertEqual(self.client.calls, [])
        mood_worker = ProactiveWorker(self.store, self.service, {self.persona.user_id: self.persona},
            self.client, self.config, members=lambda room, _: self.members.get(room, ()), clock=self.clock,
            mood_label_provider=lambda _: "happy")
        self.assertEqual(mood_worker.run_once(), 1)
        request = self.client.calls[-1]
        self.assertNotIn("mood:", request["system_text"])
        self.assertIn("mood: happy", request["messages"][0]["content"])

    def test_queue_replay_uses_same_key_and_no_duplicate(self):
        self.contact()
        original = self.service.send_ai_message
        ids = []
        def sent_once(**kwargs):
            ids.append(kwargs["idempotency_key"])
            return original(**kwargs)
        self.service.send_ai_message = sent_once
        self.assertEqual(self.worker.run_once(), 1)
        self.assertEqual(self.worker.run_once(), 0)
        self.assertEqual(len(ids), 1)
        with self.store._lock:
            row = self.store._db.execute("SELECT key FROM proactive_queue").fetchone()
            self.store._db.execute("UPDATE proactive_queue SET state='sending', sent_at=NULL WHERE key=?", (row[0],))
        self.clock.now += timedelta(minutes=5)
        self.assertEqual(self.worker.run_once(), 1)
        self.assertEqual(len(set(ids)), 1)

    def test_disk_restart_replays_sent_not_done_with_same_matrix_txn(self):
        self.store.close()
        fd, path = tempfile.mkstemp(); os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        first_store = MemoryStore(path); self.addCleanup(first_store.close)
        self.store = first_store
        self.contact()
        first = ProactiveWorker(first_store, self.service, {self.persona.user_id: self.persona}, self.client,
            self.config, members=lambda room, _: self.members.get(room, ()), clock=self.clock)
        original_write = first._write
        def crash_after_matrix(sql, args=()):
            if "state='done'" in sql: raise RuntimeError("simulated crash")
            return original_write(sql, args)
        first._write = crash_after_matrix
        self.assertEqual(first.run_once(), 0)
        self.assertEqual(len(self.transport.sent), 1)
        first_store.close()
        second_store = MemoryStore(path); self.addCleanup(second_store.close)
        retry_client = Client()
        second = ProactiveWorker(second_store, GatewayService(self.config, self.transport, now_fn=self.clock),
            {self.persona.user_id: self.persona}, retry_client, self.config,
            members=lambda room, _: self.members.get(room, ()), clock=self.clock)
        self.assertEqual(second.run_once(), 1)
        self.assertEqual(len(self.transport.sent), 1)
        self.assertEqual(retry_client.calls, [])

    def test_model_failure_releases_reservation_and_usage_is_proactive(self):
        self.contact()
        class Failing(Client):
            def complete(self, **kwargs): self.calls.append(kwargs); raise RuntimeError("unavailable")
        worker = ProactiveWorker(self.store, self.service, {self.persona.user_id: self.persona}, Failing(),
            self.config, members=lambda room, _: self.members.get(room, ()), clock=self.clock)
        self.assertEqual(worker.run_once(), 0)
        worker.client = self.client
        self.assertEqual(worker.run_once(), 1)
        usage = self.service.get_usage(persona_id=self.persona.user_id,
            from_="2026-11-13T00:00:00Z", to="2026-11-14T00:00:00Z")
        self.assertEqual(usage["totals"]["proactive_messages"], 1)
        self.assertEqual(self.service.list_usage_events(persona_id=self.persona.user_id)["items"][0]["kind"], "proactive_message")

    def test_stop_timeout_leaves_store_open_until_dead(self):
        class Stuck:
            alive = True
            def join(self, timeout): self.timeout = timeout
            def is_alive(self): return self.alive
        stuck = Stuck()
        worker = ProactiveWorker(self.store, self.service, {}, self.client, self.config,
            members=lambda *_: (), clock=self.clock, close_store=True)
        worker._thread = stuck
        self.assertFalse(worker.stop())
        self.store.items(self.persona.user_id)
        stuck.alive = False
        self.assertTrue(worker.stop())
        with self.assertRaises(Exception): self.store.items(self.persona.user_id)

    def test_require_reply_quiet_and_rate_controls(self):
        self.contact()
        self.persona = Persona(self.persona.user_id, "fixed", owner_user_id="@owner:example.test",
            max_reply_chars=80, proactive={"enabled": True, "require_reply": True,
            "quiet_hours": {"enabled": True, "start": "00:00", "end": "23:59", "timezone": "UTC"}})
        self.worker.personas = {self.persona.user_id: self.persona}
        self.assertEqual(self.worker.run_once(), 0)
        self.persona = Persona(self.persona.user_id, "fixed", owner_user_id="@owner:example.test",
            max_reply_chars=80, proactive={"enabled": True, "require_reply": True})
        self.worker.personas = {self.persona.user_id: self.persona}
        self.assertEqual(self.worker.run_once(), 1)
        self.clock.now += timedelta(minutes=5)
        self.assertEqual(self.worker.run_once(), 0)

    def test_idle_and_interval_boundaries(self):
        self.contact(age=179)
        self.worker.config = replace(self.config, proactive_min_idle_minutes=180,
            proactive_min_interval_minutes=240, proactive_per_target_per_day=24)
        self.assertEqual(self.worker.run_once(), 0)
        self.clock.now += timedelta(minutes=1)
        self.assertEqual(self.worker.run_once(), 1)
        self.clock.now += timedelta(minutes=239)
        self.assertEqual(self.worker.run_once(), 0)
        self.clock.now += timedelta(minutes=1)
        self.assertEqual(self.worker.run_once(), 1)

    def test_target_daily_limit_and_persona_limit(self):
        self.contact()
        self.assertEqual(self.worker.run_once(), 1)
        self.clock.now += timedelta(minutes=5)
        self.assertEqual(self.worker.run_once(), 0)
        self.worker.config = replace(self.config, proactive_per_target_per_day=24)
        self.worker.personas = {self.persona.user_id: replace(self.persona,
            proactive={"enabled": True, "max_per_day": 1, "require_reply": False})}
        self.assertEqual(self.worker.run_once(), 0)
        self.clock.now += timedelta(days=1)
        self.assertEqual(self.worker.run_once(), 1)

    def test_platform_budget_exhaustion_and_mood_none(self):
        self.contact()
        cfg = replace(self.config, platform_proactive_per_day=1)
        self.worker.config = replace(cfg, proactive_per_target_per_day=24)
        self.worker.service = GatewayService(cfg, self.transport, now_fn=self.clock)
        self.assertEqual(self.worker.run_once(), 1)
        request = self.client.calls[-1]
        self.assertNotIn("mood:", request['system_text'])
        self.assertNotIn("mood:", str(request['messages']))
        self.clock.now += timedelta(minutes=5)
        self.assertEqual(self.worker.run_once(), 0)
        self.assertEqual(len(self.client.calls), 1)
        usage = self.worker.service.get_usage(persona_id=self.persona.user_id,
            from_="2026-11-13T00:00:00Z", to="2026-11-14T00:00:00Z")
        self.assertGreater(usage['totals']['tokens_out'], 80)

    def test_persona_validation_does_not_echo_values(self):
        from botville_gateway.engine.personas import load_personas, PersonaConfigError
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'personas.json')
            for proactive in (None, {'SECRET_VALUE': True}, {'enabled': 'SECRET_VALUE'},
                    {'quiet_hours': {'start': 'SECRET_VALUE', 'end': '12:00', 'timezone': 'UTC'}}):
                with open(path, 'w') as out:
                    json.dump({'personas': [{'user_id': self.persona.user_id,
                        'system_prompt': 'fixed', 'proactive': proactive}]}, out)
                with self.assertRaises(PersonaConfigError) as caught:
                    load_personas(path, server_name='example.test')
                self.assertNotIn('SECRET_VALUE', str(caught.exception))

    def test_http_worker_wiring_uses_separate_connection(self):
        from botville_gateway.http_server import GatewayServer
        class Transport(InMemoryMatrixTransport):
            def joined_members(self, room, personas): return ()
        with tempfile.TemporaryDirectory() as directory:
            cfg = replace(self.config, host='127.0.0.1', port=0,
                memory_db_path=os.path.join(directory, 'memory.sqlite'),
                model_base_url='https://model.invalid/v1', model_name='fake',
                model_api_key='test-placeholder', memory_purge_interval_seconds=0)
            transport = Transport()
            with patch('botville_gateway.proactive.ProactiveWorker.start') as started:
                server = GatewayServer(cfg, GatewayService(cfg, transport), transport)
            try:
                self.assertIsNotNone(server.proactive_worker)
                self.assertIsNot(server.memory_store, server.proactive_worker.store)
                started.assert_called_once()
            finally:
                server.server_close()
            self.assertTrue(server.proactive_worker._closed)

    def test_require_reply_independent_of_daily_limit(self):
        self.contact()
        self.worker.config = replace(self.config, proactive_per_target_per_day=24)
        self.worker.personas = {self.persona.user_id: replace(self.persona,
            proactive={"enabled": True, "require_reply": True})}
        self.assertEqual(self.worker.run_once(), 1)
        self.clock.now += timedelta(days=1)
        self.assertEqual(self.worker.run_once(), 0)
        with self.store._lock, self.store._db:
            self.store._db.execute('UPDATE memory_items SET created_at=?',
                ((self.clock.now - timedelta(minutes=1)).isoformat(),))
        self.assertEqual(self.worker.run_once(), 1)

    def test_env_errors_redacted_and_quiet_default(self):
        from botville_gateway.config import load_config
        from botville_gateway.engine.personas import _validate_proactive
        for variable in ('TICK_SECONDS', 'MIN_IDLE_MINUTES', 'MIN_INTERVAL_MINUTES', 'PER_TARGET_PER_DAY'):
            for invalid in ('SECRET_VALUE', '-123456789', '999999999'):
                with self.assertRaises(ConfigError) as caught:
                    load_config({'GATEWAY_PROACTIVE_' + variable: invalid})
                self.assertNotIn(invalid, str(caught.exception))
        settings = _validate_proactive({'quiet_hours': {'start': '00:00',
            'end': '23:59', 'timezone': 'UTC'}}, 'persona')
        self.assertTrue(settings['quiet_hours']['enabled'])

    def test_config_ranges_and_contract_defaults(self):
        with self.assertRaises(ConfigError): Config(proactive_enabled=True)
        with self.assertRaises(ConfigError): Config(proactive_tick_seconds=29)
        with self.assertRaises(ConfigError): Config(proactive_per_target_per_day=25)
        with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "openapi", "gateway.v1.yaml"), encoding="utf8") as handle:
            text = handle.read()
        self.assertIn("version: 0.5.0", text)
        section = text[text.index("    ProactiveSettings:"):text.index("    RelationDefaults:")]
        self.assertIn("default: false", section)


if __name__ == "__main__":
    unittest.main()
