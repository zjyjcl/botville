# SPDX-License-Identifier: Apache-2.0
"""Pair invariants, schema repair, restart baseline and HMAC rotation."""
from dataclasses import replace
from datetime import datetime
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from botville_gateway.engine.personas import Persona
from botville_gateway.memory.owner_http import HTTPError
from botville_gateway.memory.store import MemoryStore, _MIGRATIONS
from botville_gateway.relations import Relations, WEEK

SERVER = 'example.invalid'
A, B, C, D = ['@ai_' + name + ':' + SERVER for name in 'abcd']
HUMAN = '@human:' + SERVER
SECRET = b'obviously-fake-relations-test-key'


def personas(*ids, autonomous=False):
    return {p: Persona(user_id=p, system_prompt='fixture',
                      relation_defaults={'friend_request_policy': 'autonomous' if autonomous else 'approval_required'})
            for p in ids}


class RelationsStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore(':memory:', hmac_secret=SECRET)
        self.addCleanup(self.store.close)
        self.now = 1800000000.0
        self.people = personas(A, B)
        self.service = Relations(self.store, self.people, server_name=SERVER, clock=lambda: self.now)

    def create(self, source=A, target=B):
        return self.service.create(source, {'counterpart_id': target})

    def item(self, source=A, target=B):
        return self.service.get(source, self.service._key(source, target))

    def act(self, action, source=A, target=B, revision=None):
        item = self.item(source, target)
        return self.service.action(source, item['relation_id'], action,
                                   {'revision': revision if revision is not None else item['revision']})

    def error(self, code, fn, *args, **kwargs):
        with self.assertRaises(HTTPError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def rows(self, table='relations'):
        return self.store._db.execute('SELECT * FROM ' + table).fetchall()

    def test_schema_12_upgrade_repeat_partial_and_old_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'memory.db')
            store = MemoryStore(path, hmac_secret=SECRET)
            with store._db:
                store._db.execute("INSERT INTO known_personas VALUES (?,123,1)", (A,))
                store._db.execute("INSERT INTO group_rooms VALUES (?,?,'accepted','muted',?,123,123,?)",
                                  (A, 'a'*64, 'opaque-fake-inviter', store.group_key_id()))
                for table in ('relations', 'relation_events', 'relation_tombstones'):
                    store._db.execute('DROP TABLE ' + table)
                store._db.execute('UPDATE memory_schema SET version=11')
                # Simulate a crash after only two of the four schema-12 tables.
                store._db.execute(_MIGRATIONS[12][0])
            store.close()
            for unused in range(2):
                reopened = MemoryStore(path, hmac_secret=SECRET)
                self.assertEqual(reopened._db.execute('SELECT version FROM memory_schema').fetchone()[0], 12)
                self.assertEqual(reopened._db.execute('SELECT persona_id FROM known_personas').fetchone()[0], A)
                self.assertEqual(reopened._db.execute('SELECT mode FROM group_rooms').fetchone()[0], 'muted')
                for table in ('relations', 'relation_events', 'relation_tombstones'):
                    self.assertEqual(reopened._db.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0)
                reopened.close()

    def test_hmac_exact_and_rotation_updates_events_not_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'memory.db')
            store = MemoryStore(path, hmac_secret=SECRET)
            service = Relations(store, self.people, server_name=SERVER, clock=lambda: self.now)
            old = service.create(A, {'counterpart_id': B})[1]
            expected = hmac.new(SECRET, ('relations:id:' + json.dumps([A, B],
                ensure_ascii=True, separators=(',', ':'))).encode(), hashlib.sha256).hexdigest()
            self.assertEqual(old['relation_id'], expected)
            store.close()
            rotated = MemoryStore(path, hmac_secret=b'another-obviously-fake-test-key')
            try:
                service = Relations(rotated, self.people, server_name=SERVER, clock=lambda: self.now)
                result = service.list(A, '')['items'][0]
                self.assertNotEqual(result['relation_id'], expected)
                self.assertEqual({k:v for k,v in result.items() if k != 'relation_id'},
                                 {k:v for k,v in old.items() if k != 'relation_id'})
                self.error('relation.not_found', service.get, A, expected)
                self.assertEqual(rotated._db.execute('SELECT relation_id FROM relation_events WHERE persona_id=?', (A,)).fetchone()[0],
                                 result['relation_id'])
                self.assertTrue(all(r[0] == rotated.group_key_id() for r in rotated._db.execute('SELECT key_id FROM relations')))
            finally:
                rotated.close()

    def test_clean_schema_11_upgrade_preserves_memory_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'v11.db')
            with patch('botville_gateway.memory.store.SCHEMA_VERSION', 11):
                old = MemoryStore(path, hmac_secret=SECRET)
            old.add_item(persona_id=A, scope_kind='dm', scope_key=HUMAN, room_id=None,
                         counterpart=HUMAN, event_id='$fixture', sender=HUMAN,
                         role='user', trust='trusted', text='fixture memory')
            before = dict(old._db.execute('SELECT * FROM memory_items').fetchone())
            self.assertEqual(old._db.execute('SELECT version FROM memory_schema').fetchone()[0], 11)
            old.close()
            current = MemoryStore(path, hmac_secret=SECRET)
            try:
                self.assertEqual(current._db.execute('SELECT version FROM memory_schema').fetchone()[0], 12)
                self.assertEqual(dict(current._db.execute('SELECT * FROM memory_items').fetchone()), before)
                self.assertEqual(current._db.execute('SELECT count(*) FROM relations').fetchone()[0], 0)
            finally:
                current.close()

    def test_missing_key_or_store_fails_closed_and_skips_sync(self):
        with patch.dict('os.environ', {'GATEWAY_HMAC_SECRET': ''}):
            empty = MemoryStore(':memory:')
        try:
            for store in (None, empty):
                service = Relations(store, self.people, server_name=SERVER)
                self.assertFalse(service.available)
                for fn, args in ((service.list, (A, '')), (service.get, (A, 'a'*64)),
                                 (service.create, (A, {'counterpart_id': B})), (service._key, (A,B))):
                    error = self.error('relation.unavailable', fn, *args)
                    self.assertEqual((error.status, error.retry), (503, 5))
                with self.assertLogs('botville_gateway.relations', level='WARNING') as logs:
                    service.sync_known_personas(self.people)
                self.assertNotIn(A, ''.join(logs.output))
            self.assertEqual(empty._db.execute('SELECT count(*) FROM known_personas').fetchone()[0], 0)
        finally:
            empty.close()

    def test_request_dual_rows_approve_and_revision_timestamps(self):
        status, outgoing = self.create()
        self.assertEqual(status, 201)
        incoming = self.item(B,A)
        self.assertEqual((outgoing['state'], outgoing['direction'], outgoing['origin']), ('pending','outgoing','request'))
        self.assertEqual((incoming['state'], incoming['direction']), ('pending','incoming'))
        self.assertEqual(outgoing['revision'], 1)
        self.assertEqual(datetime.fromisoformat(outgoing['expires_at'].replace('Z','+00:00')).timestamp(), self.now + WEEK)
        self.assertEqual(outgoing['expires_at'], outgoing['revocable_until'])
        self.error('relation.state_conflict', self.act, 'approve')
        self.now += 10
        approved = self.act('approve', B,A)
        self.assertEqual(approved['last_change'], {'kind':'approve', 'at':approved['updated_at']})
        for item in (approved, self.item()):
            self.assertEqual((item['state'], item['direction'], item['revision'], item['expires_at']), ('friend','none',2,None))
            self.assertEqual(datetime.fromisoformat(item['revocable_until'].replace('Z','+00:00')).timestamp(), self.now + WEEK)
        self.assertEqual(approved['created_at'], incoming['created_at'])
        self.assertTrue(all(r['request_id'] is None for r in self.rows()))

    def test_autonomous_and_human_friend_and_noop(self):
        self.people[B] = replace(self.people[B], relation_defaults={'friend_request_policy':'autonomous'})
        status, first = self.create()
        self.assertEqual((status, first['state'], first['origin'], len(self.rows())), (201,'friend','manual',2))
        count = len(self.rows('relation_events'))
        self.assertEqual(self.create(), (200,first))
        self.assertEqual(len(self.rows('relation_events')), count)
        status, item = self.create(A,HUMAN)
        self.assertEqual((status,item['counterpart']['kind'],item['state'],len(self.rows())), (201,'human','friend',3))
        self.assertIsNone(self.service._row(HUMAN,A))

    def test_matrix_validation_self_cross_server_and_case_alias(self):
        for target in (A, A.upper().replace('@AI_A', '@ai_a'), 'oops', '@:example.invalid',
                       '@space name:example.invalid', '@human:elsewhere.invalid', None, 42, '@x:' + 'a'*256):
            self.error('request.invalid', self.create, A,target)
        status, item = self.create(A, B.replace(SERVER,SERVER.upper()))
        self.assertEqual((status,item['counterpart']['kind'],item['counterpart']['user_id']), (201,'ai',B))
        self.assertEqual(self.create(A, B)[0], 200)
        human = self.create(A, HUMAN.replace(SERVER, SERVER.upper()))[1]
        self.assertEqual(human['counterpart']['user_id'], HUMAN)
        self.assertEqual(self.create(A, HUMAN)[0], 200)

    def test_pending_noop_and_incoming_cannot_bypass_approval(self):
        first = self.create()[1]
        self.assertEqual(self.create(), (200,first))
        self.error('relation.state_conflict', self.create, B,A)
        self.assertEqual(self.item()['revision'], 1)

    def test_reject_cooldown_and_retry_boundary(self):
        self.create()
        self.act('reject',B,A)
        for item in (self.item(),self.item(B,A)):
            self.assertEqual((item['state'],item['direction'],item['expires_at']), ('stranger','none',None))
        self.assertEqual(self.service._row(A,B)['rejected_at'], self.now)
        self.assertIsNone(self.service._row(B,A)['rejected_at'])
        self.now += WEEK-1
        self.error('relation.state_conflict', self.create)
        self.now += 1
        self.assertEqual(self.create()[1]['state'], 'pending')

    def test_revoke_friend_late_pending_withdraw_and_invalid_states(self):
        self.create()
        withdrawn = self.act('revoke')
        self.assertEqual(withdrawn['state'],'stranger')
        self.assertEqual(self.item(B,A)['state'],'stranger')
        self.error('relation.state_conflict', self.act, 'revoke')
        self.create()
        self.error('relation.state_conflict', self.act, 'revoke', B,A)
        self.act('approve',B,A)
        self.now += WEEK * 3
        self.assertEqual(self.act('revoke')['state'],'stranger')
        self.create(A,HUMAN)
        self.assertEqual(self.act('revoke',A,HUMAN)['state'],'stranger')
        self.assertEqual(self.rows('relation_tombstones'), [])

    def test_block_unblock_peer_and_state_guards(self):
        self.create()
        self.assertEqual(self.act('block',B,A)['state'], 'blocked')
        self.assertEqual(self.item()['state'], 'stranger')
        self.error('relation.state_conflict', self.create)
        self.error('relation.state_conflict', self.create,B,A)
        for action in ('approve','reject','revoke'):
            self.error('relation.state_conflict', self.act, action,B,A)
        self.assertEqual(self.act('unblock',B,A)['state'],'stranger')
        self.error('relation.state_conflict', self.act,'unblock',B,A)
        self.assertEqual(self.create()[1]['state'],'pending')
        # block is permitted from every state, including already blocked.
        self.act('block')
        revision = self.item()['revision']
        self.assertEqual(self.act('block')['revision'], revision+1)
        self.assertEqual(self.item(B,A)['state'],'stranger')

    def test_every_action_state_direction_matrix(self):
        valid = {'approve':{('pending','incoming')}, 'reject':{('pending','incoming')},
                 'revoke':{('friend','none'),('pending','outgoing')}, 'unblock':{('blocked','none')}}
        self.create()
        for state,direction in (('friend','none'),('stranger','none'),('blocked','none'),
                                ('pending','incoming'),('pending','outgoing')):
            for action in ('approve','reject','revoke','block','unblock'):
                with self.subTest(state=state,direction=direction,action=action):
                    with self.store._db:
                        self.store._db.execute('UPDATE relations SET state=?,direction=?,expires_at=? WHERE persona_id=?',
                                              (state,direction,self.now+WEEK,A))
                        self.store._db.execute("UPDATE relations SET state='pending',direction='outgoing',expires_at=? WHERE persona_id=?",(self.now+WEEK,B))
                    if action == 'block' or (state,direction) in valid[action]:
                        self.act(action)
                    else:
                        self.error('relation.state_conflict', self.act, action)

    def test_revision_validation_and_event_projection(self):
        item = self.create()[1]
        for body in ({},{'revision':True},{'revision':'1'},{'revision':1.0}, {'revision':0}, {'revision':1,'x':1}):
            self.error('request.invalid', self.service.action,A,item['relation_id'],'block',body)
        self.error('relation.revision_conflict', self.act, 'block', revision=99)
        self.assertEqual(len(self.rows('relation_events')), 2)
        columns = {row[1] for row in self.store._db.execute('PRAGMA table_info(relation_events)')}
        self.assertEqual(columns, {'id','persona_id','relation_id','kind','actor','from_state','to_state','at'})
        event = self.rows('relation_events')[0]
        self.assertEqual((event['actor'],event['kind'],event['from_state'],event['to_state']), ('owner','request',None,'pending'))
        self.assertNotIn(SECRET.decode(), repr([dict(r) for r in self.rows('relation_events')]))

    def test_mutual_blocks_survive_one_sided_unblock(self):
        self.create()
        self.act('block')
        blocked_a = dict(self.service._row(A, B))
        events = [dict(r) for r in self.rows('relation_events')]
        self.now += 1
        self.act('block', B, A)
        self.assertEqual((self.item()['state'], self.item(B, A)['state']),
                         ('blocked', 'blocked'))
        self.assertEqual(dict(self.service._row(A, B)), blocked_a)
        self.assertEqual([dict(r) for r in self.rows('relation_events') if r['persona_id'] == A],
                         [r for r in events if r['persona_id'] == A])
        self.assertEqual(len(self.rows('relation_events')), len(events) + 1)
        blocked_b = dict(self.service._row(B, A))
        self.now += 1
        self.act('unblock')
        self.assertEqual(self.item()['state'], 'stranger')
        self.assertEqual(dict(self.service._row(B, A)), blocked_b)
        self.assertEqual(len(self.rows('relation_events')), len(events) + 2)
        before = [dict(r) for r in self.rows()]
        error = self.error('relation.state_conflict', self.create)
        self.assertEqual(error.status, 409)
        self.assertEqual([dict(r) for r in self.rows()], before)
        self.assertEqual(len(self.rows('relation_events')), len(events) + 2)

    def test_actions_preserve_blocked_reverse_in_inconsistent_pair(self):
        for action, state, direction in (('approve', 'pending', 'incoming'),
                                         ('reject', 'pending', 'incoming'),
                                         ('revoke', 'pending', 'outgoing'),
                                         ('revoke', 'friend', 'none')):
            with self.subTest(action=action, state=state):
                # These pairs cannot arise through normal pair transitions.
                with self.service._transaction() as now:
                    self.service._write(A, B, state=state, direction=direction,
                                        kind='fixture', now=now)
                    self.service._write(B, A, state='blocked', kind='fixture', now=now)
                reverse = dict(self.service._row(B, A))
                before = [dict(r) for r in self.rows()]
                events = [dict(r) for r in self.rows('relation_events')]
                self.now += 1
                if action in ('approve', 'reject'):
                    error = self.error('relation.state_conflict', self.act, action)
                    self.assertEqual(error.status, 409)
                    self.assertEqual([dict(r) for r in self.rows()], before)
                    self.assertEqual([dict(r) for r in self.rows('relation_events')], events)
                else:
                    self.assertEqual(self.act(action)['state'], 'stranger')
                    self.assertEqual(len(self.rows('relation_events')), len(events) + 1)
                self.assertEqual(dict(self.service._row(B, A)), reverse)
                self.assertEqual([dict(r) for r in self.rows('relation_events') if r['persona_id'] == B],
                                 [r for r in events if r['persona_id'] == B])

    def test_expiry_read_and_action_persist_once_for_both(self):
        initial = self.create()[1]
        incoming = self.item(B,A)
        self.now += WEEK
        self.error('relation.revision_conflict', self.service.action,B,incoming['relation_id'],'approve',{'revision':1})
        expired = self.item()
        self.assertEqual((expired['state'],expired['revision'],expired['last_change']['kind']), ('stranger',2,'expired'))
        self.assertEqual(self.item(B,A)['state'],'stranger')
        self.error('relation.state_conflict',self.act,'approve',B,A)
        self.service.list(A,'')
        self.assertEqual(len(self.rows('relation_events')),4)
        self.assertEqual(expired['created_at'], initial['created_at'])
        self.assertTrue(all(r['actor']=='system' for r in self.rows('relation_events')[2:]))
        self.create()
        self.now += WEEK
        self.assertEqual(self.service.list(A,'state=pending')['items'],[])
        self.assertEqual(self.item(B,A)['last_change']['kind'],'expired')

    def test_pair_action_rolls_back_on_unexpected_second_write(self):
        self.create()
        before = [dict(r) for r in self.rows()]
        original = self.service._write
        def fail_second(source, target, **kw):
            if source == B:
                raise sqlite3.OperationalError('synthetic database failure')
            return original(source,target,**kw)
        with patch.object(self.service,'_write',side_effect=fail_second):
            with self.assertRaises(sqlite3.OperationalError):
                self.act('block')
        self.assertEqual([dict(r) for r in self.rows()],before)
        self.assertEqual(len(self.rows('relation_events')),2)

    def test_concurrent_actions_use_revision_under_the_pair_lock(self):
        from concurrent.futures import ThreadPoolExecutor
        item = self.create()[1]
        def block():
            try:
                return self.service.action(A, item['relation_id'], 'block', {'revision':1})['state']
            except HTTPError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda unused: block(), range(2)))
        self.assertCountEqual(results, ['blocked', 'relation.revision_conflict'])
        self.assertEqual(len(self.rows('relation_events')),4)
        self.assertEqual((self.item()['revision'],self.item(B,A)['revision']), (2,2))

    def test_baseline_two_identical_starts_never_add_edges(self):
        for unused in range(2):
            self.service.sync_known_personas(self.people)
            self.assertEqual(self.rows(),[])
        self.assertEqual({r['baseline'] for r in self.rows('known_personas')},{1})
        self.assertEqual(len(self.rows('known_personas')),2)

    def test_empty_first_start_stays_baseline_until_nonempty(self):
        self.service.sync_known_personas({})
        self.service.sync_known_personas({})
        self.service.sync_known_personas(self.people)
        self.assertEqual(self.rows(),[])
        self.assertEqual({r['baseline'] for r in self.rows('known_personas')},{1})

    def test_new_one_and_two_same_batch_pairs_restart(self):
        for additions in ((C,),(C,D)):
            with self.subTest(additions=additions):
                for table in ('relations','relation_events','known_personas'):
                    with self.store._db:
                        self.store._db.execute('DELETE FROM '+table)
                self.people.clear()
                self.people.update(personas(A,B))
                self.service.sync_known_personas(self.people)
                self.people.update(personas(*additions))
                self.service.sync_known_personas(self.people)
                count = 4 if len(additions)==1 else 10
                self.assertEqual(len(self.rows()),count)
                self.assertIsNone(self.service._row(A,B))
                for r in self.rows():
                    self.assertEqual((r['state'],r['origin'],r['last_change_kind'],r['revision']),('friend','default','default_friend',1))
                    self.assertEqual(r['revocable_until'],self.now+WEEK)
                self.assertEqual({r['actor'] for r in self.rows('relation_events')},{'system'})
                self.service.sync_known_personas(self.people)
                self.assertEqual(len(self.rows()),count)
                self.assertEqual(len(self.rows('relation_events')),count)
                flags = {r['persona_id']:r['baseline'] for r in self.rows('known_personas')}
                self.assertEqual(flags, dict({A:1,B:1}, **{p:0 for p in additions}))

    def test_default_sync_never_fills_reverse_of_existing_any_state(self):
        self.service.sync_known_personas(self.people)
        for index,state in enumerate(('friend','pending','stranger','blocked')):
            newcomer = '@ai_new_' + str(index) + ':' + SERVER
            self.people.update(personas(newcomer))
            with self.service._transaction() as now:
                self.service._write(A,newcomer,state=state,kind='fixture',now=now,
                                    direction='outgoing' if state=='pending' else 'none')
            before = dict(self.service._row(A,newcomer))
            self.service.sync_known_personas(self.people)
            self.assertEqual(dict(self.service._row(A,newcomer)), before)
            self.assertIsNone(self.service._row(newcomer,A))

    def test_removed_persona_reappears_not_new(self):
        self.service.sync_known_personas(self.people)
        self.people.pop(B)
        self.service.sync_known_personas(self.people)
        self.people.update(personas(B))
        self.service.sync_known_personas(self.people)
        self.assertEqual(self.rows(),[])
        self.assertEqual(len(self.rows('known_personas')),2)

    def test_tombstone_in_either_direction_and_existing_rows_prevent_default(self):
        self.service.sync_known_personas(self.people)
        with self.store._db:
            self.store._db.execute("INSERT INTO relation_tombstones VALUES (?,?,'revoked_default',?)", (A,C,self.now))
        self.people.update(personas(C))
        self.service.sync_known_personas(self.people)
        self.assertIsNone(self.service._row(A,C))
        self.assertIsNone(self.service._row(C,A))
        self.assertEqual(self.item(B,C)['state'],'friend')
        # A previously human counterpart row must not be overwritten on arrival.
        self.create(A,D)
        self.people.update(personas(D))
        self.service.sync_known_personas(self.people)
        self.assertEqual(self.item(A,D)['counterpart']['kind'],'human')
        self.assertIsNone(self.service._row(D,A))

    def test_revoke_default_tombstones_manual_clears_and_restart_never_rebuilds(self):
        self.service.sync_known_personas(self.people)
        self.people.update(personas(C))
        self.service.sync_known_personas(self.people)
        self.act('revoke',A,C)
        stones = self.rows('relation_tombstones')
        self.assertEqual({(r['persona_id'],r['counterpart_id'],r['reason']) for r in stones},
                         {(A,C,'revoked_default'),(C,A,'revoked_default')})
        self.service.sync_known_personas(self.people)
        self.assertEqual(self.item(A,C)['state'],'stranger')
        self.assertEqual(self.item(C,A)['state'],'stranger')
        self.assertEqual(self.create(A,C)[1]['state'],'pending')
        self.assertEqual(self.rows('relation_tombstones'),[])

    def test_list_keyset_ties_filters_and_private_ids(self):
        self.create()
        self.create(A,HUMAN)
        self.create(A,'@another:'+SERVER)
        page = self.service.list(A,'limit=1')
        ids = [page['items'][0]['relation_id']]
        while page['has_more']:
            page = self.service.list(A,'limit=1&cursor='+page['next_cursor'])
            ids.extend(item['relation_id'] for item in page['items'])
        self.assertEqual(len(ids),3)
        self.assertEqual(ids,sorted(set(ids)))
        self.assertIsNone(page['next_cursor'])
        self.assertEqual(len(self.service.list(A,'state=friend&direction=none&kind=human')['items']),2)
        self.assertEqual(len(self.service.list(A,'state=pending&direction=outgoing&kind=ai')['items']),1)
        self.error('relation.not_found',self.service.get,B,ids[0])
        for bad in ('x','A'*64,'a'*63, 'a'*65):
            self.error('relation.not_found',self.service.get,A,bad)

    def test_malformed_cursors_and_query_values(self):
        import base64
        for query in ('limit=201','limit=-1','limit=','cursor=','cursor=%%%%',
                      'kind=robot','direction=in','state=expired','limit=1&limit=2',
                      'unexpected=value','cursor='+'a'*513):
            self.error('request.invalid',self.service.list,A,query)
        for value in (None, {}, [], [1], [True,'a'*64], [float('inf'),'a'*64],
                      [float('nan'),'a'*64], [1,2], [1,'A'*64], [1,'a'*63], [1,'a'*64,2]):
            cursor = base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')
            self.error('request.invalid',self.service.list,A,'cursor='+cursor)


if __name__ == '__main__':
    unittest.main()
