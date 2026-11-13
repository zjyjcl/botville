# SPDX-License-Identifier: Apache-2.0
"""B2.8c key-rotation group ledger boundaries."""
import dataclasses
import hashlib
import hmac
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from botville_gateway.as_consumer import ConsumerContext
from botville_gateway.config import ConfigError, load_config
from botville_gateway.engine.personas import Persona
from botville_gateway.groups import GroupInvites
from botville_gateway.matrix import DeliveryError
from botville_gateway.matrix_appservice import AppServiceMatrixTransport
from botville_gateway.memory.store import MemoryStore, SCHEMA_VERSION

AI = '@ai_test:local.test'
OWNER = '@owner:local.test'
ROOM = '!room:local.test'
OLD = b'old-group-secret'
NEW = b'new-group-secret'


class GroupRotationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'memory.db')
        self.persona = Persona(AI, 'fixed', owner_user_id=OWNER,
                               groups={'enabled': True, 'invite_policy': 'owner'})

    def open(self, secret, **kwargs):
        store = MemoryStore(self.path, hmac_secret=secret)
        self.addCleanup(store.close)
        transport = Mock(spec=['join', 'leave', 'room_is_encrypted'])
        transport.room_is_encrypted.return_value = False
        return store, transport, GroupInvites(store, transport, {AI: self.persona},
                                              enabled=True, server_name='local.test', **kwargs)

    def ctx(self, room=ROOM, sender=OWNER, event='$invite', **kwargs):
        return dataclasses.replace(ConsumerContext('invite_to_ai', event, room, sender,
                                   'trusted', persona_user_id=AI), **kwargs)

    def test_migration_backfills_irreversible_key_id(self):
        store, _, groups = self.open(OLD)
        groups._record(AI, ROOM, OWNER, 'accepted')
        store.close()
        db = sqlite3.connect(self.path)
        db.execute('ALTER TABLE group_rooms DROP COLUMN key_id')
        db.execute('UPDATE memory_schema SET version=10')
        db.commit(); db.close()
        migrated = MemoryStore(self.path, hmac_secret=OLD)
        self.addCleanup(migrated.close)
        row = migrated._db.execute('SELECT key_id FROM group_rooms').fetchone()[0]
        self.assertEqual(row, hmac.new(OLD, b"groups:key-id", hashlib.sha256).hexdigest()[:16])
        self.assertEqual(len(row), 16)
        self.assertNotIn(OLD.decode(), row)
        self.assertEqual(migrated._db.execute('SELECT version FROM memory_schema').fetchone()[0],
                         SCHEMA_VERSION)

    def test_rotation_does_not_count_old_rooms_and_can_forget_them(self):
        old_store, _, old_groups = self.open(OLD)
        for number in range(20):
            old_groups._record(AI, '!old%d:local.test' % number, OWNER, 'accepted')
        old_groups._record(AI, '!encrypted:local.test', OWNER, 'accepted')
        old_groups.disable_encrypted('!encrypted:local.test')
        old_store.close()
        store, transport, groups = self.open(NEW)
        self.assertTrue(groups.stale_key_rows(AI))
        groups.on_invite_to_ai(self.ctx(event='$new'))
        self.assertTrue(groups.is_accepted(AI, ROOM))
        self.assertEqual(transport.join.call_count, 1)
        self.assertFalse(groups.is_accepted(AI, '!old0:local.test'))
        self.assertFalse(groups.is_throttled(AI, '!old0:local.test'))
        self.assertFalse(groups.is_disabled(AI, '!encrypted:local.test'))
        forget = GroupInvites(store, transport, {AI: self.persona}, enabled=True,
                              server_name='local.test', forget_stale_keys=True)
        self.assertFalse(forget.stale_key_rows(AI))
        self.assertEqual(store._db.execute('SELECT count(*) FROM group_rooms WHERE key_id != ?',
                         (store.group_key_id(),)).fetchone()[0], 0)

    def test_accepted_room_ignores_bad_repeat_and_forbidden_leave_is_done(self):
        store, transport, groups = self.open(NEW)
        groups.on_invite_to_ai(self.ctx(event='$ok'))
        transport.leave.reset_mock()
        groups.on_invite_to_ai(self.ctx(sender='@other:local.test', event='$bad'))
        self.assertTrue(groups.is_accepted(AI, ROOM))
        transport.leave.assert_not_called()
        self.assertEqual(groups.stats.snapshot()['ignored_for_accepted'], 1)
        transport.leave.side_effect = DeliveryError('M_FORBIDDEN', 'gone')
        groups.on_invite_to_ai(self.ctx(room='!bad:other.test', event='$forbidden'))
        self.assertEqual(store._db.execute("SELECT status FROM group_invites WHERE invite_key=?",
                         (groups._key('invite', AI, '$forbidden'),)).fetchone()[0], 'done')
        self.assertNotIn('leave_failed', groups.stats.snapshot())

    def test_postjoin_encryption_forbidden_leave_is_done_and_uses_cleanup_helper(self):
        store = MemoryStore(self.path, hmac_secret=NEW)
        self.addCleanup(store.close)
        transport = Mock()
        transport.room_is_encrypted.return_value = True
        transport._leave_for_cleanup = Mock()
        groups = GroupInvites(store, transport, {AI: self.persona}, enabled=True,
                              server_name='local.test')
        groups.on_invite_to_ai(self.ctx(event='$postjoin'))
        transport._leave_for_cleanup.assert_called_once_with(ROOM, AI)
        self.assertEqual(store._db.execute("SELECT status FROM group_invites WHERE invite_key=?",
                         (groups._key('invite', AI, '$postjoin'),)).fetchone()[0], 'done')
        self.assertNotIn('leave_failed', groups.stats.snapshot())

    def test_post_join_forbidden_leave_is_done(self):
        store, transport, groups = self.open(NEW)
        transport.room_is_encrypted.return_value = True
        transport.leave.side_effect = DeliveryError('M_FORBIDDEN', 'gone')
        groups.on_invite_to_ai(self.ctx(event='$post-forbidden'))
        transport.leave.assert_called_once_with(ROOM, AI)
        self.assertEqual(store._db.execute("SELECT status FROM group_invites").fetchone()[0], 'done')
        self.assertNotIn('leave_failed', groups.stats.snapshot())

    def test_forget_stale_key_config_boolean(self):
        base = {'GATEWAY_DEV_MODE': 'true', 'GATEWAY_HMAC_SECRET': 'a' * 64}
        self.assertFalse(load_config(base).group_forget_stale_keys)
        enabled = dict(base)
        enabled['GATEWAY_GROUP_FORGET_STALE_KEYS'] = 'true'
        self.assertTrue(load_config(enabled).group_forget_stale_keys)
        invalid = dict(base)
        invalid['GATEWAY_GROUP_FORGET_STALE_KEYS'] = 'not-a-bool'
        with self.assertRaises(ConfigError):
            load_config(invalid)

    def rotated_transport(self, state):
        old, _, groups = self.open(OLD)
        groups._record(AI, ROOM, OWNER, state)
        old.close()
        _, _, groups = self.open(NEW)
        tr = AppServiceMatrixTransport(homeserver_url='http://localhost', as_token='test-token',
            server_name='local.test', accepted_group=groups.is_accepted,
            disabled_group=groups.is_disabled, left_group=groups.is_left,
            quarantine_group=groups.quarantine_group, encryption_guard=True)
        tr.ensure_user = Mock()
        tr.join = Mock()
        tr.leave = Mock()
        tr._guard_room_encryption = Mock()
        tr.joined_members = Mock(return_value=[])
        tr._room_encrypted = Mock(return_value=False)
        tr._joined_room_is_dm = Mock(return_value=False)
        tr.safety_store.invitation_update(AI, ROOM, 'invite', encrypted=False)
        return groups, tr

    def test_rotated_left_with_ledger_never_joins_but_confirmed_dm_can(self):
        groups, tr = self.rotated_transport('left')
        self.assertFalse(groups.is_left(AI, ROOM))
        forbidden = DeliveryError('M_FORBIDDEN', 'gone')
        tr._request = Mock(side_effect=forbidden)
        with self.assertRaises(DeliveryError) as caught:
            tr.send_text(room_id=ROOM, sender_id=AI, text='body')
        self.assertIs(caught.exception, forbidden)
        tr.join.assert_not_called()
        tr.leave.assert_not_called()
        tr._request.side_effect = [forbidden, {'event_id': '$sent'}]
        tr.send_text(room_id=ROOM, sender_id=AI, text='body', allow_auto_join=True)
        tr.join.assert_called_once_with(ROOM, AI)
        tr._joined_room_is_dm.assert_not_called()

    def test_rotated_accepted_after_forget_does_not_bypass_dm_check(self):
        groups, tr = self.rotated_transport('accepted')
        self.assertFalse(groups.is_accepted(AI, ROOM))
        GroupInvites(groups.store, tr, {AI: self.persona}, forget_stale_keys=True)
        self.assertFalse(groups.stale_key_rows(AI))
        tr._request = Mock(side_effect=DeliveryError('M_FORBIDDEN', 'gone'))
        with self.assertRaises(DeliveryError):
            tr.send_text(room_id=ROOM, sender_id=AI, text='body')
        tr.join.assert_called_once_with(ROOM, AI)
        tr._joined_room_is_dm.assert_called_once_with(ROOM, AI)
        tr.leave.assert_called_once_with(ROOM, AI)

    def test_rotated_disabled_still_blocked_by_independent_guard(self):
        groups, tr = self.rotated_transport('encrypted_disabled')
        self.assertFalse(groups.is_disabled(AI, ROOM))
        del tr._guard_room_encryption
        tr._request = Mock(return_value={'algorithm': 'm.megolm.v1.aes-sha2'})
        with self.assertRaises(DeliveryError) as caught:
            tr.send_text(room_id=ROOM, sender_id=AI, text='body')
        self.assertEqual(caught.exception.code, 'conversation.e2ee')
        self.assertEqual(tr._request.call_args.args[0], 'GET')
        self.assertEqual(tr._request.call_count, 1)
        tr.join.assert_not_called()
        tr.leave.assert_not_called()

    def test_invalid_room_is_rejected(self):
        store, transport, groups = self.open(NEW)
        groups.on_invite_to_ai(self.ctx(room='!opaque', event='$opaque'))
        transport.join.assert_not_called()
        transport.leave.assert_called_once_with('!opaque', AI)
        self.assertEqual(groups.stats.snapshot()['cross_server'], 1)


if __name__ == '__main__':
    unittest.main()
