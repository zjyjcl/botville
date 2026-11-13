# SPDX-License-Identifier: Apache-2.0
"""B2.8c real policy/service/consumer paths, deterministic clocks and fake IO."""
import dataclasses
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from botville_gateway.as_consumer import AsTransactionConsumer
from botville_gateway.config import Config, ConfigError, load_config
from botville_gateway.engine.engine import EngineHooks
from botville_gateway.engine.personas import Persona
from botville_gateway.errors import GatewayDeliveryError
from botville_gateway.groups import GroupInvites
from botville_gateway.http_server import build_server
from botville_gateway.limits import LimitLayer
from botville_gateway.matrix import DeliveryError, InMemoryMatrixTransport
from botville_gateway.memory.store import MemoryStore
from botville_gateway.service import GatewayService
from botville_gateway.store import EventKind

ROOM = '!group:local.test'
HUMAN = '@human:local.test'
AIS = tuple(f'@ai_{i}:local.test' for i in range(5))
SECRET = b'group-throttle-test-secret' * 2


class GroupThrottleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
        self.mem = MemoryStore(os.path.join(self.tmp.name, 'memory.db'), hmac_secret=SECRET)
        self.addCleanup(self.mem.close)
        self.personas = {p: Persona(p, 'Be brief.', groups={'enabled': True}) for p in AIS}
        self.make()

    def make(self, **overrides):
        base = dict(host='127.0.0.1', port=0, dev_mode=True, hmac_secret=SECRET,
                    matrix_server_name='local.test', groups_enabled=True,
                    silent_hours_enabled=False, rate_limit_messages=0)
        self.cfg = Config(**dict(base, **overrides))
        self.tr = InMemoryMatrixTransport()
        self.svc = GatewayService(self.cfg, self.tr, now_fn=lambda: self.now)
        self.groups = GroupInvites(self.mem, self.tr, self.personas,
                                  enabled=self.cfg.groups_enabled, server_name='local.test')
        for p in AIS:
            self.groups._record(p, ROOM, HUMAN, 'accepted')
        self.svc.set_group_invites(self.groups)

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)

    def send(self, p=AIS[0], room=ROOM, **kw):
        return self.svc.send_ai_message(room, self.payload(p), **kw)

    @staticmethod
    def payload(p):
        return {'sender_persona_id': p, 'content': {'type': 'text', 'text': 'reply'}}

    def evaluate(self, p=AIS[0], room=ROOM, **kw):
        return self.svc.evaluate_policy(dict(persona_id=p, conversation_id=room, **kw))

    def blocked(self, result, code):
        policy = result['decision'] if isinstance(result.get('decision'), dict) else result
        self.assertFalse(policy['allowed'], result)
        self.assertIn(code, [r['code'] for r in policy['reasons']], result)
        self.assertIn(code, [c.get('code') for c in policy['checks']], result)

    def test_cooldown_exact_boundary_and_scope(self):
        self.assertEqual(self.send()['outcome'], 'delivered')
        self.blocked(self.send(), 'min_interval.too_soon')
        self.advance(29)
        self.blocked(self.evaluate(), 'min_interval.too_soon')
        self.assertTrue(self.evaluate(AIS[1])['allowed'])
        self.assertTrue(self.evaluate(room='!other:local.test')['allowed'])
        self.advance(1)
        self.assertEqual(self.send()['outcome'], 'delivered')

    def test_disabled_switches_and_nonaccepted_rooms(self):
        for mode in ('deployment', 'persona', 'left', 'unknown'):
            with self.subTest(mode=mode):
                self.make()
                if mode == 'deployment':
                    self.groups.enabled = False
                elif mode == 'persona':
                    self.groups.personas = {AIS[0]: dataclasses.replace(self.personas[AIS[0]], groups={'enabled': False})}
                elif mode == 'left':
                    self.groups._record(AIS[0], ROOM, HUMAN, 'left')
                room = '!unknown:local.test' if mode == 'unknown' else ROOM
                for _ in range(35):
                    self.assertEqual(self.send(room=room)['outcome'], 'delivered')
                self.assertFalse(any(c['key'].startswith('group.') for c in self.evaluate(room=room)['checks']))

    def test_hour_window_and_cooldown_zero(self):
        self.make(group_cooldown_seconds=0, group_max_consecutive_ai_messages=50)
        for _ in range(20):
            self.assertEqual(self.send()['outcome'], 'delivered')
        self.blocked(self.send(), 'group.replies_per_hour.exceeded')
        self.advance(3599)
        self.blocked(self.evaluate(), 'group.replies_per_hour.exceeded')
        self.advance(1)
        self.assertTrue(self.evaluate()['allowed'])

    def test_sliding_window_not_fixed_hour(self):
        self.make(group_cooldown_seconds=0, group_max_replies_per_hour=2, group_max_consecutive_ai_messages=50)
        self.send()
        self.advance(120)
        self.send()
        self.advance(3480)
        self.assertEqual(self.send()['outcome'], 'delivered')
        self.blocked(self.evaluate(), 'group.replies_per_hour.exceeded')
        self.advance(120)
        self.assertTrue(self.evaluate()['allowed'])

    def put(self, layer, scope, scope_id, rules):
        self.svc._limits.put(dict(layer=layer, scope=scope, scope_id=scope_id, rules=rules), now=self.now)

    def test_group_floors_zero_layers_and_stricter_owner_peer(self):
        zero = {'anti_spam': {'max_consecutive_ai_messages': 0}, 'ai_loop': {'max_ai_messages_per_hour': 0}}
        # The public API rejects consecutive=0; exercise legacy/unset layer
        # snapshots directly without relaxing its schema or validation.
        for layer, scope, pid in (('owner', 'persona', AIS[0]), ('peer', 'peer', AIS[1])):
            self.svc._limits._layers[(layer, scope, pid)] = LimitLayer(layer, scope, pid, zero)

        checks = {c['key']: c for c in self.evaluate(peer_id=AIS[1])['checks']}
        self.assertEqual(checks['ai_loop.per_hour']['limit'], 30)
        self.assertEqual(checks['anti_spam.consecutive']['limit'], 3)
        strict = {'anti_spam': {'max_consecutive_ai_messages': 2}, 'ai_loop': {'max_ai_messages_per_hour': 7}}
        self.put('owner', 'persona', AIS[0], strict)
        checks = {c['key']: c for c in self.evaluate(peer_id=AIS[1])['checks']}
        self.assertEqual((checks['ai_loop.per_hour']['limit'], checks['ai_loop.per_hour']['layer']), (7, 'owner'))
        self.assertEqual(checks['anti_spam.consecutive']['limit'], 2)
        self.put('peer', 'peer', AIS[1], {'ai_loop': {'max_ai_messages_per_hour': 4}})
        checks = {c['key']: c for c in self.evaluate(peer_id=AIS[1])['checks']}
        self.assertEqual((checks['ai_loop.per_hour']['limit'], checks['ai_loop.per_hour']['layer']), (4, 'peer'))
        self.make()
        keys = {c['key'] for c in self.evaluate(room='!dm:local.test')['checks']}
        self.assertNotIn('ai_loop.per_hour', keys)
        self.assertNotIn('anti_spam.consecutive', keys)

    def test_existing_min_interval_coexists(self):
        self.put('owner', 'persona', AIS[0], {'min_seconds_between_messages': 60})
        self.send()
        self.advance(30)
        decision = self.evaluate()
        self.blocked(decision, 'min_interval.too_soon')
        self.assertTrue(next(c for c in decision['checks'] if c['key'] == 'group.min_interval')['allowed'])
        self.advance(30)
        self.assertTrue(self.evaluate()['allowed'])

    def test_success_only_deduplication_and_inflight(self):
        self.tr.fail = True
        with self.assertRaises(GatewayDeliveryError):
            self.send()
        self.assertEqual(len(self.svc._engine._group_sends), 0)
        self.assertTrue(self.evaluate()['allowed'])
        self.tr.fail = False
        self.send(idempotency_key='one')
        self.send(idempotency_key='one')
        self.assertEqual(sum(map(len, self.svc._engine._group_sends.values())), 1)
        self.assertNotIn(ROOM, repr(self.svc._engine._group_sends))
        self.assertNotIn(AIS[0], repr(self.svc._engine._group_sends))
        self.make(group_cooldown_seconds=0, group_max_replies_per_hour=1)
        reservation = self.svc._reservations.reserve(persona_id=AIS[0], tokens=0, at=self.now, conversation_id=ROOM)
        self.blocked(self.evaluate(), 'group.replies_per_hour.exceeded')
        self.svc._reservations.release(reservation)
        self.assertTrue(self.evaluate()['allowed'])

    def start_http(self):
        path = os.path.join(self.tmp.name, 'personas.json')
        with open(path, 'w') as handle:
            json.dump({'personas': [{'user_id': p, 'system_prompt': 'Be brief.', 'groups': {'enabled': True}} for p in AIS]}, handle)
        cfg = dataclasses.replace(self.cfg, personas_file=path, memory_db_path=os.path.join(self.tmp.name, 'http.db'),
                                  memory_purge_interval_seconds=0)
        server = build_server(cfg, self.tr)
        server.service._now = lambda: self.now
        server.service._engine._now = lambda: self.now
        for p in AIS:
            server.group_invites._record(p, ROOM, HUMAN, 'accepted')
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()
        def close():
            server.shutdown()
            thread.join()
            server.server_close()
        self.addCleanup(close)
        self.http = server
        return server

    def post(self, path, payload):
        req = urllib.request.Request(f'http://127.0.0.1:{self.http.server_port}/v1/{path}',
                                     data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            response = opener.open(req, timeout=2)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, json.load(response)

    def test_e2ee_http_precheck_no_transport_or_quota_and_fallback(self):
        server = self.start_http()
        for pid, state in zip(AIS, ('encrypted_disabled', 'rejected_encrypted')):
            for enabled in (True, False):
                server.group_invites.enabled = enabled
                server.group_invites._record(pid, ROOM, HUMAN, state)
                self.assertEqual(server.group_invites._state(pid, ROOM), state)
                with patch.object(self.tr, 'send_text', wraps=self.tr.send_text) as transport:
                    for path, body in ((f'conversations/{ROOM}/messages', self.payload(pid)),
                                       ('policy/evaluate', {'persona_id': pid, 'conversation_id': ROOM})):
                        status, result = self.post(path, body)
                        self.assertEqual((status, result['code']), (403, 'conversation.e2ee'))
                        self.assertEqual(result['type'], 'urn:botville:error:conversation.e2ee')
                    transport.assert_not_called()
                self.assertEqual(server.service._store.all_events(), [])
                self.assertEqual(server.service._reservations.for_persona(pid), [])
        with patch.object(self.tr, 'send_text', side_effect=DeliveryError('conversation.e2ee', 'sensitive detail')):
            status, result = self.post('conversations/!other:local.test/messages', self.payload(pid))
        self.assertEqual((status, result['code']), (403, 'conversation.e2ee'))
        self.assertNotIn('sensitive', json.dumps(result))
        self.assertEqual(server.service._store.all_events(), [])
        self.assertEqual(server.service._reservations.for_persona(pid), [])

    def consumer(self, model):
        hooks = EngineHooks(personas=self.personas, client=model, service=self.svc, group_invites=self.groups)
        return AsTransactionConsumer(Mock(), hooks=hooks, service=self.svc,
            server_name='local.test', room_members=lambda room: (*AIS, HUMAN),
            persona_user_ids=AIS, group_max_responders=self.cfg.group_max_responders,
            now=lambda: self.now)

    @staticmethod
    def event(sender, event_id, targets):
        return {'type': 'm.room.message', 'event_id': event_id, 'room_id': ROOM,
                'sender': sender, 'content': {'msgtype': 'm.text', 'body': ' '.join(targets),
                                             'm.mentions': {'user_ids': list(targets)}}}

    def test_encrypted_engine_never_calls_model(self):
        self.groups.disable_encrypted(ROOM)
        model = Mock()
        model.config.configured = True
        consumer = self.consumer(model)
        consumer._dispatch_event(self.event(HUMAN, '$encrypted', AIS))
        model.complete.assert_not_called()
        self.assertEqual(self.tr.sent, [])

    def test_five_ai_one_hour_consumer_engine_policy_acceptance(self):
        model = Mock()
        model.config.configured = True
        model.complete.return_value = ' '.join(AIS)  # Every reply attempts to summon all peers.
        consumer = self.consumer(model)
        replies = defaultdict(list)
        for step in range(180):
            before = len(self.tr.sent)
            targets = AIS[step % 5:] + AIS[:step % 5]
            consumer._dispatch_event(self.event(HUMAN, f'$human{step}', targets))
            new = self.tr.sent[before:]
            self.assertLessEqual(len(new), self.cfg.group_max_responders)
            for sent in new:
                replies[sent.sender_id].append(self.now)
                count = len(self.tr.sent)
                calls = model.complete.call_count
                consumer._dispatch_event(self.event(sent.sender_id, sent.event_id, AIS))
                self.assertEqual(len(self.tr.sent), count)
                self.assertEqual(model.complete.call_count, calls)
            self.advance(20)
        self.assertEqual(len(replies), 5)
        self.assertEqual(len(self.tr.sent), self.cfg.group_ai_loop_max_per_hour)
        self.assertEqual(model.complete.call_count, len(self.tr.sent))
        for times in replies.values():
            self.assertLessEqual(len(times), self.cfg.group_max_replies_per_hour)
            self.assertTrue(all((b-a).total_seconds() >= self.cfg.group_cooldown_seconds for a,b in zip(times, times[1:])))

    def test_external_five_ai_http_chain_circuit_breakers(self):
        self.make(group_cooldown_seconds=0)
        server = self.start_http()
        codes = Counter()
        delivered = Counter()
        for step in range(70):
            p = AIS[step % 5]
            status, receipt = self.post(f'conversations/{ROOM}/messages', self.payload(p))
            self.assertEqual(status, 200)
            if receipt['outcome'] == 'delivered':
                delivered[p] += 1
            else:
                codes.update(r['code'] for r in receipt['decision']['reasons'])
            self.advance(1)
        self.assertGreater(codes['anti_spam.consecutive'], 0)
        self.assertGreater(codes['ai_loop.tripped'], 0)
        self.assertEqual(sum(delivered.values()), self.cfg.group_ai_loop_max_per_hour)
        self.assertFalse(any(e.kind == EventKind.HUMAN for e in server.service._store.all_events()))


class GroupConfigTests(unittest.TestCase):
    def test_defaults_boundaries_invalid_values_without_echo(self):
        for name, default, low, high in (
            ('group_cooldown_seconds', 30, 0, 3600),
            ('group_max_replies_per_hour', 20, 1, 200),
            ('group_ai_loop_max_per_hour', 30, 1, 1000),
            ('group_max_consecutive_ai_messages', 3, 1, 50),
        ):
            env = 'GATEWAY_' + name.upper()
            self.assertEqual(getattr(load_config({'GATEWAY_DEV_MODE': 'true'}), name), default)
            for boundary in (low, high):
                self.assertEqual(getattr(load_config({'GATEWAY_DEV_MODE': 'true', env: str(boundary)}), name), boundary)
            for value in (str(low-1), str(high+1), 'private-secret', '1.2', 'true'):
                with self.subTest(name=name, value=value), self.assertRaises(ConfigError) as caught:
                    load_config({'GATEWAY_DEV_MODE': 'true', env: value})
                self.assertNotIn(repr(value), str(caught.exception))
                self.assertNotIn('private-secret', str(caught.exception))
            with self.assertRaises(ConfigError):
                Config(**{name: True})


if __name__ == '__main__':
    unittest.main()
