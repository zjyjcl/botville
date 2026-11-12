# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""Bounded idle consolidation, sharing activity and policy gates with replies."""
from __future__ import annotations

import sqlite3
import threading
import time
from typing import Callable

from ..as_consumer import ConsumerStats, TransientUnavailable
from ..engine.model_client import ModelError
from .profile import ModelExtractor, apply_extractions, is_trusted_local_sender
from .summary import generate_summary

MEMORY_IDLE_SECONDS = 120
MEMORY_ROUND_CONVERSATIONS = 8
MEMORY_ITEMS_PER_SCOPE = 8
MEMORY_OUTPUT_RESERVE = 2048


class MemoryDeferred(Exception):
    """Idle/policy/reserve gate closed; not a failed attempt."""


class IdleSignal:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 pending: Callable[[], bool] = lambda: False):
        self.clock, self.pending = clock, pending
        self._lock = threading.Lock()
        self._last_activity = clock()

    def notify(self):
        with self._lock:
            self._last_activity = self.clock()

    def idle(self):
        with self._lock:
            elapsed = self.clock() - self._last_activity
        return elapsed >= MEMORY_IDLE_SECONDS and not self.pending()


class BudgetedModel:
    def __init__(self, client, service, persona_id, ready=lambda: True, conversation_id=None):
        self.client, self.service, self.persona_id, self.ready = client, service, persona_id, ready
        self.config = client.config
        self.usage_scope = {} if conversation_id is None else {"conversation_id": conversation_id}

    def complete(self, **kwargs):
        from ..engine.engine import estimate_tokens
        estimate = (estimate_tokens(kwargs['system_text']) +
                    sum(estimate_tokens(m['content']) for m in kwargs['messages']) +
                    MEMORY_OUTPUT_RESERVE)
        if not self.ready() or not self.service.authorize_memory_call(self.persona_id, estimate, **self.usage_scope):
            raise MemoryDeferred()
        result = self.client.complete(**kwargs, max_tokens=MEMORY_OUTPUT_RESERVE)
        extra = estimate_tokens(result) - MEMORY_OUTPUT_RESERVE
        if extra > 0:
            self.service.record_memory_usage(persona_id=self.persona_id, tokens=extra, **self.usage_scope)
        return result


def budgeted_embed(client, service, persona_id, text, *, ready=lambda: True, conversation_id=None):
    from ..engine.engine import estimate_tokens
    estimate = estimate_tokens(text) + 32
    usage_scope = {} if conversation_id is None else {"conversation_id": conversation_id}
    if not ready() or not service.authorize_memory_call(persona_id, estimate, **usage_scope):
        raise MemoryDeferred()
    return client.embed(text)


