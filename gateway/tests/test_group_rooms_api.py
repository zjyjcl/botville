# SPDX-License-Identifier: Apache-2.0
"""B2.8d owner HTTP, opaque references, persistence, quiet mode and versions."""
import dataclasses
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
from urllib.parse import quote

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from test_memory_owner_http import MemoryHTTPServer as BaseHTTPServer, PERSONA, OWNER, SERVER, TOKENS
from botville_gateway.caller_identity import CallerAuthenticator
from botville_gateway.config import Config, CONTRACT_VERSION, ConfigError, load_config
from botville_gateway.as_consumer import ConsumerContext
from botville_gateway.engine.engine import EngineHooks
from botville_gateway.engine.personas import load_personas
from botville_gateway.groups import GroupInvites

class MemoryHTTPServer(BaseHTTPServer):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.server.persona_detail_verifier = self.server.user_verifier
        self.server.authenticator = CallerAuthenticator(Config(
            dev_mode=False, auth_token="x" * 32, hmac_secret=b"x" * 32))


PATH = '/v1/personas/' + quote(PERSONA, safe='')
ROOM = '!room-private:' + SERVER


def seed(app, state='accepted', room=ROOM):
    groups = app.server.group_invites
    groups.enabled = True
    groups._record(PERSONA, room, OWNER, state)
    return groups._key('room', room)


