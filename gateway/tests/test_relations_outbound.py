# SPDX-License-Identifier: Apache-2.0
"""Deterministic outbox pump tests; no sleeps and no real pairing calls."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import quote

from botville_gateway.config import load_config
from botville_gateway.memory.owner_http import HTTPError
from botville_gateway.memory.store import MemoryStore
from botville_gateway.relations import Relations, WEEK
from botville_gateway.relations_federation import ulid
from botville_gateway.relations_outbound import (BACKOFF, OUTBOUND_TOKEN_ENV, OUTBOUND_URL_ENV,
                                               OutboundWorker, transport)
from test_relations_inbound import Fixture, REMOTE, RID, TOKEN
from test_relations_store import A, B, SERVER, SECRET, personas
from test_group_rooms_api import MemoryHTTPServer
from test_memory_owner_http import PERSONA


class OutboundTests(Fixture):
    def job(self):
        return dict(self.store._db.execute('SELECT * FROM relation_outbox ORDER BY id DESC LIMIT 1').fetchone())

    def deliver(self, status=200, result=None):
        mock = Mock(return_value=(status, result or {}))
        self.fed.worker.transport = mock
        self.fed.worker.run_once(self.now)
        return mock

    def test_exact_body_authorization_and_timeout(self):
        rid = self.outgoing()
        request = self.deliver(200, dict(delivered=True, peer_status=202)).call_args
        url, token, body, timeout = request[0]
        self.assertEqual(url, 'http://pairing.invalid/v1/pairing/internal/relations/request')
        self.assertEqual(token, 'fake-outbound-token')
        self.assertEqual(set(body), {'destination', 'request_id', 'payload'})
        self.assertEqual(body['request_id'], rid)
        self.assertEqual(body['payload'], dict(from_user=A, to_user=REMOTE, from_kind='ai', message='',
                                               created_at=self.now, expires_at=self.now+WEEK))
        response = Mock(status=200)
        response.read.return_value = b'{"delivered":true,"peer_status":202}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        opener = Mock(); opener.open.return_value = response
        with patch('botville_gateway.relations_outbound.urllib.request.build_opener', return_value=opener):
            self.assertEqual(transport(url, token, body, 7), (200, dict(delivered=True, peer_status=202)))
        request = opener.open.call_args[0][0]
        self.assertEqual(request.get_header('Authorization'), 'Bearer '+token)
        self.assertEqual(json.loads(request.data), body)
        self.assertEqual(opener.open.call_args[1], {'timeout': 7})

    def test_retry_matrix_same_id_and_payload(self):
        rid = self.outgoing()
        original = self.job()['payload']
        for status, result in ((502, {}), (504, {}), (429, {'errcode':'M_LIMIT_EXCEEDED'}),
                               (200, dict(delivered=False, peer_status=503))):
            with self.subTest(status=status, result=result):
                mock = self.deliver(status, result)
                self.assertEqual(mock.call_args[0][2]['request_id'], rid)
                self.assertEqual(self.job()['status'], 'pending')
                self.assertEqual(self.job()['payload'], original)
                self.now = self.job()['next_at']

    def test_backoff_sequence_and_no_early_delivery(self):
        self.outgoing()
        for delay in (5, 30, 120, 600, 3600, 3600, 3600):
            self.deliver(502)
            self.assertEqual(self.job()['next_at'], self.now+delay)
            mock = self.deliver(502)
            mock.assert_not_called()
            self.now += delay
        self.assertEqual(self.job()['attempts'], 7)

    def test_local_401_no_retry_sanitized_warning(self):
        self.outgoing()
        with self.assertLogs('botville_gateway.relations_federation', level='WARNING') as logs:
            self.deliver(401, {'error':TOKEN})
        self.assertNotIn(TOKEN, '\n'.join(logs.output))
        self.assertIn('pair again', '\n'.join(logs.output))
        self.assertEqual(self.job()['status'], 'done')

    def test_peer_401_no_retry_sanitized_warning(self):
        self.outgoing()
        with self.assertLogs('botville_gateway.relations_federation', level='WARNING') as logs:
            self.deliver(200, dict(delivered=False, peer_status=401, body={'error': TOKEN}))
        self.assertNotIn(TOKEN, '\n'.join(logs.output))
        self.assertEqual(self.job()['status'], 'done')

    def test_non_retry_statuses(self):
        for status, result in ((400, {}), (500, {}), (503, {}), (429, {}),
                               (200, dict(delivered=False, peer_status=400)),
                               (200, dict(delivered=False, peer_status=429, body={'errcode':'relation.rate_limited'}))):
            with self.subTest(status=status, result=result):
                # New independent pair for each terminal outcome.
                counterpart = '@ai_test'+str(status)+str(len(str(result)))+':remote.invalid'
                self.service.create(A, {'counterpart_id': counterpart})
                self.deliver(status, result)
                self.assertEqual(self.job()['status'], 'done')

    def test_peer_inactive_severs_all_relations(self):
        self.outgoing()
        self.receive(self.body(to_user=B))
        self.deliver(409, {'errcode': 'BV_PEER_INACTIVE'})
        self.assertTrue(self.fed.peer_severed('remote.invalid'))
        self.assertTrue(all(r[0] == 'stranger' for r in self.store._db.execute('SELECT state FROM relations')))
        self.assertEqual(self.job()['status'], 'cancelled')

    def test_delivered_false_peer_403_severs(self):
        for code in ('BV_PEER_INACTIVE', 'M_FORBIDDEN'):
            self.service.mark_peer_active('remote.invalid')
            self.outgoing()
            self.deliver(200, dict(delivered=False, peer_status=403, body={'errcode':code}))
            self.assertTrue(self.fed.peer_severed('remote.invalid'))
            self.assertEqual(self.row()['state'], 'stranger')

    def test_delivered_true_states_and_active(self):
        self.outgoing()
        self.deliver(200, dict(delivered=True, peer_status=202))
        self.assertEqual(self.row()['state'], 'pending')
        self.assertEqual(self.store._db.execute('SELECT status FROM relation_peers').fetchone()[0], 'active')
        self.service.create(B, {'counterpart_id': REMOTE})
        self.deliver(200, dict(delivered=True, peer_status=201))
        self.assertEqual(self.service._row(B, REMOTE)['state'], 'friend')

    def test_delivered_200_syncs_body_state(self):
        self.receive(); self.action('approve')
        self.deliver(200, dict(delivered=True, peer_status=200, body={'state':'accepted'}))
        self.assertEqual(self.row()['state'], 'friend')
        self.action('revoke')
        self.deliver(200, dict(delivered=True, peer_status=200, body={'state':'revoked'}))
        self.assertEqual(self.row()['state'], 'stranger')

    def test_business_answers_sync_state(self):
        for peer_status, body, expected in (
                (409, {'state':'accepted'}, 'accepted'),
                (410, {'errcode':'relation.expired'}, 'expired'),
                (404, {'errcode':'relation.unknown_user'}, 'revoked'),
                (429, {'errcode':'relation.cooldown','retry_after_s':500}, 'rejected')):
            counterpart = '@ai_status'+str(peer_status)+':remote.invalid'
            self.service.create(A, {'counterpart_id':counterpart})
            self.deliver(200, dict(delivered=False, peer_status=peer_status, body=body))
            meta = self.fed.metadata(A, counterpart)
            self.assertEqual(meta['wire_state'], expected)
            self.assertEqual(self.service._row(A, counterpart)['state'], 'friend' if expected == 'accepted' else 'stranger')
            if peer_status == 429: self.assertEqual(meta['cooldown_until'], self.now+500)

    def test_simultaneous_conflict_stops_without_local_change(self):
        self.outgoing(); before = dict(self.row())
        self.deliver(200, dict(delivered=False, peer_status=409, body={'state':'pending','request_id':'0'*26}))
        self.assertEqual(dict(self.row()), before)
        self.assertEqual(self.job()['status'], 'done')

    def test_late_delivery_cannot_override_winner_or_block(self):
        self.outgoing(); job = self.fed.next_job(self.now)
        self.receive(self.body(rid='0'*26))
        self.fed.delivered(job, 200, dict(delivered=True, peer_status=201), self.now)
        self.assertEqual((self.row()['state'], self.row()['direction']), ('pending', 'incoming'))
        self.action('approve'); job = self.fed.next_job(self.now)
        self.action('block')
        self.fed.delivered(job, 200, dict(delivered=True, peer_status=201), self.now)
        self.assertEqual(self.row()['state'], 'blocked')

    def test_success_reactivates_peer_without_restoring_cancelled_relation(self):
        self.outgoing(); job = self.fed.next_job(self.now)
        self.service.mark_peer_severed('remote.invalid')
        self.fed.delivered(job, 200, dict(delivered=True, peer_status=201), self.now)
        self.assertFalse(self.fed.peer_severed('remote.invalid'))
        self.assertEqual(self.row()['state'], 'stranger')

    def test_malformed_remote_state_does_not_retry(self):
        self.outgoing()
        self.deliver(200, dict(delivered=False, peer_status=409, body={'state':[]}))
        self.assertEqual(self.job()['status'], 'done')
        self.assertEqual(self.row()['state'], 'pending')

    def test_expiry_stops_and_sets_expired(self):
        self.outgoing()
        self.now += WEEK+1
        self.deliver(502).assert_not_called()
        self.assertEqual(self.job()['status'], 'expired')
        self.assertEqual(self.fed.metadata(A, REMOTE)['wire_state'], 'expired')
        self.assertEqual(self.row()['state'], 'stranger')

    def test_owner_read_expiration_marks_queued_job_expired(self):
        self.outgoing()
        self.now += WEEK+1
        self.service.list(A, '')
        self.assertEqual(self.job()['status'], 'expired')
        self.deliver(502).assert_not_called()

    def test_unconfigured_does_not_send_or_fail(self):
        self.outgoing()
        for url, token in (('', ''), ('http://pairing.invalid', ''), ('', 'fake-token')):
            worker = OutboundWorker(self.fed, url=url, token=token, transport=Mock())
            worker.run_once(self.now)
            worker.transport.assert_not_called()
            self.assertEqual(self.job()['status'], 'pending')

    def test_worker_can_stop_without_sleep(self):
        self.fed.worker.start()
        self.assertTrue(self.fed.worker.stop())
        self.assertFalse(self.fed.worker._thread.is_alive())

    def test_ulid_shape_time_and_uniqueness(self):
        values = {ulid(self.now) for _ in range(100)}
        self.assertEqual(len(values), 100)
        for value in values: self.assertRegex(value, r'^[0-9A-HJKMNP-TV-Z]{26}$')
        self.assertLess(max(values), ulid(self.now+1))

    def test_persistent_outbox_and_idempotency_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory)/'memory.db')
            store = MemoryStore(path, hmac_secret=SECRET)
            service = Relations(store, personas(A, B), server_name=SERVER, clock=lambda:self.now)
            worker = OutboundWorker(service.federation, url='http://pairing.invalid', token='fake-token')
            service.federation.worker = worker
            service.create(A, {'counterpart_id':REMOTE})
            before = dict(store._db.execute('SELECT * FROM relation_outbox').fetchone())
            body = self.body(to_user=B)
            receipt = service.federation.inbound('request', body['envelope'], body['meta'])
            store.close()
            store = MemoryStore(path, hmac_secret=SECRET)
            try:
                service = Relations(store, personas(A, B), server_name=SERVER, clock=lambda:self.now)
                self.assertEqual(service.federation.next_job(self.now), before)
                self.assertEqual(service.federation.inbound('request', body['envelope'], body['meta']), receipt)
                self.assertEqual(service._row(B, REMOTE)['revision'], 1)
            finally:
                store.close()

    def test_outbox_and_owner_state_rollback_together(self):
        self.store._db.execute('''CREATE TRIGGER fail_outbox BEFORE INSERT ON relation_outbox
            BEGIN SELECT RAISE(ABORT, 'injected'); END''')
        with self.assertRaises(sqlite3.IntegrityError): self.outgoing()
        for table in ('relations', 'relation_federation', 'relation_outbox', 'relation_events'):
            self.assertEqual(self.count(table), 0)

    def test_placeholder_names_defined_once_and_config_uses_them(self):
        src = Path(__file__).resolve().parents[1]/'src'
        contents = [p.read_text() for p in src.rglob('*.py')]
        for name in (OUTBOUND_TOKEN_ENV, OUTBOUND_URL_ENV):
            self.assertEqual(sum(s.count(name) for s in contents), 1)
        config = load_config({'GATEWAY_DEV_MODE':'true', OUTBOUND_URL_ENV:'http://pairing.invalid', OUTBOUND_TOKEN_ENV:TOKEN})
        self.assertEqual(config.pairing_relations_url, 'http://pairing.invalid')
        self.assertEqual(config.pairing_relations_token, TOKEN)
        self.assertNotIn(TOKEN, repr(config))


class OwnerFederationTests(Fixture):
    def test_owner_approve_reject_and_revoke_payloads(self):
        self.receive(); item = self.action('approve')
        self.assertEqual(item['last_change']['kind'], 'approve')
        event = self.store._db.execute('SELECT actor FROM relation_events ORDER BY id DESC LIMIT 1').fetchone()
        self.assertEqual(event[0], 'owner')
        job = dict(self.store._db.execute("SELECT * FROM relation_outbox WHERE status='pending'").fetchone())
        self.assertEqual((job['kind'], json.loads(job['payload'])['decision'], job['request_id']), ('respond','accept',RID))
        self.action('revoke')
        jobs = self.store._db.execute("SELECT * FROM relation_outbox WHERE status='pending'").fetchall()
        self.assertEqual(len(jobs), 1)
        self.assertEqual((jobs[0]['kind'], json.loads(jobs[0]['payload'])['reason']), ('revoke','unfriend'))
        self.receive(self.body(rid='B'*26)); self.action('reject')
        job = self.store._db.execute('SELECT * FROM relation_outbox ORDER BY id DESC').fetchone()
        self.assertEqual((job['kind'], json.loads(job['payload'])['decision']), ('respond','reject'))
        self.assertEqual(self.row()['state'], 'stranger')
        self.assertEqual(self.fed.metadata(A, REMOTE)['cooldown_until'], self.now+WEEK)

    def test_owner_outgoing_withdrawal_and_block_no_send(self):
        rid = self.outgoing(); self.action('revoke')
        job = self.store._db.execute('SELECT * FROM relation_outbox ORDER BY id DESC').fetchone()
        self.assertEqual((job['kind'], job['request_id'], json.loads(job['payload'])['reason']), ('revoke',rid,'withdrawn'))
        count = self.count('relation_outbox')
        self.action('block')
        self.assertEqual(self.count('relation_outbox'), count)
        self.assertFalse(self.store._db.execute("SELECT 1 FROM relation_outbox WHERE status='pending'").fetchone())
        self.action('unblock')
        self.assertEqual(self.row()['state'], 'stranger')
        self.assertEqual(self.count('relation_tombstones'), 0)

    def test_owner_cross_server_unconfigured_and_severed(self):
        self.fed.worker.url = ''
        with self.assertRaises(HTTPError) as error:
            self.service.create(A, {'counterpart_id':REMOTE})
        self.assertEqual((error.exception.status,error.exception.code), (400,'request.invalid'))
        self.fed.worker.url = 'http://pairing.invalid'
        self.service.mark_peer_severed('remote.invalid')
        with self.assertRaises(HTTPError) as error:
            self.service.create(A, {'counterpart_id':REMOTE})
        self.assertEqual((error.exception.status,error.exception.code), (409,'relation.peer_inactive'))
        self.service.mark_peer_active('remote.invalid')
        status, item = self.service.create(A, {'counterpart_id':REMOTE})
        self.assertEqual((status,item['state'],item['direction']), (201,'pending','outgoing'))
        self.assertEqual(self.count('relation_outbox'), 1)

    def test_owner_http_add_and_actions(self):
        path = '/v1/personas/'+quote(PERSONA,safe='')+'/relations'
        with MemoryHTTPServer() as app:
            status, _, problem = app.request('POST', path, body={'counterpart_id': REMOTE})
            self.assertEqual((status, problem['code']), (400,'request.invalid'))
            # Enable configuration after stopping the background worker: no test network.
            app.server.relations_outbound.stop()
            app.server.relations_outbound.url = 'http://pairing.invalid'
            app.server.relations_outbound._token = 'fake-token'
            status, _, item = app.request('POST', path, body={'counterpart_id': REMOTE})
            self.assertEqual((status,item['state'],item['origin']), (201,'pending','cross_server'))
            status, _, result = app.request('POST', path+'/'+item['relation_id']+'/revoke', body={'revision':item['revision']})
            self.assertEqual((status,result['state']), (200,'stranger'))
            app.server.relations.mark_peer_severed('remote.invalid')
            status, _, problem = app.request('POST', path, body={'counterpart_id':REMOTE})
            self.assertEqual((status,problem['code']), (409,'relation.peer_inactive'))
