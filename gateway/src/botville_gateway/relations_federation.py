# SPDX-License-Identifier: Apache-2.0
"""Cross-server business transitions. All helpers run under the relation transaction."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import secrets

from .matrix_appservice import AI_NAMESPACE_PREFIX
from .memory.owner_http import HTTPError, sliding_rate
from .relations import WEEK, relation_item
from .relations_outbound import BACKOFF

ORIGIN_PER_MINUTE = 60
PERSONA_PER_MINUTE = 20
RATE_WINDOW = 60
WIRE_STATES = frozenset(('pending', 'accepted', 'rejected', 'revoked', 'expired'))
_LOG = logging.getLogger(__name__)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def ulid(now):
    value = (int(now * 1000) << 80) | secrets.randbits(80)
    alphabet = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'
    return ''.join(alphabet[(value >> (5 * i)) & 31] for i in range(25, -1, -1))


def wire_state(row, metadata=None):
    """The only local -> wire mapping; local blocked/stranger never escape."""
    if row is not None:
        if row['state'] == 'friend':
            return 'accepted'
        if row['state'] == 'pending':
            return 'pending'
    return metadata['wire_state'] if metadata is not None else 'revoked'


def failure(status, code, request_id, *, state=None, retry=None):
    descriptions = {'bad_payload': 'Invalid relation payload', 'unsupported': 'Unsupported relation operation',
        'unknown_user': 'User unavailable', 'unknown_request': 'Request not found',
        'state_conflict': 'Relation state conflicts with operation', 'expired': 'Request expired',
        'peer_inactive': 'Peer is inactive', 'cooldown': 'Relation is cooling down',
        'rate_limited': 'Too many relation requests'}
    body = {'errcode': 'relation.' + code, 'error': descriptions.get(code, 'Relation unavailable'),
            'request_id': request_id}
    if state is not None:
        body['state'] = state
    if retry is not None:
        body['retry_after_s'] = retry
    return status, body


class FederationRelations:
    def __init__(self, relations):
        self.relations = relations
        self.worker = None
        self.rates = {}
        self.policy_allows = lambda persona, counterpart: True

    @property
    def db(self):
        return self.relations.store._db

    @property
    def configured(self):
        return self.worker is not None and self.worker.configured

    def metadata(self, persona, counterpart):
        return self.db.execute('SELECT * FROM relation_federation WHERE persona_id=? AND counterpart_id=?',
                               (persona, counterpart)).fetchone()

    def _metadata(self, persona, counterpart, rid, direction, state, expiry, cooldown=0):
        self.db.execute('''INSERT INTO relation_federation VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(persona_id,counterpart_id) DO UPDATE SET request_id=excluded.request_id,
            origin=excluded.origin,direction=excluded.direction,wire_state=excluded.wire_state,
            expiry=excluded.expiry,cooldown_until=excluded.cooldown_until''',
            (persona, counterpart, rid, counterpart.partition(':')[2].lower(), direction, state, expiry, cooldown))

    def _cancel(self, persona, counterpart):
        self.db.execute("UPDATE relation_outbox SET status='cancelled' WHERE persona_id=? AND counterpart_id=? AND status='pending'",
                        (persona, counterpart))

    def _enqueue(self, persona, counterpart, rid, kind, payload, expiry, now):
        self.db.execute('''INSERT INTO relation_outbox
            (persona_id,counterpart_id,destination,request_id,kind,payload,expiry,next_at)
            VALUES (?,?,?,?,?,?,?,?)''', (persona, counterpart, counterpart.partition(':')[2].lower(),
            rid, kind, canonical(payload).decode('utf-8'), expiry, now))

    def _set(self, persona, counterpart, state, now, *, direction=None, counterpart_kind=None, cooldown=0, actor='system', kind=None):
        meta = self.metadata(persona, counterpart)
        row = self.relations._row(persona, counterpart)
        # Owner blocks cannot be undone by a delayed remote result.
        if row is None or row['state'] != 'blocked':
            self.relations._write(persona, counterpart,
                state='friend' if state == 'accepted' else 'pending' if state == 'pending' else 'stranger',
                direction=(direction or meta['direction']) if state == 'pending' else 'none',
                origin='cross_server', kind=kind or state, now=now, actor=actor,
                counterpart_kind=counterpart_kind, rejected_at=now if cooldown else None)
            if state == 'pending':
                self.db.execute('UPDATE relations SET expires_at=? WHERE persona_id=? AND counterpart_id=?',
                                (meta['expiry'], persona, counterpart))
        self.db.execute('''UPDATE relation_federation SET wire_state=?,cooldown_until=max(cooldown_until,?)
            WHERE persona_id=? AND counterpart_id=?''', (state, cooldown, persona, counterpart))

    def expired(self, persona, counterpart):
        self.db.execute("UPDATE relation_federation SET wire_state='expired' WHERE persona_id=? AND counterpart_id=?",
                        (persona, counterpart))
        self.db.execute("UPDATE relation_outbox SET status='expired' WHERE persona_id=? AND counterpart_id=? AND status='pending'",
                        (persona, counterpart))

    def peer_severed(self, server):
        row = self.db.execute('SELECT status FROM relation_peers WHERE server_name=?', (server.lower(),)).fetchone()
        return row is not None and row['status'] == 'severed'

    def _mark_peer(self, server, status, now):
        server = server.lower()
        self.db.execute('''INSERT INTO relation_peers VALUES (?,?,?) ON CONFLICT(server_name)
            DO UPDATE SET status=excluded.status,changed_at=excluded.changed_at''', (server, status, now))
        if status == 'severed':
            for meta in self.db.execute('SELECT * FROM relation_federation WHERE origin=?', (server,)).fetchall():
                row = self.relations._row(meta['persona_id'], meta['counterpart_id'])
                if row is not None and row['state'] in ('pending', 'friend'):
                    self._set(meta['persona_id'], meta['counterpart_id'], 'revoked', now)
            self.db.execute("UPDATE relation_outbox SET status='cancelled' WHERE destination=? AND status='pending'", (server,))

    def mark_peer_severed(self, server):
        with self.relations._transaction() as now:
            self._mark_peer(server, 'severed', now)

    def mark_peer_active(self, server):
        with self.relations._transaction() as now:
            self._mark_peer(server, 'active', now)

    def create(self, persona, counterpart, now):
        if self.peer_severed(counterpart.partition(':')[2]):
            raise HTTPError(409, 'relation.peer_inactive')
        row, meta = self.relations._row(persona, counterpart), self.metadata(persona, counterpart)
        if (meta and meta['cooldown_until'] > now) or (row and row['state'] == 'blocked'):
            raise HTTPError(409, 'relation.state_conflict')
        if row and row['state'] in ('friend', 'pending'):
            if row['state'] == 'pending' and row['direction'] == 'incoming':
                raise HTTPError(409, 'relation.state_conflict')
            return 200, relation_item(row)
        # Retain the stored kind on re-add since it may come from an inbound declaration.
        if row is not None:
            counterpart_kind = row['counterpart_kind']
        else:
            counterpart_kind = 'ai' if counterpart.startswith(AI_NAMESPACE_PREFIX) else 'human'
        rid, expiry = ulid(now), int(now) + WEEK
        self._cancel(persona, counterpart)
        self._metadata(persona, counterpart, rid, 'outgoing', 'pending', expiry)
        self._set(persona, counterpart, 'pending', now, counterpart_kind=counterpart_kind, actor='owner', kind='request')
        self._enqueue(persona, counterpart, rid, 'request', dict(from_user=persona, to_user=counterpart,
            from_kind='ai', message='', created_at=int(now), expires_at=expiry), expiry, now)
        return 201, relation_item(self.relations._row(persona, counterpart))

    def owner_action(self, row, action, now):
        persona, counterpart = row['persona_id'], row['counterpart_id']
        meta = self.metadata(persona, counterpart)
        if meta is None:
            raise HTTPError(409, 'relation.state_conflict')
        self._cancel(persona, counterpart)
        state = 'accepted' if action == 'approve' else 'rejected' if action == 'reject' else 'revoked'
        cooldown = now + WEEK if action == 'reject' else 0
        if action in ('block', 'unblock'):
            self.db.execute("UPDATE relation_federation SET wire_state='revoked' WHERE persona_id=? AND counterpart_id=?",
                            (persona, counterpart))
            self.relations._write(persona, counterpart, state='blocked' if action == 'block' else 'stranger',
                                  kind=action, now=now)
        else:
            self._set(persona, counterpart, state, now, cooldown=cooldown, actor='owner', kind=action)
            payload = dict(from_user=persona, to_user=counterpart)
            kind = 'respond' if action in ('approve', 'reject') else 'revoke'
            if kind == 'respond':
                payload['decision'] = 'accept' if action == 'approve' else 'reject'
            else:
                payload['reason'] = 'withdrawn' if row['state'] == 'pending' else 'unfriend'
            self._enqueue(persona, counterpart, meta['request_id'], kind, payload, int(now) + WEEK, now)
        return relation_item(self.relations._row(persona, counterpart))

    def inbound(self, kind, envelope, meta):
        """Called only after structural and sender identity validation."""
        origin, rid, payload = envelope['origin'], envelope['request_id'], envelope['payload']
        digest = hashlib.sha256(canonical(payload)).hexdigest()
        from .relations_inbound import user_id
        payload = dict(payload, from_user=user_id(payload['from_user']), to_user=user_id(payload['to_user']))
        origin = origin.lower()
        for known in self.relations.personas:
            if user_id(known) == payload['to_user']:
                payload['to_user'] = known
                break
        persona, counterpart = payload['to_user'], payload['from_user']
        with self.relations._transaction(bounded=True) as now:
            self.db.execute('DELETE FROM relation_inbound_idempotency WHERE expires_at<?', (now,))
            cached = self.db.execute('''SELECT * FROM relation_inbound_idempotency
                WHERE origin=? AND request_id=? AND kind=?''', (origin, rid, kind)).fetchone()
            if cached:
                if cached['payload_hash'] != digest:
                    return failure(409, 'state_conflict', rid,
                        state=wire_state(self.relations._row(persona, counterpart), self.metadata(persona, counterpart)))
                return cached['status'], json.loads(cached['response'])
            self.relations._expire(persona, now)
            current = self.metadata(persona, counterpart)
            expiry = meta['sent_at'] + WEEK
            if kind == 'request':
                expiry = min(payload['expires_at'], expiry)
            elif kind == 'respond' and current and current['request_id'] == rid:
                expiry = current['expiry']
            result = self._inbound_business(kind, origin, rid, payload, expiry, now)
            self.db.execute('INSERT INTO relation_inbound_idempotency VALUES (?,?,?,?,?,?,?)',
                (origin, rid, kind, digest, result[0], canonical(result[1]).decode('utf-8'), expiry + WEEK))
            return result

    def _inbound_business(self, kind, origin, rid, payload, expiry, now):
        persona, counterpart = payload['to_user'], payload['from_user']
        row, current = self.relations._row(persona, counterpart), self.metadata(persona, counterpart)
        if self.peer_severed(origin):
            return failure(403, 'peer_inactive', rid)
        if (persona not in self.relations.personas or
                persona.partition(':')[2].lower() != (self.relations.server_name or '').lower() or
                (row is not None and row['state'] == 'blocked') or not self.policy_allows(persona, counterpart)):
            return failure(404, 'unknown_user', rid)
        retry = max(sliding_rate(self.rates, ('origin', origin), ORIGIN_PER_MINUTE, now, RATE_WINDOW),
                    sliding_rate(self.rates, ('persona', persona), PERSONA_PER_MINUTE, now, RATE_WINDOW))
        if retry:
            return failure(429, 'rate_limited', rid, retry=retry)
        if kind != 'request' and (current is None or current['request_id'] != rid):
            return failure(404, 'unknown_request', rid)
        unfriending = (kind == 'revoke' and payload['reason'] == 'unfriend'
                       and row is not None and row['state'] == 'friend')
        if now > expiry or (kind != 'request' and current['wire_state'] == 'expired' and not unfriending):
            return failure(410, 'expired', rid, state='expired')
        if kind == 'request':
            if current and current['cooldown_until'] > now:
                return failure(429, 'cooldown', rid, retry=math.ceil(current['cooldown_until'] - now))
            simultaneous = row is not None and row['state'] == 'pending' and row['direction'] == 'outgoing'
            if row is not None and row['state'] in ('friend', 'pending'):
                if not simultaneous or current is None or current['request_id'] <= rid:
                    return failure(409, 'state_conflict', current['request_id'] if simultaneous and current else rid,
                                   state=wire_state(row, current))
            self._cancel(persona, counterpart)
            autonomous = self.relations.personas[persona].relation_defaults.get('friend_request_policy') == 'autonomous'
            state = 'accepted' if autonomous and not simultaneous else 'pending'
            self._metadata(persona, counterpart, rid, 'incoming', state, expiry)
            self._set(persona, counterpart, state, now, counterpart_kind=payload['from_kind'])
            return (201, dict(request_id=rid, state=state)) if state == 'accepted' else (
                202, dict(accepted=True, request_id=rid, state=state))
        allowed = row is not None and (
            (kind == 'respond' and current['direction'] == 'outgoing' and row['state'] == 'pending') or
            (kind == 'revoke' and payload['reason'] == 'withdrawn' and row['state'] == 'pending'
             and current['direction'] == 'incoming') or unfriending)
        if not allowed:
            return failure(409, 'state_conflict', rid, state=wire_state(row, current))
        state = ('accepted' if payload['decision'] == 'accept' else 'rejected') if kind == 'respond' else 'revoked'
        self._cancel(persona, counterpart)
        self._set(persona, counterpart, state, now, cooldown=now + WEEK if state == 'rejected' else 0)
        return 200, dict(request_id=rid, state=state)

    def next_job(self, now):
        with self.relations._transaction():
            for job in self.db.execute("SELECT * FROM relation_outbox WHERE status='pending' AND expiry<?", (now,)).fetchall():
                self.db.execute("UPDATE relation_outbox SET status='expired' WHERE id=?", (job['id'],))
                meta = self.metadata(job['persona_id'], job['counterpart_id'])
                if meta and meta['request_id'] == job['request_id'] and meta['wire_state'] == 'pending':
                    self._set(job['persona_id'], job['counterpart_id'], 'expired', now)
            row = self.db.execute("SELECT * FROM relation_outbox WHERE status='pending' AND next_at<=? ORDER BY next_at,id LIMIT 1", (now,)).fetchone()
            return dict(row) if row else None

    def delivered(self, job, status, result, now):
        result = result if isinstance(result, dict) else {}
        with self.relations._transaction():
            delivered = status == 200 and result.get('delivered') is True
            if delivered:
                self._mark_peer(job['destination'], 'active', now)
            live = self.db.execute('SELECT status FROM relation_outbox WHERE id=?', (job['id'],)).fetchone()
            meta = self.metadata(job['persona_id'], job['counterpart_id'])
            if not live or live['status'] != 'pending' or not meta or meta['request_id'] != job['request_id']:
                return
            peer_status = result.get('peer_status')
            body = result.get('body', {})
            body = body if isinstance(body, dict) else {}
            undelivered = status == 200 and result.get('delivered') is False
            if (status == 409 and result.get('errcode') == 'BV_PEER_INACTIVE') or (
                    undelivered and peer_status == 403 and body.get('errcode') in ('BV_PEER_INACTIVE', 'M_FORBIDDEN')):
                self._mark_peer(job['destination'], 'severed', now)
                return
            retry = status in (502, 504) or (status == 429 and result.get('errcode') == 'M_LIMIT_EXCEEDED') or (
                undelivered and peer_status == 503)
            if retry and now <= job['expiry']:
                self.db.execute('UPDATE relation_outbox SET attempts=attempts+1,next_at=? WHERE id=?',
                    (now + BACKOFF[min(job['attempts'], len(BACKOFF)-1)], job['id']))
                return
            if status == 401 or (undelivered and peer_status == 401):
                _LOG.warning('Relations authorization failed; check pairing credentials or pair again')
            self.db.execute("UPDATE relation_outbox SET status='done',attempts=attempts+1 WHERE id=?", (job['id'],))
            state, cooldown = None, 0
            if delivered:
                state = 'pending' if peer_status == 202 else 'accepted' if peer_status == 201 else body.get('state') if peer_status == 200 else None
            elif undelivered:
                if peer_status == 409:
                    # Losing simultaneous request waits for the winning inbound request.
                    if body.get('request_id', job['request_id']) != job['request_id']:
                        return
                    state = body.get('state')
                elif peer_status == 410:
                    state = 'expired'
                elif peer_status == 404 and body.get('errcode') == 'relation.unknown_user':
                    state = 'revoked'
                elif peer_status == 429 and body.get('errcode') == 'relation.cooldown':
                    state = 'rejected'
                    seconds = body.get('retry_after_s', WEEK)
                    cooldown = now + (seconds if type(seconds) is int and seconds > 0 else WEEK)
            if state == 'rejected' and not cooldown:
                cooldown = now + WEEK
            if isinstance(state, str) and state in WIRE_STATES:
                self._set(job['persona_id'], job['counterpart_id'], state, now, cooldown=cooldown)
