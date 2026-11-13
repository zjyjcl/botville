# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""Opt-in local group invitations; no identifiers or exception values in logs.

Uses the memory connection under its lock, with separate tables and no retention
purge: forgetting an invite would allow old AS transactions to join again.
One gateway consumer owns a memory DB. A committed claim precedes side effects;
a crash can leave an invitation unhandled but cannot replay join/leave.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time

from .as_consumer import ConsumerStats, DEFAULT_AS_SENDER_LOCALPART
from .engine.personas import _is_local_owner

_LOG = logging.getLogger(__name__)
RATE_LIMIT = 10
RATE_WINDOW = 60
MAX_RATE_PERSONAS = 4096


class GroupInvites:
    def __init__(self, store, transport, personas, *, enabled=False,
                 server_name=None, max_rooms=20, clock=time.monotonic):
        self.store, self.transport, self.personas = store, transport, personas
        self.enabled = enabled and store is not None
        self.server_name, self.max_rooms, self.clock = server_name, max_rooms, clock
        self.stats = ConsumerStats()
        self._lock = threading.RLock()
        self._rate = {}
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

    def is_accepted(self, persona, room):
        return self._state(persona, room) == 'accepted'

    def is_disabled(self, persona, room):
        return self._state(persona, room) in ('encrypted_disabled', 'rejected_encrypted')

    def _record(self, persona, room, inviter, state):
        now = time.time()
        with self.store._lock, self.store._db:
            self.store._db.execute('''INSERT INTO group_rooms
                (persona_id,room_key,state,inviter,joined_at,updated_at) VALUES(?,?,?,?,?,?)
                ON CONFLICT(persona_id,room_key) DO UPDATE SET
                state=CASE WHEN group_rooms.state='encrypted_disabled' THEN group_rooms.state
                           ELSE excluded.state END,
                inviter=excluded.inviter, joined_at=COALESCE(group_rooms.joined_at,excluded.joined_at),
                updated_at=excluded.updated_at''',
                (persona, self._key('room', room), state, self._key('inviter', inviter),
                 now if state == 'accepted' else None, now))

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
            with self.store._lock, self.store._db:
                claimed = self.store._db.execute(
                    'INSERT OR IGNORE INTO group_invites VALUES(?,?)',
                    (self._key('invite', pid, ctx.event_id), time.time())).rowcount
            if not claimed:
                self.stats.inc('duplicate')
                return
            # Count every new invitation, including policy rejects. The reason
            # chain remains deterministic; no unbounded event/persona buckets.
            limited = self._limited(pid)
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
            elif ':' in room and room.partition(':')[2].lower() != (self.server_name or '').lower():
                reason = 'cross_server'
            else:
                with self.store._lock:
                    count = self.store._db.execute(
                        "SELECT count(*) FROM group_rooms WHERE persona_id=? AND state IN ('accepted','encrypted_disabled')",
                        (pid,)).fetchone()[0]
                if count >= self.max_rooms and not self.is_accepted(pid, room):
                    reason = 'room_limit'
                elif limited:
                    reason = 'rate_limited'
            if reason:
                # Persist fail-closed before the network call, including failures.
                self._record(pid, room, inviter, 'rejected_encrypted' if reason == 'encrypted' else 'left')
                self._result(reason)
                try:
                    self.transport.leave(room, pid)
                except Exception:
                    self._result('leave_failed')
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
                self._record(pid, room, inviter, 'rejected_encrypted')
                self._result('rejected_encrypted')
                try:
                    self.transport.leave(room, pid)
                except Exception:
                    self._result('leave_failed')
                return
            self._record(pid, room, inviter, 'accepted')
            self._result('accepted')