class RoomsAPITests(unittest.TestCase):
    def test_identity_matrix(self):
        with MemoryHTTPServer() as app:
            ref = seed(app)
            app.server.authenticator = CallerAuthenticator(Config(
                dev_mode=False, auth_token='x' * 32, hmac_secret=b'x' * 32))
            for method, path, body in [('GET', PATH + '/rooms', None),
                    ('PATCH', PATH + '/rooms/' + ref, {'mode': 'muted'}),
                    ('PATCH', PATH, {'groups': {'enabled': True}})]:
                missing = app.request(method, path, body=body, headers={'Authorization': ''})
                self.assertEqual((missing[0], missing[2]['code']), (401, 'auth.missing'))
                for token, expected, code in [('bad', 401, 'auth.invalid_token'),
                        ('other-token', 404, 'persona.not_found')]:
                    status, _, result = app.request(method, path, body=body, token=token)
                    self.assertEqual((status, result['code']), (expected, code))
                status, _, result = app.request(method, path, body=body, token='x' * 32, scheme='Bearer')
                self.assertEqual((status, result['code']), (403, 'persona.not_owned'))
                with patch.dict(TOKENS, {'remote-token': '@owner:remote.test'}):
                    self.assertEqual(app.request(method, path, body=body, token='remote-token')[0], 404)
            for pid in ('@ai_missing:' + SERVER, '@ai_memory:elsewhere.test', 'invalid'):
                self.assertEqual(app.request('GET', '/v1/personas/' + quote(pid, safe='') + '/rooms')[0], 404)

    def test_list_filter_pagination_privacy_rotation_and_disabled(self):
        with MemoryHTTPServer() as app:
            for i, state in enumerate(('accepted', 'encrypted_disabled', 'left', 'rejected_encrypted')):
                seed(app, state, '!private' + str(i) + ':' + SERVER)
            # Equal timestamps exercise the room_ref tie breaker.
            store = app.server.memory_store
            with store._lock, store._db:
                store._db.execute('UPDATE group_rooms SET updated_at=1')
            page = app.request('GET', PATH + '/rooms')[2]
            self.assertEqual(len(page['items']), 2)
            self.assertNotIn('inviter', json.dumps(page))
            self.assertNotIn('!private', json.dumps(page))
            refs = []
            query = '?include=all&limit=1'
            while True:
                status, _, page = app.request('GET', PATH + '/rooms' + query)
                self.assertEqual(status, 200)
                refs += [r['room_ref'] for r in page['items']]
                if not page['has_more']:
                    self.assertIsNone(page['next_cursor'])
                    break
                query = '?include=all&limit=1&cursor=' + page['next_cursor']
            self.assertEqual(refs, sorted(set(refs)))
            self.assertEqual(len(refs), 4)
            for query in ('?limit=0', '?limit=201', '?limit=a', '?include=no', '?cursor=bad', '?limit=1&limit=2'):
                self.assertEqual(app.request('GET', PATH + '/rooms' + query)[0], 400)
            store._hmac_secret = b'rotated-secret-for-rooms-test-1234'
            self.assertEqual(app.request('GET', PATH + '/rooms?include=all')[2]['items'], [])
            self.assertEqual(app.request('PATCH', PATH + '/rooms/' + refs[0], body={'mode': 'muted'})[0], 404)
            app.server.group_invites.enabled = False
            self.assertEqual(app.request('GET', PATH + '/rooms')[2],
                dict(items=[], has_more=False, next_cursor=None, groups_enabled=False))
        with MemoryHTTPServer(enabled=False) as app:
            self.assertFalse(app.request('GET', PATH + '/rooms')[2]['groups_enabled'])

    def test_mode_validation_state_and_unknown_refs(self):
        with MemoryHTTPServer() as app:
            ref = seed(app)
            path = PATH + '/rooms/' + ref
            for mode in ('muted', 'mention_only'):
                status, _, item = app.request('PATCH', path, body={'mode': mode})
                self.assertEqual((status, item['mode']), (200, mode))
                self.assertTrue(item['updated_at'].endswith('Z'))
            for body in ({}, {'mode': 'other'}, {'mode': True}, {'mode': []}, {'extra': 1}, {'action': 'join'}):
                self.assertEqual(app.request('PATCH', path, body=body)[0], 400)
            self.assertEqual(app.request('PATCH', PATH + '/rooms/' + '0'*64, body={'mode': 'muted'})[0], 404)
            for state in ('left', 'rejected_encrypted'):
                seed(app, state)
                status, _, error = app.request('PATCH', path, body={'mode': 'muted'})
                self.assertEqual((status, error['code']), (409, 'room.state_conflict'))
            seed(app, 'encrypted_disabled')
            self.assertEqual(app.request('PATCH', path, body={'mode': 'muted'})[0], 200)

    def test_persona_groups_persist_and_validate(self):
        with MemoryHTTPServer() as app:
            groups = {'enabled': True, 'invite_policy': 'local_humans'}
            status, _, item = app.request('PATCH', PATH, body={'groups': groups})
            self.assertEqual((status, item['groups']), (200, groups))
            loaded = load_personas(app.server.config.personas_file, server_name=SERVER)
            self.assertEqual(loaded[PERSONA].groups, groups)
            self.assertEqual(app.server.group_invites.personas[PERSONA].groups, groups)
            self.assertNotIn('groups', app.request('GET', PATH, token='other-token')[2])
            for value in (None, [], {'enabled': 1}, {'invite_policy': 'any'}, {'secret': 'value'}):
                self.assertEqual(app.request('PATCH', PATH, body={'groups': value})[0], 400)
            before = Path(app.server.config.personas_file).read_bytes()
            with patch('botville_gateway.personas_update.os.replace', side_effect=OSError):
                self.assertEqual(app.request('PATCH', PATH, body={'groups': {}})[0], 503)
            self.assertEqual(Path(app.server.config.personas_file).read_bytes(), before)
            self.assertEqual(app.server.memory_personas[PERSONA].groups, groups)
            self.assertEqual(app.request('PATCH', PATH, body={'groups': {}})[2]['groups'],
                             {'enabled': False, 'invite_policy': 'owner'})

    def test_muted_retains_group_memory_before_policy(self):
        with MemoryHTTPServer() as app:
            ref = seed(app)
            client, service = Mock(), Mock()
            memory = app.server.memory_store
            client.complete.return_value = 'reply'
            service.evaluate_policy.return_value = {'decision': 'allow'}
            service.send_ai_message.return_value = {'outcome': 'sent'}
            hooks = EngineHooks(personas=app.server.memory_personas, client=client,
                service=service, memory_store=memory, group_invites=app.server.group_invites)
            context = ConsumerContext('human_text', '$private', ROOM, OWNER, 'trusted',
                text='@ai_memory hello', persona_user_id=PERSONA, target_user_ids=(PERSONA,),
                room_ai_user_ids=(PERSONA,), room_members_confirmed=True)
            self.assertEqual(app.request('PATCH', PATH + '/rooms/' + ref, body={'mode': 'muted'})[0], 200)
            hooks.on_human_text(context)
            self.assertEqual(client.method_calls, [])
            service.evaluate_policy.assert_not_called()
            service.send_ai_message.assert_not_called()
            with memory._lock:
                rows = memory._db.execute('SELECT scope_kind FROM memory_items').fetchall()
            self.assertEqual([r[0] for r in rows], ['group'])
            self.assertEqual(app.request('PATCH', PATH + '/rooms/' + ref, body={'mode': 'mention_only'})[0], 200)
            with patch('botville_gateway.memory.recall.assemble_recall', return_value=[]):
                hooks.on_human_text(dataclasses.replace(context, event_id='$second'))
            client.complete.assert_called_once()
            service.send_ai_message.assert_called_once()

    def test_joined_rooms_masquerades_and_rejects_malformed_response(self):
        from botville_gateway.matrix_appservice import AppServiceMatrixTransport
        from botville_gateway.matrix import DeliveryError
        transport = AppServiceMatrixTransport(homeserver_url='http://localhost',
            as_token='private-token', server_name=SERVER)
        transport._request = Mock(return_value={'joined_rooms': [ROOM]})
        self.assertEqual(transport.joined_rooms(PERSONA), [ROOM])
        args, kwargs = transport._request.call_args
        self.assertEqual(args, ('GET', '/_matrix/client/v3/joined_rooms'))
        self.assertEqual(kwargs['query'], {'user_id': PERSONA})
        for payload in ({}, {'joined_rooms': None}, {'joined_rooms': ['invalid']}, {'joined_rooms': [None]}):
            transport._request.return_value = payload
            with self.assertRaises(DeliveryError):
                transport.joined_rooms(PERSONA)

    def test_wrong_persona_and_noncurrent_key_hidden(self):
        with MemoryHTTPServer(two_owners=True) as app:
            from test_memory_owner_http import OTHER_PERSONA, OTHER
            groups = app.server.group_invites
            groups._record(OTHER_PERSONA, ROOM, OTHER, 'accepted')
            ref = groups._key('room', ROOM)
            status, _, error = app.request('PATCH', PATH + '/rooms/' + ref, body={'mode': 'muted'})
            self.assertEqual((status, error['code']), (404, 'room.not_found'))
            seed(app)
            with groups.store._lock, groups.store._db:
                groups.store._db.execute("UPDATE group_rooms SET key_id='stale'")
            self.assertEqual(app.request('PATCH', PATH + '/rooms/' + ref, body={'mode': 'muted'})[0], 404)

    def test_versions_and_env_errors(self):
        root = Path(__file__).resolve().parents[2]
        example = root / 'gateway/openapi/examples/capability-manifest.json'
        mock = root / 'app-apple/Sources/BotvilleCore/Resources/mock-capability-manifest.json'
        self.assertEqual(CONTRACT_VERSION, '0.6.0')
        self.assertEqual(json.loads(example.read_bytes())['contract_version'], CONTRACT_VERSION)
        self.assertEqual(example.read_bytes(), mock.read_bytes())
        with MemoryHTTPServer() as app:
            self.assertEqual(app.request('GET', '/v1/capabilities')[2]['contract_version'], '0.6.0')
        for name, bounds in [('GROUP_MAX_ROOMS_PER_PERSONA', '1–200'),
                ('GROUP_COOLDOWN_SECONDS', '0–3600'), ('PROACTIVE_TICK_SECONDS', '30–86400'),
                ('GROUP_MAX_RESPONDERS', '1–20'), ('MOOD_HALF_LIFE_MINUTES', '1–10080'),
                ('PROACTIVE_MIN_IDLE_MINUTES', '0–43200'), ('PROACTIVE_MIN_INTERVAL_MINUTES', '0–43200'),
                ('PROACTIVE_PER_TARGET_PER_DAY', '0–24'), ('GROUP_MAX_REPLIES_PER_HOUR', '1–200'),
                ('GROUP_AI_LOOP_MAX_PER_HOUR', '1–1000'), ('GROUP_MAX_CONSECUTIVE_AI_MESSAGES', '1–50'),
                ('INVITE_LEDGER_TTL_SECONDS', '60–2592000')]:
            for value in ('private-invalid-value', '-1', '999999999'):
                with self.assertRaises(ConfigError) as caught:
                    load_config({'GATEWAY_DEV_MODE': 'true', 'GATEWAY_' + name: value})
                self.assertEqual(str(caught.exception), 'GATEWAY_' + name + ' must be an integer within ' + bounds)
                self.assertNotIn(value, str(caught.exception))
