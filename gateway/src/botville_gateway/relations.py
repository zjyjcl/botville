# SPDX-License-Identifier: Apache-2.0
"""Owner relations: one locked SQLite transaction for each pair transition.

Identifiers and bodies are never logged. Expiry is persisted on reads and writes;
expected request failures still commit expiry, but never a partial owner action.
"""
from __future__ import annotations

import base64
import binascii
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import math
import re
import time
from urllib.parse import parse_qs

from .engine.personas import _MATRIX_LOCALPART_RE
from .memory.owner_http import HTTPError

WEEK = 7 * 86400
RELATION_ID = re.compile(r'[a-f0-9]{64}\Z')
_STATES = ('friend', 'pending', 'stranger', 'blocked')
_DIRECTIONS = ('none', 'outgoing', 'incoming')
_ACTIONS = ('approve', 'reject', 'revoke', 'block', 'unblock')
_LOG = logging.getLogger(__name__)


def relation_item(row):
    def stamp(value):
        return (datetime.fromtimestamp(value, timezone.utc).isoformat().replace('+00:00', 'Z')
                if value is not None else None)
    return dict(relation_id=row['relation_id'],
                counterpart=dict(user_id=row['counterpart_id'], kind=row['counterpart_kind'],
                                 server=row['counterpart_server']),
                state=row['state'], direction=row['direction'], origin=row['origin'],
                revision=row['revision'],
                expires_at=stamp(row['expires_at']) if row['state'] == 'pending' else None,
                revocable_until=stamp(row['revocable_until']),
                last_change=dict(kind=row['last_change_kind'], at=stamp(row['last_change_at'])),
                created_at=stamp(row['created_at']), updated_at=stamp(row['updated_at']))


