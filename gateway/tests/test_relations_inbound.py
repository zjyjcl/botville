# SPDX-License-Identifier: Apache-2.0
"""Private ingress protocol, persistence and transition acceptance tests."""
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from botville_gateway.config import Config, ConfigError, load_config
from botville_gateway.memory.store import MemoryStore, SCHEMA_VERSION, _MIGRATIONS
from botville_gateway.relations import Relations, WEEK
from botville_gateway.relations_federation import canonical
from botville_gateway.relations_inbound import validate, Invalid, InboundServer, MAX_BYTES
from botville_gateway.relations_outbound import OutboundWorker
from test_relations_store import A, B, SERVER, SECRET, personas
from test_group_rooms_api import MemoryHTTPServer

REMOTE = '@ai_remote:remote.invalid'
RID = '01JD3Q7Z9F6V7K2N8W4X5Y6Z7A'
TOKEN = 'obviously-fake-inbound-token-for-tests-only'


class Fixture(unittest.TestCase):
    def setUp(self):
        self.now = 1800000000
        self.store = MemoryStore(':memory:', hmac_secret=SECRET)
        self.addCleanup(self.store.close)
        self.service = Relations(self.store, personas(A, B), server_name=SERVER, clock=lambda: self.now)
        self.fed = self.service.federation
        self.fed.worker = OutboundWorker(self.fed, url='http://pairing.invalid', token='fake-outbound-token', clock=lambda: self.now)

    def body(self, kind='request', rid=RID, **payload):
        data = dict(from_user=REMOTE, to_user=A)
        if kind == 'request':
            data.update(from_kind='ai', message='Hello\n世界', created_at=self.now, expires_at=self.now + WEEK)
        elif kind == 'respond':
            data['decision'] = 'accept'
        else:
            data['reason'] = 'withdrawn'
        data.update(payload)
        return dict(kind=kind, envelope=dict(v=1, kind=kind, origin='remote.invalid', destination=SERVER,
                    request_id=rid, payload=data), meta=dict(origin='remote.invalid', sent_at=self.now, received_at=self.now))

    def receive(self, body=None, kind=None):
        body = body or self.body()
        kind = kind or body.get('kind', 'request')
        try:
            envelope, meta = validate(kind, body, SERVER, self.now)
        except Invalid as error:
            return error.response
        return self.fed.inbound(kind, envelope, meta)

    def row(self):
        return self.service._row(A, REMOTE)

    def action(self, action):
        row = self.row()
        return self.service.action(A, row['relation_id'], action, {'revision': row['revision']})

    def outgoing(self):
        self.service.create(A, {'counterpart_id': REMOTE})
        return self.fed.metadata(A, REMOTE)['request_id']

    def count(self, table):
        return self.store._db.execute('SELECT count(*) FROM ' + table).fetchone()[0]


