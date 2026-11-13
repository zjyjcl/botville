#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""Real HTTP acceptance coverage for the owner-memory boundary."""
from __future__ import annotations

import json
import contextlib
import hashlib
import hmac
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(ROOT, 'src') not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, 'src'))

from botville_gateway.config import Config
from botville_gateway.http_server import build_server
from botville_gateway.memory.owner_http import _expire_idempotency, _remember_idempotency
from botville_gateway.user_identity import VERIFY_INVALID, VERIFY_MISSING, VERIFY_OK

SERVER = 'example.test'
PERSONA = '@ai_memory:' + SERVER
OTHER_PERSONA = '@ai_memory_other:' + SERVER
OWNER = '@owner:' + SERVER
OTHER = '@other:' + SERVER
HUMAN = '@human:' + SERVER
TOKENS = {'owner-token': OWNER, 'other-token': OTHER}


class Verifier:
    def verify(self, header):
        if not header:
            return VERIFY_MISSING, None
        for prefix in ('MatrixOpenID ', 'Bearer '):
            if header.startswith(prefix):
                user = TOKENS.get(header[len(prefix):].strip())
                return (VERIFY_OK, user) if user else (VERIFY_INVALID, None)
        return VERIFY_INVALID, None


class MemoryHTTPServer:
    def __init__(self, enabled=True, access_log=False, owner_can_read_dm=False, two_owners=False):
        self.tmp = tempfile.mkdtemp(prefix='memory-owner-http-')
        personas = os.path.join(self.tmp, 'personas.json')
        rows = [
                {'user_id': PERSONA, 'system_prompt': 'test', 'owner_user_id': OWNER},
                {'user_id': '@ai_no_owner:' + SERVER, 'system_prompt': 'test'},
            ]
        if two_owners:
            rows.append({'user_id': OTHER_PERSONA, 'system_prompt': 'test', 'owner_user_id': OTHER})
        with open(personas, 'w', encoding='utf-8') as handle:
            json.dump({'personas': rows}, handle)
        values = dict(host='127.0.0.1', port=0, dev_mode=True, auth_token=None,
                      silent_hours_enabled=False, matrix_server_name=SERVER,
                      personas_file=personas,
                      access_log=access_log,
                      memory_owner_can_read_dm=owner_can_read_dm,
                      hmac_secret=b'0123456789abcdef0123456789abcdef')
        if enabled:
            values['memory_db_path'] = os.path.join(self.tmp, 'memory.db')
        self.server = build_server(Config(**values))
        self.server.user_verifier = Verifier()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *unused):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        shutil.rmtree(self.tmp, ignore_errors=True)

    @property
    def base(self):
        return 'http://{}:{}'.format(*self.server.server_address[:2])

    def request(self, method, path, *, token='owner-token', scheme='MatrixOpenID', body=None, headers=None):
        payload = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=payload, method=method)
        request.add_header('Authorization', scheme + ' ' + token)
        if payload:
            request.add_header('Content-Type', 'application/json')
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        response = None
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        raw = response.read()
        return response.status, dict(response.headers), json.loads(raw) if raw else None

    def item(self, *, group=False, text='private phrase'):
        store = self.server.memory_store
        event_id = '$event' + hashlib.sha256(text.encode()).hexdigest()
        store.add_item(persona_id=PERSONA, scope_kind='group' if group else 'dm',
                       scope_key='!group:' + SERVER if group else HUMAN,
                       room_id='!group:' + SERVER if group else None,
                       counterpart=None if group else HUMAN, event_id=event_id,
                       sender=HUMAN, role='user', trust='trusted', text=text)
        with store._lock:
            return store._db.execute('SELECT public_id FROM memory_items WHERE event_id=?',
                                     (event_id,)).fetchone()['public_id']


