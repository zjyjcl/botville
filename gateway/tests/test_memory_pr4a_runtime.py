# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""Runtime regressions for PR4a memory vector and worker safety fixes."""
from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'src'))

from botville_gateway.http_server import GatewayServer
from botville_gateway.memory.store import MemoryStore
from botville_gateway.memory.vector import PythonVectorStore, SqliteVecStore, VectorRecall
from botville_gateway.memory.worker import IdleSignal, MemoryIdleWorker


class _RecallStore:
    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()

    def search(self, **kwargs):
        with self.lock:
            self.calls.append(kwargs)
        marker = kwargs['query_vec'][0]
        return [{'persona_id': kwargs['persona_id'], 'scope_kind': kwargs['scope_kind'],
                 'scope_key': kwargs['scope_key'], 'visibility': 'stored',
                 'text': 'query-%s' % marker}]


class VectorRecallRuntimeTests(unittest.TestCase):
    def test_with_query_isolation_under_concurrent_reuse(self):
        store = _RecallStore()
        base = VectorRecall(store)
        results = {}
        lock = threading.Lock()

        def recall(marker):
            found = base.with_query([marker, 0]).recall(
                persona_id='p', scope_kind='dm', scope_key='u')
            with lock:
                results[marker] = found[0]['text']

        threads = [threading.Thread(target=recall, args=(marker,)) for marker in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(results, {marker: 'query-%s' % marker for marker in range(20)})
        self.assertEqual(base.recall(persona_id='p', scope_kind='dm', scope_key='u'), [])
        self.assertEqual({tuple(call['query_vec']) for call in store.calls},
                         {(marker, 0) for marker in range(20)})

    def test_recall_missing_visibility_defaults_to_dm_only(self):
        for kind in ('dm', 'group'):
            row = dict(persona_id='p', scope_kind=kind, scope_key='u', text='fixture')
            store = Mock()
            store.search.return_value = [row]
            result = VectorRecall(store, [1]).recall(persona_id='p', scope_kind=kind, scope_key='u')
            self.assertEqual(result[0]['visibility'], 'dm_only')

    def test_recall_discards_every_scope_mismatch_and_keeps_row_visibility(self):
        class Store:
            def search(self, **kwargs):
                return [
                    {'persona_id': 'p', 'scope_kind': 'dm', 'scope_key': 'u',
                     'visibility': 'stored_visibility', 'text': 'accepted'},
                    {'persona_id': 'other', 'scope_kind': 'dm', 'scope_key': 'u', 'text': 'persona'},
                    {'persona_id': 'p', 'scope_kind': 'group', 'scope_key': 'u', 'text': 'kind'},
                    {'persona_id': 'p', 'scope_kind': 'dm', 'scope_key': 'other', 'text': 'key'},
                ]
        result = VectorRecall(Store(), [1]).recall(persona_id='p', scope_kind='dm', scope_key='u')
        self.assertEqual(result, [{'persona_id': 'p', 'scope_kind': 'dm', 'scope_key': 'u',
                                   'visibility': 'stored_visibility', 'text': 'accepted'}])


class VectorStoreVisibilityRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore(':memory:')
        self.addCleanup(self.store.close)
        self.vectors = PythonVectorStore(self.store)
        self.assertTrue(self.store.add_item(
            persona_id='p', scope_kind='dm', scope_key='u', room_id='!r', counterpart='u',
            event_id='$vector', sender='u', role='user', trust='trusted', text='fixture'))
        self.item_id = self.store.items('p')[0]['id']
        with self.store._lock, self.store._db:
            self.store._db.execute("UPDATE memory_items SET visibility='stored_visibility' WHERE id=?",
                                   (self.item_id,))
        self.assertTrue(self.vectors.upsert(persona_id='p', scope_kind='dm', scope_key='u',
                                            vector=[1, 0], model='m', item_id=self.item_id))

    def test_python_store_returns_item_visibility(self):
        rows = self.vectors.search(persona_id='p', scope_kind='dm', scope_key='u',
                                   query_vec=[1, 0], model='m', dim=2)
        self.assertEqual(rows[0]['visibility'], 'stored_visibility')

    def test_sqlite_vec_missing_visibility_and_portable_fallback(self):
        native = object.__new__(SqliteVecStore)
        native.store, native.scan_limit = self.store, 500
        # Without the extension function the native query must use portable search.
        rows = native.search(persona_id='p', scope_kind='dm', scope_key='u', query_vec=[1, 0])
        self.assertEqual(rows[0]['visibility'], 'stored_visibility')
        row = dict(item_text='fixture', summary_text=None, item_trust='trusted',
                   summary_trust=None, item_created_at='2026', summary_updated_at=None,
                   created_at='2026', distance=0, scope_kind='group')
        fake_store = SimpleNamespace(_lock=threading.Lock(), _db=Mock())
        fake_store._db.execute.return_value.fetchall.return_value = [row]
        native.store = fake_store
        with patch.object(PythonVectorStore, 'search', side_effect=AssertionError('unexpected fallback')):
            rows = native.search(persona_id='p', scope_kind='group', scope_key='u', query_vec=[1])
        self.assertEqual(rows[0]['visibility'], 'dm_only')

    def test_sqlite_vec_path_returns_item_visibility(self):
        # Exercise the native SQL path without requiring sqlite-vec to be
        # installed: this function is the only extension primitive it uses.
        self.store._db.create_function('vec_distance_cosine', 2, lambda left, right: 0.0)
        native = object.__new__(SqliteVecStore)
        native.store = self.store
        native.scan_limit = 500
        with patch.object(PythonVectorStore, 'search', side_effect=AssertionError('unexpected fallback')):
            rows = native.search(persona_id='p', scope_kind='dm', scope_key='u',
                                 query_vec=[1, 0], model='m', dim=2)
        self.assertEqual(rows[0]['visibility'], 'stored_visibility')


class WorkerStopRuntimeTests(unittest.TestCase):
    def test_server_closes_storage_only_after_worker_exit(self):
        for blocked in (True, False):
            with self.subTest(blocked=blocked):
                store = Mock()
                worker = MemoryIdleWorker(store, None, {}, idle_signal=IdleSignal())
                release = threading.Event()
                thread = threading.Thread(target=release.wait)
                worker._thread = thread
                if not blocked:
                    release.set()
                thread.start()
                server = object.__new__(GatewayServer)
                server.memory_worker, server.memory_store = worker, store
                try:
                    with patch('botville_gateway.memory.worker.MEMORY_WORKER_STOP_TIMEOUT_SECONDS', 0.01), \
                            patch('http.server.ThreadingHTTPServer.server_close'):
                        if blocked:
                            with self.assertLogs('botville_gateway.memory.worker', 'WARNING') as logs:
                                server.server_close()
                            self.assertEqual(len(logs.records), 1)
                            store.close.assert_not_called()
                        else:
                            server.server_close()
                            store.close.assert_called_once_with()
                finally:
                    release.set()
                    thread.join(2)

    def test_stop_times_out_while_a_real_model_call_is_blocked(self):
        store = MemoryStore(':memory:')
        self.addCleanup(store.close)
        for number in range(41):
            store.add_item(persona_id='p', scope_kind='dm', scope_key='u', room_id='!r',
                           counterpart='u', event_id='$%d' % number, sender='u', role='user',
                           trust='trusted', text='fixture')
        clock = [0]
        signal = IdleSignal(clock=lambda: clock[0])
        entered, release = threading.Event(), threading.Event()

        class BlockingClient:
            config = SimpleNamespace()
            def complete(self, **kwargs):
                entered.set()
                release.wait(5)
                return 'summary'

        class Service:
            def authorize_memory_call(self, *args, **kwargs): return True
            def record_memory_usage(self, **kwargs): pass

        worker = MemoryIdleWorker(store, Service(), {'p': SimpleNamespace()}, idle_signal=signal,
                                  model_client=BlockingClient(), clock=lambda: clock[0])
        clock[0] = 120
        thread = threading.Thread(target=worker.run_once, daemon=True)
        worker._thread = thread
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            with patch('botville_gateway.memory.worker.MEMORY_WORKER_STOP_TIMEOUT_SECONDS', 0.05), \
                    self.assertLogs('botville_gateway.memory.worker', 'WARNING') as logged:
                started = time.monotonic()
                self.assertFalse(worker.stop())
                elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.5)
            self.assertEqual(logged.output, ['WARNING:botville_gateway.memory.worker:Memory idle worker did not stop before timeout; storage left open'])
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())


if __name__ == '__main__':
    unittest.main()