class ValidationTests(Fixture):
    def test_exact_keys_top_envelope_meta(self):
        for section in (None, 'envelope', 'meta'):
            for change in ('extra', 'missing'):
                body = self.body()
                target = body if section is None else body[section]
                if change == 'extra':
                    target['extra'] = 1
                else:
                    target.pop(next(iter(target)))
                with self.subTest(section=section, change=change):
                    self.assertEqual(self.receive(body)[0], 400)
        self.assertEqual(self.count('relation_inbound_idempotency'), 0)

    def test_version_is_integer_one(self):
        for version in (True, False, '1', 1.0, 0, 2, None):
            body = self.body(); body['envelope']['v'] = version
            self.assertEqual(self.receive(body)[0], 400)

    def test_kind_mismatch_and_unknown(self):
        for field in ('top', 'envelope', 'url'):
            body = self.body()
            if field == 'top': body['kind'] = 'respond'
            if field == 'envelope': body['envelope']['kind'] = 'respond'
            self.assertEqual(self.receive(body, 'bogus' if field == 'url' else 'request')[0], 422)
        self.assertEqual(self.count('relation_inbound_idempotency'), 0)

    def test_destination_and_origin(self):
        for field, value in (('destination', 'elsewhere.invalid'), ('origin', 'another.invalid')):
            body = self.body(); body['envelope'][field] = value
            self.assertEqual(self.receive(body)[0], 400)
        body = self.body(); body['meta']['origin'] = 'another.invalid'
        self.assertEqual(self.receive(body)[0], 400)
        body = self.body(from_user='@evil:another.invalid')
        self.assertEqual(self.receive(body)[1]['errcode'], 'relation.bad_payload')
        body = self.body(); body['envelope']['destination'] = SERVER.upper()
        self.assertEqual(self.receive(body)[0], 202)

    def test_normalizes_all_identity_servers(self):
        body = self.body(from_user=REMOTE.upper().replace('@AI_REMOTE', '@ai_remote'), to_user=A.replace(SERVER, SERVER.upper()))
        body['envelope']['origin'] = 'REMOTE.INVALID'
        self.assertEqual(self.receive(body)[0], 202)
        self.assertIsNotNone(self.row())

    def test_request_id(self):
        for value in ('a'*15, 'a'*65, 'a'*15+'!', 'a'*16+'\n', True, None):
            body = self.body(); body['envelope']['request_id'] = value
            self.assertEqual(self.receive(body)[0], 400)
        for value in ('a'*16, 'Z_9-'*16):
            body = self.body(rid=value, from_user='@' + value.lower().replace('_', '') + ':remote.invalid')
            self.assertEqual(self.receive(body)[0], 202)

    def test_payload_keys_and_types(self):
        for kind in ('request', 'respond', 'revoke'):
            for data in ([], None, {}, dict(self.body(kind)['envelope']['payload'], extra=True)):
                body = self.body(kind); body['envelope']['payload'] = data
                self.assertEqual(self.receive(body)[0], 400)
            for field in self.body(kind)['envelope']['payload']:
                body = self.body(kind); body['envelope']['payload'][field] = []
                self.assertEqual(self.receive(body)[0], 400)
        for field in ('created_at', 'expires_at'):
            self.assertEqual(self.receive(self.body(**{field: True}))[0], 400)
        for field in ('sent_at', 'received_at'):
            body = self.body(); body['meta'][field] = True
            self.assertEqual(self.receive(body)[0], 400)

    def test_message_length_unicode_and_controls(self):
        for message in ('字'*281, 'bad\x00', '\t', '\r', '\x7f', '\u200b'):
            self.assertEqual(self.receive(self.body(message=message))[0], 400)
        self.assertEqual(self.receive(self.body(message='🙂'*279+'\n'))[0], 202)

    def test_decision_and_reason_enums(self):
        for kind, field in (('respond', 'decision'), ('revoke', 'reason')):
            result = self.receive(self.body(kind, **{field: 'unsupported'}))
            self.assertEqual((result[0], result[1]['errcode']), (422, 'relation.unsupported'))
        self.assertEqual(self.count('relation_inbound_idempotency'), 0)

    def test_envelope_size(self):
        body = self.body(); body['envelope']['payload']['extra'] = 'x'*MAX_BYTES
        self.assertEqual(self.receive(body)[0], 400)

    def test_unknown_and_foreign_target_are_cached_404(self):
        for target in ('@ai_unknown:'+SERVER, '@ai_else:elsewhere.invalid'):
            body = self.body(rid=target.replace(':', '_').replace('@', '_').replace('.', '_'), to_user=target)
            result = self.receive(body)
            self.assertEqual((result[0], result[1]['errcode']), (404, 'relation.unknown_user'))
            self.assertEqual(self.receive(body), result)
        self.assertEqual(self.count('relation_inbound_idempotency'), 2)

    def test_time_tolerance_and_created_at(self):
        body = self.body(); body['meta']['sent_at'] += 301
        self.assertEqual(self.receive(body)[0], 400)
        self.assertEqual(self.receive(self.body(created_at=self.now+301))[0], 400)
        for offset in (-300, 300):
            body = self.body(rid='T'*16+str(offset).replace('-', '_'), from_user='@ai_t'+str(abs(offset))+('a' if offset < 0 else 'b')+':remote.invalid')
            body['meta']['sent_at'] += offset
            self.assertEqual(self.receive(body)[0], 202)


