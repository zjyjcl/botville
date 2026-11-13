# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""B2.8b invitation, persistence and send gates; standard-library fakes only."""
import dataclasses
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from botville_gateway.as_consumer import ConsumerContext, AsTransactionConsumer
from botville_gateway.config import ConfigError, load_config
from botville_gateway.engine.engine import EngineHooks
from botville_gateway.engine.personas import Persona, PersonaConfigError, load_personas
from botville_gateway.groups import GroupInvites, MAX_RATE_PERSONAS
from botville_gateway.matrix import DeliveryError
from botville_gateway.matrix_appservice import AppServiceMatrixTransport
from botville_gateway.memory.store import MemoryStore, SCHEMA_VERSION

AI = '@ai_test:local.test'
OWNER = '@owner:local.test'
ROOM = '!private:local.test'
SECRET = b'persistent-test-secret-for-groups'


class GroupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = os.path.join(self.temp.name, 'memory.db')
        self.store = MemoryStore(self.path, hmac_secret=SECRET)
        self.addCleanup(self.store.close)
        self.transport = Mock()
        self.transport.room_is_encrypted.return_value = False
        self.persona = Persona(AI, 'instructions', owner_user_id=OWNER,
                               groups={'enabled': True, 'invite_policy': 'owner'})
        self.now = 0
        self.groups = self.make()

    def make(self, **kwargs):
        return GroupInvites(self.store, self.transport, {AI: self.persona},
                            server_name='local.test', clock=lambda: self.now,
                            **dict({'enabled': True}, **kwargs))

    def ctx(self, **kwargs):
        return dataclasses.replace(ConsumerContext('invite_to_ai', '$private', ROOM,
                    OWNER, 'trusted', persona_user_id=AI), **kwargs)

    def invite(self, **kwargs):
        self.groups.on_invite_to_ai(self.ctx(**kwargs))

    def test_membership_departure_and_reinvite(self):
        hooks = EngineHooks(personas={AI: self.persona}, client=Mock(),
                            service=Mock(), group_invites=self.groups)
        consumer = AsTransactionConsumer(Mock(), hooks=hooks, server_name='local.test')
        for index, (membership, sender, encrypted) in enumerate((
                ('leave', OWNER, False), ('ban', OWNER, False),
                ('leave', AI, True), ('ban', OWNER, True))):
            with self.subTest(membership=membership, sender=sender, encrypted=encrypted):
                self.invite(event_id=f'$before{index}')
                if encrypted:
                    self.groups.disable_encrypted(ROOM)
                consumer._dispatch_event({'type': 'm.room.member', 'event_id': '$depart',
                    'room_id': ROOM, 'sender': sender, 'state_key': AI,
                    'content': {'membership': membership}})
                self.assertEqual(self.groups._state(AI, ROOM), 'left')
                tr = AppServiceMatrixTransport(homeserver_url='http://localhost',
                    as_token='private-token', server_name='local.test',
                    accepted_group=self.groups.is_accepted, left_group=self.groups.is_left,
                    disabled_group=self.groups.is_disabled)
                tr.ensure_user = Mock()
                tr.join = Mock()
                error = DeliveryError('M_FORBIDDEN', 'denied')
                tr._request = Mock(side_effect=error)
                with self.assertRaises(DeliveryError) as caught:
                    tr.send_text(room_id=ROOM, sender_id=AI, text='body', allow_auto_join=True)
                self.assertIs(caught.exception, error)
                tr.join.assert_not_called()
                self.assertEqual(tr._request.call_count, 1)
                self.invite(event_id=f'$after{index}')
                self.assertTrue(self.groups.is_accepted(AI, ROOM))

    def test_encrypted_decline_self_leave_is_left(self):
        self.invite(invite_encrypted=True)
        self.groups.on_membership(self.ctx(membership='leave', sender=AI))
        self.assertTrue(self.groups.is_left(AI, ROOM))

    def test_join_crash_pending_replay(self):
        for restart in (False, True):
            with self.subTest(restart=restart):
                event = '$crash' + str(restart)
                with patch.object(self.groups, '_record', side_effect=SystemExit):
                    with self.assertRaises(SystemExit):
                        self.invite(event_id=event)
                row = self.store._db.execute(
                    "SELECT status FROM group_invites WHERE invite_key=?",
                    (self.groups._key('invite', AI, event),)).fetchone()
                self.assertEqual(row[0], 'pending')
                calls = self.transport.join.call_count
                self.invite(event_id=event)
                self.assertEqual(self.transport.join.call_count, calls)
                if restart:
                    self.groups = self.make()
                    self.invite(event_id=event)
                else:
                    with patch('botville_gateway.groups.time.time', return_value=time.time() + 301):
                        self.invite(event_id=event)
                self.assertEqual(self.transport.join.call_count, calls + 1)
                self.assertEqual(self.store._db.execute(
                    "SELECT count(*) FROM group_rooms WHERE state='accepted'").fetchone()[0], 1)
                self.invite(event_id=event)
                self.assertEqual(self.transport.join.call_count, calls + 1)

    def test_crash_after_accepted_before_done_upserts_once(self):
        with patch.object(self.groups, '_done', side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                self.invite()
        self.assertTrue(self.groups.is_accepted(AI, ROOM))
        self.groups = self.make()
        self.invite()
        self.invite()
        self.assertEqual(self.transport.join.call_count, 2)
        self.assertEqual(self.store._db.execute(
            "SELECT count(*) FROM group_rooms WHERE state='accepted'").fetchone()[0], 1)
        self.assertEqual(self.store._db.execute(
            'SELECT status FROM group_invites').fetchone()[0], 'done')

    def test_pending_leave_failure_retries_after_restart(self):
        self.transport.leave.side_effect = RuntimeError('private')
        self.invite(invite_encrypted=True)
        self.assertEqual(self.store._db.execute('SELECT status FROM group_invites').fetchone()[0], 'pending')
        self.transport.leave.side_effect = None
        self.groups = self.make()
        self.invite(invite_encrypted=True)
        self.invite(invite_encrypted=True)
        self.assertEqual(self.transport.leave.call_count, 2)
        self.assertEqual(self.store._db.execute('SELECT status FROM group_invites').fetchone()[0], 'done')

    def test_legacy_schema_ten_claims_remain_done(self):
        self.store._db.executescript("DROP TABLE group_invites; CREATE TABLE group_invites (invite_key TEXT PRIMARY KEY, processed_at REAL NOT NULL);")
        self.store._db.execute('INSERT INTO group_invites VALUES (?,0)',
                               (self.groups._key('invite', AI, '$private'),))
        self.store._db.commit()
        other = MemoryStore(self.path, hmac_secret=SECRET)
        self.addCleanup(other.close)
        groups = GroupInvites(other, self.transport, {AI: self.persona}, enabled=True, server_name='local.test')
        groups.on_invite_to_ai(self.ctx())
        self.transport.join.assert_not_called()

    def test_default_off_noop(self):
        self.groups = self.make(enabled=False)
        self.invite()
        self.assertEqual(self.transport.mock_calls, [])
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM group_invites').fetchone()[0], 0)

    def test_no_db_warning_noop(self):
        with self.assertLogs('botville_gateway.groups', 'WARNING'):
            groups = GroupInvites(None, self.transport, {}, enabled=True)
        groups.on_invite_to_ai(self.ctx())
        self.assertEqual(self.transport.mock_calls, [])

    def test_persona_disabled_declines(self):
        self.persona = dataclasses.replace(self.persona, groups={})
        self.groups = self.make()
        self.invite()
        self.transport.leave.assert_called_once_with(ROOM, AI)
        self.transport.join.assert_not_called()
        self.assertEqual(self.groups.stats.snapshot()['groups_disabled'], 1)

    def test_owner_accepted(self):
        self.invite()
        self.transport.join.assert_called_once_with(ROOM, AI)
        self.transport.room_is_encrypted.assert_called_once_with(ROOM, AI)
        self.assertTrue(self.groups.is_accepted(AI, ROOM))

    def test_nonowner_declined(self):
        self.invite(sender='@other:local.test')
        self.transport.leave.assert_called_once()
        self.transport.join.assert_not_called()

    def test_local_humans(self):
        self.persona = dataclasses.replace(self.persona, groups={'enabled': True, 'invite_policy': 'local_humans'})
        self.groups = self.make()
        self.invite(sender='@other:local.test')
        self.assertTrue(self.groups.is_accepted(AI, ROOM))

    def test_disallowed_senders(self):
        for index, sender in enumerate(('@ai_x:local.test', '@AI_x:local.test',
                '@botville-gateway:local.test', '@owner:remote.test')):
            with self.subTest(sender=sender):
                self.invite(sender=sender, event_id=f'$event{index}')
        self.assertEqual(self.transport.leave.call_count, 4)
        self.transport.join.assert_not_called()

    def test_untrusted_local_declined(self):
        self.invite(trust='untrusted')
        self.transport.join.assert_not_called()

    def test_stripped_encryption(self):
        self.invite(invite_encrypted=True)
        self.transport.join.assert_not_called()
        self.transport.leave.assert_called_once()
        self.assertTrue(self.groups.is_disabled(AI, ROOM))

    def test_post_join_encryption(self):
        self.transport.room_is_encrypted.return_value = True
        self.invite()
        self.transport.join.assert_called_once()
        self.transport.leave.assert_called_once()
        self.assertEqual(self.groups._state(AI, ROOM), 'rejected_encrypted')

    def test_post_join_error_closed(self):
        self.transport.room_is_encrypted.side_effect = RuntimeError('private token')
        self.invite()
        self.assertTrue(self.groups.is_disabled(AI, ROOM))
        self.transport.leave.assert_called_once()

    def test_cross_server_room(self):
        self.invite(room_id='!private:remote.test')
        self.transport.join.assert_not_called()
        self.assertEqual(self.groups.stats.snapshot()['cross_server'], 1)

    def test_opaque_room_without_domain(self):
        self.invite(room_id='!opaque')
        self.assertTrue(self.groups.is_accepted(AI, '!opaque'))

    def test_twenty_room_limit(self):
        for i in range(21):
            self.now += 61
            self.invite(room_id=f'!room{i}:local.test', event_id=f'$event{i}')
        self.assertEqual(self.transport.join.call_count, 20)
        self.assertEqual(self.groups.stats.snapshot()['room_limit'], 1)

    def test_rate_limit_and_expiry(self):
        for i in range(11):
            self.invite(room_id=f'!room{i}:local.test', event_id=f'$event{i}')
        self.assertEqual(self.transport.join.call_count, 10)
        self.assertEqual(self.groups.stats.snapshot()['rate_limited'], 1)
        self.now = 60
        self.invite(event_id='$after', room_id='!after:local.test')
        self.assertEqual(self.transport.join.call_count, 11)
        self.assertLessEqual(len(self.groups._rate[AI]), 10)

    def test_rate_bucket_bound(self):
        self.groups._rate = {str(i): [0] for i in range(MAX_RATE_PERSONAS)}
        self.invite()
        self.assertEqual(len(self.groups._rate), MAX_RATE_PERSONAS)
        self.assertEqual(self.groups.stats.snapshot()['rate_limited'], 1)

    def test_replay_and_restart_accept(self):
        self.invite()
        self.invite()
        other = MemoryStore(self.path, hmac_secret=SECRET)
        self.addCleanup(other.close)
        groups = GroupInvites(other, self.transport, {AI: self.persona}, enabled=True, server_name='local.test')
        groups.on_invite_to_ai(self.ctx())
        self.transport.join.assert_called_once()
        self.assertTrue(groups.is_accepted(AI, ROOM))

    def test_replay_and_restart_reject(self):
        self.invite(invite_encrypted=True)
        self.invite(invite_encrypted=True)
        other = MemoryStore(self.path, hmac_secret=SECRET)
        self.addCleanup(other.close)
        groups = GroupInvites(other, self.transport, {AI: self.persona}, enabled=True, server_name='local.test')
        groups.on_invite_to_ai(self.ctx(invite_encrypted=True))
        self.transport.leave.assert_called_once()

    def test_pending_failure_defers_immediate_replay(self):
        self.transport.join.side_effect = RuntimeError('secret')
        self.invite()
        self.invite()
        self.transport.join.assert_called_once()
        self.assertFalse(self.groups.is_accepted(AI, ROOM))

    def test_encryption_state_stops_engine(self):
        self.invite()
        client = Mock()
        hooks = EngineHooks(personas={AI: self.persona}, client=client,
                            service=Mock(), group_invites=self.groups)
        hooks.on_other(self.ctx(encryption_state=True))
        hooks._reply_as(self.persona, ROOM, 'private body', '$new')
        client.complete.assert_not_called()
        self.assertEqual(self.groups._state(AI, ROOM), 'encrypted_disabled')
        self.transport.leave.assert_not_called()
        self.groups.enabled = False
        self.assertTrue(self.groups.is_disabled(AI, ROOM))

    def test_disabled_room_still_counts(self):
        self.invite()
        self.groups.disable_encrypted(ROOM)
        self.groups.max_rooms = 1
        self.invite(room_id='!new:local.test', event_id='$new')
        self.assertEqual(self.transport.join.call_count, 1)

    def test_logs_and_storage_no_sensitive_values(self):
        self.transport.leave.side_effect = RuntimeError('TOKEN private body')
        with self.assertLogs('botville_gateway.groups', 'INFO') as logs:
            self.invite(invite_encrypted=True)
        for value in (AI, OWNER, ROOM, '$private', 'TOKEN', 'private body'):
            self.assertNotIn(value, '\n'.join(logs.output))
        row = dict(self.store._db.execute('SELECT * FROM group_rooms').fetchone())
        self.assertNotIn('room_id', row)
        self.assertNotIn(ROOM, str(row))
        self.assertNotIn(OWNER, str(row))
        self.assertNotIn('$private', str(self.store._db.execute('SELECT * FROM group_invites').fetchall()))

    def test_migration_idempotent(self):
        self.invite()
        other = MemoryStore(self.path, hmac_secret=SECRET)
        self.addCleanup(other.close)
        self.assertEqual(other._db.execute('SELECT version FROM memory_schema').fetchone()[0], SCHEMA_VERSION)
        self.assertEqual(other._db.execute('SELECT count(*) FROM group_rooms').fetchone()[0], 1)

    def test_upgrade_from_nine(self):
        self.store._db.executescript('DROP TABLE group_rooms; DROP TABLE group_invites; UPDATE memory_schema SET version=9;')
        other = MemoryStore(self.path, hmac_secret=SECRET)
        self.addCleanup(other.close)
        self.assertEqual(other._db.execute('SELECT version FROM memory_schema').fetchone()[0], 10)
        self.assertEqual(other._db.execute('SELECT count(*) FROM group_rooms').fetchone()[0], 0)


class TransportTests(unittest.TestCase):
    def transport(self, **kwargs):
        tr = AppServiceMatrixTransport(homeserver_url='http://localhost', as_token='private-token',
                                       server_name='local.test', **kwargs)
        tr.ensure_user = Mock()
        tr.join = Mock()
        tr.leave = Mock()
        tr.joined_members = Mock(return_value=(AI, OWNER, '@other:local.test'))
        return tr

    def test_accepted_group_forbidden_retry(self):
        tr = self.transport(accepted_group=lambda p, r: (p, r) == (AI, ROOM))
        tr._request = Mock(side_effect=[DeliveryError('M_FORBIDDEN', 'private'), {'event_id': '$sent'}])
        tr.send_text(room_id=ROOM, sender_id=AI, text='body')
        tr.join.assert_called_once_with(ROOM, AI)
        tr.joined_members.assert_not_called()
        tr.leave.assert_not_called()

    def test_unaccepted_group_keeps_b28a(self):
        tr = self.transport(accepted_group=lambda p, r: False)
        tr._request = Mock(side_effect=DeliveryError('M_FORBIDDEN', 'private'))
        with self.assertRaises(DeliveryError):
            tr.send_text(room_id=ROOM, sender_id=AI, text='body')
        tr.join.assert_called_once()
        tr.leave.assert_called_once()

    def test_disabled_stops_direct_send(self):
        tr = self.transport(disabled_group=lambda p, r: True)
        tr._request = Mock()
        with self.assertRaises(DeliveryError) as caught:
            tr.send_text(room_id=ROOM, sender_id=AI, text='body', allow_auto_join=True)
        self.assertEqual(caught.exception.code, 'conversation.e2ee')
        tr._request.assert_not_called()

    def test_encryption_errors_fail_closed(self):
        tr = self.transport()
        for status, expected in ((404, False), (403, True), (500, True), (None, True)):
            err = DeliveryError('M_NOT_FOUND', 'private')
            err.http_status = status
            tr._request = Mock(side_effect=err)
            self.assertEqual(tr.room_is_encrypted(ROOM, AI), expected)
            self.assertEqual(tr._request.call_args.kwargs['query'], {'user_id': AI})
        tr._request = Mock(return_value={})
        self.assertTrue(tr.room_is_encrypted(ROOM, AI))

    def test_encryption_storage_failure_is_retryable(self):
        hooks = Mock()
        hooks.on_other.side_effect = RuntimeError('storage unavailable')
        consumer = AsTransactionConsumer(Mock(), hooks=hooks, server_name='local.test')
        with self.assertRaises(RuntimeError):
            consumer._dispatch_event({'type': 'm.room.encryption', 'event_id': '$e',
                'room_id': ROOM, 'sender': OWNER, 'state_key': '', 'content': {}})

    def test_consumer_invite_and_encryption_routing(self):
        hooks = Mock()
        consumer = AsTransactionConsumer(Mock(), hooks=hooks, server_name='local.test')
        consumer._dispatch_event({'type': 'm.room.member', 'event_id': '$i', 'room_id': ROOM,
            'sender': OWNER, 'state_key': AI, 'content': {'membership': 'invite'},
            'unsigned': {'invite_room_state': [{'type': 'm.room.encryption', 'content': {}}]}})
        self.assertTrue(hooks.on_invite_to_ai.call_args.args[0].invite_encrypted)
        consumer._dispatch_event({'type': 'm.room.encryption', 'event_id': '$e', 'room_id': ROOM,
                                  'sender': OWNER, 'state_key': '', 'content': {}})
        self.assertTrue(hooks.on_other.call_args.args[0].encryption_state)


class ConfigTests(unittest.TestCase):
    def test_defaults_and_ranges(self):
        cfg = load_config({'GATEWAY_DEV_MODE': 'true'})
        self.assertFalse(cfg.groups_enabled)
        self.assertEqual(cfg.group_max_rooms_per_persona, 20)
        for value in ('0', '201', '-1', 'sensitive', '1.5', 'true'):
            with self.subTest(value=value), self.assertRaises(ConfigError) as caught:
                load_config({'GATEWAY_DEV_MODE': 'true', 'GATEWAY_GROUP_MAX_ROOMS_PER_PERSONA': value})
            self.assertNotIn(repr(value), str(caught.exception))
        for value in ('1', '200'):
            self.assertEqual(load_config({'GATEWAY_DEV_MODE': 'true',
                'GATEWAY_GROUP_MAX_ROOMS_PER_PERSONA': value}).group_max_rooms_per_persona, int(value))

    def test_invalid_bool_and_persistent_secret(self):
        for extra in ({'GATEWAY_GROUPS_ENABLED': 'sensitive'},
                      {'GATEWAY_GROUPS_ENABLED': 'true', 'GATEWAY_MEMORY_DB_PATH': '/tmp/unused'}):
            with self.assertRaises(ConfigError) as caught:
                load_config(dict({'GATEWAY_DEV_MODE': 'true'}, **extra))
            self.assertNotIn('sensitive', str(caught.exception))

    def test_persona_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'personas.json')
            for value in (None, [], {'sensitive': True}, {'enabled': 1},
                          {'invite_policy': []}, {'invite_policy': 'sensitive'}):
                with open(path, 'w') as f:
                    json.dump({'personas': [{'user_id': AI, 'system_prompt': 'test', 'groups': value}]}, f)
                with self.assertRaises(PersonaConfigError) as caught:
                    load_personas(path, server_name='local.test')
                self.assertNotIn('sensitive', str(caught.exception))
            for value in ({}, {'enabled': True, 'invite_policy': 'local_humans'}):
                with open(path, 'w') as f:
                    json.dump({'personas': [{'user_id': AI, 'system_prompt': 'test', 'groups': value}]}, f)
                p = load_personas(path, server_name='local.test')[AI]
                self.assertEqual(p.groups['enabled'], value.get('enabled', False))


