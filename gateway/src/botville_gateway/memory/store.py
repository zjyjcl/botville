# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""Private SQLite memory, serialized within one gateway process per database.

The owner CLI is intended for offline administration. Database/WAL/backups carry
sensitive user data. No operation logs identifiers or stored content.
"""
from __future__ import annotations

import json
import hashlib
import hmac
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

MEMORY_PURGE_INTERVAL_MIN_SECONDS = 60
SCHEMA_VERSION = 7
OWNER_PIN_LIMIT = 200
_log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


# v1 is the initial dialogue/profile schema. v2 adds explicit dialogue visibility.
# Each migration is additive and committed together with its version marker.
_MIGRATIONS = {
    1: (
        '''CREATE TABLE IF NOT EXISTS memory_items (
            id INTEGER PRIMARY KEY, persona_id TEXT NOT NULL,
            scope_kind TEXT NOT NULL CHECK(scope_kind IN ('dm','group')),
            scope_key TEXT NOT NULL, room_id TEXT, counterpart TEXT, event_id TEXT,
            sender TEXT, role TEXT NOT NULL CHECK(role IN ('user','assistant')),
            trust TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL,
            pinned INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL)''',
        '''CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_item_event
           ON memory_items(persona_id,event_id) WHERE event_id IS NOT NULL''',
        '''CREATE INDEX IF NOT EXISTS idx_memory_items_scope
           ON memory_items(persona_id,scope_kind,scope_key,created_at)''',
        '''CREATE TABLE IF NOT EXISTS memory_facts (
            id INTEGER PRIMARY KEY, persona_id TEXT NOT NULL, counterpart TEXT NOT NULL,
            key TEXT NOT NULL, value TEXT NOT NULL, visibility TEXT NOT NULL DEFAULT 'dm_only',
            source TEXT NOT NULL, source_event_ids TEXT NOT NULL DEFAULT '[]',
            trust TEXT NOT NULL DEFAULT 'trusted', pinned INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(persona_id,counterpart,key))''',
        '''CREATE TABLE IF NOT EXISTS memory_tombstones (
            event_id TEXT PRIMARY KEY, persona_id TEXT, room_id TEXT,
            created_at TEXT NOT NULL)''',
    ),
    2: (
        '''CREATE INDEX IF NOT EXISTS idx_memory_items_expiry
           ON memory_items(created_at) WHERE pinned=0''',
        '''CREATE INDEX IF NOT EXISTS idx_memory_facts_expiry
           ON memory_facts(updated_at) WHERE pinned=0''',
        '''CREATE INDEX IF NOT EXISTS idx_memory_tombstones_expiry
           ON memory_tombstones(created_at)''',
    ),
    # PR3b consolidation state.  The watermark is deliberately on the raw
    # item, rather than in a process-local queue, so restarts cannot re-extract.
    3: (
        '''CREATE TABLE IF NOT EXISTS memory_summaries (
            id INTEGER PRIMARY KEY, persona_id TEXT NOT NULL,
            scope_kind TEXT NOT NULL CHECK(scope_kind IN ('dm','group')),
            scope_key TEXT NOT NULL, text TEXT NOT NULL, trust TEXT NOT NULL,
            covers_until_item_id INTEGER NOT NULL, dirty INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL, model TEXT NOT NULL,
            UNIQUE(persona_id,scope_kind,scope_key))''',
        '''CREATE INDEX IF NOT EXISTS idx_memory_summaries_scope
           ON memory_summaries(persona_id,scope_kind,scope_key,dirty)''',
        '''CREATE TABLE IF NOT EXISTS memory_vectors (
            id INTEGER PRIMARY KEY, item_id INTEGER, summary_id INTEGER,
            persona_id TEXT NOT NULL, scope_kind TEXT NOT NULL,
            scope_key TEXT NOT NULL, model TEXT NOT NULL, dim INTEGER NOT NULL,
            vec BLOB NOT NULL, created_at TEXT NOT NULL,
            UNIQUE(item_id), UNIQUE(summary_id))''',
        '''CREATE INDEX IF NOT EXISTS idx_memory_vectors_scope
           ON memory_vectors(persona_id,scope_kind,scope_key,id)''',
        '''CREATE TRIGGER IF NOT EXISTS memory_vectors_item_delete AFTER DELETE ON memory_items
           BEGIN DELETE FROM memory_vectors WHERE item_id=OLD.id; END''',
        '''CREATE TRIGGER IF NOT EXISTS memory_vectors_summary_delete AFTER DELETE ON memory_summaries
           BEGIN DELETE FROM memory_vectors WHERE summary_id=OLD.id; END''',
        '''CREATE TRIGGER IF NOT EXISTS memory_vectors_summary_update AFTER UPDATE ON memory_summaries
           BEGIN DELETE FROM memory_vectors WHERE summary_id=OLD.id; END''',
    ),
    4: (
        "UPDATE memory_facts SET trust='owner' WHERE source='owner'",
        "UPDATE memory_facts SET trust='derived' WHERE source='extraction'",
    ),
    # Owner HTTP management is deliberately additive.  Public IDs are filled
    # with a deployment HMAC after this migration, never with row identities.
    5: (
        '''CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_items_public_id ON memory_items(public_id)''',
        '''CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_facts_public_id ON memory_facts(public_id)''',
        '''CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_summaries_public_id ON memory_summaries(public_id)''',
        '''CREATE TABLE IF NOT EXISTS memory_suppressed (
            persona_id TEXT NOT NULL, counterpart TEXT NOT NULL, key TEXT NOT NULL,
            created_at TEXT NOT NULL, PRIMARY KEY(persona_id,counterpart,key))''',
        '''CREATE TABLE IF NOT EXISTS memory_audit (
            occurred_at TEXT NOT NULL, request_id TEXT, caller TEXT NOT NULL,
            persona_id TEXT NOT NULL, action TEXT NOT NULL, memory_id TEXT,
            layer TEXT, result_code TEXT NOT NULL, counterpart_hmac TEXT)''',
        '''CREATE INDEX IF NOT EXISTS idx_memory_audit_occurred ON memory_audit(occurred_at)''',
    ),
    6: (
    ),
    7: (
        '''CREATE TABLE IF NOT EXISTS memory_id_sequences (table_name TEXT PRIMARY KEY, next_id INTEGER NOT NULL)''',
    ),
}


class MemoryStore:
    def __init__(self, path: str, retention_days: int = 90, *, hmac_secret: Any = '') -> None:
        if (not isinstance(retention_days, int) or isinstance(retention_days, bool)
                or retention_days < 1):
            raise ValueError('memory retention must be a positive integer')
        if not isinstance(path, str) or not path or path.startswith('file:'):
            raise ValueError('memory database requires a filesystem path')
        self.path = path
        self.retention_days = retention_days
        self._hmac_secret = hmac_secret or os.environ.get('GATEWAY_HMAC_SECRET', '')
        if not hmac_secret and isinstance(self._hmac_secret, str) and self._hmac_secret:
            try:
                self._hmac_secret = bytes.fromhex(self._hmac_secret)
            except ValueError:
                # Configuration validation rejects non-hex deployment secrets;
                # retain compatibility for direct local test construction.
                self._hmac_secret = self._hmac_secret.encode('utf-8')
        self._lock = threading.RLock()
        if path != ':memory:':
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(str(target), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
        self._tighten_permissions()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        try:
            # SQLite can reject concurrent journal-mode changes without honoring
            # busy_timeout. Bound retries for that startup-only lock race.
            deadline = time.monotonic() + 5
            while True:
                try:
                    self._db.execute('PRAGMA journal_mode=WAL')
                    break
                except sqlite3.OperationalError as exc:
                    if str(exc) != 'database is locked' or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.01)
            self._db.execute('PRAGMA synchronous=FULL')
            self._db.execute('PRAGMA foreign_keys=ON')
            self._migrate()
            self._backfill_public_ids()
            self._tighten_permissions()
        except Exception:
            self._db.close()
            raise

    def _tighten_permissions(self) -> None:
        if self.path == ':memory:':
            return
        for name in (self.path, self.path + '-wal', self.path + '-shm'):
            try:
                os.chmod(name, 0o600)
            except FileNotFoundError:
                if name == self.path:
                    raise

    def _migrate(self) -> None:
        with self._lock, self._db:
            self._db.execute('BEGIN IMMEDIATE')
            self._db.execute('CREATE TABLE IF NOT EXISTS memory_schema(version INTEGER NOT NULL)')
            versions = [row[0] for row in self._db.execute('SELECT version FROM memory_schema')]
            if any(not isinstance(v, int) or v < 0 or v > SCHEMA_VERSION for v in versions):
                raise RuntimeError('unsupported memory schema version')
            # Legacy duplicate rows can reflect an interrupted concurrent first open.
            # Replay additive migrations from the oldest marker under a write lock.
            version = min(versions, default=0)
            columns = {row[1] for row in self._db.execute('PRAGMA table_info(memory_schema)')}
            if 'id' not in columns:
                self._db.execute('DROP TABLE memory_schema')
                self._db.execute('CREATE TABLE memory_schema(id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL)')
            self._db.execute('INSERT OR IGNORE INTO memory_schema(id,version) VALUES(1,?)', (version,))
            for next_version in range(version + 1, SCHEMA_VERSION + 1):
                if next_version == 5:
                    for table in ('memory_items', 'memory_facts', 'memory_summaries'):
                        columns = {row[1] for row in self._db.execute('PRAGMA table_info(' + table + ')')}
                        if 'public_id' not in columns:
                            self._db.execute('ALTER TABLE ' + table + ' ADD COLUMN public_id TEXT')
                if next_version == 6:
                    columns = {row[1] for row in self._db.execute('PRAGMA table_info(memory_items)')}
                    if 'updated_at' not in columns:
                        self._db.execute('ALTER TABLE memory_items ADD COLUMN updated_at TEXT')
                    summary_columns = {row[1] for row in self._db.execute('PRAGMA table_info(memory_summaries)')}
                    if 'created_at' not in summary_columns:
                        self._db.execute('ALTER TABLE memory_summaries ADD COLUMN created_at TEXT')
                for statement in _MIGRATIONS[next_version]:
                    self._db.execute(statement)
                if next_version == 2:
                    columns = {row[1] for row in self._db.execute('PRAGMA table_info(memory_items)')}
                    if 'visibility' not in columns:
                        self._db.execute("ALTER TABLE memory_items ADD COLUMN visibility TEXT NOT NULL DEFAULT 'dm_only'")
                        self._db.execute("UPDATE memory_items SET visibility='shared' WHERE scope_kind='group'")
                if next_version == 3:
                    columns = {row[1] for row in self._db.execute('PRAGMA table_info(memory_items)')}
                    if 'extracted' not in columns:
                        self._db.execute('ALTER TABLE memory_items ADD COLUMN extracted INTEGER NOT NULL DEFAULT 0')
                    # A summary can only claim a contiguous prefix.  Removing
                    # any covered row makes it unsafe until the idle worker
                    # recomputes it from remaining rows.
                    self._db.execute('''CREATE TRIGGER IF NOT EXISTS memory_summary_item_delete
                    AFTER DELETE ON memory_items BEGIN
                      UPDATE memory_summaries SET dirty=1
                      WHERE persona_id=OLD.persona_id AND scope_kind=OLD.scope_kind
                        AND scope_key=OLD.scope_key AND covers_until_item_id>=OLD.id;
                    END''')
                self._db.execute('UPDATE memory_schema SET version=?', (next_version,))

            columns = {row[1] for row in self._db.execute('PRAGMA table_info(memory_tombstones)')}
            if 'permanent' not in columns:
                self._db.execute('ALTER TABLE memory_tombstones ADD COLUMN permanent INTEGER NOT NULL DEFAULT 0')
            self._db.execute('UPDATE memory_items SET updated_at=created_at WHERE updated_at IS NULL')
            self._db.execute('UPDATE memory_summaries SET created_at=updated_at WHERE created_at IS NULL')
            for table in ('memory_items', 'memory_summaries', 'memory_facts'):
                self._db.execute('''INSERT OR IGNORE INTO memory_id_sequences(table_name,next_id)
                    VALUES(?, COALESCE((SELECT max(id) FROM memory_items), 0) + 1)''' if table == 'memory_items'
                    else '''INSERT OR IGNORE INTO memory_id_sequences(table_name,next_id)
                    VALUES(?, COALESCE((SELECT max(id) FROM memory_summaries), 0) + 1)''' if table == 'memory_summaries'
                    else '''INSERT OR IGNORE INTO memory_id_sequences(table_name,next_id)
                    VALUES(?, COALESCE((SELECT max(id) FROM memory_facts), 0) + 1)''', (table,))

    def _next_id(self, table: str) -> int:
        row = self._db.execute('SELECT next_id FROM memory_id_sequences WHERE table_name=?', (table,)).fetchone()
        if row is None:
            raise RuntimeError('memory ID sequence unavailable')
        self._db.execute('UPDATE memory_id_sequences SET next_id=? WHERE table_name=?', (row['next_id'] + 1, table))
        return row['next_id']

    def _public_id(self, layer: str, row_id: int) -> str:
        if not self._hmac_secret:
            raise RuntimeError('memory owner IDs require GATEWAY_HMAC_SECRET')
        secret = self._hmac_secret if isinstance(self._hmac_secret, bytes) else self._hmac_secret.encode('utf-8')
        digest = hmac.new(secret,
                          (layer + ':' + str(row_id)).encode('ascii'), hashlib.sha256).hexdigest()[:16]
        return {'recent': 'mr_', 'summary': 'ms_', 'profile': 'mf_'}[layer] + digest

    def _backfill_public_ids(self) -> None:
        """Persist opaque management IDs so lookup remains indexed after restart."""
        if not self._hmac_secret:
            return
        with self._lock, self._db:
            for table, layer in (('memory_items', 'recent'), ('memory_summaries', 'summary'),
                                 ('memory_facts', 'profile')):
                for row in self._db.execute('SELECT id FROM ' + table + ' WHERE public_id IS NULL'):
                    self._db.execute('UPDATE ' + table + ' SET public_id=? WHERE id=?',
                                     (self._public_id(layer, row['id']), row['id']))

    def set_hmac_secret(self, secret: Any) -> None:
        """Install the deployment HMAC and backfill IDs before owner HTTP use."""
        if not isinstance(secret, (str, bytes)) or not secret:
            raise ValueError('memory owner IDs require an HMAC secret')
        with self._lock:
            old = self._hmac_secret.encode('utf-8') if isinstance(self._hmac_secret, str) else self._hmac_secret
            new = secret.encode('utf-8') if isinstance(secret, str) else secret
            if old and not hmac.compare_digest(old, new):
                raise RuntimeError('memory owner HMAC secret changed')
            self._hmac_secret = secret
            self._backfill_public_ids()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def record_inbound(self, **kwargs: Any) -> bool:
        kwargs['role'] = 'user'
        return self.add_item(**kwargs)

    def record_outbound(self, **kwargs: Any) -> bool:
        kwargs['role'] = 'assistant'
        return self.add_item(**kwargs)

    def is_tombstoned(self, event_id: Optional[str]) -> bool:
        with self._lock:
            return bool(event_id and self._db.execute(
                'SELECT 1 FROM memory_tombstones WHERE event_id=?', (event_id,)
            ).fetchone())

    def add_item(
        self, *, persona_id: str, scope_kind: str, scope_key: str,
        room_id: Optional[str], counterpart: Optional[str], event_id: Optional[str],
        sender: Optional[str], role: str, trust: str, text: str,
        source: str = 'event', pinned: bool = False,
    ) -> bool:
        if (scope_kind not in ('dm', 'group') or not scope_key or not persona_id
                or role not in ('user', 'assistant') or trust not in ('trusted', 'untrusted')
                or not isinstance(text, str)):
            raise ValueError('invalid memory item')
        if event_id is not None and (not isinstance(event_id, str) or not event_id):
            raise ValueError('invalid memory event identity')
        # 'event' is an adapter shorthand; persisted source records actual provenance.
        source = (event_id or 'owner') if source == 'event' else source
        visibility = 'dm_only' if scope_kind == 'dm' else 'shared'
        with self._lock, self._db:
            self._db.execute('BEGIN IMMEDIATE')
            if self.is_tombstoned(event_id):
                return False
            cursor = self._db.execute('''INSERT INTO memory_items
                (id,persona_id,scope_kind,scope_key,room_id,counterpart,event_id,sender,
                 role,trust,text,created_at,pinned,source,visibility,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(persona_id,event_id) WHERE event_id IS NOT NULL DO NOTHING''',
                (self._next_id('memory_items'), persona_id, scope_kind, scope_key, room_id, counterpart, event_id,
                 sender, role, trust, text[:8000], _now(), int(pinned), source, visibility, _now()))
            if cursor.rowcount and self._hmac_secret:
                self._db.execute('UPDATE memory_items SET public_id=? WHERE id=?',
                                 (self._public_id('recent', cursor.lastrowid), cursor.lastrowid))
            return bool(cursor.rowcount)

    def items(self, persona_id: str, *, scope_kind: Optional[str] = None,
              scope_key: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        query = 'SELECT * FROM memory_items WHERE persona_id=?'
        args = [persona_id]
        if scope_kind is not None:
            query += ' AND scope_kind=?'
            args.append(scope_kind)
        if scope_key is not None:
            query += ' AND scope_key=?'
            args.append(scope_key)
        query += ' ORDER BY pinned DESC,created_at DESC,id DESC'
        if limit is not None:
            query += ' LIMIT ?'
            args.append(limit)
        with self._lock:
            return [dict(row) for row in self._db.execute(query, args)]

    def items_for_summary(self, persona_id: str, scope_kind: str, scope_key: str,
                          after_id: int = 0, limit: int = 1000) -> List[Dict[str, Any]]:
        """Chronological raw items not covered by the current clean summary."""
        with self._lock:
            return [dict(row) for row in self._db.execute('''SELECT * FROM memory_items
                WHERE persona_id=? AND scope_kind=? AND scope_key=? AND id>?
                ORDER BY id ASC LIMIT ?''', (persona_id, scope_kind, scope_key, after_id, limit))]

    def summary(self, persona_id: str, scope_kind: str, scope_key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._db.execute('''SELECT * FROM memory_summaries WHERE persona_id=?
                AND scope_kind=? AND scope_key=?''', (persona_id, scope_kind, scope_key)).fetchone()
            return dict(row) if row else None

    def summary_scopes(self, limit: int = 8) -> List[Dict[str, Any]]:
        """Return scopes with new raw content or a dirty derived summary."""
        with self._lock:
            return [dict(row) for row in self._db.execute('''SELECT i.persona_id,i.scope_kind,i.scope_key
                FROM memory_items i LEFT JOIN memory_summaries s ON s.persona_id=i.persona_id
                 AND s.scope_kind=i.scope_kind AND s.scope_key=i.scope_key
                GROUP BY i.persona_id,i.scope_kind,i.scope_key
                HAVING max(i.id)>coalesce(max(s.covers_until_item_id),0) OR max(coalesce(s.dirty,0))=1
                LIMIT ?''', (limit,))]

    def save_summary(self, *, persona_id: str, scope_kind: str, scope_key: str,
                     text: str, trust: str, covers_until_item_id: int, model: str,
                     source_snapshot: Optional[List[tuple]] = None,
                     previous_updated_at: Optional[str] = None,
                     previous_dirty: Optional[int] = None) -> bool:
        if trust not in ('derived', 'untrusted') or not text or covers_until_item_id < 1:
            raise ValueError('invalid memory summary')
        with self._lock, self._db:
            self._db.execute('BEGIN IMMEDIATE')
            # Model calls happen outside the lock. Compare all source identities
            # (not merely the final id, which SQLite may reuse) before committing.
            for item_id, event_id, item_text in source_snapshot or []:
                row = self._db.execute('''SELECT event_id,text FROM memory_items WHERE id=? AND persona_id=?
                  AND scope_kind=? AND scope_key=?''', (item_id, persona_id, scope_kind, scope_key)).fetchone()
                if not row or row['event_id'] != event_id or row['text'] != item_text:
                    return False
            old = self._db.execute('''SELECT updated_at,dirty FROM memory_summaries WHERE persona_id=?
              AND scope_kind=? AND scope_key=?''', (persona_id, scope_kind, scope_key)).fetchone()
            if (previous_updated_at is None and old is not None) or (
                    previous_updated_at is not None and (not old or old['updated_at'] != previous_updated_at
                    or old['dirty'] != previous_dirty)):
                return False
            self._db.execute('''INSERT INTO memory_summaries(id,persona_id,scope_kind,scope_key,text,trust,
              covers_until_item_id,dirty,created_at,updated_at,model) VALUES(?,?,?,?,?,?,?,0,?,?,?)
              ON CONFLICT(persona_id,scope_kind,scope_key) DO UPDATE SET text=excluded.text,
              trust=excluded.trust,covers_until_item_id=excluded.covers_until_item_id,dirty=0,
              updated_at=excluded.updated_at,model=excluded.model''',
              (self._next_id('memory_summaries'), persona_id,scope_kind,scope_key,text[:8000],trust,covers_until_item_id,_now(),_now(),model))
            if self._hmac_secret:
                row = self._db.execute('SELECT id FROM memory_summaries WHERE persona_id=? AND scope_kind=? AND scope_key=?',
                                       (persona_id, scope_kind, scope_key)).fetchone()
                self._db.execute('UPDATE memory_summaries SET public_id=? WHERE id=?',
                                 (self._public_id('summary', row['id']), row['id']))
            return True

    def extraction_items(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._db.execute('''SELECT * FROM memory_items
              WHERE extracted=0 AND role='user' ORDER BY id ASC LIMIT ?''', (limit,))]

    def mark_extracted(self, item_id: int) -> None:
        with self._lock, self._db:
            self._db.execute('UPDATE memory_items SET extracted=1 WHERE id=?', (item_id,))

    def facts(self, persona_id: str, counterpart: Optional[str] = None, *,
              limit: Optional[int] = None, visibility: Optional[str] = None) -> List[Dict[str, Any]]:
        query = 'SELECT * FROM memory_facts WHERE persona_id=?'
        args = [persona_id]
        if counterpart is not None:
            query += ' AND counterpart=?'
            args.append(counterpart)
        if visibility is not None:
            query += ' AND visibility=?'
            args.append(visibility)
        query += ' ORDER BY pinned DESC,updated_at DESC,id DESC'
        if limit is not None:
            query += ' LIMIT ?'
            args.append(limit)
        with self._lock:
            return [dict(row) for row in self._db.execute(query, args)]

    def upsert_fact(
        self, persona_id: str, counterpart: str, key: str, value: str,
        visibility: str = 'dm_only', source: str = 'owner',
        source_event_ids: Optional[List[str]] = None, trust: str = 'trusted',
        pinned: bool = False, force: bool = False,
    ) -> bool:
        from .profile import FACT_KEYS

        if (key not in FACT_KEYS or visibility not in ('dm_only', 'shared')
                or source not in ('owner', 'extraction')
                or trust not in ('trusted', 'owner', 'derived')
                or not isinstance(value, str) or not value or not persona_id or not counterpart):
            raise ValueError('invalid memory fact')
        # Provenance determines trust; legacy callers may still pass trusted.
        trust = 'owner' if source == 'owner' else 'derived'
        ids = list(dict.fromkeys(source_event_ids or []))
        if any(not isinstance(event, str) or not event for event in ids):
            raise ValueError('invalid memory fact source')
        if source == 'extraction' and not ids:
            raise ValueError('extracted memory requires an event source')
        with self._lock, self._db:
            self._db.execute('BEGIN IMMEDIATE')
            if any(self.is_tombstoned(event) for event in ids):
                return False
            if source == 'extraction' and self._db.execute(
                    'SELECT 1 FROM memory_suppressed WHERE persona_id=? AND counterpart=? AND key=?',
                    (persona_id, counterpart, key)).fetchone():
                return False
            old = self._db.execute('''SELECT source,pinned FROM memory_facts
                WHERE persona_id=? AND counterpart=? AND key=?''',
                (persona_id, counterpart, key)).fetchone()
            # force exists for API compatibility, never permits model overrides.
            if old and source == 'extraction' and (old['source'] == 'owner' or old['pinned']):
                return False
            pin = bool(pinned or (old and old['pinned']))
            now = _now()
            self._db.execute('''INSERT INTO memory_facts
                (id,persona_id,counterpart,key,value,visibility,source,source_event_ids,
                 trust,pinned,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(persona_id,counterpart,key) DO UPDATE SET
                value=excluded.value,visibility=excluded.visibility,source=excluded.source,
                source_event_ids=excluded.source_event_ids,trust=excluded.trust,
                pinned=excluded.pinned,updated_at=excluded.updated_at''',
                (self._next_id('memory_facts'), persona_id, counterpart, key, value[:8000], visibility, source,
                 json.dumps(ids), trust, int(pin), now, now))
            if source == 'owner':
                self._db.execute('DELETE FROM memory_suppressed WHERE persona_id=? AND counterpart=? AND key=?',
                                 (persona_id, counterpart, key))
            if self._hmac_secret:
                row = self._db.execute('SELECT id FROM memory_facts WHERE persona_id=? AND counterpart=? AND key=?',
                                       (persona_id, counterpart, key)).fetchone()
                self._db.execute('UPDATE memory_facts SET public_id=? WHERE id=?',
                                 (self._public_id('profile', row['id']), row['id']))
            return True

    def delete_item(self, persona_id: str, item_id: int) -> bool:
        with self._lock, self._db:
            self._db.execute('BEGIN IMMEDIATE')
            row = self._db.execute('SELECT event_id FROM memory_items WHERE persona_id=? AND id=?',
                                   (persona_id, item_id)).fetchone()
            if row and row['event_id']:
                self._owner_tombstone(row['event_id'], persona_id)
            return bool(self._db.execute('DELETE FROM memory_items WHERE persona_id=? AND id=?',
                                        (persona_id, item_id)).rowcount)

    def pin_item(self, persona_id: str, item_id: int, pinned: bool) -> bool:
        with self._lock, self._db:
            return bool(self._db.execute('UPDATE memory_items SET pinned=?,updated_at=? WHERE persona_id=? AND id=?',
                                        (int(pinned), _now(), persona_id, item_id)).rowcount)

    def delete_fact(self, persona_id: str, key: str, counterpart: str) -> bool:
        with self._lock, self._db:
            self._db.execute('BEGIN IMMEDIATE')
            row = self._db.execute('SELECT source,source_event_ids FROM memory_facts WHERE persona_id=? AND counterpart=? AND key=?',
                                   (persona_id, counterpart, key)).fetchone()
            if row:
                for event in json.loads(row['source_event_ids']):
                    self._owner_tombstone(event, persona_id)
                if row['source'] == 'extraction':
                    self._db.execute('''INSERT OR REPLACE INTO memory_suppressed
                        (persona_id,counterpart,key,created_at) VALUES(?,?,?,?)''',
                                     (persona_id, counterpart, key, _now()))
            return bool(self._db.execute('DELETE FROM memory_facts WHERE persona_id=? AND counterpart=? AND key=?',
                                        (persona_id, counterpart, key)).rowcount)

    def _owner_tombstone(self, event_id: str, persona_id: str) -> None:
        self._db.execute("""INSERT INTO memory_tombstones
            (event_id,persona_id,created_at,permanent) VALUES(?,?,?,1)
            ON CONFLICT(event_id) DO UPDATE SET permanent=1""",
            (event_id, persona_id, _now()))

    def pin_fact(self, persona_id: str, key: str, counterpart: str, pinned: bool) -> bool:
        with self._lock, self._db:
            return bool(self._db.execute('UPDATE memory_facts SET pinned=? WHERE persona_id=? AND counterpart=? AND key=?',
                                        (int(pinned), persona_id, counterpart, key)).rowcount)

    def audit(self, *, request_id: Optional[str], caller: str, persona_id: str,
              action: str, memory_id: Optional[str], layer: Optional[str],
              result_code: str, counterpart_hmac: Optional[str]) -> None:
        """Write a non-content audit row; callers must provide only a HMAC counterpart."""
        with self._lock, self._db:
            self._db.execute('''INSERT INTO memory_audit
                (occurred_at,request_id,caller,persona_id,action,memory_id,layer,result_code,counterpart_hmac)
                VALUES(?,?,?,?,?,?,?,?,?)''',
                (_now(), request_id, caller, persona_id, action, memory_id, layer,
                 result_code, counterpart_hmac))

    def redact_event(self, event_id: str, persona_id: Optional[str] = None,
                     room_id: Optional[str] = None) -> None:
        """Event IDs are globally unique; removal spans every persona copy."""
        if not isinstance(event_id, str) or not event_id:
            raise ValueError('invalid redaction target')
        with self._lock, self._db:
            self._db.execute('''INSERT OR IGNORE INTO memory_tombstones
                (event_id,persona_id,room_id,created_at) VALUES(?,?,?,?)''',
                (event_id, persona_id, room_id, _now()))
            self._db.execute('DELETE FROM memory_items WHERE event_id=?', (event_id,))
            for row in self._db.execute('SELECT id,source_event_ids FROM memory_facts').fetchall():
                ids = json.loads(row['source_event_ids'])
                if event_id not in ids:
                    continue
                remaining = [event for event in ids if event != event_id]
                if not remaining:
                    self._db.execute('DELETE FROM memory_facts WHERE id=?', (row['id'],))
                else:
                    self._db.execute('UPDATE memory_facts SET source_event_ids=? WHERE id=?',
                                     (json.dumps(remaining), row['id']))

    def purge(self, now: Optional[datetime] = None) -> int:
        now = now or datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=self.retention_days)).strftime('%Y-%m-%dT%H:%M:%SZ')
        with self._lock, self._db:
            count = 0
            for table, timestamp, pin_filter in (
                    ('memory_items', 'created_at', ' AND pinned=0'),
                    ('memory_facts', 'updated_at', ' AND pinned=0'),
                    ('memory_tombstones', 'created_at', ' AND permanent=0'),
                    ('memory_audit', 'occurred_at', '')):
                count += self._db.execute(
                    f'DELETE FROM {table} WHERE {timestamp} < ?{pin_filter}', (cutoff,)).rowcount
            return count


class MemoryPurger:
    """Startup purge and stoppable daemon; exception values never enter logs."""

    def __init__(self, store: MemoryStore, interval_seconds: int) -> None:
        if (not isinstance(interval_seconds, int) or isinstance(interval_seconds, bool)
                or (interval_seconds != 0 and interval_seconds < MEMORY_PURGE_INTERVAL_MIN_SECONDS)):
            raise ValueError('memory purge interval must be zero or at least 60 seconds')
        self._store = store
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def run_purge_safely(self) -> None:
        try:
            removed = self._store.purge()
        except Exception as exc:
            _log.warning('Memory purge failed: %s', type(exc).__name__)
        else:
            _log.info('Memory purge removed %d rows', removed)

    def start(self) -> None:
        if not self._interval or self._thread is not None:
            return
        self.run_purge_safely()
        self._thread = threading.Thread(target=self._run, name='memory-purger', daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self.run_purge_safely()