class BusinessTests(Fixture):
    def test_pending_and_autonomous(self):
        result = self.receive()
        self.assertEqual(result, (202, dict(accepted=True, request_id=RID, state='pending')))
        self.assertEqual((self.row()['state'], self.row()['direction'], self.row()['origin']), ('pending', 'incoming', 'cross_server'))
        self.service.personas[B].relation_defaults['friend_request_policy'] = 'autonomous'
        result = self.receive(self.body(rid='B'*26, to_user=B, from_kind='human'))
        self.assertEqual(result, (201, dict(request_id='B'*26, state='accepted')))
        self.assertEqual(self.service._row(B, REMOTE)['counterpart_kind'], 'human')
        self.assertEqual(self.count('relation_tombstones'), 0)

    def test_block_and_policy_hide_target(self):
        first = self.receive()
        self.assertEqual(first[0], 202)
        self.action('block')
        blocked = dict(self.row())
        self.assertEqual(self.receive(), first)
        self.assertEqual(dict(self.row()), blocked)
        result = self.receive(self.body(rid='B'*26))
        self.assertEqual((result[0], result[1]['errcode']), (404, 'relation.unknown_user'))
        self.fed.policy_allows = lambda *args: False
        self.assertEqual(self.receive(self.body(rid='C'*26, to_user=B))[0], 404)
        # The B-R1 same-server owner rule deliberately remains 409.
        self.service.create(A, {'counterpart_id': B})
        reverse = self.service._row(B, A)
        self.service.action(B, reverse['relation_id'], 'block', {'revision': reverse['revision']})
        with self.assertRaises(Exception) as caught:
            self.service.create(A, {'counterpart_id': B})
        self.assertEqual(caught.exception.status, 409)

    def test_respond_accept_and_reject(self):
        rid = self.outgoing()
        result = self.receive(self.body('respond', rid))
        self.assertEqual(result, (200, dict(request_id=rid, state='accepted')))
        self.assertEqual(self.row()['state'], 'friend')
        self.assertEqual(self.store._db.execute('SELECT status FROM relation_outbox').fetchone()[0], 'cancelled')
        self.service.create(B, {'counterpart_id': REMOTE})
        rid = self.fed.metadata(B, REMOTE)['request_id']
        self.assertEqual(self.receive(self.body('respond', rid, to_user=B, decision='reject'))[1]['state'], 'rejected')
        self.assertEqual(self.fed.metadata(B, REMOTE)['cooldown_until'], self.now+WEEK)
        with self.assertRaises(Exception) as caught:
            self.service.create(B, {'counterpart_id': REMOTE})
        self.assertEqual(caught.exception.code, 'relation.state_conflict')

    def test_revoke_withdrawn_and_unfriend(self):
        self.receive()
        self.assertEqual(self.receive(self.body('revoke'))[1]['state'], 'revoked')
        self.assertEqual(self.row()['state'], 'stranger')
        self.receive(self.body(rid='B'*26)); self.action('approve')
        self.assertEqual(self.receive(self.body('revoke', 'B'*26, reason='unfriend'))[0], 200)
        self.assertEqual(self.row()['state'], 'stranger')

    def test_unknown_request_and_conflicts(self):
        for kind in ('respond', 'revoke'):
            result = self.receive(self.body(kind))
            self.assertEqual((result[0], result[1]['errcode']), (404, 'relation.unknown_request'))
        self.receive(self.body(rid='B'*26)); self.action('reject')
        result = self.receive(self.body('respond', 'B'*26))
        self.assertEqual((result[0], result[1]['state']), (409, 'rejected'))
        self.receive(self.body(rid='C'*26, to_user=B))
        result = self.receive(self.body('revoke', 'C'*26, to_user=B, reason='unfriend'))
        self.assertEqual((result[0], result[1]['state']), (409, 'pending'))

    def test_request_on_friend_conflicts(self):
        self.receive(); self.action('approve')
        result = self.receive(self.body(rid='B'*26))
        self.assertEqual((result[0], result[1]['state']), (409, 'accepted'))

    def test_min_expiry_and_lazy_expiration(self):
        body = self.body(expires_at=self.now+10*WEEK)
        body['meta']['sent_at'] = self.now-60
        self.receive(body)
        self.assertEqual(self.row()['expires_at'], self.now-60+WEEK)
        self.now += WEEK
        result = self.receive(self.body('revoke'))
        self.assertEqual((result[0], result[1]['state']), (410, 'expired'))
        self.assertEqual(self.row()['state'], 'stranger')
        self.assertEqual(self.fed.metadata(A, REMOTE)['wire_state'], 'expired')
        self.assertEqual(self.receive(self.body(rid='B'*26, expires_at=self.now-1))[0], 410)

    def test_expiry_boundary_and_owner_lazy_expiration(self):
        self.receive(self.body(expires_at=self.now))
        self.assertEqual(self.row()['state'], 'pending')
        self.now += 1
        self.service.list(A, '')
        self.assertEqual(self.fed.metadata(A, REMOTE)['wire_state'], 'expired')

    def test_cooldown_after_owner_reject(self):
        self.receive(); self.action('reject')
        result = self.receive(self.body(rid='B'*26))
        self.assertEqual((result[0], result[1]['errcode'], result[1]['retry_after_s']), (429, 'relation.cooldown', WEEK))
        self.now += WEEK+1
        self.assertEqual(self.receive(self.body(rid='C'*26))[0], 202)

    def test_two_rate_limits(self):
        for name in ('ORIGIN_PER_MINUTE', 'PERSONA_PER_MINUTE'):
            self.fed.rates.clear()
            with patch('botville_gateway.relations_federation.'+name, 1):
                suffix = 'O' if name.startswith('ORIGIN') else 'P'
                self.receive(self.body(rid=suffix*26, from_user='@first'+suffix.lower()+':remote.invalid'))
                result = self.receive(self.body(rid=suffix*25+'X', from_user='@second'+suffix.lower()+':remote.invalid'))
                self.assertEqual((result[0], result[1]['errcode'], result[1]['retry_after_s']), (429, 'relation.rate_limited', 60))

    def test_simultaneous_local_wins(self):
        rid = self.outgoing()
        result = self.receive(self.body(rid='Z'*26))
        self.assertEqual(result, (409, dict(errcode='relation.state_conflict', error='Relation state conflicts with operation', request_id=rid, state='pending')))
        self.assertEqual(self.row()['direction'], 'outgoing')
        self.assertEqual(self.store._db.execute('SELECT status FROM relation_outbox').fetchone()[0], 'pending')

    def test_simultaneous_inbound_wins_even_autonomous(self):
        self.outgoing()
        self.service.personas[A].relation_defaults['friend_request_policy'] = 'autonomous'
        result = self.receive(self.body(rid='0'*26))
        self.assertEqual((result[0], result[1]['state']), (202, 'pending'))
        self.assertEqual(self.row()['direction'], 'incoming')
        self.assertEqual(self.fed.metadata(A, REMOTE)['request_id'], '0'*26)
        self.assertEqual(self.store._db.execute('SELECT status FROM relation_outbox').fetchone()[0], 'cancelled')

    def test_sever_and_reactivate_do_not_restore(self):
        self.outgoing()
        inbound = self.body(to_user=B)
        first = self.receive(inbound)
        self.assertEqual(first[0], 202)
        row = self.service._row(B, REMOTE)
        self.service.action(B, row['relation_id'], 'approve', {'revision': row['revision']})
        self.service.mark_peer_severed('REMOTE.INVALID')
        self.assertEqual([r[0] for r in self.store._db.execute('SELECT state FROM relations')], ['stranger', 'stranger'])
        self.assertFalse(self.store._db.execute("SELECT 1 FROM relation_outbox WHERE status='pending'").fetchone())
        severed = dict(self.service._row(B, REMOTE))
        self.assertEqual(self.receive(inbound), first)
        self.assertEqual(dict(self.service._row(B, REMOTE)), severed)
        result = self.receive(self.body(rid='B'*26))
        self.assertEqual((result[0], result[1]['errcode']), (403, 'relation.peer_inactive'))
        self.service.mark_peer_active('remote.invalid')
        self.assertEqual(self.row()['state'], 'stranger')
        self.assertEqual(self.receive(self.body(rid='C'*26))[0], 202)

    def test_error_has_no_internal_identifiers(self):
        self.receive(); self.action('approve')
        body = self.receive(self.body(rid='B'*26))[1]
        self.assertEqual(set(body), {'errcode', 'error', 'request_id', 'state'})
        for value in (self.row()['relation_id'], A, REMOTE):
            self.assertNotIn(value, json.dumps(body))


