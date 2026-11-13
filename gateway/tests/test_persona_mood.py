# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""PR4b mood configuration and deterministic state-machine regressions."""
from __future__ import annotations

import os
import tempfile
import traceback
import unittest
from types import SimpleNamespace
import json
import urllib.error
import urllib.request

from botville_gateway.config import Config, ConfigError, CallerToken, load_config
from botville_gateway.caller_identity import CallerAuthenticator
from botville_gateway.memory.mood import MoodService
from botville_gateway.memory.store import MemoryStore, SCHEMA_VERSION
from botville_gateway.memory.owner_http import MemoryHTTPState
from botville_gateway.http_server import build_server
from test_personas_list import PersonaHTTP, OWNER, OTHER


class MoodConfigTests(unittest.TestCase):
    def test_defaults_boolean_and_range(self):
        config = load_config({'GATEWAY_DEV_MODE': 'true'})
        self.assertFalse(config.mood_enabled)
        self.assertEqual(config.mood_half_life_minutes, 240)
        for value in ('1', 'true', 'yes', 'on'):
            self.assertTrue(load_config({'GATEWAY_DEV_MODE': 'true', 'GATEWAY_MOOD_ENABLED': value}).mood_enabled)
        for value in ('0', '10081', 'invalid-value'):
            with self.subTest(value=value), self.assertRaises(ConfigError) as raised:
                load_config({'GATEWAY_DEV_MODE': 'true', 'GATEWAY_MOOD_HALF_LIFE_MINUTES': value})
            self.assertNotIn(value, str(raised.exception))
            self.assertNotIn(value, ''.join(traceback.format_exception(
                type(raised.exception), raised.exception, raised.exception.__traceback__)))
        self.assertEqual(1, load_config({'GATEWAY_DEV_MODE': 'true', 'GATEWAY_MOOD_HALF_LIFE_MINUTES': '1'}).mood_half_life_minutes)
        self.assertEqual(10080, load_config({'GATEWAY_DEV_MODE': 'true', 'GATEWAY_MOOD_HALF_LIFE_MINUTES': '10080'}).mood_half_life_minutes)
        with self.assertRaises(ConfigError):
            Config(dev_mode=True, mood_half_life_minutes=0)

    def test_boolean_rejection_and_real_database_startup(self):
        with self.assertRaises(ConfigError) as error:
            load_config({'GATEWAY_DEV_MODE': 'true', 'GATEWAY_MOOD_ENABLED': 'BOOL_SENTINEL'})
        self.assertNotIn('BOOL_SENTINEL', str(error.exception))
        with tempfile.TemporaryDirectory() as directory:
            for enabled in (False, True):
                server = build_server(Config(host='127.0.0.1', port=0, dev_mode=True,
                    mood_enabled=enabled, memory_db_path=os.path.join(directory, 'memory.db'),
                    memory_purge_interval_seconds=0))
                try:
                    self.assertEqual(server.mood_service is not None, enabled)
                    if enabled:
                        self.assertEqual(server.mood_service.get('@ai_a:example.invalid')['label'], 'calm')
                finally:
                    server.server_close()

    def test_enabled_without_database_warns_and_stays_disabled(self):
        with self.assertLogs('botville_gateway.http_server', level='WARNING') as logs:
            server = build_server(Config(host='127.0.0.1', port=0, dev_mode=True,
                                         mood_enabled=True))
        try:
            self.assertIsNone(server.mood_service)
            self.assertTrue(any('Mood disabled' in line for line in logs.output))
        finally:
            server.server_close()