class AssemblyTests(unittest.TestCase):
    def test_consumer_without_model_and_transport_callbacks(self):
        from botville_gateway.config import Config
        from botville_gateway.http_server import build_server
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'personas.json')
            with open(path, 'w') as handle:
                json.dump({'personas': [{'user_id': AI, 'system_prompt': 'test',
                    'owner_user_id': OWNER, 'groups': {'enabled': True}}]}, handle)
            config = Config(host='127.0.0.1', port=0, dev_mode=True,
                hmac_secret=SECRET, matrix_server_name='local.test',
                personas_file=path, groups_enabled=True, consumer_enabled=True,
                as_txn_db_path=os.path.join(tmp, 'as.db'),
                as_txn_purge_interval_seconds=0, memory_purge_interval_seconds=0,
                memory_db_path=os.path.join(tmp, 'memory.db'))
            tr = AppServiceMatrixTransport(homeserver_url='http://localhost',
                as_token='private-token', server_name='local.test')
            tr.join = Mock()
            tr.room_is_encrypted = Mock(return_value=False)
            with patch('botville_gateway.as_consumer.AsTransactionConsumer.start'):
                server = build_server(config, tr)
            try:
                self.assertIsNotNone(server.engine_hooks)
                server.as_consumer._dispatch_event({'type': 'm.room.member', 'event_id': '$i',
                    'room_id': ROOM, 'sender': OWNER, 'state_key': AI,
                    'content': {'membership': 'invite'}})
                self.assertTrue(tr.accepted_group(AI, ROOM))
                server.as_consumer._dispatch_event({'type': 'm.room.encryption', 'event_id': '$e',
                    'room_id': ROOM, 'sender': OWNER, 'state_key': '', 'content': {}})
                self.assertTrue(tr.disabled_group(AI, ROOM))
                tr._request = Mock()
                with self.assertRaises(DeliveryError):
                    tr.send_text(room_id=ROOM, sender_id=AI, text='body')
                tr._request.assert_not_called()
            finally:
                server.server_close()

    def test_encryption_http_status_and_masquerade(self):
        import io
        import urllib.error
        tr = AppServiceMatrixTransport(homeserver_url='http://localhost',
            as_token='private-token', server_name='local.test')
        for status in (404, 403, 500):
            tr._opener = Mock()
            tr._opener.open.side_effect = urllib.error.HTTPError(
                'http://localhost', status, 'private', {}, io.BytesIO(b'{"errcode":"M_NOT_FOUND"}'))
            self.assertEqual(tr.room_is_encrypted(ROOM, AI), status != 404)
            request = tr._opener.open.call_args.args[0]
            self.assertIn('/state/m.room.encryption/?user_id=', request.full_url)
            self.assertEqual(request.method, 'GET')


if __name__ == '__main__':
    unittest.main()