class PersistenceTests(Fixture):
    def test_replays_202_201_and_errors_exactly(self):
        body = self.body(); first = self.receive(body)
        self.action('approve')
        self.assertEqual(self.receive(body), first)
        self.service.personas[B].relation_defaults['friend_request_policy'] = 'autonomous'
        body = self.body(rid='B'*26, to_user=B)
        first = self.receive(body); self.assertEqual(first[0], 201)
        self.assertEqual(self.receive(body), first)
        body = self.body(rid='C'*26, to_user='@unknown:'+SERVER)
        first = self.receive(body)
        self.now += 90
        self.assertEqual(self.receive(body), first)

    def test_concurrent_replay_only_writes_once(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda _: self.receive(), range(4)))
        self.assertTrue(all(response == responses[0] for response in responses))
        self.assertEqual(responses[0][0], 202)
        self.assertEqual(self.count('relation_events'), 1)
        self.assertEqual(self.count('relation_inbound_idempotency'), 1)

    def test_cached_cooldown_replays_original_retry_after(self):
        self.receive(); self.action('reject')
        body = self.body(rid='B'*26)
        first = self.receive(body)
        self.assertEqual(first[0], 429)
        self.now += WEEK+1
        self.assertEqual(self.receive(body), first)

    def test_same_key_different_payload_conflicts(self):
        self.receive()
        result = self.receive(self.body(message='changed'))
        self.assertEqual((result[0], result[1]['state']), (409, 'pending'))
        self.assertEqual(self.row()['revision'], 1)

    def test_same_transaction_rollback(self):
        # Fault after relation writes, before idempotency insertion.
        self.store._db.execute('''CREATE TRIGGER fail_idempotency BEFORE INSERT ON relation_inbound_idempotency
            BEGIN SELECT RAISE(ABORT, 'injected test failure'); END''')
        with self.assertRaises(sqlite3.IntegrityError):
            self.receive()
        for table in ('relations', 'relation_events', 'relation_federation', 'relation_inbound_idempotency'):
            self.assertEqual(self.count(table), 0)

    def test_idempotency_retention_and_lazy_cleanup(self):
        expiry = self.now+20
        self.receive(self.body(expires_at=expiry))
        self.now = expiry+WEEK
        self.receive(self.body(rid='B'*26, to_user=B))
        self.assertIsNotNone(self.store._db.execute('SELECT 1 FROM relation_inbound_idempotency WHERE request_id=?', (RID,)).fetchone())
        self.now += 1
        self.receive(self.body(rid='C'*26, to_user=B))
        self.assertIsNone(self.store._db.execute('SELECT 1 FROM relation_inbound_idempotency WHERE request_id=?', (RID,)).fetchone())

    def test_schema_12_upgrade_reentrant_preserves_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory)/'test.db')
            with patch('botville_gateway.memory.store.SCHEMA_VERSION', 12):
                store = MemoryStore(path, hmac_secret=SECRET)
            service = Relations(store, personas(A, B), server_name=SERVER, clock=lambda: self.now)
            before = service.create(A, {'counterpart_id': B})[1]
            with store._db:
                store._db.execute(_MIGRATIONS[13][0])
            store.close()
            for _ in range(2):
                store = MemoryStore(path, hmac_secret=SECRET)
                try:
                    service = Relations(store, personas(A, B), server_name=SERVER, clock=lambda: self.now)
                    self.assertEqual(service.get(A, before['relation_id']), before)
                    self.assertEqual(store._db.execute('SELECT version FROM memory_schema').fetchone()[0], 13)
                finally:
                    store.close()

    def test_sqlite_busy_timeout_is_bounded(self):
        self.assertLessEqual(self.store._db.execute('PRAGMA busy_timeout').fetchone()[0], 800)
        self.assertEqual(SCHEMA_VERSION, 13)