class MoodStoreTests(unittest.TestCase):
    def test_rules_lazy_decay_and_idempotent_migration(self):
        now = [1_700_000_000.0]
        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, 'memory.sqlite')
            store = MemoryStore(path, retention_days=90, hmac_secret=b'x' * 32)
            mood = MoodService(store, 1, clock=lambda: now[0])
            self.assertEqual(mood.record_reply('@ai_a:example.invalid')['label'], 'happy')
            for _ in range(4):
                mood.record_reply('@ai_a:example.invalid')
            self.assertEqual(mood.get('@ai_a:example.invalid')['label'], 'excited')
            for _ in range(15):
                mood.record_reply('@ai_a:example.invalid')
            self.assertEqual(mood.get('@ai_a:example.invalid')['label'], 'tired')
            self.assertEqual(mood.record_block('@ai_a:example.invalid')['label'], 'sad')
            now[0] += 60
            self.assertEqual(mood.get('@ai_a:example.invalid')['label'], 'sad')
            now[0] += .001
            self.assertEqual(mood.get('@ai_a:example.invalid')['label'], 'calm')
            mood.patch('@ai_a:example.invalid', mood='happy')
            now[0] += 60
            self.assertEqual(mood.get('@ai_a:example.invalid')['label'], 'happy')
            now[0] += .001
            self.assertEqual(mood.get('@ai_a:example.invalid')['label'], 'calm')
            self.assertEqual(mood.record_reply('@ai_idle:example.invalid', human=False)['label'], 'calm')
            # Window cutoffs retain a timestamp exactly on the boundary, then
            # remove it one millisecond later.
            now[0] = 1_700_010_000.0
            for _ in range(4):
                mood.record_reply('@ai_window:example.invalid')
            now[0] += 600
            self.assertEqual(mood.record_reply('@ai_window:example.invalid')['label'], 'excited')
            now[0] += .001
            self.assertEqual(mood.record_reply('@ai_window:example.invalid')['label'], 'happy')
            now[0] = 1_700_020_000.0
            for _ in range(19):
                mood.record_reply('@ai_tired_window:example.invalid')
            now[0] += 3600
            self.assertEqual(mood.record_reply('@ai_tired_window:example.invalid')['label'], 'tired')
            now[0] += .001
            self.assertEqual(mood.record_reply('@ai_tired_window:example.invalid')['label'], 'happy')
            with store._lock:
                self.assertIsNotNone(store._db.execute("SELECT 1 FROM persona_state").fetchone())
                self.assertEqual(store._db.execute("SELECT version FROM memory_schema WHERE id=1").fetchone()[0], SCHEMA_VERSION)
            store.close()
            again = MemoryStore(path, retention_days=90, hmac_secret=b'x' * 32)
            self.assertEqual(again._db.execute("SELECT version FROM memory_schema WHERE id=1").fetchone()[0], SCHEMA_VERSION)
            again.close()


