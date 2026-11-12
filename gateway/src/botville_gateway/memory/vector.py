# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""Scope-filtered SQLite vector stores.  The portable store is the default."""
from __future__ import annotations

from array import array
import math
from typing import Any, Dict, List, MutableMapping, Optional, Protocol, Sequence

from .store import MemoryStore, _now

VECTOR_SCAN_LIMIT = 500
VECTOR_TOP_K = 5


class VectorStore(Protocol):
    def upsert(self, *, persona_id: str, scope_kind: str, scope_key: str,
               vector: Sequence[float], model: str, item_id: Optional[int] = None,
               summary_id: Optional[int] = None, expected_text: Optional[str] = None) -> bool: ...
    def delete(self, *, item_id: Optional[int] = None, summary_id: Optional[int] = None) -> None: ...
    def search(self, *, persona_id: str, scope_kind: str, scope_key: str,
               query_vec: Sequence[float], k: int = VECTOR_TOP_K) -> List[Dict[str, Any]]: ...


def _blob(values: Sequence[float]) -> bytes:
    return array('f', (float(value) for value in values)).tobytes()

def _values(blob: bytes) -> array:
    value = array('f'); value.frombytes(blob); return value


class PythonVectorStore:
    def __init__(self, store: MemoryStore, scan_limit: int = VECTOR_SCAN_LIMIT) -> None:
        self.store, self.scan_limit = store, scan_limit
        with store._lock, store._db:
            store._db.execute('''CREATE TABLE IF NOT EXISTS memory_vectors (
                id INTEGER PRIMARY KEY, item_id INTEGER, summary_id INTEGER,
                persona_id TEXT NOT NULL, scope_kind TEXT NOT NULL,
                scope_key TEXT NOT NULL, model TEXT NOT NULL, dim INTEGER NOT NULL,
                vec BLOB NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(item_id), UNIQUE(summary_id))''')
            store._db.execute('CREATE INDEX IF NOT EXISTS idx_memory_vectors_scope ON memory_vectors(persona_id,scope_kind,scope_key,id)')
            # Items are redacted/deleted by store.py; this trigger makes vector
            # removal atomic even when that code does not know about this layer.
            store._db.execute('''CREATE TRIGGER IF NOT EXISTS memory_vectors_item_delete
                AFTER DELETE ON memory_items BEGIN
                DELETE FROM memory_vectors WHERE item_id=OLD.id;
            END''')
            store._db.execute('''CREATE TRIGGER IF NOT EXISTS memory_vectors_summary_delete
                AFTER DELETE ON memory_summaries BEGIN
                DELETE FROM memory_vectors WHERE summary_id=OLD.id;
            END''')
            store._db.execute('''CREATE TRIGGER IF NOT EXISTS memory_vectors_summary_dirty
                AFTER UPDATE OF dirty ON memory_summaries WHEN NEW.dirty=1 BEGIN
                DELETE FROM memory_vectors WHERE summary_id=NEW.id;
            END''')

    def upsert(self, *, persona_id: str, scope_kind: str, scope_key: str,
               vector: Sequence[float], model: str, item_id: Optional[int] = None,
               summary_id: Optional[int] = None, expected_text: Optional[str] = None) -> bool:
        if (scope_kind not in ('dm', 'group') or not persona_id or not scope_key or not model
                or (item_id is None) == (summary_id is None) or not vector):
            raise ValueError('invalid memory vector')
        params = (item_id, summary_id, persona_id, scope_kind, scope_key, model, len(vector), _blob(vector), _now())
        with self.store._lock, self.store._db:
            if item_id is not None:
                source = self.store._db.execute('SELECT text FROM memory_items WHERE id=? AND persona_id=? AND scope_kind=? AND scope_key=?', (item_id, persona_id, scope_kind, scope_key)).fetchone()
            else:
                source = self.store._db.execute('SELECT text FROM memory_summaries WHERE id=? AND persona_id=? AND scope_kind=? AND scope_key=? AND dirty=0', (summary_id, persona_id, scope_kind, scope_key)).fetchone()
            if source is None or (expected_text is not None and source['text'] != expected_text):
                return False
            if item_id is not None:
                self.store._db.execute('DELETE FROM memory_vectors WHERE item_id=?', (item_id,))
            else:
                self.store._db.execute('DELETE FROM memory_vectors WHERE summary_id=?', (summary_id,))
            self.store._db.execute('''INSERT INTO memory_vectors(item_id,summary_id,persona_id,scope_kind,scope_key,model,dim,vec,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)''', params)
        return True

    def delete(self, *, item_id: Optional[int] = None, summary_id: Optional[int] = None) -> None:
        if item_id is None and summary_id is None: return
        with self.store._lock, self.store._db:
            if item_id is not None: self.store._db.execute('DELETE FROM memory_vectors WHERE item_id=?', (item_id,))
            if summary_id is not None: self.store._db.execute('DELETE FROM memory_vectors WHERE summary_id=?', (summary_id,))

    def stale_pending(self, *, persona_id: Optional[str] = None, model: str = '',
                      dim: int = 0, limit: int = VECTOR_SCAN_LIMIT) -> List[Dict[str, Any]]:
        """Return bounded rows requiring re-embedding after model/dimension changes."""
        where, args = [], []
        if persona_id:
            where.append('persona_id=?'); args.append(persona_id)
        mismatch = []
        if model: mismatch.append('model<>?'); args.append(model)
        if dim: mismatch.append('dim<>?'); args.append(dim)
        if mismatch: where.append('(' + ' OR '.join(mismatch) + ')')
        sql = 'SELECT * FROM memory_vectors' + (' WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY id LIMIT ?'
        args.append(limit)
        with self.store._lock:
            return [dict(row) for row in self.store._db.execute(sql, args)]

    def search(self, *, persona_id: str, scope_kind: str, scope_key: str,
               query_vec: Sequence[float], k: int = VECTOR_TOP_K,
               model: str = '', dim: int = 0) -> List[Dict[str, Any]]:
        if not query_vec or k <= 0: return []
        q = [float(x) for x in query_vec]; qn = math.sqrt(sum(x*x for x in q))
        if not qn: return []
        with self.store._lock:
            conditions = ['memory_vectors.persona_id=?', 'memory_vectors.scope_kind=?', 'memory_vectors.scope_key=?']
            args: List[Any] = [persona_id, scope_kind, scope_key]
            if model:
                conditions.append('memory_vectors.model=?'); args.append(model)
            if dim:
                conditions.append('memory_vectors.dim=?'); args.append(dim)
            args.append(self.scan_limit)
            rows = self.store._db.execute('''SELECT memory_vectors.*, memory_items.text AS item_text, memory_items.trust AS item_trust,
                memory_items.created_at AS item_created_at, memory_summaries.text AS summary_text,
                memory_summaries.trust AS summary_trust, memory_summaries.updated_at AS summary_updated_at
                FROM memory_vectors
                LEFT JOIN memory_items ON memory_items.id=memory_vectors.item_id
                LEFT JOIN memory_summaries ON memory_summaries.id=memory_vectors.summary_id AND memory_summaries.dirty=0
                WHERE ''' + ' AND '.join(conditions) + ' ORDER BY memory_vectors.id DESC LIMIT ?', args).fetchall()
        found = []
        for row in rows:
            if row['dim'] != len(q): continue
            values = _values(row['vec']); norm = math.sqrt(sum(x*x for x in values))
            if norm:
                found.append((sum(a*b for a,b in zip(q, values)) / (qn*norm), dict(row)))
        found.sort(key=lambda pair: pair[0], reverse=True)
        return [dict(row, text=row.get('item_text') or row.get('summary_text') or '',
                     trust=row.get('item_trust') or row.get('summary_trust') or 'untrusted',
                     created_at=row.get('item_created_at') or row.get('summary_updated_at') or row['created_at'], score=score)
                for score, row in found[:k]]


class SqliteVecStore(PythonVectorStore):
    """Best-effort optional accelerator. Extension absence is deliberately invisible."""
    def __init__(self, store: MemoryStore, scan_limit: int = VECTOR_SCAN_LIMIT) -> None:
        try:
            with store._lock:
                store._db.enable_load_extension(True)
                try:
                    store._db.load_extension('sqlite_vec')
                finally:
                    store._db.enable_load_extension(False)
        except Exception as exc:
            raise RuntimeError('sqlite-vec unavailable') from exc
        super().__init__(store, scan_limit)

    def search(self, **kwargs: Any) -> List[Dict[str, Any]]:
        """Use sqlite-vec distance ordering when its SQL function is available.

        Extension releases differ in accepted BLOB formats, so a supported
        extension that declines this query safely uses the portable path.
        """
        query = kwargs.get('query_vec')
        if not query:
            return []
        try:
            conditions = ['persona_id=?', 'scope_kind=?', 'scope_key=?']
            args: List[Any] = [kwargs['persona_id'], kwargs['scope_kind'], kwargs['scope_key']]
            if kwargs.get('model'):
                conditions.append('model=?'); args.append(kwargs['model'])
            if kwargs.get('dim'):
                conditions.append('dim=?'); args.append(kwargs['dim'])
            args.extend([self.scan_limit, _blob(query), kwargs.get('k', VECTOR_TOP_K)])
            with self.store._lock:
                rows = self.store._db.execute('''WITH bounded AS (
                    SELECT * FROM memory_vectors WHERE ''' + ' AND '.join(conditions) + ''' ORDER BY id DESC LIMIT ?)
                    SELECT bounded.*, memory_items.text AS item_text, memory_items.trust AS item_trust,
                    memory_items.created_at AS item_created_at, memory_summaries.text AS summary_text,
                    memory_summaries.trust AS summary_trust, memory_summaries.updated_at AS summary_updated_at,
                    vec_distance_cosine(bounded.vec, ?) AS distance
                    FROM bounded LEFT JOIN memory_items ON memory_items.id=bounded.item_id
                    LEFT JOIN memory_summaries ON memory_summaries.id=bounded.summary_id AND memory_summaries.dirty=0
                    ORDER BY distance LIMIT ?''', args).fetchall()
            return [dict(row, text=row['item_text'] or row['summary_text'] or '',
                         trust=row['item_trust'] or row['summary_trust'] or 'untrusted',
                         created_at=row['item_created_at'] or row['summary_updated_at'] or row['created_at'], score=1.0-row['distance']) for row in rows]
        except Exception:
            return super().search(**kwargs)


def build_vector_store(store: MemoryStore, backend: str = 'python', *,
                       stats: Optional[MutableMapping[str, int]] = None,
                       scan_limit: int = VECTOR_SCAN_LIMIT) -> Optional[PythonVectorStore]:
    """Construct the requested store; unavailable sqlite-vec quietly uses Python."""
    if backend == 'none':
        return None
    if backend == 'python':
        return PythonVectorStore(store, scan_limit)
    if backend != 'sqlite-vec':
        raise ValueError('invalid memory vector backend')
    try:
        return SqliteVecStore(store, scan_limit)
    except RuntimeError:
        if stats is not None:
            stats['memory_vector_sqlite_vec_fallbacks'] = stats.get('memory_vector_sqlite_vec_fallbacks', 0) + 1
        return PythonVectorStore(store, scan_limit)


class VectorRecall:
    """Adapter used by the worker/engine after it has embedded the live message."""
    def __init__(self, store: VectorStore, query_vec: Optional[Sequence[float]] = None,
                 *, model: str = '', dim: int = 0) -> None:
        self.store = store
        self._query = tuple(query_vec) if query_vec is not None else None
        self._model, self._dim = model, dim

    def set_query(self, vector: Sequence[float]) -> None:
        """Compatibility helper; construct a new adapter per live turn instead."""
        self._query = vector

    def with_query(self, vector: Sequence[float]) -> 'VectorRecall':
        return VectorRecall(self.store, vector, model=self._model, dim=self._dim)

    def recall(self, *, persona_id: str, scope_kind: str, scope_key: str) -> List[Dict[str, Any]]:
        if self._query is None: return []
        try:
            rows = self.store.search(persona_id=persona_id, scope_kind=scope_kind, scope_key=scope_key,
                                     query_vec=self._query, model=self._model, dim=self._dim)
        except Exception:
            return []
        return [dict(row, text=row.get('text', ''), persona_id=persona_id,
                     scope_kind=scope_kind, scope_key=scope_key, visibility='dm_only' if scope_kind == 'dm' else 'shared') for row in rows]
