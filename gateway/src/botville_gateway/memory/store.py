# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""Private SQLite memory, serialized within one gateway process per database.

The owner CLI is intended for offline administration. Database/WAL/backups carry
sensitive user data. No operation logs identifiers or stored content.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

MEMORY_PURGE_INTERVAL_MIN_SECONDS = 60
SCHEMA_VERSION = 2
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
}


class MemoryStore:
    def __init__(self, path: str, retention_days: int = 90) -> None:
        if (not isinstance(retention_days, int) or isinstance(retention_days, bool)
                or retention_days < 1):
            raise ValueError('memory retention must be a positive integer')
        if not isinstance(path, str) or not path or path.startswith('file:'):
            raise ValueError('memory database requires a filesystem path')
        self.path = path
        self.retention_days = retention_days
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
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute('PRAGMA journal_mode=WAL')
            self._db.execute('PRAGMA synchronous=FULL')
            self._db.execute('PRAGMA foreign_keys=ON')
            self._migrate()
        except Exception:
            self._db.close()
            raise

    def _migrate(self) -> None:
        with self._lock, self._db:
            self._db.execute('CREATE TABLE IF NOT EXISTS memory_schema(version INTEGER NOT NULL)')
            versions = self._db.execute('SELECT version FROM memory_schema').fetchall()
            if len(versions) > 1:
                raise RuntimeError('invalid memory schema version')
            version = versions[0][0] if versions else 0
            if not isinstance(version, int) or version < 0 or version > SCHEMA_VERSION:
                raise RuntimeError('unsupported memory schema version')
            if not versions:
                self._db.execute('INSERT INTO memory_schema VALUES(0)')
            for next_version in range(version + 1, SCHEMA_VERSION + 1):
                for statement in _MIGRATIONS[next_version]:
                    self._db.execute(statement)
                if next_version == 2:
                    columns = {row[1] for row in self._db.execute('PRAGMA table_info(memory_items)')}
                    if 'visibility' not in columns:
                        self._db.execute("ALTER TABLE memory_items ADD COLUMN visibility TEXT NOT NULL DEFAULT 'dm_only'")
                        self._db.execute("UPDATE memory_items SET visibility='shared' WHERE scope_kind='group'")
                self._db.execute('UPDATE memory_schema SET version=?', (next_version,))

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
            if self.is_tombstoned(event_id):
                return False
            cursor = self._db.execute('''INSERT INTO memory_items
                (persona_id,scope_kind,scope_key,room_id,counterpart,event_id,sender,
                 role,trust,text,created_at,pinned,source,visibility)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(persona_id,event_id) WHERE event_id IS NOT NULL DO NOTHING''',
                (persona_id, scope_kind, scope_key, room_id, counterpart, event_id,
                 sender, role, trust, text[:8000], _now(), int(pinned), source, visibility))
            return bool(cursor.rowcount)

    def items(self, persona_id: str, *, scope_kind: Optional[str] = None,
              scope_key: Optional[str] = None) -> List[Dict[str, Any]]:
        query = 'SELECT * FROM memory_items WHERE persona_id=?'
        args = [persona_id]
        if scope_kind is not None:
            query += ' AND scope_kind=?'
            args.append(scope_kind)
        if scope_key is not None:
            query += ' AND scope_key=?'
            args.append(scope_key)
        with self._lock:
            return [dict(row) for row in self._db.execute(
                query + ' ORDER BY created_at DESC,id DESC', args)]

    def facts(self, persona_id: str, counterpart: Optional[str] = None) -> List[Dict[str, Any]]:
        query = 'SELECT * FROM memory_facts WHERE persona_id=?'
        args = [persona_id]
        if counterpart is not None:
            query += ' AND counterpart=?'
            args.append(counterpart)
        with self._lock:
            return [dict(row) for row in self._db.execute(
                query + ' ORDER BY updated_at DESC,id DESC', args)]

    def upsert_fact(
        self, persona_id: str, counterpart: str, key: str, value: str,
        visibility: str = 'dm_only', source: str = 'owner',
        source_event_ids: Optional[List[str]] = None, trust: str = 'trusted',
        pinned: bool = False, force: bool = False,
    ) -> bool:
        from .profile import FACT_KEYS

        if (key not in FACT_KEYS or visibility not in ('dm_only', 'shared')
                or source not in ('owner', 'extraction') or trust != 'trusted'
                or not isinstance(value, str) or not value or not persona_id or not counterpart):
            raise ValueError('invalid memory fact')
        ids = list(dict.fromkeys(source_event_ids or []))
        if any(not isinstance(event, str) or not event for event in ids):
            raise ValueError('invalid memory fact source')
        if source == 'extraction' and not ids:
            raise ValueError('extracted memory requires an event source')
        with self._lock, self._db:
            if any(self.is_tombstoned(event) for event in ids):
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
                (persona_id,counterpart,key,value,visibility,source,source_event_ids,
                 trust,pinned,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(persona_id,counterpart,key) DO UPDATE SET
                value=excluded.value,visibility=excluded.visibility,source=excluded.source,
                source_event_ids=excluded.source_event_ids,trust=excluded.trust,
                pinned=excluded.pinned,updated_at=excluded.updated_at''',
                (persona_id, counterpart, key, value[:8000], visibility, source,
                 json.dumps(ids), trust, int(pin), now, now))
            return True

    def delete_item(self, persona_id: str, item_id: int) -> bool:
        with self._lock, self._db:
            return bool(self._db.execute('DELETE FROM memory_items WHERE persona_id=? AND id=?',
                                        (persona_id, item_id)).rowcount)

    def pin_item(self, persona_id: str, item_id: int, pinned: bool) -> bool:
        with self._lock, self._db:
            return bool(self._db.execute('UPDATE memory_items SET pinned=? WHERE persona_id=? AND id=?',
                                        (int(pinned), persona_id, item_id)).rowcount)

    def delete_fact(self, persona_id: str, key: str, counterpart: str) -> bool:
        with self._lock, self._db:
            return bool(self._db.execute('DELETE FROM memory_facts WHERE persona_id=? AND counterpart=? AND key=?',
                                        (persona_id, counterpart, key)).rowcount)

    def pin_fact(self, persona_id: str, key: str, counterpart: str, pinned: bool) -> bool:
        with self._lock, self._db:
            return bool(self._db.execute('UPDATE memory_facts SET pinned=? WHERE persona_id=? AND counterpart=? AND key=?',
                                        (int(pinned), persona_id, counterpart, key)).rowcount)

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
                    ('memory_tombstones', 'created_at', '')):
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
