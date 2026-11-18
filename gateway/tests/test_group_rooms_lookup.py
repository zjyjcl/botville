# SPDX-License-Identifier: Apache-2.0
"""B2.8f real HTTP lookup, privacy, shared read quotas and manifest contract."""
import contextlib
import io
import json
import logging
from pathlib import Path
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock, patch
from urllib.parse import quote

from test_group_rooms_api import MemoryHTTPServer, seed, PATH, ROOM
from test_memory_owner_http import PERSONA, OWNER, OTHER_PERSONA, SERVER
from botville_gateway.capability_manifest import GROUPS_MANAGE
from botville_gateway.config import CONTRACT_VERSION
from botville_gateway.group_rooms_http import lookup_rooms

LOOKUP = PATH + '/rooms/lookup'


class LookupTests(unittest.TestCase):
    def lookup(self, app, rooms, **kwargs):
        result = app.request('POST', LOOKUP, body={'room_ids': rooms}, **kwargs)
        for room in rooms:
            if isinstance(room, str) and room:
                self.assertNotIn(room, json.dumps(result[2]))
        return result

    def raw(self, app, body, token='owner-token', method='POST'):
        headers = {'Content-Type': 'application/json'}
        if token is not None:
            headers['Authorization'] = ('Bearer ' if token == 'x' * 32 else 'MatrixOpenID ') + token
        request = urllib.request.Request(app.base + LOOKUP, data=body,
                                         headers=headers, method=method)
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            data = response.read()
            return response.status, dict(response.headers), json.loads(data) if data else None

    def test_hits_misses_order_all_states_and_patch(self):
        with MemoryHTTPServer() as app:
            rooms = ['!room-' + state + ':' + SERVER for state in
                     ('accepted', 'left', 'encrypted_disabled', 'rejected_encrypted')]
            refs = [seed(app, state=state, room=room) for state, room in zip(
                ('accepted', 'left', 'encrypted_disabled', 'rejected_encrypted'), rooms)]
            listed = app.request('GET', PATH + '/rooms?include=all')[2]['items']
            by_ref = {item['room_ref']: item for item in listed}
            missing = '!missing:' + SERVER
            for requested in ([rooms[0]], [missing], [rooms[3], missing, rooms[1], rooms[0], rooms[2]]):
                status, _, result = self.lookup(app, requested)
                self.assertEqual(status, 200)
                self.assertTrue(result['groups_enabled'])
                self.assertEqual(result['items'], [None if room == missing else
                    by_ref[refs[rooms.index(room)]] for room in requested])
            ref = self.lookup(app, [rooms[0]])[2]['items'][0]['room_ref']
            status, _, changed = app.request('PATCH', PATH + '/rooms/' + ref, body={'mode': 'muted'})
            self.assertEqual((status, changed['mode']), (200, 'muted'))
            self.assertEqual(self.lookup(app, [rooms[0]])[2]['items'], [changed])

    def test_old_key_and_other_persona_are_not_visible(self):
        with MemoryHTTPServer(two_owners=True) as app:
            ref = seed(app)
            store = app.server.memory_store
            with store._lock, store._db:
                store._db.execute('UPDATE group_rooms SET key_id=? WHERE room_key=?', ('old-test-key', ref))
            self.assertEqual(self.lookup(app, [ROOM])[2]['items'], [None])
            foreign = '!other-owner:' + SERVER
            app.server.group_invites._record(OTHER_PERSONA, foreign, '@other:' + SERVER, 'accepted')
            self.assertEqual(self.lookup(app, [foreign])[2]['items'], [None])

    def test_disabled_and_no_db_validate_before_empty_response(self):
        for has_db in (True, False):
            with self.subTest(has_db=has_db), MemoryHTTPServer(enabled=has_db) as app:
                expected = None
                if has_db:
                    seed(app)
                    expected = self.lookup(app, [ROOM])[2]['items'][0]
                app.server.group_invites.enabled = False
                self.assertEqual(self.lookup(app, [ROOM])[2],
                                 {'items': [expected], 'groups_enabled': False})
                self.assertEqual(self.lookup(app, ['!cross:remote.invalid'])[0], 400)

    def test_invalid_inputs_never_reflected(self):
        bad_rooms = [[], ['!n%d:%s' % (i, SERVER) for i in range(51)], [ROOM, ROOM],
                     [ROOM, ROOM.rsplit(':', 1)[0] + ':' + SERVER.upper()]]
        for value in (123, None, {}, '!x:remote.invalid', '#alias:' + SERVER, 'missing-colon', '',
                      '!:' + SERVER, '!bad space:' + SERVER, '!bad\n:' + SERVER,
                      '!bad\x00:' + SERVER, '!bad\x7f:' + SERVER, '!bad\x85:' + SERVER,
                      '!bad/: ' + SERVER, '!bad/:' + SERVER, '!bad@:' + SERVER,
                      '!bad%:' + SERVER, '!中文:' + SERVER, '!x:' + SERVER + '\n',
                      '!' + 'x' * 256 + ':' + SERVER):
            bad_rooms.append([value])
        bodies = [{'room_ids': rooms} for rooms in bad_rooms]
        bodies += [{'room_ids': [ROOM], 'extra': ROOM}, [], 'not an object', {},
                   {'room_ids': ROOM}, {'room_ids': None}, {'room_ids': {}}]
        with MemoryHTTPServer() as app:
            for body in bodies:
                with self.subTest(body=body):
                    status, _, result = app.request('POST', LOOKUP, body=body)
                    self.assertEqual((status, result['code'], result['title']),
                                     (400, 'request.invalid', 'Room request rejected'))
                    self.assertNotIn(ROOM, json.dumps(result))
                    for value in body.get('room_ids', []) if isinstance(body, dict) and isinstance(body.get('room_ids'), list) else []:
                        if isinstance(value, str) and value:
                            self.assertNotIn(value, json.dumps(result))
            for raw in (b'null', b'{not json', ('{"room_ids": ["' + ROOM).encode()):
                status, _, result = self.raw(app, raw)
                self.assertEqual((status, result['code'], result['title']),
                                 (400, 'request.invalid', 'Room request rejected'))
                self.assertNotIn(ROOM, json.dumps(result))

    def test_valid_boundaries_server_case_and_missing_server(self):
        with MemoryHTTPServer() as app:
            groups = app.server.group_invites
            longest = '!' + 'a' * (255 - len(SERVER) - 2) + ':' + SERVER
            valid = ['!Az09._~=-:' + SERVER, longest, '!upper:' + SERVER.upper(),
                     '!case:' + SERVER, '!Case:' + SERVER]
            self.assertEqual(self.lookup(app, valid)[0], 200)
            self.assertEqual(self.lookup(app, ['!n%d:%s' % (i, SERVER) for i in range(50)])[0], 200)
            upper = valid[2]
            seed(app, room=upper)
            self.assertIsNotNone(self.lookup(app, [upper])[2]['items'][0])
            self.assertEqual(self.lookup(app, [upper.lower()])[2]['items'], [None])
            for server_name in (None, ''):
                groups.server_name = server_name
                self.assertEqual(self.lookup(app, [ROOM])[0], 400)

    def test_auth_and_unsupported_methods(self):
        with MemoryHTTPServer() as app:
            for token, expected, code in ((None, 401, 'auth.missing'),
                    ('bad-test-token', 401, 'auth.invalid_token'),
                    ('x' * 32, 403, 'persona.not_owned'), ('other-token', 404, 'persona.not_found')):
                status, headers, result = self.raw(app, json.dumps({'room_ids': [ROOM]}).encode(), token)
                self.assertEqual((status, result['code']), (expected, code))
                self.assertNotIn('Allow', headers)
                self.assertNotIn(ROOM, json.dumps(result))
            status, _, result = app.request('POST', '/v1/personas/' + quote('@unknown:' + SERVER, safe='') + '/rooms/lookup', body={'room_ids': [ROOM]})
            self.assertEqual((status, result['code']), (404, 'persona.not_found'))
            for method in ('GET', 'HEAD', 'PATCH', 'DELETE', 'PUT', 'OPTIONS', 'TRACE'):
                status, headers, result = app.request(method, LOOKUP)
                self.assertEqual((status, headers['Allow']), (405, 'POST'))
                if method != 'HEAD':
                    self.assertEqual(result['code'], 'request.invalid')
                for token, expected in (('other-token', 404), ('bad', 401), ('x' * 32, 403)):
                    status, headers, result = app.request(method, LOOKUP, token=token, scheme='Bearer')
                    self.assertEqual(status, expected)
                    self.assertNotIn('Allow', headers)
                    if method != 'HEAD' and expected == 404:
                        self.assertEqual(result['code'], 'persona.not_found')
            self.assertEqual(app.server.memory_http.rates, {})

    def test_read_quota_is_shared_owner_only_and_patch_still_works(self):
        with MemoryHTTPServer() as app:
            ref = seed(app)
            # Freeze the window so slow CI cannot expire early calls.
            with patch('botville_gateway.memory.owner_http.time.monotonic', return_value=1000):
                for _ in range(61):
                    self.assertEqual(self.lookup(app, [ROOM], token='other-token')[0], 404)
                self.assertEqual(app.server.memory_http.rates, {})
                for _ in range(60):
                    self.assertEqual(self.lookup(app, [ROOM])[0], 200)
                status, headers, result = self.lookup(app, [ROOM])
                self.assertEqual((status, result['code'], result['title']),
                                 (429, 'rate_limited.transport', 'rate_limited.transport'))
                self.assertEqual(int(headers['Retry-After']), result['retry_after_seconds'])
                memory = app.request('GET', '/v1/memories?persona_id=' + quote(PERSONA, safe=''))
                self.assertEqual(memory[0], 429)
                for key in ('code', 'title', 'status', 'retry_after_seconds'):
                    self.assertEqual(memory[2][key], result[key])
                self.assertEqual(app.request('PATCH', PATH + '/rooms/' + ref, body={'mode': 'muted'})[0], 200)

    def test_absent_rate_state_and_no_db_rate_state(self):
        with MemoryHTTPServer(enabled=False) as app:
            self.assertEqual(self.lookup(app, [ROOM])[0], 200)
            self.assertEqual(len(app.server.memory_http.rates[(OWNER, True)]), 1)
            app.server.memory_http = None
            for _ in range(61):
                self.assertEqual(self.lookup(app, [ROOM])[0], 200)

    def test_privacy_logs_audit_no_writes_or_transport(self):
        with MemoryHTTPServer(access_log=True) as app:
            seed(app)
            groups = app.server.group_invites
            groups.transport = Mock()
            store = app.server.memory_store
            before = store._db.total_changes
            captured = io.StringIO()
            statements = []
            store._db.set_trace_callback(statements.append)
            try:
                with self.assertLogs(level='INFO') as logs, contextlib.redirect_stderr(captured):
                    logging.getLogger('botville_gateway').info('lookup privacy test')
                    self.assertEqual(self.lookup(app, [ROOM, '!absent:' + SERVER])[0], 200)
                    self.assertEqual(self.lookup(app, [ROOM, '!invalid:remote.invalid'])[0], 400)
                    with patch.object(groups, '_key', side_effect=RuntimeError(ROOM)):
                        status, _, result = self.lookup(app, [ROOM])
                        self.assertEqual((status, result['code']), (503, 'room.unavailable'))
                output = '\n'.join(logs.output) + captured.getvalue() + '\n'.join(statements)
                for room in (ROOM, '!absent:' + SERVER, '!invalid:remote.invalid'):
                    self.assertNotIn(room, output)
                # Persona routes suppress access logs entirely via log_message.
                self.assertEqual(captured.getvalue(), '')
                self.assertEqual(store._db.total_changes, before)
                self.assertEqual(groups.transport.mock_calls, [])
            finally:
                store._db.set_trace_callback(None)

    def test_batch_uses_one_lock_and_no_transaction_writes(self):
        with MemoryHTTPServer() as app:
            seed(app)
            groups = app.server.group_invites
            original = groups.store._lock
            lock = Mock()
            lock.__enter__ = Mock(side_effect=original.__enter__)
            lock.__exit__ = Mock(side_effect=original.__exit__)
            with patch.object(groups.store, '_lock', lock):
                result = lookup_rooms(groups, PERSONA, {'room_ids': [ROOM, '!missing:' + SERVER]})
            self.assertIsNotNone(result['items'][0])
            lock.__enter__.assert_called_once()
            lock.__exit__.assert_called_once()