class Relations:
    def __init__(self, store, personas, *, server_name=None, clock=time.time):
        self.store, self.personas = store, personas
        self.server_name, self.clock = server_name, clock

    @property
    def available(self):
        return bool(self.store is not None and self.store._hmac_secret)

    def _key(self, persona, counterpart):
        if not self.available:
            raise HTTPError(503, 'relation.unavailable', 5)
        secret = self.store._hmac_secret
        if isinstance(secret, str):
            secret = secret.encode('utf-8')
        return hmac.new(secret, ('relations:id:' + json.dumps(
            [persona, counterpart], ensure_ascii=True, separators=(',', ':'))).encode(),
            hashlib.sha256).hexdigest()

    def _rekey(self):
        key_id = self.store.group_key_id()
        rows = self.store._db.execute('SELECT * FROM relations WHERE key_id != ?', (key_id,)).fetchall()
        for row in rows:
            new_id = self._key(row['persona_id'], row['counterpart_id'])
            self.store._db.execute('UPDATE relation_events SET relation_id=? WHERE persona_id=? AND relation_id=?',
                                  (new_id, row['persona_id'], row['relation_id']))
            self.store._db.execute('UPDATE relations SET relation_id=?,key_id=? WHERE relation_id=?',
                                  (new_id, key_id, row['relation_id']))

    @contextmanager
    def _transaction(self, persona=None):
        if not self.available:
            raise HTTPError(503, 'relation.unavailable', 5)
        with self.store._lock:
            db = self.store._db
            db.execute('BEGIN IMMEDIATE')
            try:
                self._rekey()
                now = self.clock()
                if persona is not None:
                    self._expire(persona, now)
                yield now
            except HTTPError:
                # Business validation always precedes owner writes. Keep system
                # expiry/rekey changes even when a stale revision is rejected.
                db.commit()
                raise
            except BaseException:
                db.rollback()
                raise
            else:
                db.commit()

    def _row(self, persona, counterpart):
        return self.store._db.execute(
            'SELECT * FROM relations WHERE persona_id=? AND counterpart_id=? AND key_id=?',
            (persona, counterpart, self.store.group_key_id())).fetchone()

    def _lookup(self, persona, ref):
        if not isinstance(ref, str) or not RELATION_ID.fullmatch(ref):
            raise HTTPError(404, 'relation.not_found')
        row = self.store._db.execute(
            'SELECT * FROM relations WHERE persona_id=? AND relation_id=? AND key_id=?',
            (persona, ref, self.store.group_key_id())).fetchone()
        if row is None:
            raise HTTPError(404, 'relation.not_found')
        return row

    def _write(self, persona, counterpart, *, state, direction='none', origin=None,
               kind, now, actor='owner', counterpart_kind=None, rejected_at=None):
        old = self._row(persona, counterpart)
        ref = self._key(persona, counterpart)
        counterpart_kind = counterpart_kind or (old['counterpart_kind'] if old else 'ai')
        origin = origin or (old['origin'] if old else 'manual')
        rejected_at = rejected_at if rejected_at is not None else (old['rejected_at'] if old else None)
        values = (ref, self.store.group_key_id(), persona, counterpart, counterpart_kind,
                  counterpart.partition(':')[2], state, direction, origin,
                  now + WEEK if state == 'pending' else None, now + WEEK,
                  old['revision'] + 1 if old else 1, kind, now, rejected_at,
                  old['created_at'] if old else now, now)
        self.store._db.execute('''INSERT INTO relations (
            relation_id,key_id,persona_id,counterpart_id,counterpart_kind,counterpart_server,
            state,direction,origin,expires_at,revocable_until,revision,last_change_kind,
            last_change_at,rejected_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(persona_id,counterpart_id) DO UPDATE SET
            state=excluded.state,direction=excluded.direction,origin=excluded.origin,
            expires_at=excluded.expires_at,revocable_until=excluded.revocable_until,
            revision=excluded.revision,last_change_kind=excluded.last_change_kind,
            last_change_at=excluded.last_change_at,rejected_at=excluded.rejected_at,
            updated_at=excluded.updated_at''', values)
        self.store._db.execute('''INSERT INTO relation_events
            (persona_id,relation_id,kind,actor,from_state,to_state,at) VALUES (?,?,?,?,?,?,?)''',
            (persona, ref, kind, actor, old['state'] if old else None, state, now))
        return self._row(persona, counterpart)

    def _expire(self, persona, now):
        rows = self.store._db.execute("SELECT * FROM relations WHERE persona_id=? AND state='pending' AND expires_at<=?",
                                      (persona, now)).fetchall()
        for row in rows:
            for source, target in ((row['persona_id'], row['counterpart_id']),
                                   (row['counterpart_id'], row['persona_id'])):
                current = self._row(source, target)
                if current is not None and current['state'] == 'pending':
                    self._write(source, target, state='stranger', kind='expired', now=now, actor='system')

    def _counterpart(self, value):
        if not isinstance(value, str) or len(value) > 255 or not value.startswith('@'):
            raise HTTPError(400, 'request.invalid')
        local, sep, server = value[1:].partition(':')
        if (not sep or not _MATRIX_LOCALPART_RE.fullmatch(local) or not self.server_name
                or server.lower() != self.server_name.lower()):
            raise HTTPError(400, 'request.invalid')
        # Server aliases must not create a second relationship or disguise a
        # known AI as a human. Preserve the configured persona spelling.
        canonical = '@' + local + ':' + self.server_name.lower()
        for persona_id in self.personas:
            p_local, _, p_server = persona_id.partition(':')
            if p_local == '@' + local and p_server.lower() == server.lower():
                return persona_id
        return canonical

    def create(self, persona, body):
        if not isinstance(body, dict) or set(body) != {'counterpart_id'}:
            raise HTTPError(400, 'request.invalid')
        counterpart = self._counterpart(body['counterpart_id'])
        if counterpart == persona:
            raise HTTPError(400, 'request.invalid')
        with self._transaction(persona) as now:
            row = self._row(persona, counterpart)
            reverse = self._row(counterpart, persona)
            if ((reverse is not None and reverse['state'] == 'blocked')
                    or (row is not None and row['rejected_at'] is not None and row['rejected_at'] + WEEK > now)):
                raise HTTPError(409, 'relation.state_conflict')
            if row is not None and (row['state'] == 'blocked' or
                                    (row['state'] == 'pending' and row['direction'] == 'incoming')):
                raise HTTPError(409, 'relation.state_conflict')
            for source, target in ((persona, counterpart), (counterpart, persona)):
                self.store._db.execute('DELETE FROM relation_tombstones WHERE persona_id=? AND counterpart_id=?',
                                      (source, target))
            if row is not None and (row['state'] == 'friend' or
                                    (row['state'] == 'pending' and row['direction'] == 'outgoing')):
                return 200, relation_item(row)
            peer = self.personas.get(counterpart)
            ai = peer is not None
            pending = ai and peer.relation_defaults.get('friend_request_policy', 'approval_required') != 'autonomous'
            state, origin = ('pending', 'request') if pending else ('friend', 'manual')
            result = self._write(persona, counterpart, state=state,
                direction='outgoing' if pending else 'none', origin=origin, kind='request' if pending else 'manual_friend',
                now=now, counterpart_kind='ai' if ai else 'human')
            if ai:
                self._write(counterpart, persona, state=state,
                    direction='incoming' if pending else 'none', origin=origin,
                    kind='request' if pending else 'manual_friend', now=now, counterpart_kind='ai')
            return (200 if row is not None else 201), relation_item(result)

    def action(self, persona, ref, action, body):
        if not isinstance(body, dict) or set(body) != {'revision'} or type(body['revision']) is not int or body['revision'] < 1:
            raise HTTPError(400, 'request.invalid')
        if action not in _ACTIONS:
            raise HTTPError(404, 'relation.not_found')
        with self._transaction(persona) as now:
            row = self._lookup(persona, ref)
            if row['revision'] != body['revision']:
                raise HTTPError(409, 'relation.revision_conflict')
            state, direction = row['state'], row['direction']
            allowed = (action == 'block' or
                       (action in ('approve', 'reject') and state == 'pending' and direction == 'incoming') or
                       (action == 'revoke' and (state == 'friend' or (state == 'pending' and direction == 'outgoing'))) or
                       (action == 'unblock' and state == 'blocked'))
            if not allowed:
                raise HTTPError(409, 'relation.state_conflict')
            counterpart = row['counterpart_id']
            reverse = self._row(counterpart, persona) if row['counterpart_kind'] == 'ai' else None
            if action in ('approve', 'reject') and (reverse is None or reverse['state'] != 'pending'
                                                     or reverse['direction'] != 'outgoing'):
                raise HTTPError(409, 'relation.state_conflict')
            if action == 'revoke' and row['origin'] == 'default':
                for source, target in ((persona, counterpart), (counterpart, persona)):
                    self.store._db.execute('''INSERT INTO relation_tombstones VALUES (?,?,'revoked_default',?)
                        ON CONFLICT(persona_id,counterpart_id) DO UPDATE SET reason=excluded.reason,created_at=excluded.created_at''',
                        (source, target, now))
            new_state = 'friend' if action == 'approve' else 'blocked' if action == 'block' else 'stranger'
            result = self._write(persona, counterpart, state=new_state, kind=action, now=now)
            # Only the counterpart can clear its own block.
            if reverse is not None and reverse['state'] != 'blocked' and action != 'unblock':
                self._write(counterpart, persona, state='friend' if action == 'approve' else 'stranger',
                            kind=action, now=now, rejected_at=now if action == 'reject' else None)
            return relation_item(result)

    def get(self, persona, ref):
        with self._transaction(persona):
            return relation_item(self._lookup(persona, ref))

    def list(self, persona, query):
        params = parse_qs(query, keep_blank_values=True)
        if set(params) - {'state', 'direction', 'kind', 'limit', 'cursor'} or any(len(v) != 1 for v in params.values()):
            raise HTTPError(400, 'request.invalid')
        for name, choices in (('state', _STATES), ('direction', _DIRECTIONS), ('kind', ('ai', 'human'))):
            if name in params and params[name][0] not in choices:
                raise HTTPError(400, 'request.invalid')
        limit = params.get('limit', ['50'])[0]
        if not re.fullmatch(r'[0-9]{1,3}', limit) or not 1 <= int(limit) <= 200:
            raise HTTPError(400, 'request.invalid')
        after = None
        if 'cursor' in params:
            try:
                cursor = params['cursor'][0]
                if len(cursor) > 512 or not re.fullmatch(r'[A-Za-z0-9_-]+', cursor):
                    raise ValueError()
                after = json.loads(base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True))
                if (not isinstance(after, list) or len(after) != 2 or type(after[0]) not in (float, int)
                        or not math.isfinite(after[0]) or not isinstance(after[1], str) or not RELATION_ID.fullmatch(after[1])):
                    raise ValueError()
            except (ValueError, OverflowError, UnicodeError, binascii.Error):
                raise HTTPError(400, 'request.invalid') from None
        with self._transaction(persona):
            where, args = 'persona_id=? AND key_id=?', [persona, self.store.group_key_id()]
            for name in ('state', 'direction', 'kind'):
                if name in params:
                    where += ' AND ' + ('counterpart_kind' if name == 'kind' else name) + '=?'
                    args.append(params[name][0])
            if after is not None:
                where += ' AND (updated_at>? OR (updated_at=? AND relation_id>?))'
                args.extend((after[0], after[0], after[1]))
            rows = self.store._db.execute('SELECT * FROM relations WHERE ' + where +
                ' ORDER BY updated_at,relation_id LIMIT ?', (*args, int(limit) + 1)).fetchall()
            more, rows = len(rows) > int(limit), rows[:int(limit)]
            cursor = (base64.urlsafe_b64encode(json.dumps([rows[-1]['updated_at'], rows[-1]['relation_id']],
                      separators=(',', ':')).encode()).decode().rstrip('=') if more else None)
            return dict(items=[relation_item(row) for row in rows], next_cursor=cursor, has_more=more)

    def sync_known_personas(self, personas):
        if not self.available:
            _LOG.warning('Relations disabled: memory database or HMAC key unavailable')
            return
        with self._transaction() as now:
            current = sorted(p for p in personas if self.server_name and
                             p.partition(':')[2].lower() == self.server_name.lower())
            known = {row[0] for row in self.store._db.execute('SELECT persona_id FROM known_personas')}
            if not known:
                self.store._db.executemany('INSERT INTO known_personas VALUES (?,?,1)', ((p, now) for p in current))
                return
            for new in sorted(set(current) - known):
                for peer in current:
                    if new == peer:
                        continue
                    tombstone = self.store._db.execute('''SELECT 1 FROM relation_tombstones
                        WHERE (persona_id=? AND counterpart_id=?) OR (persona_id=? AND counterpart_id=?)''',
                        (new, peer, peer, new)).fetchone()
                    if tombstone or self._row(new, peer) is not None or self._row(peer, new) is not None:
                        continue
                    for source, target in ((new, peer), (peer, new)):
                        self._write(source, target, state='friend', origin='default', kind='default_friend',
                                    now=now, actor='system', counterpart_kind='ai')
                self.store._db.execute('INSERT INTO known_personas VALUES (?,?,0)', (new, now))