class InboundHTTPTests(Fixture):
    def listener(self, token=TOKEN):
        server = InboundServer(('127.0.0.1', 0), self.service, token)
        self.addCleanup(server.stop)
        return server

    def http(self, server, method='POST', path='/_internal/relations/request', token=TOKEN, body=None, raw=None):
        connection = http.client.HTTPConnection(*server.server_address[:2], timeout=3)
        headers = {'Content-Type': 'application/json'}
        if token is not None: headers['Authorization'] = 'Bearer '+token
        payload = raw if raw is not None else canonical(body or self.body())
        try:
            connection.request(method, path, payload, headers)
            response = connection.getresponse()
            raw = response.read()
            return response.status, dict(response.getheaders()), json.loads(raw) if raw else None
        finally:
            connection.close()

    def test_auth_fail_closed_and_no_token_logging(self):
        for configured in ('', 'short-fake-token', TOKEN):
            if len(configured) < 32:
                with self.assertLogs('botville_gateway.relations_inbound', level='WARNING') as logs:
                    server = self.listener(configured)
                if configured: self.assertNotIn(configured, '\n'.join(logs.output))
            else:
                server = self.listener(configured)
            for supplied in (None, 'wrong-fake-token', configured):
                if configured == TOKEN and supplied == TOKEN: continue
                with patch('botville_gateway.relations_inbound.logging.getLogger') as logger:
                    status, _, result = self.http(server, token=supplied)
                    logger.assert_not_called()
                self.assertEqual((status, result['errcode']), (401, 'M_UNKNOWN_TOKEN'))
                for secret in (configured, supplied):
                    if secret: self.assertNotIn(secret, json.dumps(result))

    def test_http_success_and_method_and_unknown_path(self):
        server = self.listener()
        self.assertEqual(self.http(server)[0], 202)
        for method in ('GET', 'PUT', 'DELETE', 'OPTIONS', 'HEAD', 'TRACE', 'CUSTOM'):
            status, headers, _ = self.http(server, method=method)
            self.assertEqual((status, headers['Allow']), (405, 'POST'))
        self.assertEqual(self.http(server, path='/v1/health')[0], 404)
        self.assertEqual(self.http(server, path='/_internal/relations/bogus')[0], 422)

    def test_body_limit_malformed_and_duplicate_json(self):
        server = self.listener()
        for raw in (b' '* (MAX_BYTES+1), b'{', b'{}', b'{"kind":1,"kind":2}', b'\xff'):
            self.assertEqual(self.http(server, raw=raw)[0], 400)

    def test_internal_exception_is_sanitized_500(self):
        server = self.listener()
        with patch.object(self.fed, 'inbound', side_effect=RuntimeError(TOKEN)):
            result = self.http(server)
        self.assertEqual(result[0], 500)
        self.assertNotIn(TOKEN, json.dumps(result))

    def test_all_internal_exceptions_are_500(self):
        server = self.listener()
        for error in (ValueError(TOKEN), TimeoutError(TOKEN), sqlite3.OperationalError(TOKEN)):
            with patch.object(self.fed, 'inbound', side_effect=error):
                result = self.http(server)
            self.assertEqual(result[0], 500)
            self.assertNotIn(TOKEN, json.dumps(result))

    def test_process_lock_contention_returns_500_within_budget(self):
        server = self.listener()
        held, release = threading.Event(), threading.Event()
        def hold():
            with self.store._lock:
                held.set()
                release.wait(3)
        thread = threading.Thread(target=hold)
        thread.start()
        self.assertTrue(held.wait(1))
        try:
            start = time.monotonic()
            self.assertEqual(self.http(server)[0], 500)
            self.assertLess(time.monotonic()-start, 1)
        finally:
            release.set()
            thread.join()
        self.assertEqual(self.count('relation_inbound_idempotency'), 0)

    def test_sqlite_write_contention_returns_500_within_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(str(Path(directory)/'busy.db'), hmac_secret=SECRET)
            service = Relations(store, personas(A), server_name=SERVER, clock=lambda:self.now)
            server = InboundServer(('127.0.0.1', 0), service, TOKEN)
            blocker = sqlite3.connect(store.path)
            try:
                blocker.execute('BEGIN IMMEDIATE')
                start = time.monotonic()
                self.assertEqual(self.http(server)[0], 500)
                self.assertLess(time.monotonic()-start, 1)
            finally:
                blocker.rollback()
                blocker.close()
                server.stop()
                store.close()

    def test_three_http_success_paths(self):
        server = self.listener()
        rid = self.outgoing()
        response = self.http(server, path='/_internal/relations/respond', body=self.body('respond', rid))
        self.assertEqual((response[0], response[2]['state']), (200, 'accepted'))
        response = self.http(server, path='/_internal/relations/revoke', body=self.body('revoke', rid, reason='unfriend'))
        self.assertEqual((response[0], response[2]['state']), (200, 'revoked'))
        self.assertEqual(self.http(server)[0], 202)

    def test_main_listener_hides_every_method(self):
        with MemoryHTTPServer() as app:
            self.assertIsNone(app.server.relations_inbound)
            for method in ('GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'TRACE', 'CUSTOM'):
                for token in ('', TOKEN):
                    for path in ('/_internal', '/_internal/relations/request', '/_internal_hidden', '/_internal%zz'):
                        self.assertEqual(app.request(method, path, token=token, scheme='Bearer')[0], 404)

    def test_gateway_listener_lifecycle(self):
        with MemoryHTTPServer(config_overrides={'relations_inbound_bind': '127.0.0.1:0',
                                                'relations_inbound_token': TOKEN}) as app:
            server = app.server.relations_inbound
            self.assertIsNotNone(server)
            self.assertNotEqual(server.server_address[1], app.server.server_address[1])
            self.assertEqual(self.http(server, path='/v1/health')[0], 404)
        self.assertFalse(server.thread.is_alive())
        self.assertEqual(server.socket.fileno(), -1)

    def test_config_bind_and_outbound_url_validation(self):
        for bind in ('localhost', ':80', 'x:bad', 'x:65536', 'x:-1', 'x:80/path', 'user@x:80', 'x:80?q', 'x :80'):
            with self.assertRaises(ConfigError): Config(dev_mode=True, relations_inbound_bind=bind)
        for bind in ('', '127.0.0.1:0', '[::1]:8091', 'localhost:80'):
            Config(dev_mode=True, relations_inbound_bind=bind)
        for url in ('ftp://pairing', 'file:///a', 'http://', 'http://user:secret@pairing', 'http://pairing:bad'):
            with self.assertRaises(ConfigError): Config(dev_mode=True, pairing_relations_url=url)
        config = load_config({'GATEWAY_DEV_MODE':'true', 'GATEWAY_RELATIONS_INBOUND_BIND':'127.0.0.1:0',
                              'GATEWAY_RELATIONS_INBOUND_TOKEN':TOKEN})
        self.assertEqual(config.relations_inbound_bind, '127.0.0.1:0')
        self.assertNotIn(TOKEN, repr(config))