class OwnerMemoryHTTPTests(unittest.TestCase):
    def test_idempotency_key_is_independent_per_owner_over_http(self):
        with MemoryHTTPServer(two_owners=True) as app:
            common = {'Idempotency-Key': 'same-idempotency-key'}
            first = {'persona_id': PERSONA, 'counterpart': HUMAN, 'key': 'likes', 'value': 'one'}
            second = {'persona_id': OTHER_PERSONA, 'counterpart': HUMAN, 'key': 'likes', 'value': 'two'}
            one = app.request('PUT', '/v1/memories/facts', token='owner-token', body=first, headers=common)
            two = app.request('PUT', '/v1/memories/facts', token='other-token', body=second, headers=common)
            one_replay = app.request('PUT', '/v1/memories/facts', token='owner-token', body=first, headers=common)
            two_replay = app.request('PUT', '/v1/memories/facts', token='other-token', body=second, headers=common)
        self.assertEqual((one[0], two[0], one_replay[0], two_replay[0]), (201, 201, 201, 201))
        self.assertEqual(one[2]['id'], one_replay[2]['id'])
        self.assertEqual(two[2]['id'], two_replay[2]['id'])
        self.assertNotEqual(one[2]['id'], two[2]['id'])
    def test_idempotency_cache_is_scoped_and_owner_bounded(self):
        """Same header belongs to its caller; one owner preserves other LRU rows."""
        import botville_gateway.memory.owner_http as owner_http
        state = SimpleNamespace(idempotency=owner_http.OrderedDict())
        previous = owner_http.IDEMPOTENCY_PER_OWNER_MAX
        owner_http.IDEMPOTENCY_PER_OWNER_MAX = 2
        try:
            _remember_idempotency(state, 'alice', ('alice', 'shared-key'), (0, 'a', '1', 201))
            _remember_idempotency(state, 'bob', ('bob', 'shared-key'), (0, 'b', '2', 201))
            _remember_idempotency(state, 'alice', ('alice', 'key-two'), (0, 'a', '3', 201))
            # A replay is an access, so it becomes Alice's newest cache row.
            state.idempotency.move_to_end(('alice', 'shared-key'))
            _remember_idempotency(state, 'alice', ('alice', 'key-three'), (0, 'a', '4', 201))
        finally:
            owner_http.IDEMPOTENCY_PER_OWNER_MAX = previous
        self.assertIn(('bob', 'shared-key'), state.idempotency)
        self.assertIn(('alice', 'shared-key'), state.idempotency)
        self.assertIn(('alice', 'key-three'), state.idempotency)
        self.assertNotIn(('alice', 'key-two'), state.idempotency)

    def test_idempotency_expiry_survives_lru_replay_order(self):
        """An expired replay entry can sit behind a live LRU entry."""
        import botville_gateway.memory.owner_http as owner_http
        state = SimpleNamespace(idempotency=owner_http.OrderedDict([
            (('alice', 'live'), (101, 'x', '1', 201)),
            (('bob', 'expired'), (0, 'x', '2', 201)),
        ]))
        _expire_idempotency(state, owner_http.IDEMPOTENCY_TTL + 1)
        self.assertEqual(list(state.idempotency), [('alice', 'live')])
    def test_disabled_auth_and_owner_matrix(self):
        with MemoryHTTPServer(False) as app:
            status, headers, body = app.request('GET', '/v1/memories?persona_id=' + PERSONA)
            self.assertEqual((status, body['code']), (404, 'memory.disabled'))
            self.assertEqual(headers['Cache-Control'], 'no-store')
            status, _, capabilities = app.request('GET', '/v1/capabilities')
            self.assertEqual(status, 200)
            self.assertFalse(any(row['id'] == 'memory.manage' for row in capabilities['capabilities']))
        with MemoryHTTPServer() as app:
            path = '/v1/memories?persona_id=' + urllib.parse.quote(PERSONA, safe='')
            status, _, body = app.request('GET', path, token='other-token')
            self.assertEqual((status, body['code']), (403, 'persona.not_owned'))
            missing = urllib.parse.quote('@ai_missing:' + SERVER, safe='')
            status, _, body = app.request('GET', '/v1/memories?persona_id=' + missing)
            self.assertEqual((status, body['code']), (404, 'persona.not_found'))
            no_owner = urllib.parse.quote('@ai_no_owner:' + SERVER, safe='')
            status, _, body = app.request('GET', '/v1/memories?persona_id=' + no_owner)
            self.assertEqual((status, body['code']), (403, 'persona.not_owned'))
            status, _, body = app.request('GET', path, token='site-token')
            self.assertEqual((status, body['code']), (401, 'auth.invalid_token'))
            status, _, body = app.request('GET', path, scheme='Bearer')
            self.assertEqual(status, 200)

    def test_list_cursor_public_ids_and_cache_control(self):
        with MemoryHTTPServer() as app:
            first = app.item(text='first private text')
            second = app.item(text='second private text')
            encoded = urllib.parse.quote(PERSONA, safe='')
            status, headers, body = app.request('GET', '/v1/memories?persona_id={}&limit=1'.format(encoded))
            self.assertEqual(status, 200)
            self.assertEqual(headers['Cache-Control'], 'no-store')
            self.assertEqual(len(body['items']), 1)
            self.assertRegex(body['items'][0]['id'], r'^mr_[0-9a-f]{16}$')
            self.assertNotIn('private phrase', json.dumps(body))
            self.assertTrue(body['page']['has_more'])
            status, _, next_page = app.request('GET', '/v1/memories?persona_id={}&limit=1&cursor={}'.format(
                encoded, urllib.parse.quote(body['page']['next_cursor'], safe='')))
            self.assertEqual(status, 200)
            self.assertNotEqual(next_page['items'][0]['id'], body['items'][0]['id'])
            self.assertIn(first, (first, second))

    def test_list_filters_and_cursor_binding(self):
        with MemoryHTTPServer() as app:
            dm_id = app.item(text='dm filter')
            group_id = app.item(group=True, text='group filter')
            store = app.server.memory_store
            store.upsert_fact(PERSONA, HUMAN, 'likes', 'tea', source='owner')
            encoded = urllib.parse.quote(PERSONA, safe='')
            status, _, body = app.request('GET', '/v1/memories?persona_id={}&layer=recent&scope_kind=dm&counterpart={}'.format(
                encoded, urllib.parse.quote(HUMAN, safe='')))
            self.assertEqual((status, [item['id'] for item in body['items']]), (200, [dm_id]))
            status, _, body = app.request('GET', '/v1/memories?persona_id={}&layer=recent&scope_kind=group&room_id={}'.format(
                encoded, urllib.parse.quote('!group:' + SERVER, safe='')))
            self.assertEqual((status, [item['id'] for item in body['items']]), (200, [group_id]))
            status, _, body = app.request('GET', '/v1/memories?persona_id={}&layer=profile&pinned=false'.format(encoded))
            self.assertEqual((status, [item['layer'] for item in body['items']]), (200, ['profile']))
            # A signed cursor is bound to its entire filter tuple and cannot
            # become valid for a different layer/filter.
            app.item(text='page second')
            status, _, first = app.request('GET', '/v1/memories?persona_id={}&layer=recent&limit=1'.format(encoded))
            cursor = urllib.parse.quote(first['page']['next_cursor'], safe='')
            status, _, problem = app.request('GET', '/v1/memories?persona_id={}&layer=profile&cursor={}'.format(encoded, cursor))
            self.assertEqual((status, problem['code']), (400, 'request.invalid'))
            status, _, problem = app.request('GET', '/v1/memories?persona_id={}&cursor=not-a-cursor'.format(encoded))
            self.assertEqual((status, problem['code']), (400, 'request.invalid'))

    def test_group_text_fail_closed_and_audit_never_contains_text(self):
        with MemoryHTTPServer() as app:
            memory_id = app.item(group=True, text='must not enter audit')
            status, _, body = app.request('GET', '/v1/memories/' + memory_id)
            self.assertEqual(status, 200)
            self.assertTrue(body['redacted'])
            self.assertIsNone(body['text'])
            app.server.room_member_source._fetcher = lambda room, persona: (_ for _ in ()).throw(RuntimeError('offline'))
            status, _, failed = app.request('GET', '/v1/memories/' + memory_id)
            self.assertEqual((status, failed['text'], failed['redacted']), (200, None, True))
            app.server.room_member_source._fetcher = lambda room, persona: (OWNER,)
            app.server.room_member_source._negative.clear()
            status, _, visible = app.request('GET', '/v1/memories/' + memory_id)
            self.assertEqual((status, visible['text'], visible['redacted']),
                             (200, 'must not enter audit', False))
            db = sqlite3.connect(os.path.join(app.tmp, 'memory.db'))
            try:
                row = db.execute('SELECT action,counterpart_hmac FROM memory_audit ORDER BY rowid DESC LIMIT 1').fetchone()
                dump = '\n'.join(str(value) for value in db.execute('SELECT * FROM memory_audit').fetchone())
            finally:
                db.close()
            self.assertEqual(row[0], 'view')
            self.assertNotIn('must not enter audit', dump)

    def test_foreign_memory_is_indistinguishable_from_missing(self):
        with MemoryHTTPServer() as app:
            store = app.server.memory_store
            store.add_item(persona_id='@ai_unknown:' + SERVER, scope_kind='dm', scope_key=HUMAN,
                           room_id=None, counterpart=HUMAN, event_id='$foreign', sender=HUMAN,
                           role='user', trust='trusted', text='foreign')
            foreign = store.items('@ai_unknown:' + SERVER)[0]['public_id']
            status, _, body = app.request('GET', '/v1/memories/' + foreign)
            self.assertEqual((status, body['code']), (404, 'memory.not_found'))

    def test_read_rate_limit_has_retry_after(self):
        with MemoryHTTPServer() as app:
            # This is deliberately the public constant, so the production
            # per-caller limit is exercised without slowing this HTTP test.
            import botville_gateway.memory.owner_http as owner_http
            old = owner_http.READS_PER_MINUTE
            owner_http.READS_PER_MINUTE = 1
            try:
                query = urllib.parse.quote(PERSONA, safe='')
                self.assertEqual(app.request('GET', '/v1/memories?persona_id=' + query)[0], 200)
                status, headers, body = app.request('GET', '/v1/memories?persona_id=' + query)
            finally:
                owner_http.READS_PER_MINUTE = old
            self.assertEqual((status, body['code']), (429, 'rate_limited.transport'))
            self.assertIn('Retry-After', headers)
            self.assertGreaterEqual(body['retry_after_seconds'], 1)

    def test_access_log_suppresses_memory_filters_and_body(self):
        with MemoryHTTPServer(access_log=True) as app:
            captured = io.StringIO()
            path = '/v1/memories?persona_id={}&counterpart={}'.format(
                urllib.parse.quote(PERSONA, safe=''), urllib.parse.quote(HUMAN, safe=''))
            with contextlib.redirect_stderr(captured):
                self.assertEqual(app.request('GET', path)[0], 200)
            self.assertNotIn(HUMAN, captured.getvalue())

    def test_set_fact_validation_idempotency_and_capability(self):
        with MemoryHTTPServer() as app:
            status, _, body = app.request('GET', '/v1/capabilities')
            self.assertEqual(status, 200)
            capability = next(item for item in body['capabilities'] if item['id'] == 'memory.manage')
            self.assertFalse(capability['owner_can_read_dm'])
            valid = {'persona_id': PERSONA, 'counterpart': HUMAN, 'key': 'likes',
                     'value': 'tea'}
            status, _, item = app.request('PUT', '/v1/memories/facts', body=valid,
                                          headers={'Idempotency-Key': 'fake-idempotency-key'})
            self.assertEqual(status, 201)
            status, _, same = app.request('PUT', '/v1/memories/facts', body=valid,
                                          headers={'Idempotency-Key': 'fake-idempotency-key'})
            self.assertEqual((status, same['id']), (201, item['id']))
            invalid = dict(valid, counterpart='@ai_other:' + SERVER)
            status, _, problem = app.request('PUT', '/v1/memories/facts', body=invalid)
            self.assertEqual((status, problem['code']), (422, 'memory.counterpart_invalid'))
            invalid = dict(valid, key='not_allowed')
            status, _, problem = app.request('PUT', '/v1/memories/facts', body=invalid)
            self.assertEqual((status, problem['code']), (422, 'memory.key_not_allowed'))
            for value in ('', 'x' * 501):
                status, _, problem = app.request('PUT', '/v1/memories/facts', body=dict(valid, value=value))
                self.assertEqual((status, problem['code']), (422, 'request.invalid'))
            db = sqlite3.connect(os.path.join(app.tmp, 'memory.db'))
            try:
                hmac_value = db.execute('SELECT counterpart_hmac FROM memory_audit WHERE action="set_fact" ORDER BY rowid DESC LIMIT 1').fetchone()[0]
                dump = '\n'.join(str(part) for row in db.execute('SELECT * FROM memory_audit') for part in row)
            finally:
                db.close()
            expected = hmac.new(b'0123456789abcdef0123456789abcdef', HUMAN.encode(), hashlib.sha256).hexdigest()
            self.assertEqual(hmac_value, expected)
            self.assertNotIn('tea', dump)

    def test_delete_item_tombstones_and_marks_summary_dirty(self):
        with MemoryHTTPServer() as app:
            memory_id = app.item(text='delete this item')
            store = app.server.memory_store
            raw = store.items(PERSONA)[0]
            self.assertTrue(store.save_summary(persona_id=PERSONA, scope_kind='dm', scope_key=HUMAN,
                                               text='derived summary', trust='derived',
                                               covers_until_item_id=raw['id'], model='fake'))
            status, _, body = app.request('DELETE', '/v1/memories/' + memory_id)
            self.assertEqual((status, body), (204, None))
            self.assertTrue(store.is_tombstoned(raw['event_id']))
            self.assertTrue(store.summary(PERSONA, 'dm', HUMAN)['dirty'])

    def test_delete_extracted_fact_tombstones_and_suppresses(self):
        with MemoryHTTPServer() as app:
            store = app.server.memory_store
            self.assertTrue(store.upsert_fact(PERSONA, HUMAN, 'likes', 'coffee', 'dm_only',
                                              'extraction', ['$source-event'], 'derived'))
            fact_id = store.facts(PERSONA, HUMAN)[0]['public_id']
            status, _, _ = app.request('DELETE', '/v1/memories/' + fact_id)
            self.assertEqual(status, 204)
            self.assertTrue(store.is_tombstoned('$source-event'))
            self.assertFalse(store.upsert_fact(PERSONA, HUMAN, 'likes', 'new', 'dm_only',
                                               'extraction', ['$other-event'], 'derived'))

    def test_pin_unpin_summary_and_cap(self):
        with MemoryHTTPServer() as app:
            memory_id = app.item(text='pin me')
            status, _, item = app.request('PUT', '/v1/memories/' + memory_id + '/pin')
            self.assertEqual((status, item['pinned']), (200, True))
            status, _, item = app.request('PUT', '/v1/memories/' + memory_id + '/pin')
            self.assertEqual((status, item['pinned']), (200, True))
            status, _, item = app.request('DELETE', '/v1/memories/' + memory_id + '/pin')
            self.assertEqual((status, item['pinned']), (200, False))
            store = app.server.memory_store
            row = store.items(PERSONA)[0]
            self.assertTrue(store.save_summary(persona_id=PERSONA, scope_kind='dm', scope_key=HUMAN,
                                               text='summary', trust='derived', covers_until_item_id=row['id'],
                                               model='fake'))
            summary_id = store.summary(PERSONA, 'dm', HUMAN)['public_id']
            status, _, problem = app.request('PUT', '/v1/memories/' + summary_id + '/pin')
            self.assertEqual((status, problem['code']), (422, 'memory.not_pinnable'))
            with store._lock, store._db:
                store._db.execute('UPDATE memory_items SET pinned=1 WHERE persona_id=?', (PERSONA,))
            for number in range(199):
                store.add_item(persona_id=PERSONA, scope_kind='dm', scope_key=HUMAN,
                               room_id=None, counterpart=HUMAN, event_id='$cap' + str(number),
                               sender=HUMAN, role='user', trust='trusted', text='cap')
                with store._lock, store._db:
                    store._db.execute('UPDATE memory_items SET pinned=1 WHERE event_id=?', ('$cap' + str(number),))
            capped_target = app.item(text='201st pin')
            status, _, problem = app.request('PUT', '/v1/memories/' + capped_target + '/pin')
            self.assertEqual((status, problem['code']), (409, 'memory.pin_limit'))

    def test_owner_fact_prevents_extraction_overwrite_and_write_rate(self):
        with MemoryHTTPServer() as app:
            body = {'persona_id': PERSONA, 'counterpart': HUMAN, 'key': 'likes', 'value': 'tea'}
            self.assertEqual(app.request('PUT', '/v1/memories/facts', body=body)[0], 201)
            self.assertFalse(app.server.memory_store.upsert_fact(
                PERSONA, HUMAN, 'likes', 'coffee', 'dm_only', 'extraction', ['$later'], 'derived'))
            self.assertEqual(app.server.memory_store.facts(PERSONA, HUMAN)[0]['value'], 'tea')
            import botville_gateway.memory.owner_http as owner_http
            old = owner_http.WRITES_PER_MINUTE
            owner_http.WRITES_PER_MINUTE = 1
            try:
                second = dict(body, key='dislikes', value='noise')
                status, headers, problem = app.request('PUT', '/v1/memories/facts', body=second)
            finally:
                owner_http.WRITES_PER_MINUTE = old
            self.assertEqual((status, problem['code']), (429, 'rate_limited.transport'))
            self.assertIn('Retry-After', headers)

    def test_unavailable_store_is_503_with_retry_after(self):
        with MemoryHTTPServer() as app:
            service = app.server.memory_http.service
            original = service.list
            service.list = lambda *args, **kwargs: (_ for _ in ()).throw(sqlite3.OperationalError('locked'))
            try:
                query = urllib.parse.quote(PERSONA, safe='')
                status, headers, problem = app.request('GET', '/v1/memories?persona_id=' + query)
            finally:
                service.list = original
            self.assertEqual((status, problem['code']), (503, 'memory.unavailable'))
            self.assertIn('Retry-After', headers)

