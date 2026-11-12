# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""Cross-layer PR3b paths: construction, consolidation and optional enrichment."""
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'src'))

from botville_gateway.config import Config
from botville_gateway.http_server import build_server
from botville_gateway.memory.store import MemoryStore
from botville_gateway.memory.vector import PythonVectorStore
from botville_gateway.memory.worker import IdleSignal, MemoryIdleWorker


class Service:
    def __init__(self): self.authorized = []; self.used = []
    def authorize_memory_call(self, persona, tokens):
        self.authorized.append((persona, tokens)); return True
    def record_memory_usage(self, **kwargs): self.used.append(kwargs)


class Model:
    config = SimpleNamespace(configured=True)
    def __init__(self): self.calls = []
    def complete(self, **kwargs): self.calls.append(kwargs); return 'summary'


class Embedding:
    def __init__(self, model='old', dim=2):
        self.config = SimpleNamespace(model=model); self.dimension = dim; self.calls = []
    def embed(self, text): self.calls.append(text); return [1.0] + [0.0] * (self.dimension - 1)


class IdleIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore(':memory:')
        self.addCleanup(self.store.close)
        self.service = Service()
        self.now = [0.0]
        self.idle = IdleSignal(clock=lambda: self.now[0])
        self.persona = SimpleNamespace(user_id='p')

    def add(self, event, text='text', trust='trusted'):
        self.store.add_item(persona_id='p', scope_kind='dm', scope_key='@u:local', room_id='!r',
                            counterpart='@u:local', event_id=event, sender='@u:local', role='user',
                            trust=trust, text=text)

    def worker(self, model=None, embedding=None, vectors=None):
        return MemoryIdleWorker(self.store, self.service, {'p': self.persona}, idle_signal=self.idle,
            model_client=model, embedding=embedding, vectors=vectors, server_name='local',
            clock=lambda: self.now[0])

    def test_real_worker_summary_dirty_rebuild_and_embedding_reembed(self):
        for n in range(41): self.add('$%d' % n, 'record %d' % n)
        model, embed, vectors = Model(), Embedding(), PythonVectorStore(self.store)
        worker = self.worker(model, embed, vectors)
        self.now[0] = 121
        worker._process_scope({'persona_id': 'p', 'scope_kind': 'dm', 'scope_key': '@u:local'}, self.persona)
        summary = self.store.summary('p', 'dm', '@u:local')
        self.assertEqual('summary', summary['text'])
        self.assertTrue(self.service.authorized)
        old_vectors = vectors.search(persona_id='p', scope_kind='dm', scope_key='@u:local', query_vec=[1, 0], model='old', dim=2)
        self.assertTrue(old_vectors)
        self.store.redact_event('$0')
        self.assertTrue(self.store.summary('p', 'dm', '@u:local')['dirty'])
        worker._process_scope({'persona_id': 'p', 'scope_kind': 'dm', 'scope_key': '@u:local'}, self.persona)
        self.assertFalse(self.store.summary('p', 'dm', '@u:local')['dirty'])
        embed.config.model, embed.dimension = 'new', 3
        worker._process_scope({'persona_id': 'p', 'scope_kind': 'dm', 'scope_key': '@u:local'}, self.persona)
        self.assertTrue(vectors.search(persona_id='p', scope_kind='dm', scope_key='@u:local', query_vec=[1, 0, 0], model='new', dim=3))

    def test_idle_signal_prevents_work_until_quiet(self):
        self.add('$x')
        model = Model(); worker = self.worker(model)
        self.assertEqual(0, worker.run_once())
        self.idle.notify(); self.now[0] = 119
        self.assertEqual(0, worker.run_once())
        self.now[0] = 120
        worker.run_once()
        # No summary threshold, but the trusted item was extracted only after idle.
        self.assertEqual(1, self.store.items('p')[0]['extracted'])

    def test_server_constructs_worker_only_with_memory_and_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'memory.db')
            with patch('botville_gateway.memory.worker.MemoryIdleWorker.start') as started:
                server = build_server(Config(port=0, dev_mode=True, hmac_secret=b'x' * 64, memory_db_path=path,
                    model_base_url='http://localhost:1', model_name='fake'))
                try:
                    self.assertIsNotNone(server.memory_worker)
                    started.assert_called_once()
                finally:
                    server.server_close()
            server = build_server(Config(port=0, dev_mode=True, hmac_secret=b'x' * 64, memory_db_path=path))
            try: self.assertIsNone(server.memory_worker)
            finally: server.server_close()


if __name__ == '__main__': unittest.main()
