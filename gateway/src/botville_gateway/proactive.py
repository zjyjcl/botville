# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""Opt-in, DM-only proactive scheduler with durable Matrix replay identity.

Queue lives alongside memory_items, but has its own additive migration and
connection lifecycle. Only one scheduler may own a deployment's memory DB.
No row content, identifiers or provider exception messages are logged.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .engine.personas import _is_local_owner
from .isolation import SYSTEM_ZONE_DECLARATION, build_untrusted_segment
from .policy import estimate_tokens, silent_hours_active

_LOG = logging.getLogger(__name__)
STOP_TIMEOUT_SECONDS = 35
LABELS = frozenset(('calm', 'happy', 'sad', 'excited', 'tired'))
INSTRUCTION = '主动向对方简短、自然地问候；不要编造事实，不要要求对方必须回复。'


class ProactiveWorker:
    def __init__(self, store, service, personas, client, config, *, members,
                 clock=lambda: datetime.now(timezone.utc), mood_label_provider=None,
                 close_store=False):
        self.store, self.service, self.personas = store, service, personas
        self.client, self.config, self.members = client, config, members
        self.clock, self.mood_label_provider = clock, mood_label_provider
        self.close_store = close_store
        self._stop = threading.Event()
        self._thread = None
        self._round_lock = threading.Lock()
        self._closed = False
        with store._lock, store._db:
            store._db.execute('''CREATE TABLE IF NOT EXISTS proactive_queue (
                key TEXT PRIMARY KEY, persona_id TEXT NOT NULL, target_user_id TEXT NOT NULL,
                room_id TEXT NOT NULL, slot INTEGER NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('queued','sending','done')),
                body TEXT, tokens INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL, sent_at REAL,
                UNIQUE(persona_id,target_user_id,slot))''')
            store._db.execute('''CREATE INDEX IF NOT EXISTS proactive_queue_target
                ON proactive_queue(persona_id,target_user_id,state)''')

    def start(self):
        if self.config.proactive_enabled and self._thread is None:
            self._thread = threading.Thread(target=self._run, name='proactive-worker', daemon=True)
            self._thread.start()

    def _close(self):
        if self.close_store and not self._closed:
            self.store.close()
            self._closed = True

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(STOP_TIMEOUT_SECONDS)
            if self._thread.is_alive():
                _LOG.warning('Proactive worker stop timeout; storage left open')
                return False
        self._close()
        return True

    def _run(self):
        try:
            while not self._stop.wait(self.config.proactive_tick_seconds):
                self.run_once()
        finally:
            self._close()

    def _rows(self, sql, args=()):
        with self.store._lock:
            return [dict(r) for r in self.store._db.execute(sql, args)]

    def _write(self, sql, args=()):
        with self.store._lock, self.store._db:
            self.store._db.execute(sql, args)

    def _dm(self, persona, target, room):
        return bool(room and set(self.members(room, (persona,))) == {persona, target})

    def candidates(self, persona, now):
        # Scope labels alone are not proof of a DM: older consumer versions can
        # label a one-AI multi-human room dm. Always verify complete membership.
        rows = self._rows('''SELECT counterpart,room_id,MAX(created_at) AS last_at,
            MAX(CASE WHEN role='user' AND sender=counterpart THEN created_at END) AS human_at
            FROM memory_items WHERE persona_id=? AND scope_kind='dm'
            GROUP BY counterpart,room_id ORDER BY last_at DESC''', (persona.user_id,))
        seen = set()
        for row in rows:
            target = row['counterpart']
            if target in seen or target in self.personas or not _is_local_owner(target, self.config.matrix_server_name):
                continue
            if target != persona.owner_user_id:
                if not self.config.proactive_recent_contacts_enabled or not row['human_at']:
                    continue
                human_at = datetime.fromisoformat(row['human_at'].replace('Z', '+00:00'))
                if not timedelta(0) <= now - human_at <= timedelta(days=7):
                    continue
            if not self._dm(persona.user_id, target, row['room_id']):
                continue
            seen.add(target)
            yield row
        if persona.owner_user_id and persona.owner_user_id not in seen:
            _LOG.info('Proactive owner skipped: no confirmed existing DM')

    def _eligible(self, persona, contact, now, key):
        settings = persona.proactive
        if silent_hours_active(settings.get('quiet_hours', {}), now):
            return False
        last = datetime.fromisoformat(contact['last_at'].replace('Z', '+00:00'))
        if (now - last).total_seconds() < self.config.proactive_min_idle_minutes * 60:
            return False
        target = contact['counterpart']
        history = self._rows('''SELECT * FROM proactive_queue WHERE persona_id=?
            AND state IN ('sending','done') AND key!=?''', (persona.user_id, key))
        # Successful proactive delivery is itself an interaction, even though
        # its body is deliberately not duplicated into conversational memory.
        if any(r['target_user_id'] == target and r['sent_at'] is not None and
               now.timestamp() - r['sent_at'] < self.config.proactive_min_idle_minutes * 60
               for r in history):
            return False
        tz = ZoneInfo(settings.get('quiet_hours', {}).get('timezone', self.config.silent_hours_timezone))
        day = now.astimezone(tz).date()
        daily = [r for r in history if datetime.fromtimestamp(
            r['sent_at'] if r['sent_at'] is not None else r['created_at'], timezone.utc).astimezone(tz).date() == day]
        if len(daily) >= settings.get('max_per_day', 6):
            return False
        if sum(r['target_user_id'] == target for r in daily) >= self.config.proactive_per_target_per_day:
            return False
        if any(now.timestamp() - (r['sent_at'] if r['sent_at'] is not None else r['created_at']) <
               self.config.proactive_min_interval_minutes * 60 for r in history):
            return False
        if settings.get('require_reply', True):
            human = datetime.fromisoformat(contact['human_at'].replace('Z', '+00:00')).timestamp() if contact['human_at'] else 0
            if any(r['target_user_id'] == target and human <= (
                    r['sent_at'] if r['sent_at'] is not None else r['created_at']) for r in history):
                return False
        return True

    def run_once(self):
        if not self.config.proactive_enabled or self._stop.is_set() or not self.client.config.configured:
            return 0
        if not self._round_lock.acquire(blocking=False):
            return 0
        sent = 0
        try:
            now = self.clock()
            for persona in self.personas.values():
                if not persona.proactive.get('enabled', False):
                    continue
                try:
                    contacts = list(self.candidates(persona, now))
                except Exception:
                    _LOG.info('Proactive candidates unavailable')
                    continue
                for contact in contacts:
                    if self._stop.is_set():
                        return sent
                    try:
                        sent += self._process(persona, contact, now)
                    except Exception:
                        _LOG.warning('Proactive attempt incomplete; retry retained')
            return sent
        finally:
            self._round_lock.release()

    def _process(self, persona, contact, now):
        target, room = contact['counterpart'], contact['room_id']
        pending = self._rows('''SELECT * FROM proactive_queue WHERE persona_id=?
            AND target_user_id=? AND state!='done' ORDER BY slot LIMIT 1''', (persona.user_id, target))
        slot = int(now.timestamp()) // self.config.proactive_tick_seconds
        key = hashlib.sha256(json.dumps([persona.user_id, target, slot], separators=(',', ':')).encode()).hexdigest()
        if pending:
            row, key = pending[0], pending[0]['key']
            # Replay never changes the room component of Matrix's identity.
            if row['room_id'] != room:
                return 0
        else:
            existing = self._rows('SELECT * FROM proactive_queue WHERE key=?', (key,))
            if existing:
                return 0
            row = None
        if not self._eligible(persona, contact, now, key):
            return 0
        system = SYSTEM_ZONE_DECLARATION + '\n' + persona.instruction_text() + '\n' + INSTRUCTION
        data = '现在可以发送一条简短的主动问候。'
        if self.mood_label_provider is not None:
            label = self.mood_label_provider(persona.user_id)
            if isinstance(label, str) and label in LABELS:
                data += '\nmood: ' + label
        wrapped, _ = build_untrusted_segment(data)
        messages = [{'role': 'user', 'content': wrapped}]
        estimate = (estimate_tokens(system) + estimate_tokens(wrapped) + persona.max_reply_chars)
        if row and row['body'] is not None:
            estimate = row['tokens']
        reservation = self.service.reserve_proactive(
            persona_id=persona.user_id, conversation_id=room, peer_id=target, token_estimate=estimate)
        if reservation is None:
            return 0
        try:
            if row is None:
                self._write('''INSERT OR IGNORE INTO proactive_queue
                    (key,persona_id,target_user_id,room_id,slot,state,created_at)
                    VALUES (?,?,?,?,?,'queued',?)''', (key, persona.user_id, target, room, slot, now.timestamp()))
            body = row['body'] if row else None
            if body is None:
                body = self.client.complete(system_text=system, messages=messages, persona=persona,
                                            max_tokens=persona.max_reply_chars)[:persona.max_reply_chars]
            if self._stop.is_set() or not self._dm(persona.user_id, target, room):
                return 0
            self._write("UPDATE proactive_queue SET state='sending',body=?,tokens=? WHERE key=?", (body, estimate, key))
            receipt = self.service.send_ai_message(
                conversation_id=room,
                payload={'sender_persona_id': persona.user_id, 'peer_id': target,
                         'content': {'type': 'text', 'text': body},
                         'token_estimate': estimate, 'allow_deferred': False},
                idempotency_key='proactive:' + key, proactive_reservation=reservation)
            if receipt['outcome'] not in ('sent', 'delivered'):
                return 0
            self._write("UPDATE proactive_queue SET state='done',body=NULL,sent_at=? WHERE key=?", (self.clock().timestamp(), key))
            return 1
        finally:
            self.service.release_proactive(reservation)
