# SPDX-License-Identifier: Apache-2.0
"""Disabled groups retain management and memory, but never engine speech."""
import dataclasses
import unittest
from unittest.mock import Mock, patch

from test_group_rooms_api import MemoryHTTPServer, seed, PATH, ROOM
from test_memory_owner_http import PERSONA, OWNER, SERVER
from botville_gateway.as_consumer import AsTransactionConsumer, ConsumerContext
from botville_gateway.engine.engine import EngineHooks


class DisabledGroupsTests(unittest.TestCase):
    def setup_engine(self, app):
        client, service = Mock(), Mock()
        client.complete.return_value = 'reply'
        service.evaluate_policy.return_value = {'decision': 'allow'}
        service.send_ai_message.return_value = {'outcome': 'sent'}
        groups = app.server.group_invites
        groups.transport = Mock()
        hooks = EngineHooks(personas=app.server.memory_personas, client=client,
            service=service, memory_store=app.server.memory_store, group_invites=groups)
        return hooks, client, service

    def disable(self, app, deployment):
        groups = app.server.group_invites
        groups.enabled = not deployment
        groups.personas[PERSONA].groups['enabled'] = deployment

    def test_mentions_reply_unknown_members_and_dm(self):
        for deployment in (True, False):
            with self.subTest(deployment=deployment), MemoryHTTPServer() as app:
                seed(app)
                self.disable(app, deployment)
                hooks, client, service = self.setup_engine(app)
                members = Mock(return_value=[PERSONA, OWNER, '@other:' + SERVER])
                consumer = AsTransactionConsumer(Mock(), hooks=hooks, server_name=SERVER,
                    room_members=members, persona_user_ids=(PERSONA,),
                    sender_fetcher=lambda room, event: PERSONA)
                event = dict(type='m.room.message', event_id='$mention', room_id=ROOM,
                    sender=OWNER, content={'msgtype': 'm.text', 'body': 'hello',
                        'm.mentions': {'user_ids': [PERSONA]}})
                consumer._dispatch_event(event)
                reply = dict(event, event_id='$reply', content={'msgtype': 'm.text',
                    'body': 'reply without mention',
                    'm.relates_to': {'m.in_reply_to': {'event_id': '$ai'}}})
                consumer._dispatch_event(reply)
                members.side_effect = OSError('unavailable')
                consumer._dispatch_event(dict(event, event_id='$unknown'))
                hooks._reply_as(app.server.memory_personas[PERSONA], ROOM, 'hello', '$noctx')
                self.assertEqual(hooks.stats.snapshot()['group_disabled_skipped'], 4)
                client.complete.assert_not_called()
                service.evaluate_policy.assert_not_called()
                service.send_ai_message.assert_not_called()
                app.server.group_invites.transport.leave.assert_not_called()
                app.server.group_invites.transport._leave_for_cleanup.assert_not_called()
                with app.server.memory_store._lock:
                    rows = app.server.memory_store._db.execute(
                        'SELECT scope_kind FROM memory_items').fetchall()
                self.assertTrue(rows)
                self.assertTrue(all(r[0] == 'group' for r in rows))
                members.side_effect = None
                members.return_value = [PERSONA, OWNER]
                with patch('botville_gateway.memory.recall.assemble_recall', return_value=[]):
                    consumer._dispatch_event(dict(event, event_id='$dm'))
                client.complete.assert_called_once()
                service.send_ai_message.assert_called_once()
                self.assertEqual(hooks.stats.snapshot()['group_disabled_skipped'], 4)

    def test_no_store_blocks_group_but_not_dm_and_none_preserves_behavior(self):
        with MemoryHTTPServer(enabled=False) as app:
            hooks, client, service = self.setup_engine(app)
            ctx = ConsumerContext('human_text', '$event', ROOM, OWNER, 'trusted',
                text='hello', target_user_ids=(PERSONA,), room_members_confirmed=False,
                room_is_dm=True)
            hooks.on_human_text(ctx)
            client.complete.assert_not_called()
            self.assertEqual(hooks.stats.snapshot()['group_disabled_skipped'], 1)
            hooks.on_human_text(dataclasses.replace(ctx, room_members_confirmed=True))
            client.complete.assert_called_once()
            hooks._group_invites = None
            hooks.on_human_text(dataclasses.replace(ctx, event_id='$legacy'))
            self.assertEqual(client.complete.call_count, 2)

    def test_patch_disabled_validation_and_atomicity(self):
        for deployment in (True, False):
            with self.subTest(deployment=deployment), MemoryHTTPServer() as app:
                ref = seed(app)
                self.disable(app, deployment)
                groups = app.server.group_invites
                groups.transport = Mock()
                groups.transport.joined_rooms.return_value = []
                path = PATH + '/rooms/' + ref
                def row():
                    with groups.store._lock:
                        return tuple(groups.store._db.execute('SELECT * FROM group_rooms').fetchone())
                for body in ({'mode': 'mention_only'}, {'mode': 'mention_only', 'action': 'leave'}):
                    before = row()
                    status, _, error = app.request('PATCH', path, body=body)
                    self.assertEqual((status, error['code'], error['title']),
                        (409, 'room.state_conflict', 'Room request rejected'))
                    self.assertEqual(row(), before)
                    groups.transport.joined_rooms.assert_not_called()
                self.assertEqual(app.request('PATCH', path, body={'mode': 'bad'})[0], 400)
                self.assertEqual(app.request('PATCH', PATH + '/rooms/' + '0'*64,
                    body={'mode': 'mention_only'})[0], 404)
                self.assertEqual(app.request('PATCH', path, body={'mode': 'muted'})[0], 200)
                self.assertEqual(app.request('PATCH', path, body={'mode': 'muted', 'action': 'leave'})[0], 200)
                self.assertEqual(app.request('PATCH', path, body={'action': 'leave'})[0], 200)
                self.assertEqual(app.request('PATCH', path, body={'mode': 'muted'})[0], 409)
        with MemoryHTTPServer(enabled=False) as app:
            self.assertEqual(app.request('PATCH', PATH + '/rooms/' + '0'*64,
                body={'mode': 'mention_only'})[0], 404)

    def test_list_disabled_pagination_and_no_store(self):
        for deployment in (True, False):
            with self.subTest(deployment=deployment), MemoryHTTPServer() as app:
                seed(app)
                seed(app, room='!second:' + SERVER)
                seed(app, state='left', room='!left:' + SERVER)
                self.disable(app, deployment)
                status, _, page = app.request('GET', PATH + '/rooms?limit=1')
                self.assertEqual(status, 200)
                self.assertEqual(page['groups_enabled'], not deployment)
                self.assertEqual(len(page['items']), 1)
                self.assertTrue(page['has_more'])
                next_page = app.request('GET', PATH + '/rooms?limit=1&cursor=' + page['next_cursor'])[2]
                self.assertEqual(len(next_page['items']), 1)
                self.assertNotEqual(page['items'][0]['room_ref'], next_page['items'][0]['room_ref'])
                self.assertFalse(next_page['has_more'])
                self.assertEqual(len(app.request('GET', PATH + '/rooms?include=all')[2]['items']), 3)
        with MemoryHTTPServer(enabled=False) as app:
            self.assertEqual(app.request('GET', PATH + '/rooms')[2],
                dict(items=[], next_cursor=None, has_more=False, groups_enabled=False))
