# SPDX-License-Identifier: Apache-2.0
"""HTTP acceptance tests for the owner relation boundary."""
from __future__ import annotations

import json
import contextlib
import io
import sqlite3
from types import SimpleNamespace
from urllib.parse import quote
import unittest
from unittest.mock import PropertyMock, patch

from test_group_rooms_api import MemoryHTTPServer
from test_memory_owner_http import (OWNER, OTHER, OTHER_PERSONA, PERSONA,
                                    SERVER)


PATH = '/v1/personas/' + quote(PERSONA, safe='') + '/relations'
OTHER_PATH = '/v1/personas/' + quote(OTHER_PERSONA, safe='') + '/relations'
HUMAN = '@human:' + SERVER


class RelationsAPITests(unittest.TestCase):
    def create(self, app, counterpart=HUMAN, **kwargs):
        return app.request('POST', PATH, body={'counterpart_id': counterpart}, **kwargs)

    def test_owner_visibility_and_unavailable(self):
        with MemoryHTTPServer(two_owners=True) as app:
            status, _, problem = app.request('GET', PATH, token='bad')
            self.assertEqual((status, problem['code']), (401, 'auth.invalid_token'))
            status, _, problem = app.request('GET', PATH, token='other-token')
            self.assertEqual((status, problem['code']), (404, 'persona.not_found'))
            status, _, problem = app.request('GET', OTHER_PATH)
            self.assertEqual((status, problem['code']), (404, 'persona.not_found'))
            status, _, problem = app.request('GET', PATH, token='x' * 32, scheme='Bearer')
            self.assertEqual((status, problem['code']), (403, 'persona.not_owned'))
        with MemoryHTTPServer(enabled=False) as app:
            status, headers, problem = app.request('GET', PATH)
            self.assertEqual((status, problem['code']), (503, 'relation.unavailable'))
            self.assertIn('Retry-After', headers)

    def test_auth_then_method_then_availability(self):
        for missing in ('database', 'key'):
            with self.subTest(missing=missing), MemoryHTTPServer(enabled=missing != 'database') as app:
                if missing == 'key':
                    app.server.memory_store._hmac_secret = b''
                for path, allow in ((PATH, 'GET, HEAD, POST'),
                                    (PATH + '/' + 'a' * 64, 'GET, HEAD'),
                                    (PATH + '/' + 'a' * 64 + '/block', 'POST')):
                    status, headers, problem = app.request('DELETE', path, token='bad')
                    self.assertEqual((status, problem['code']), (401, 'auth.invalid_token'))
                    self.assertNotIn('Allow', headers)
                    status, headers, problem = app.request('DELETE', path)
                    self.assertEqual((status, problem['code'], headers['Allow']),
                                     (405, 'request.invalid', allow))
                status, headers, problem = app.request('GET', PATH)
                self.assertEqual((status, problem['code']), (503, 'relation.unavailable'))
                self.assertIn('Retry-After', headers)

    def test_human_create_get_and_paging_validation(self):
        with MemoryHTTPServer() as app:
            status, _, item = self.create(app)
            self.assertEqual(status, 201)
            self.assertEqual((item['counterpart']['user_id'], item['counterpart']['kind'], item['state']),
                             (HUMAN, 'human', 'friend'))
            status, _, page = app.request('GET', PATH + '?limit=1')
            self.assertEqual((status, page['items']), (200, [item]))
            self.assertEqual(app.request('GET', PATH + '?limit=0')[0], 400)
            self.assertEqual(app.request('GET', PATH + '?bogus=x')[0], 400)
            self.assertEqual(app.request('GET', PATH + '?state=friend&state=pending')[0], 400)
            self.assertEqual(app.request('GET', PATH + '?state=nope')[0], 400)
            self.assertEqual(app.request('GET', PATH + '?direction=nope')[0], 400)
            self.assertEqual(app.request('GET', PATH + '?kind=nope')[0], 400)
            self.assertEqual(app.request('GET', PATH + '?cursor=%%')[0], 400)
            status, _, got = app.request('GET', PATH + '/' + item['relation_id'])
            self.assertEqual((status, got), (200, item))
            self.assertEqual(app.request('GET', PATH + '/bad')[0], 404)

    def test_filters_paging_head_and_other_persona_item_hidden(self):
        with MemoryHTTPServer(two_owners=True) as app:
            self.create(app, '@one:' + SERVER)
            self.create(app, '@two:' + SERVER)
            _, _, page = app.request('GET', PATH + '?state=friend&kind=human&limit=1')
            self.assertEqual((len(page['items']), page['has_more']), (1, True))
            next_page = app.request('GET', PATH + '?cursor=' + page['next_cursor'])[2]
            self.assertEqual(len(next_page['items']), 1)
            self.assertIsNone(app.request('HEAD', PATH)[2])
            foreign = app.request('GET', OTHER_PATH + '/' + page['items'][0]['relation_id'],
                                  token='other-token')
            self.assertEqual((foreign[0], foreign[2]['code']), (404, 'relation.not_found'))

    def test_ai_pending_approval_actions_and_revision(self):
        with MemoryHTTPServer(two_owners=True) as app:
            status, _, outgoing = self.create(app, OTHER_PERSONA)
            self.assertEqual((status, outgoing['state'], outgoing['direction']),
                             (201, 'pending', 'outgoing'))
            status, _, incoming_page = app.request('GET', OTHER_PATH, token='other-token')
            incoming = incoming_page['items'][0]
            self.assertEqual((status, incoming['direction']), (200, 'incoming'))
            target = OTHER_PATH + '/' + incoming['relation_id'] + '/approve'
            self.assertEqual(app.request('POST', target, token='other-token', body={})[0], 400)
            self.assertEqual(app.request('POST', target, token='other-token',
                                         body={'revision': incoming['revision'] + 1})[0], 409)
            status, _, friend = app.request('POST', target, token='other-token',
                                            body={'revision': incoming['revision']})
            self.assertEqual((status, friend['state'], friend['direction']), (200, 'friend', 'none'))

    def test_405_allow_and_idempotency_replay(self):
        with MemoryHTTPServer() as app:
            status, headers, problem = app.request('DELETE', PATH)
            self.assertEqual((status, problem['code'], headers['Allow']),
                             (405, 'request.invalid', 'GET, HEAD, POST'))
            _, _, item = self.create(app)
            item_url = PATH + '/' + item['relation_id']
            status, headers, _ = app.request('POST', item_url, body={})
            self.assertEqual((status, headers['Allow']), (405, 'GET, HEAD'))
            status, headers, _ = app.request('GET', item_url + '/revoke')
            self.assertEqual((status, headers['Allow']), (405, 'POST'))
            key = 'relation-idempotency-key'
            first = self.create(app, headers={'Idempotency-Key': key})
            again = self.create(app, headers={'Idempotency-Key': key})
            self.assertEqual(first[0:3:2], again[0:3:2])
            status, _, problem = self.create(app, '@changed:' + SERVER,
                                             headers={'Idempotency-Key': key})
            self.assertEqual((status, problem['code']), (409, 'idempotency.conflict'))

    def test_failed_auth_never_discloses_allow(self):
        with MemoryHTTPServer() as app:
            ref = 'a' * 64
            for method, url, body in (
                    ('DELETE', PATH, None), ('POST', PATH + '/' + ref, {}),
                    ('GET', PATH + '/' + ref + '/revoke', None)):
                status, headers, _ = app.request(method, url, token='bad', body=body)
                self.assertEqual(status, 401)
                self.assertNotIn('Allow', headers)

    def test_action_replay_and_keys_are_owner_scoped(self):
        with MemoryHTTPServer(two_owners=True) as app:
            _, _, item = self.create(app)
            url = PATH + '/' + item['relation_id'] + '/revoke'
            key = 'revoke-idempotency-key'
            first = app.request('POST', url, body={'revision': item['revision']},
                                headers={'Idempotency-Key': key})
            replay = app.request('POST', url, body={'revision': item['revision']},
                                 headers={'Idempotency-Key': key})
            self.assertEqual(first[0:3:2], replay[0:3:2])
            other = app.request('POST', OTHER_PATH, token='other-token',
                                body={'counterpart_id': HUMAN}, headers={'Idempotency-Key': key})
            self.assertEqual(other[0], 201)

    def test_service_error_and_missing_hmac_are_unavailable(self):
        with MemoryHTTPServer() as app:
            app.server.memory_store._hmac_secret = b''
            status, headers, problem = app.request('GET', PATH)
            self.assertEqual((status, problem['code']), (503, 'relation.unavailable'))
            self.assertEqual(headers['Retry-After'], '5')
        with MemoryHTTPServer() as app:
            with patch('botville_gateway.relations.Relations.available',
                       new_callable=PropertyMock, side_effect=sqlite3.OperationalError):
                status, headers, problem = app.request('GET', PATH)
            self.assertEqual((status, problem['code']), (503, 'relation.unavailable'))
            self.assertEqual(headers['Retry-After'], '5')

    def test_unexpected_service_error_is_internal_error(self):
        with MemoryHTTPServer() as app:
            with patch('botville_gateway.relations.Relations.available',
                       new_callable=PropertyMock, side_effect=RuntimeError):
                status, headers, problem = app.request('GET', PATH)
            self.assertEqual((status, problem['code']), (500, 'internal.error'))
            self.assertNotIn('Retry-After', headers)
            self.assertNotIn('retry_after_seconds', problem)

    def test_rate_limit_runs_after_owner_hiding(self):
        from botville_gateway.memory import owner_http
        old = owner_http.READS_PER_MINUTE
        owner_http.READS_PER_MINUTE = 1
        try:
            with MemoryHTTPServer(two_owners=True) as app:
                self.assertEqual(app.request('GET', PATH, token='other-token')[0], 404)
                self.assertEqual(app.request('GET', PATH)[0], 200)
                self.assertEqual(app.request('GET', PATH)[2]['code'], 'rate_limited.transport')
        finally:
            owner_http.READS_PER_MINUTE = old

    def test_access_log_does_not_emit_relation_identifiers(self):
        with MemoryHTTPServer(access_log=True) as app:
            _, _, item = self.create(app)
            stream = io.StringIO()
            with contextlib.redirect_stderr(stream):
                app.request('GET', PATH + '/' + item['relation_id'])
            self.assertNotIn(HUMAN, stream.getvalue())
            self.assertNotIn(item['relation_id'], stream.getvalue())

    def test_capability_states_and_social_filters(self):
        with MemoryHTTPServer(enabled=False) as app:
            disabled = app.request('GET', '/v1/capabilities?module=social')[2]['capabilities']
            self.assertEqual((len(disabled), disabled[0]['status'], disabled[0]['unavailable_reason']),
                             (1, 'disabled', 'module_disabled'))
            self.assertEqual(app.request('GET', '/v1/capabilities?module=social&status=enabled')[2]
                             ['capabilities'], [])
        with MemoryHTTPServer() as app:
            app.server.memory_store._hmac_secret = b''
            disabled = app.request('GET', '/v1/capabilities?module=social&status=disabled')[2]
            self.assertEqual(disabled['capabilities'][0]['id'], 'relations.manage')

    def test_write_rate_is_shared_with_memory(self):
        from botville_gateway.memory import owner_http
        old = owner_http.WRITES_PER_MINUTE
        owner_http.WRITES_PER_MINUTE = 1
        try:
            with MemoryHTTPServer() as app:
                status, _, _ = app.request('PUT', '/v1/memories/facts', body={
                    'persona_id': PERSONA, 'counterpart': HUMAN, 'key': 'name',
                    'value': 'test', 'visibility': 'dm_only'})
                self.assertEqual(status, 201)
                status, _, problem = self.create(app)
                self.assertEqual((status, problem['code']), (429, 'rate_limited.transport'))
        finally:
            owner_http.WRITES_PER_MINUTE = old

    def test_idempotency_expiry_and_replay_keep_first_object(self):
        from botville_gateway.memory.owner_http import IDEMPOTENCY_TTL
        from botville_gateway.relations_http import _idem_key
        import time
        with MemoryHTTPServer() as app:
            key = 'relation-object-replay'
            _, _, first = self.create(app, headers={'Idempotency-Key': key})
            revoked = app.request('POST', PATH + '/' + first['relation_id'] + '/revoke',
                                  body={'revision': first['revision']})[2]
            self.assertEqual(revoked['state'], 'stranger')
            replay = self.create(app, headers={'Idempotency-Key': key})[2]
            self.assertEqual(replay, first)
            cache_key = _idem_key(app.server.memory_http, OWNER, key)
            cached = app.server.memory_http.idempotency[cache_key]
            app.server.memory_http.idempotency[cache_key] = (time.monotonic() - IDEMPOTENCY_TTL - 1,
                                                              *cached[1:])
            fresh = self.create(app, headers={'Idempotency-Key': key})[2]
            self.assertNotEqual(fresh['revision'], first['revision'])

    def test_idempotency_key_is_shared_with_memory_resource(self):
        from botville_gateway.relations_http import _idem_key

        with MemoryHTTPServer() as app:
            state = app.server.memory_http
            fact = {'persona_id': PERSONA, 'counterpart': HUMAN, 'key': 'name',
                    'value': 'test', 'visibility': 'dm_only'}
            memory_key = 'memory-first-idempotency'
            status, _, _ = app.request('PUT', '/v1/memories/facts', body=fact,
                                       headers={'Idempotency-Key': memory_key})
            self.assertEqual(status, 201)
            memory_tag, digest = state.idempotency[_idem_key(state, OWNER, memory_key)][1]
            self.assertEqual(memory_tag, 'memory')
            # Even identical digests must not replay a different resource's response.
            with patch('botville_gateway.relations_http._fingerprint', return_value=digest):
                status, _, problem = self.create(app, headers={'Idempotency-Key': memory_key})
            self.assertEqual((status, problem['code']), (409, 'idempotency.conflict'))

            relations_key = 'relations-first-idempotency'
            status, _, _ = self.create(app, headers={'Idempotency-Key': relations_key})
            self.assertEqual(status, 201)
            relations_tag, _ = state.idempotency[_idem_key(state, OWNER, relations_key)][1]
            self.assertEqual(relations_tag, 'relations')
            status, _, problem = app.request('PUT', '/v1/memories/facts', body=fact,
                                             headers={'Idempotency-Key': relations_key})
            self.assertEqual((status, problem['code']), (409, 'idempotency.conflict'))

    def test_relation_defaults_patch_and_owner_detail(self):
        persona_path = '/v1/personas/' + quote(PERSONA, safe='')
        with MemoryHTTPServer() as app:
            status, _, detail = app.request('PATCH', persona_path,
                body={'relation_defaults': {'friend_request_policy': 'autonomous'}})
            self.assertEqual((status, detail['relation_defaults']),
                             (200, {'friend_request_policy': 'autonomous'}))
            self.assertEqual(app.server.memory_personas[PERSONA].relation_defaults,
                             {'friend_request_policy': 'autonomous'})
            self.assertEqual(app.request('PATCH', persona_path,
                             body={'relation_defaults': {'private_chat': 'x'}})[0], 400)
            self.assertEqual(app.request('PATCH', persona_path,
                             body={'relation_defaults': {'friend_request_policy': 'bad'}})[0], 400)
            status, _, combined = app.request('PATCH', persona_path, body={
                'groups': {'enabled': True, 'invite_policy': 'owner'},
                'relation_defaults': {'friend_request_policy': 'approval_required'}})
            self.assertEqual((status, combined['groups']['enabled'],
                              combined['relation_defaults']['friend_request_policy']),
                             (200, True, 'approval_required'))

    def test_patch_updates_engine_and_next_relation_policy_and_detects_file_race(self):
        other_path = '/v1/personas/' + quote(OTHER_PERSONA, safe='')
        with MemoryHTTPServer(two_owners=True) as app:
            app.server.engine_hooks = SimpleNamespace(_personas=dict(app.server.memory_personas))
            status, _, detail = app.request('PATCH', other_path, token='other-token', body={
                'relation_defaults': {'friend_request_policy': 'autonomous'}})
            self.assertEqual((status, detail['relation_defaults']['friend_request_policy']),
                             (200, 'autonomous'))
            self.assertEqual(app.server.engine_hooks._personas[OTHER_PERSONA].relation_defaults,
                             {'friend_request_policy': 'autonomous'})
            status, _, relation = self.create(app, OTHER_PERSONA)
            self.assertEqual((status, relation['state']), (201, 'friend'))

        with MemoryHTTPServer() as app:
            import botville_gateway.personas_update as updates
            actual = updates.load_personas
            calls = []
            def raced(path, **kwargs):
                result = actual(path, **kwargs)
                calls.append(path)
                if len(calls) == 1:
                    with open(app.server.config.personas_file, 'a', encoding='utf-8') as source:
                        source.write(' ')
                return result
            with patch.object(updates, 'load_personas', side_effect=raced):
                status, _, problem = app.request('PATCH', '/v1/personas/' + quote(PERSONA, safe=''), body={
                    'relation_defaults': {'friend_request_policy': 'autonomous'}})
            self.assertEqual((status, problem['code']), (409, 'persona.state_conflict'))

    def test_loader_rejects_invalid_relation_defaults(self):
        from botville_gateway.engine.personas import PersonaConfigError, load_personas
        with MemoryHTTPServer() as app:
            with open(app.server.config.personas_file, 'w', encoding='utf-8') as source:
                json.dump({'personas': [{'user_id': PERSONA, 'system_prompt': 'x',
                    'owner_user_id': OWNER, 'relation_defaults': {'unknown': True}}]}, source)
            with self.assertRaises(PersonaConfigError):
                load_personas(app.server.config.personas_file, server_name=SERVER)


if __name__ == '__main__':
    unittest.main()