class MemoryIdleWorker:
    def __init__(self, store, service, personas, *, idle_signal, model_client=None,
                 summary_model='', embedding=None, vectors=None, server_name='',
                 clock=time.monotonic):
        self.store, self.service, self.personas = store, service, personas
        self.idle_signal, self.clock = idle_signal, clock
        self.model_client, self.summary_model = model_client, summary_model
        self.embedding, self.vectors, self.server_name = embedding, vectors, server_name
        self.stats = ConsumerStats()
        self._stop = threading.Event()
        self._thread = None
        self._cursor = ('', '', '')
        self._failures, self._retry_at = 0, 0.0
        self._scope_backoff = {}

    def ready(self):
        return not self._stop.is_set() and self.idle_signal.idle()

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name='memory-idle-worker', daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            # Network calls have finite timeouts; never close SQLite under this thread.
            self._thread.join()

    def _run(self):
        while not self._stop.wait(1):
            self.run_once()

    def _scopes(self):
        with self.store._lock:
            rows = self.store._db.execute('''SELECT persona_id,scope_kind,scope_key FROM (
                SELECT persona_id,scope_kind,scope_key FROM memory_items UNION
                SELECT persona_id,scope_kind,scope_key FROM memory_summaries)
                WHERE (persona_id,scope_kind,scope_key)>(?,?,?)
                ORDER BY persona_id,scope_kind,scope_key LIMIT ?''',
                (*self._cursor, MEMORY_ROUND_CONVERSATIONS)).fetchall()
        if not rows:
            self._cursor = ('', '', '')
        return [dict(row) for row in rows]

    def run_once(self):
        count = 0
        try:
            if self.clock() < self._retry_at or not self.ready():
                return 0
            for scope in self._scopes():
                if not self.ready():
                    break
                next_cursor = tuple(scope[k] for k in ('persona_id', 'scope_kind', 'scope_key'))
                failures, retry_at = self._scope_backoff.get(next_cursor, (0, 0.0))
                if self.clock() < retry_at:
                    self._cursor = next_cursor
                    continue
                persona = self.personas.get(scope['persona_id'])
                if persona is None:
                    self._cursor = next_cursor
                    continue
                count += 1
                try:
                    self._process_scope(scope, persona)
                    self._scope_backoff.pop(next_cursor, None)
                except (TransientUnavailable, sqlite3.Error, OSError):
                    raise
                except MemoryDeferred:
                    self.stats.inc('deferred')
                except Exception:
                    failures += 1
                    self._scope_backoff[next_cursor] = (failures, self.clock() +
                        min(3600, 30 * 2 ** min(failures - 1, 7)))
                    self.stats.inc('permanent_failures')
                self._cursor = next_cursor
            self._failures = 0
            self.stats.inc('rounds')
        except (TransientUnavailable, sqlite3.Error, OSError):
            self._failures += 1
            self._retry_at = self.clock() + min(3600, 30 * 2 ** min(self._failures - 1, 7))
            self.stats.inc('transient_failures')
        except Exception:
            # Permanent/provider validation errors do not kill the daemon or leak data.
            self._retry_at = self.clock() + 3600
            self.stats.inc('permanent_failures')
        return count

    def _conversation_id(self, scope):
        if scope['scope_kind'] == 'group':
            return scope['scope_key']
        with self.store._lock:
            row = self.store._db.execute('''SELECT room_id FROM memory_items
                WHERE persona_id=? AND scope_kind=? AND scope_key=?
                  AND room_id IS NOT NULL ORDER BY id DESC LIMIT 1''',
                (scope['persona_id'], scope['scope_kind'], scope['scope_key'])).fetchone()
        return row['room_id'] if row else None

    def _process_scope(self, scope, persona):
        if self.model_client is not None:
            model = BudgetedModel(self.model_client, self.service, scope['persona_id'], self.ready,
                                  conversation_id=self._conversation_id(scope))
            generate_summary(self.store, model, **scope, model=self.summary_model, persona=persona)
            with self.store._lock:
                items = [dict(row) for row in self.store._db.execute('''SELECT * FROM memory_items
                    WHERE persona_id=? AND scope_kind=? AND scope_key=? AND extracted=0
                    ORDER BY id LIMIT ?''', (*scope.values(), MEMORY_ITEMS_PER_SCOPE))]
            extractor = ModelExtractor(model, model=self.summary_model, persona=persona)
            for item in items:
                if not self.ready():
                    raise MemoryDeferred()
                if (item['role'] == 'user' and item['event_id'] and
                        is_trusted_local_sender(item['sender'], self.server_name, item['trust'])):
                    facts = extractor.extract(item['text'])
                    apply_extractions(self.store, persona_id=item['persona_id'],
                        counterpart=item['sender'], trusted_local=True, role=item['role'],
                        facts=facts, event_id=item['event_id'], sender=item['sender'],
                        server_name=self.server_name, trust=item['trust'])
                self.store.mark_extracted(item['id'])
        if self.embedding is not None and self.vectors is not None:
            self._embed_scope(scope)

    def _embed_scope(self, scope):
        model = self.embedding.config.model
        dim = self.embedding.dimension
        with self.store._lock:
            items = [dict(row) for row in self.store._db.execute('''SELECT i.* FROM memory_items i
                LEFT JOIN memory_vectors v ON v.item_id=i.id
                WHERE i.persona_id=? AND i.scope_kind=? AND i.scope_key=?
                  AND (v.id IS NULL OR v.model!=? OR (? > 0 AND v.dim!=?))
                ORDER BY i.id LIMIT ?''', (*scope.values(), model, dim, dim, MEMORY_ITEMS_PER_SCOPE))]
        for item in items:
            vector = budgeted_embed(self.embedding, self.service, scope['persona_id'],
                                    item['text'], ready=self.ready, conversation_id=self._conversation_id(scope))
            self.vectors.upsert(**scope, item_id=item['id'], vector=vector, model=model,
                                expected_text=item['text'])
        summary = self.store.summary(**scope)
        if summary and not summary['dirty']:
            with self.store._lock:
                row = self.store._db.execute('''SELECT 1 FROM memory_vectors
                    WHERE summary_id=? AND model=? AND dim=?''',
                    (summary['id'], model, self.embedding.dimension)).fetchone()
            if row is None:
                vector = budgeted_embed(self.embedding, self.service, scope['persona_id'],
                                        summary['text'], ready=self.ready, conversation_id=self._conversation_id(scope))
                # The vector store checks live source metadata before insertion.
                self.vectors.upsert(**scope, summary_id=summary['id'], vector=vector, model=model,
                                    expected_text=summary['text'])
