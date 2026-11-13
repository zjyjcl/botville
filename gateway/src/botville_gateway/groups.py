# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""Opt-in local group invitations; no identifiers or exception values in logs.

Uses the memory connection under its lock, with separate tables and no retention
purge: forgetting an invite would allow old AS transactions to join again.
One gateway consumer owns a memory DB. Pending attempts may replay after restart
or five minutes; Matrix join/leave are idempotent for joined/left rooms.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
import uuid

from .as_consumer import ConsumerStats, DEFAULT_AS_SENDER_LOCALPART
from .engine.personas import _is_local_owner
from .matrix import DeliveryError

_LOG = logging.getLogger(__name__)
RATE_LIMIT = 10
RATE_WINDOW = 60
MAX_RATE_PERSONAS = 4096


class GroupInvites:
    def __init__(self, store, transport, personas, *, enabled=False,
                 server_name=None, max_rooms=20, clock=time.monotonic, forget_stale_keys=False):
        self.store, self.transport, self.personas = store, transport, personas
        self.enabled = enabled and store is not None
        self.server_name, self.max_rooms, self.clock = server_name, max_rooms, clock
        self.stats = ConsumerStats()
        self._lock = threading.RLock()
        self._rate = {}
        self._attempt_owner = uuid.uuid4().hex
        if forget_stale_keys and store is not None:
            with store._lock, store._db:
                count = store._db.execute('DELETE FROM group_rooms WHERE key_id != ?',
                                          (store.group_key_id(),)).rowcount
            _LOG.info('Group stale key rows deleted: %d', count)
        if enabled and store is None:
            _LOG.warning('Groups disabled: memory database is not configured')

    def _key(self, purpose, *parts):
        secret = self.store._hmac_secret
        if not secret:
            raise ValueError('Groups require a persistent HMAC secret')
        if isinstance(secret, str):
            secret = secret.encode('utf-8')
        return hmac.new(secret, ('groups:' + purpose + ':' + json.dumps(
            parts, ensure_ascii=True, separators=(',', ':'))).encode(), hashlib.sha256).hexdigest()

    def _state(self, persona, room):
        if self.store is None:
            return None
        with self.store._lock:
            row = self.store._db.execute(
                'SELECT state FROM group_rooms WHERE persona_id=? AND room_key=?',
                (persona, self._key('room', room))).fetchone()
        return row[0] if row else None

    def stale_key_rows(self, persona):
        if self.store is None:
            return False
        with self.store._lock:
            return self.store._db.execute(
                'SELECT 1 FROM group_rooms WHERE persona_id=? AND key_id != ? LIMIT 1',
                (persona, self.store.group_key_id())).fetchone() is not None

    def quarantine_group(self, persona, room):
        return self.stale_key_rows(persona) and not self.is_accepted(persona, room)

    def _leave(self, room, persona):
        cleanup = getattr(self.transport, '_leave_for_cleanup', None)
        try:
            if cleanup is not None:
                cleanup(room, persona)
            else:
                self.transport.leave(room, persona)
        except DeliveryError as exc:
            if exc.code != 'M_FORBIDDEN':
                raise

    def is_accepted(self, persona, room):
        return self._state(persona, room) == 'accepted'

    def is_throttled(self, persona, room):
        entry = self.personas.get(persona)
        return bool(self.enabled and entry and entry.groups.get('enabled', False)
                    and self.is_accepted(persona, room))

    def is_disabled(self, persona, room):
        return self._state(persona, room) in ('encrypted_disabled', 'rejected_encrypted')

    def is_left(self, persona, room):
        return self._state(persona, room) == 'left'

    def on_membership(self, ctx):
        if (self.store is None or ctx.persona_user_id not in self.personas
                or not ctx.room_id or ctx.membership not in ('leave', 'ban')):
            return
        with self._lock, self.store._lock, self.store._db:
            self.store._db.execute("""UPDATE group_rooms SET state='left',updated_at=?
                WHERE persona_id=? AND room_key=?
                AND state IN ('accepted','encrypted_disabled','rejected_encrypted')""",
                (time.time(), ctx.persona_user_id, self._key('room', ctx.room_id)))

    def _done(self, key):
        with self.store._lock, self.store._db:
            self.store._db.execute(
                "UPDATE group_invites SET status='done',processed_at=? WHERE invite_key=?",
                (time.time(), key))

    def _record(self, persona, room, inviter, state):
        now = time.time()
        with self.store._lock, self.store._db:
            self.store._db.execute('''INSERT INTO group_rooms
                (persona_id,room_key,state,inviter,joined_at,updated_at,key_id) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(persona_id,room_key) DO UPDATE SET
                state=CASE WHEN group_rooms.state='encrypted_disabled' THEN group_rooms.state
                           ELSE excluded.state END,
                inviter=excluded.inviter, joined_at=COALESCE(group_rooms.joined_at,excluded.joined_at),
                updated_at=excluded.updated_at,key_id=excluded.key_id''',
                (persona, self._key('room', room), state, self._key('inviter', inviter),
                 now if state == 'accepted' else None, now, self.store.group_key_id()))

    def disable_encrypted(self, room):
        if self.store is None:
            return
        with self._lock, self.store._lock, self.store._db:
            changed = self.store._db.execute('''UPDATE group_rooms SET
                state='encrypted_disabled',updated_at=? WHERE room_key=? AND state='accepted' ''',
                (time.time(), self._key('room', room))).rowcount
        if changed:
            self._result('encrypted_disabled')

    def _result(self, reason):
        self.stats.inc(reason)
        _LOG.info('Group invitation: %s', reason)

    def _limited(self, persona):
        now = self.clock()
        self._rate = {key: stamps for key, times in self._rate.items()
                      if (stamps := [t for t in times if now - t < RATE_WINDOW])}
        if persona not in self._rate and len(self._rate) >= MAX_RATE_PERSONAS:
            return True
        stamps = self._rate.setdefault(persona, [])
        if len(stamps) >= RATE_LIMIT:
            return True
        stamps.append(now)
        return False

    def on_invite_to_ai(self, ctx):
        # Preserve B2.8a's DM no-op when the deployment switch is off.
        if not self.enabled:
            self.stats.inc('groups_disabled_noop')
            return
        persona = self.personas.get(ctx.persona_user_id)
        if persona is None or not all(isinstance(v, str) and v for v in (
                ctx.room_id, ctx.event_id, ctx.sender)):
            return
        pid, room, inviter = persona.user_id, ctx.room_id, ctx.sender
        with self._lock:
            key = self._key('invite', pid, ctx.event_id)
            now = time.time()
            with self.store._lock, self.store._db:
                row = self.store._db.execute(
                    'SELECT status,processed_at,attempt_owner FROM group_invites WHERE invite_key=?',
                    (key,)).fetchone()
                if row and (row[0] == 'done' or (
                        row[2] == self._attempt_owner and now - row[1] < 300)):
                    self.stats.inc('duplicate')
                    return
                self.store._db.execute("""INSERT INTO group_invites
                    (invite_key,processed_at,status,attempt_owner) VALUES(?,?,'pending',?)
                    ON CONFLICT(invite_key) DO UPDATE SET processed_at=excluded.processed_at,
                    attempt_owner=excluded.attempt_owner""", (key, now, self._attempt_owner))
            # Only new invitations consume the rate budget.
            limited = self._limited(pid) if row is None else False
            reason = None
            if not persona.groups.get('enabled', False):
                reason = 'groups_disabled'
            elif (ctx.trust != 'trusted' or not _is_local_owner(inviter, self.server_name)
                  or inviter[1:].partition(':')[0].lower() == DEFAULT_AS_SENDER_LOCALPART):
                reason = 'inviter_not_allowed'
            elif persona.groups.get('invite_policy', 'owner') == 'owner' and inviter != persona.owner_user_id:
                reason = 'inviter_not_allowed'
            elif ctx.invite_encrypted or self.is_disabled(pid, room):
                reason = 'encrypted'
            elif (not room.startswith('!') or not room.partition(':')[0][1:]
                  or not room.partition(':')[2]
                  or room.partition(':')[2].lower() != (self.server_name or '').lower()):
                reason = 'cross_server'
            else:
                with self.store._lock:
                    count = self.store._db.execute(
                        "SELECT count(*) FROM group_rooms WHERE persona_id=? AND key_id=? AND state IN ('accepted','encrypted_disabled')",
                        (pid, self.store.group_key_id())).fetchone()[0]
                if count >= self.max_rooms and not self.is_accepted(pid, room):
                    reason = 'room_limit'
                elif limited:
                    reason = 'rate_limited'
            if reason:
                if self.is_accepted(pid, room):
                    self._done(key)
                    self._result('ignored_for_accepted')
                    return
                # Persist fail-closed before the network call, including failures.
                self._record(pid, room, inviter, 'rejected_encrypted' if reason == 'encrypted' else 'left')
                self._result(reason)
                try:
                    self._leave(room, pid)
                except Exception:
                    self._result('leave_failed')
                    return
                self._done(key)
                return
            try:
                self.transport.join(room, pid)
            except Exception:
                self._result('join_failed')
                return
            try:
                encrypted = self.transport.room_is_encrypted(room, pid)
            except Exception:
                encrypted = True
            if encrypted:
                if self.is_accepted(pid, room):
                    self._done(key)
                    self._result('ignored_for_accepted')
                    return
                self._record(pid, room, inviter, 'rejected_encrypted')
                self._result('rejected_encrypted')
                try:
                    self._leave(room, pid)
                except Exception:
                    self._result('leave_failed')
                    return
                self._done(key)
                return
            self._record(pid, room, inviter, 'accepted')
            self._done(key)
            self._result('accepted')
