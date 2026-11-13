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

    def _assembled_mood_worker(self, enabled):
        from botville_gateway.http_server import build_server
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        config = replace(self.config, host="127.0.0.1", port=0,
            mood_enabled=enabled, memory_db_path=os.path.join(temp.name, "memory.db"),
            memory_purge_interval_seconds=0, model_base_url="https://model.invalid/v1",
            model_name="test", model_api_key="TEST_KEY_SENTINEL")
        self.transport.joined_members = lambda room, _: self.members.get(room, ())
        with patch.object(ProactiveWorker, "start"), patch(
                "botville_gateway.memory.worker.MemoryIdleWorker.start"):
            server = build_server(config, self.transport)
        self.addCleanup(server.server_close)
        worker = server.proactive_worker
        self.assertIsNotNone(worker)
        worker.personas = {self.persona.user_id: self.persona}
        worker.client = self.client
        worker.clock = self.clock
        worker.service = self.service
        self.store = worker.store
        self.contact()
        return server, worker

    def _assert_mood_prompt(self, expected_label):
        from botville_gateway.isolation import build_untrusted_segment, SYSTEM_ZONE_DECLARATION
        from botville_gateway.proactive import INSTRUCTION
        self.assertEqual(len(self.client.calls), 1)
        request = self.client.calls[0]
        self.assertEqual(request["system_text"], SYSTEM_ZONE_DECLARATION + "\n" +
                         self.persona.instruction_text() + "\n" + INSTRUCTION)
        data = "现在可以发送一条简短的主动问候。"
        if expected_label is not None:
            data += "\nmood: " + expected_label
        with patch("botville_gateway.isolation.generate_nonce", return_value="a" * 64):
            wrapped, _ = build_untrusted_segment(data)
        self.assertEqual(request["messages"], [{"role": "user", "content": wrapped}])

    def test_assembled_mood_enabled_uses_decayed_untrusted_label(self):
        server, worker = self._assembled_mood_worker(True)
        now = [self.now.timestamp()]
        server.mood_service._clock = lambda: now[0]
        server.mood_service.patch(self.persona.user_id, mood="happy")
        self.assertEqual(worker.mood_label_provider(self.persona.user_id), "happy")
        now[0] += self.config.mood_half_life_minutes * 60 + 1
        with patch("botville_gateway.isolation.generate_nonce", return_value="a" * 64):
            self.assertEqual(worker.run_once(), 1)
        self._assert_mood_prompt("calm")

    def test_assembled_mood_disabled_omits_provider_and_prompt(self):
        server, worker = self._assembled_mood_worker(False)
        self.assertIsNone(server.mood_service)
        self.assertIsNone(worker.mood_label_provider)
        with patch("botville_gateway.isolation.generate_nonce", return_value="a" * 64):
            self.assertEqual(worker.run_once(), 1)
        self._assert_mood_prompt(None)

    def test_assembled_mood_failure_preserves_decision_and_send(self):
        server, worker = self._assembled_mood_worker(True)
        candidates = list(worker.candidates(self.persona, self.now))
        with patch.object(server.mood_service, "get", side_effect=RuntimeError(
                "TEST_KEY_SENTINEL BODY_SENTINEL BIO_SENTINEL")), patch.object(
                self.service, "send_ai_message", wraps=self.service.send_ai_message) as send:
            self.assertEqual(list(worker.candidates(self.persona, self.now)), candidates)
            self.assertTrue(candidates)
            with self.assertLogs("botville_gateway.http_server", level="WARNING") as logs, patch(
                    "botville_gateway.isolation.generate_nonce", return_value="a" * 64):
                self.assertEqual(worker.run_once(), 1)
            self.assertEqual(send.call_count, 1)
        self.assertEqual(logs.output, ["WARNING:botville_gateway.http_server:"
                                     "Proactive mood unavailable; omitting label"])
        self._assert_mood_prompt(None)

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

    def test_mood_labels_do_not_change_candidates_or_send_decisions(self):
        from botville_gateway.isolation import SYSTEM_ZONE_DECLARATION, build_untrusted_segment
        from botville_gateway.policy import estimate_tokens
        from botville_gateway.proactive import INSTRUCTION

        self.contact()
        self.contact("@other:example.test", "!other")
        self.members["!other"] = (self.persona.user_id, "@other:example.test")
        system = SYSTEM_ZONE_DECLARATION + "\n" + self.persona.instruction_text() + "\n" + INSTRUCTION
        wrapped, _ = build_untrusted_segment("现在可以发送一条简短的主动问候。\nmood: excited")
        budget = estimate_tokens(system) + estimate_tokens(wrapped) + self.persona.max_reply_chars
        scenarios = (
            ("allowed", {}, {}, None, 2),
            ("idle", {"proactive_min_idle_minutes": 182}, {}, None, 0),
            ("persona_daily", {}, {"max_per_day": 0}, None, 0),
            ("target_daily", {"proactive_per_target_per_day": 0}, {}, None, 0),
            ("policy_daily", {"platform_proactive_per_day": 1}, {}, None, 1),
            ("quiet", {}, {"quiet_hours": {"enabled": True, "start": "11:00",
                "end": "13:00", "timezone": "UTC"}}, None, 0),
            ("reply", {"proactive_per_target_per_day": 24}, {"require_reply": True}, 180, 0),
            ("interval", {"proactive_min_interval_minutes": 240, "proactive_per_target_per_day": 24}, {}, 200, 0),
            ("token_below", {"platform_token_budget": budget - 1}, {}, None, 0),
            ("token_exact", {"platform_token_budget": budget}, {}, None, 1),
        )
        for name, overrides, settings, history_age, expected in scenarios:
            baseline = None
            prompts = []
            for label in ("calm", "happy", "sad", "excited", "tired", None):
                with self.subTest(scenario=name, label=label):
                    self.worker._write("DELETE FROM proactive_queue")
                    cfg = replace(self.config, proactive_recent_contacts_enabled=True, **overrides)
                    persona = replace(self.persona, proactive={**self.persona.proactive, **settings})
                    client = Client()
                    service = GatewayService(cfg, InMemoryMatrixTransport(), now_fn=self.clock)
                    worker = ProactiveWorker(self.store, service, {persona.user_id: persona}, client,
                        cfg, members=lambda room, _: self.members.get(room, ()), clock=self.clock,
                        mood_label_provider=lambda _, value=label: value)
                    if history_age is not None:
                        for i, (room, (_, target)) in enumerate(self.members.items()):
                            at = (self.now - timedelta(minutes=history_age)).timestamp()
                            worker._write("INSERT INTO proactive_queue "
                                "(key,persona_id,target_user_id,room_id,slot,state,created_at,sent_at) "
                                "VALUES (?,?,?,?,?,'done',?,?)",
                                (str(i), persona.user_id, target, room, i, at, at))
                    candidates = list(worker.candidates(persona, self.now))
                    with patch.object(service, "send_ai_message", wraps=service.send_ai_message) as send:
                        with patch("botville_gateway.isolation.generate_nonce", return_value="a" * 64):
                            count = worker.run_once()
                    targets = {call.kwargs["payload"]["peer_id"] for call in send.call_args_list}
                    outcome = (candidates, count, len(client.calls), targets)
                    self.assertEqual(count, expected)
                    self.assertEqual(len(client.calls), expected)
                    self.assertEqual(len(targets), expected)
                    self.assertEqual({c["counterpart"] for c in candidates},
                        {"@owner:example.test", "@other:example.test"})
                    if baseline is None:
                        baseline = outcome
                    self.assertEqual(outcome, baseline)
                    for request in client.calls:
                        self.assertEqual(request["system_text"], system)
                        data = "现在可以发送一条简短的主动问候。"
                        if label is not None:
                            data += "\nmood: " + label
                        with patch("botville_gateway.isolation.generate_nonce", return_value="a" * 64):
                            expected_prompt, _ = build_untrusted_segment(data)
                        self.assertEqual(request["messages"], [{"role": "user", "content": expected_prompt}])
                    if client.calls:
                        prompts.append(client.calls[0]["messages"][0]["content"])
            if expected:
                self.assertEqual(len(set(prompts)), 6)

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

    def test_enabled_without_prerequisites_warns(self):
        from botville_gateway.http_server import GatewayServer
        for missing in ('memory', 'model', 'members'):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as directory:
                cfg = replace(self.config, host='127.0.0.1', port=0,
                    memory_db_path=None if missing == 'memory' else os.path.join(directory, 'memory.db'),
                    model_base_url=None if missing == 'model' else 'https://model.invalid/v1',
                    model_name=None if missing == 'model' else 'fake', memory_purge_interval_seconds=0)
                transport = InMemoryMatrixTransport()
                if missing != 'members':
                    transport.joined_members = lambda *_: ()
                with self.assertLogs('botville_gateway.http_server', level='WARNING') as logs:
                    server = GatewayServer(cfg, GatewayService(cfg, transport), transport)
                try:
                    self.assertIsNone(server.proactive_worker)
                    self.assertEqual([r.getMessage() for r in logs.records],
                        ['Proactive inactive: requires memory DB, model and membership transport'])
                finally:
                    server.server_close()

    def test_quiet_hours_cross_midnight_with_timezone(self):
        from zoneinfo import ZoneInfo
        persona = replace(self.persona, proactive={**self.persona.proactive,
            'quiet_hours': {'enabled': True, 'start': '22:00', 'end': '07:00',
                            'timezone': 'Asia/Shanghai'}})
        self.worker.personas = {persona.user_id: persona}
        self.contact(age=2880)
        for hour, minute, expected in ((23, 30, 0), (6, 59, 0), (7, 0, 1), (21, 59, 1)):
            with self.subTest(hour=hour, minute=minute):
                self.worker._write('DELETE FROM proactive_queue')
                self.clock.now = datetime(2026, 11, 13, hour, minute,
                    tzinfo=ZoneInfo('Asia/Shanghai')).astimezone(timezone.utc)
                self.assertEqual(self.worker.run_once(), expected)

    def test_missing_owner_log_throttled_and_bounded(self):
        logger = 'botville_gateway.proactive'
        with self.assertLogs(logger, level='INFO') as logs:
            self.worker.run_once()
            self.worker.run_once()
            self.clock.now += timedelta(hours=24) - timedelta(seconds=1)
            self.worker.run_once()
        self.assertEqual([r.getMessage() for r in logs.records],
                         ['Proactive owner skipped: no confirmed existing DM'])
        self.clock.now += timedelta(seconds=1)
        with self.assertLogs(logger, level='INFO'):
            self.worker.run_once()
        with patch('botville_gateway.proactive.OWNER_LOG_MAX_ENTRIES', 2):
            for number in range(10):
                list(self.worker.candidates(replace(self.persona,
                    owner_user_id=f'@owner{number}:example.test'), self.clock.now))
            self.assertEqual(len(self.worker._owner_log_times), 2)
            with patch('botville_gateway.proactive._LOG.info') as info:
                self.worker.run_once()
                info.assert_not_called()

    def _queue(self, key, state, age, room='!old'):
        self.worker._write('''INSERT INTO proactive_queue
            (key,persona_id,target_user_id,room_id,slot,state,body,created_at)
            VALUES (?,?,?,?,?,?,?,?)''', (key, self.persona.user_id,
                self.persona.owner_user_id, room, -len(self.worker._rows('SELECT * FROM proactive_queue')) - 1,
                state, 'private', (self.clock.now - age).timestamp()))

    def test_expired_mismatch_abandoned_and_new_slot_allowed(self):
        self.contact()
        self.members['!old'] = (*self.members['!owner'], '@extra:example.test')
        self._queue('old', 'sending', timedelta(hours=25))
        self.assertEqual(self.worker.run_once(), 1)
        row = self.worker._rows("SELECT * FROM proactive_queue WHERE key='old'")[0]
        self.assertEqual(row['state'], 'abandoned')
        self.assertIsNone(row['body'])
        self.assertEqual(self.worker.run_once(), 0)

    def test_pending_age_boundary_valid_dm_and_lookup_failure_retained(self):
        self._queue('young', 'queued', timedelta(hours=24))
        self._queue('valid', 'sending', timedelta(hours=25), '!owner')
        self.worker._maintain_queue(self.clock.now)
        self.assertEqual([r['state'] for r in self.worker._rows(
            'SELECT * FROM proactive_queue ORDER BY key')], ['sending', 'queued'])
        self.clock.now += timedelta(seconds=1)
        with patch.object(self.worker, 'members', side_effect=RuntimeError('unavailable')):
            self.worker._maintain_queue(self.clock.now)
        self.assertEqual(self.worker._rows("SELECT state FROM proactive_queue WHERE key='young'")[0]['state'], 'queued')
        self.worker._maintain_queue(self.clock.now)
        self.assertEqual(self.worker._rows("SELECT state FROM proactive_queue WHERE key='young'")[0]['state'], 'abandoned')

    def test_pending_expiry_respects_three_long_ticks(self):
        self.worker.config = replace(self.config, proactive_tick_seconds=86400)
        self._queue('pending', 'queued', timedelta(days=2))
        self.worker._maintain_queue(self.clock.now)
        self.assertEqual(self.worker._rows('SELECT state FROM proactive_queue')[0]['state'], 'queued')
        self.clock.now += timedelta(days=1, seconds=1)
        self.worker._maintain_queue(self.clock.now)
        self.assertEqual(self.worker._rows('SELECT state FROM proactive_queue')[0]['state'], 'abandoned')

    def test_terminal_retention_cleanup(self):
        for state in ('done', 'abandoned'):
            self._queue(state + '-old', state, timedelta(days=31))
            self._queue(state + '-fresh', state, timedelta(days=30))
        self._queue('recent-delivery', 'done', timedelta(days=40))
        self.worker._write("UPDATE proactive_queue SET sent_at=? WHERE key='recent-delivery'",
                           (self.clock.now.timestamp(),))
        self.worker._maintain_queue(self.clock.now)
        self.assertEqual({r['key'] for r in self.worker._rows('SELECT * FROM proactive_queue')},
                         {'done-fresh', 'abandoned-fresh', 'recent-delivery'})

    def test_legacy_queue_migration_is_idempotent_and_preserves_replay(self):
        self.worker._write('DROP TABLE proactive_queue')
        self.worker._write('''CREATE TABLE proactive_queue (
            key TEXT PRIMARY KEY, persona_id TEXT NOT NULL, target_user_id TEXT NOT NULL,
            room_id TEXT NOT NULL, slot INTEGER NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('queued','sending','done')),
            body TEXT, tokens INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
            sent_at REAL, UNIQUE(persona_id,target_user_id,slot))''')
        self._queue('legacy', 'sending', timedelta(hours=1), '!owner')
        before = self.worker._rows('SELECT * FROM proactive_queue')
        self.worker._migrate_queue()
        self.worker._migrate_queue()
        self.assertEqual(self.worker._rows('SELECT * FROM proactive_queue'), before)
        self.worker._write("UPDATE proactive_queue SET state='abandoned'")

    def test_confirmed_dm_sends_without_auto_join(self):
        from botville_gateway.matrix_appservice import AppServiceMatrixTransport
        transport = AppServiceMatrixTransport(homeserver_url='https://matrix.invalid',
            as_token='test-placeholder', server_name='example.test')
        self.worker.service = GatewayService(self.config, transport, now_fn=self.clock)
        self.worker.members = transport.joined_members
        self.contact()
        def request(method, path, **kwargs):
            if method == 'GET' and path.endswith('/joined_members'):
                return {'joined': {uid: {} for uid in self.members['!owner']}}
            if method == 'PUT' and '/send/m.room.message/' in path:
                return {'event_id': '$test'}
            self.fail('unexpected Matrix request')
        with patch.object(transport, '_request', side_effect=request), \
                patch.object(transport, 'ensure_user'), patch.object(transport, 'join') as join, \
                patch.object(transport, 'send_text', wraps=transport.send_text) as send:
            self.assertEqual(self.worker.run_once(), 1)
        join.assert_not_called()
        self.assertFalse(send.call_args.kwargs.get('allow_auto_join', False))

    def test_proactive_forbidden_existing_member_never_joins_or_leaves(self):
        from botville_gateway.matrix import DeliveryError
        from botville_gateway.matrix_appservice import AppServiceMatrixTransport
        transport = AppServiceMatrixTransport(homeserver_url='https://matrix.invalid',
            as_token='test-placeholder', server_name='example.test')
        self.worker.service = GatewayService(self.config, transport, now_fn=self.clock)
        self.worker.members = transport.joined_members
        self.contact()
        calls = []
        def request(method, path, **kwargs):
            calls.append((method, path))
            if method == 'GET' and path.endswith('/joined_members'):
                return {'joined': {uid: {} for uid in self.members['!owner']}}
            if method == 'POST' and '/join/' in path:
                return {'room_id': '!owner'}
            if method == 'PUT':
                if sum(m == 'PUT' for m, _ in calls) == 1:
                    raise DeliveryError('M_FORBIDDEN', 'not joined')
                return {'event_id': '$after-join'}
            self.fail('unexpected Matrix request')
        with patch.object(transport, '_request', side_effect=request), \
                patch.object(transport, 'ensure_user'), \
                patch.object(transport, 'send_text', wraps=transport.send_text) as send:
            self.assertEqual(self.worker.run_once(), 0)
        self.assertFalse(send.call_args.kwargs.get('allow_auto_join', False))
        self.assertEqual([m for m, _ in calls][-2:], ['PUT', 'GET'])
        self.assertFalse(any(method == 'POST' for method, _ in calls))

    def test_recent_contacts_recheck_legacy_dm_membership(self):
        self.worker.config = replace(self.config, proactive_recent_contacts_enabled=True)
        target = '@friend:example.test'
        self.contact(target, '!friend')
        for members in ((self.persona.user_id, target, '@extra:example.test'),
                        (self.persona.user_id, target)):
            self.members['!friend'] = members
            self.assertEqual(self.worker.run_once(), int(len(members) == 2))

    def test_recent_contacts_exclude_appservice_sender(self):
        from botville_gateway.as_consumer import DEFAULT_AS_SENDER_LOCALPART
        self.worker.config = replace(self.config, proactive_recent_contacts_enabled=True)
        target = '@' + DEFAULT_AS_SENDER_LOCALPART + ':example.test'
        self.contact(target, '!service')
        self.members['!service'] = (self.persona.user_id, target)
        self.assertEqual(self.worker.run_once(), 0)
        self.assertEqual(self.client.calls, [])

    def test_config_ranges_and_contract_defaults(self):
        with self.assertRaises(ConfigError): Config(proactive_enabled=True)
        with self.assertRaises(ConfigError): Config(proactive_tick_seconds=29)
        with self.assertRaises(ConfigError): Config(proactive_per_target_per_day=25)
        with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "openapi", "gateway.v1.yaml"), encoding="utf8") as handle:
            text = handle.read()
        self.assertIn("version: 0.6.0", text)
        section = text[text.index("    ProactiveSettings:"):text.index("    RelationDefaults:")]
        self.assertIn("default: false", section)


if __name__ == "__main__":
    unittest.main()