class MoodHTTPTests(unittest.TestCase):
    PERSONA = '@ai_a:example.invalid'

    @staticmethod
    def _request(app, path, *, method='GET', body=None, auth='MatrixOpenID user'):
        request = urllib.request.Request('http://{}:{}'.format(*app.server.server_address[:2]) + path,
                                         method=method)
        if auth:
            request.add_header('Authorization', auth)
        if body is not None:
            request.add_header('Content-Type', 'application/json')
            request.data = json.dumps(body).encode()
        try:
            response = urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, dict(response.headers), json.load(response)

    def test_disabled_owner_state_and_unknown_subpath(self):
        with PersonaHTTP() as app:
            status, headers, body = self._request(app, '/v1/personas/%40ai_a%3Aexample.invalid/state')
            self.assertEqual((status, body['code']), (404, 'mood.disabled'))
            self.assertEqual(headers['Cache-Control'], 'no-store')
            status, _, body = self._request(app, '/v1/personas/%40ai_a%3Aexample.invalid/anything',
                                            auth='Bearer site')
            self.assertEqual((status, body['code']), (404, 'not_found'))

    def test_enabled_patch_shape_validation_and_owner_visibility(self):
        with PersonaHTTP() as app, tempfile.TemporaryDirectory() as temp:
            store = MemoryStore(os.path.join(temp, 'state.sqlite'), retention_days=90, hmac_secret=b'x' * 32)
            app.server.memory_store = store
            app.server.memory_http = MemoryHTTPState(store, b'x' * 32)
            app.server.mood_service = MoodService(store, 240)
            path = '/v1/personas/%40ai_a%3Aexample.invalid/state'
            for body in ({}, {'unknown': 1}, {'mood': 'neutral'}, {'mood': 'anxious'},
                         {'mood': None}, {'mood': []}, {'mood': 2},
                         {'mood_public': 'yes'}, {'mood_public': None}, {'mood_public': 0}):
                status, _, result = self._request(app, path, method='PATCH', body=body)
                self.assertEqual((status, result['code']), (422, 'request.invalid'))
            status, headers, result = self._request(app, path, method='PATCH',
                                                     body={'mood': 'happy', 'mood_public': False})
            self.assertEqual(status, 200)
            self.assertEqual(result['persona_id'], self.PERSONA)
            self.assertEqual(result['mood'], {'label': 'happy', 'source': 'owner',
                                              'set_at': result['mood']['set_at']})
            self.assertFalse(result['mood_public'])
            self.assertEqual(headers['Cache-Control'], 'no-store')
            # A valid other end-user identity sees the existing public detail, not mood.
            app.server.persona_detail_verifier.verify = lambda header: ('ok', OTHER)
            status, _, detail = self._request(app, '/v1/personas/%40ai_a%3Aexample.invalid')
            self.assertEqual(status, 200)
            self.assertIsNone(detail['mood'])
            # State itself remains owner-only and indistinguishable from missing.
            status, _, result = self._request(app, path)
            self.assertEqual((status, result['code']), (404, 'persona.not_found'))
            store.close()

    def test_access_matrix_rate_limit_and_content_free_audit(self):
        with PersonaHTTP() as app, tempfile.TemporaryDirectory() as temp:
            store = MemoryStore(os.path.join(temp, 'state.sqlite'), retention_days=90, hmac_secret=b'x' * 32)
            app.server.memory_store = store
            app.server.memory_http = MemoryHTTPState(store, b'x' * 32)
            app.server.mood_service = MoodService(store, 240)
            app.server.authenticator = CallerAuthenticator(SimpleNamespace(
                dev_mode=True, auth_token='site', legacy_token_enabled=True,
                caller_tokens=(CallerToken('owner', 'owner-token', OWNER),
                               CallerToken('other', 'other-token', OTHER),
                               CallerToken('remote', 'remote-token', '@remote:elsewhere.invalid')),
            ))
            path = '/v1/personas/%40ai_a%3Aexample.invalid/state'
            cases = ((None, path, 401, 'auth.missing'),
                     ('Bearer site', path, 403, 'persona.not_owned'),
                     ('Bearer other-token', path, 404, 'persona.not_found'),
                     ('Bearer remote-token', path, 404, 'persona.not_found'),
                     ('Bearer owner-token', '/v1/personas/%40ai_unknown%3Aexample.invalid/state', 404, 'persona.not_found'),
                     ('Bearer owner-token', '/v1/personas/%40ai_orphan%3Aexample.invalid/state', 404, 'persona.not_found'),
                     ('Bearer owner-token', '/v1/personas/not-an-id/state', 404, 'persona.not_found'))
            for method in ('GET', 'PATCH'):
                for auth, target, expected, code in cases:
                    status, _, body = self._request(app, target, method=method, auth=auth)
                    self.assertEqual((status, body['code']), (expected, code))
            self.assertEqual(self._request(app, path, auth='Bearer owner-token')[0], 200)
            self.assertEqual(self._request(app, path, method='PATCH', auth='Bearer owner-token',
                                           body={'mood': 'happy'})[0], 200)
            with store._lock:
                audit_rows = [tuple(row) for row in store._db.execute('SELECT * FROM memory_audit').fetchall()]
                self.assertNotIn('happy', repr(audit_rows))
                self.assertIn('mood_update', {row[4] for row in audit_rows})
                self.assertIn('200', {row[7] for row in audit_rows})
            # Twenty write permits per caller; the next request carries the normal retry header.
            for _ in range(19):
                self.assertEqual(self._request(app, path, method='PATCH', auth='Bearer owner-token',
                                               body={'mood': 'happy'})[0], 200)
            status, headers, body = self._request(app, path, method='PATCH', auth='Bearer owner-token',
                                                  body={'mood': 'sad', 'ignored': 'AUDIT_BODY_SENTINEL'})
            self.assertEqual((status, body['code']), (429, 'rate_limited.transport'))
            self.assertIn('Retry-After', headers)
            with store._lock:
                self.assertNotIn('AUDIT_BODY_SENTINEL', repr([
                    tuple(row) for row in store._db.execute('SELECT * FROM memory_audit').fetchall()]))
            store.close()
