# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""PR3b portable vector and embedding transport checks."""
import json
import os
import sys
import urllib.error
from unittest.mock import patch
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'src'))
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from botville_gateway.as_consumer import TransientUnavailable
from botville_gateway.engine.model_client import ModelError
from botville_gateway.memory.embedding import EmbeddingClient, EmbeddingConfig
from botville_gateway.memory.store import MemoryStore
from botville_gateway.memory.vector import PythonVectorStore, SqliteVecStore, build_vector_store


class _Server(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'
    code, body, seen_auth = 200, {'data': [{'embedding': [1, 0]}]}, ''
    def do_POST(self):
        type(self).seen_auth = self.headers.get('Authorization', '')
        body = json.dumps(type(self).body).encode()
        self.send_response(type(self).code)
        if type(self).code == 302: self.send_header('Location', '/redirect-target')
        self.send_header('Content-Length', str(len(body))); self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args): pass


class EmbeddingTests(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(('127.0.0.1', 0), _Server)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.base = 'http://127.0.0.1:%d' % self.server.server_port
        _Server.code, _Server.body, _Server.seen_auth = 200, {'data': [{'embedding': [1, 0]}]}, ''
    def tearDown(self): self.server.shutdown(); self.thread.join(); self.server.server_close()
    def client(self, **extra):
        env = {'GATEWAY_EMBEDDING_MODEL': 'fake', 'GATEWAY_EMBEDDING_BASE_URL': self.base, **extra}
        return EmbeddingClient(EmbeddingConfig.from_env(env), .5)
    def test_response_pins_dimension_and_does_not_expose_key(self):
        client = self.client(GATEWAY_EMBEDDING_API_KEY='fake-secret')
        self.assertEqual([1., 0.], client.embed('hello'))
        self.assertEqual(2, client.dimension); self.assertEqual('Bearer fake-secret', _Server.seen_auth)
        self.assertNotIn('fake-secret', repr(client.config))
        _Server.body = {'data': [{'embedding': [1]}]}
        with self.assertRaises(ModelError): client.embed('next')
    def test_error_mapping(self):
        _Server.code = 503
        with self.assertRaises(TransientUnavailable): self.client().embed('x')
        _Server.code = 400
        with self.assertRaises(ModelError): self.client().embed('x')
    def test_redirects_errors_and_connection_failures_are_sanitized(self):
        client = self.client(GATEWAY_EMBEDDING_API_KEY='fake-secret')
        _Server.body = {'error':'fake-secret secret-memory-body'}
        for code in (302, 400, 401, 408, 429, 503):
            _Server.code = code
            error = TransientUnavailable if code in (408,429,503) else ModelError
            with self.assertRaises(error) as caught:
                client.embed('secret-memory-body')
            self.assertIn(str(code), str(caught.exception))
            self.assertNotIn('fake-secret', str(caught.exception))
            self.assertNotIn('secret-memory-body', str(caught.exception))
        for error in (TimeoutError('secret-memory-body'), urllib.error.URLError('fake-secret')):
            with patch.object(client._opener, 'open', side_effect=error):
                with self.assertRaises(TransientUnavailable) as caught:
                    client.embed('secret-memory-body')
                self.assertNotIn('fake-secret', str(caught.exception))
                self.assertNotIn('secret-memory-body', str(caught.exception))

    def test_validation(self):
        with self.assertRaises(ValueError): EmbeddingConfig.from_env({'GATEWAY_EMBEDDING_MODEL': 'x'})
        with self.assertRaises(ValueError): EmbeddingConfig.from_env({'GATEWAY_EMBEDDING_DIM': 'bad'})


class VectorTests(unittest.TestCase):
    def setUp(self):
        self.memory = MemoryStore(':memory:'); self.vectors = PythonVectorStore(self.memory, scan_limit=2)
    def tearDown(self): self.memory.close()
    def item(self, event, text='text'):
        self.memory.add_item(persona_id='p', scope_kind='dm', scope_key='u', room_id=None, counterpart='u', event_id=event, sender='u', role='user', trust='trusted', text=text)
        return self.memory.items('p')[0]['id']
    def test_cosine_scope_and_delete_trigger(self):
        one = self.item('one', 'one'); self.vectors.upsert(persona_id='p', scope_kind='dm', scope_key='u', vector=[1, 0], model='m', item_id=one)
        self.memory.add_item(persona_id='p', scope_kind='group', scope_key='r', room_id='r', counterpart=None, event_id='two', sender='u', role='user', trust='trusted', text='two')
        group = self.memory.items('p', scope_kind='group')[0]['id']
        self.vectors.upsert(persona_id='p', scope_kind='group', scope_key='r', vector=[1, 0], model='m', item_id=group)
        rows = self.vectors.search(persona_id='p', scope_kind='dm', scope_key='u', query_vec=[1, 0])
        self.assertEqual(['one'], [r['text'] for r in rows])
        self.memory.delete_item('p', one)
        self.assertEqual([], self.vectors.search(persona_id='p', scope_kind='dm', scope_key='u', query_vec=[1, 0]))
    def test_limit_stale_and_fallback(self):
        a = self.item('a'); self.vectors.upsert(persona_id='p', scope_kind='dm', scope_key='u', vector=[0, 1], model='old', item_id=a)
        self.assertEqual(1, len(self.vectors.stale_pending(model='new', dim=2)))
        self.assertEqual([], self.vectors.search(persona_id='p', scope_kind='dm', scope_key='u', query_vec=[0, 1], model='new'))
        stats = {}
        with patch('botville_gateway.memory.vector.SqliteVecStore', side_effect=RuntimeError):
            got = build_vector_store(self.memory, 'sqlite-vec', stats=stats)
        self.assertIsInstance(got, PythonVectorStore)
        self.assertIn('memory_vector_sqlite_vec_fallbacks', stats)
    def test_scan_limit_and_cosine_order(self):
        ids = []
        for event, vector in (('old-best',[1,0]), ('middle',[0,1]), ('new',[1,1])):
            item = self.item(event, event); ids.append(item)
            self.vectors.upsert(persona_id='p',scope_kind='dm',scope_key='u',item_id=item,model='m',vector=vector)
        rows = self.vectors.search(persona_id='p',scope_kind='dm',scope_key='u',query_vec=[1,0])
        self.assertEqual([row['text'] for row in rows], ['new','middle'])
        self.assertAlmostEqual(rows[0]['score'], 2**-0.5)
        self.assertNotIn(ids[0], [row['item_id'] for row in rows])

    def test_sqlite_vec_searches_entire_scope_not_portable_scan_limit(self):
        # Exercise the optional SQL path without an extension: this minimal
        # deterministic function makes the oldest exact vector rank first.
        self.memory._db.create_function('vec_distance_cosine', 2,
                                        lambda left, right: 0.0 if left == right else 1.0)
        old = self.item('old-best', 'old-best')
        self.vectors.upsert(persona_id='p', scope_kind='dm', scope_key='u', item_id=old,
                            model='m', vector=[1, 0])
        for number in range(500):
            item = self.item('new-%d' % number, 'new')
            self.vectors.upsert(persona_id='p', scope_kind='dm', scope_key='u', item_id=item,
                                model='m', vector=[0, 1])
        vectors = object.__new__(SqliteVecStore)
        vectors.store, vectors.scan_limit = self.memory, 2
        rows = vectors.search(persona_id='p', scope_kind='dm', scope_key='u',
                              query_vec=[1, 0], model='m', dim=2, k=1)
        self.assertEqual([old], [row['item_id'] for row in rows])

    def test_dirty_summary_removes_summary_vector(self):
        item = self.item('event', 'source')
        self.assertTrue(self.memory.save_summary(persona_id='p', scope_kind='dm', scope_key='u', text='summary', trust='derived', covers_until_item_id=item, model='m'))
        summary = self.memory.summary('p', 'dm', 'u')
        self.assertTrue(self.vectors.upsert(persona_id='p', scope_kind='dm', scope_key='u', vector=[1, 0], model='m', summary_id=summary['id'], expected_text='summary'))
        self.memory.redact_event('event')
        with self.memory._lock:
            self.assertEqual(0, self.memory._db.execute('SELECT count(*) FROM memory_vectors').fetchone()[0])

if __name__ == '__main__': unittest.main()