class GroupsManifestTests(unittest.TestCase):
    def test_deployment_states_filters_exact_keys_and_persona_independence(self):
        keys = {'id', 'module', 'version', 'title', 'summary', 'icon', 'status', 'entry',
                'scopes', 'room_lookup', 'updated_at'}
        for has_db, enabled, status, reason in ((True, True, 'enabled', None),
                (True, False, 'degraded', 'groups_disabled'),
                (False, False, 'disabled', 'module_disabled')):
            with self.subTest(status=status), MemoryHTTPServer(enabled=has_db) as app:
                app.server.group_invites.enabled = enabled
                for persona_enabled in (False, True):
                    app.server.group_invites.personas[PERSONA].groups['enabled'] = persona_enabled
                    manifest = app.request('GET', '/v1/capabilities')[2]
                    self.assertEqual(manifest['manifest_version'], '1.5.1')
                    capability = next(item for item in manifest['capabilities']
                                      if item['id'] == 'groups.manage')
                    self.assertEqual(set(capability), keys | ({'unavailable_reason'} if reason else set()))
                    expected = dict(GROUPS_MANAGE, status=status)
                    if reason:
                        expected['unavailable_reason'] = reason
                    self.assertEqual(capability, expected)
                    page = app.request('GET', PATH + '/rooms')[2]
                    self.assertEqual(capability['status'] == 'enabled', page['groups_enabled'])
                    for query in ('module=groups', 'module=groups&status=' + status):
                        self.assertEqual(app.request('GET', '/v1/capabilities?' + query)[2]['capabilities'], [expected])
                    for filtered_status in ('enabled', 'degraded', 'disabled'):
                        items = app.request('GET', '/v1/capabilities?status=' + filtered_status)[2]['capabilities']
                        self.assertEqual([item for item in items if item['id'] == 'groups.manage'],
                                         [expected] if filtered_status == status else [])

    def test_full_runtime_example_and_mock_match(self):
        root = Path(__file__).resolve().parents[2]
        example_path = root / 'gateway/openapi/examples/capability-manifest.json'
        example = json.loads(example_path.read_text())
        mock = root / 'app-apple/Sources/BotvilleCore/Resources/mock-capability-manifest.json'
        self.assertEqual(example_path.read_bytes(), mock.read_bytes())
        self.assertEqual(example['contract_version'], CONTRACT_VERSION)
        with MemoryHTTPServer() as app:
            app.server.group_invites.enabled = True
            runtime = app.request('GET', '/v1/capabilities')[2]
            for value in (runtime, example):
                value.pop('generated_at')
            self.assertEqual(runtime, example)
