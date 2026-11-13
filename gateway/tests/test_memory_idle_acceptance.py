# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""PR3b real-path regression checks for isolation, priorities and human replies."""
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'src'))
from botville_gateway.as_consumer import ConsumerContext, TransientUnavailable
from botville_gateway.config import Config
from botville_gateway.engine import EngineHooks, ModelClient, ModelConfig, Persona
from botville_gateway.engine.engine import estimate_tokens
from botville_gateway.matrix import InMemoryMatrixTransport
from botville_gateway.memory.profile import ModelExtractor
from botville_gateway.memory.embedding import EmbeddingClient, EmbeddingConfig
from botville_gateway.memory.recall import assemble_recall
from botville_gateway.memory.store import MemoryStore
from botville_gateway.memory.summary import summary_input, generate_summary, StoreSummaryProvider
from botville_gateway.memory.vector import PythonVectorStore, VectorRecall
from botville_gateway.service import GatewayService

AI = '@ai_test:local'
HUMAN = '@human:local'

class Acceptance(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore(':memory:')
        self.addCleanup(self.store.close)
    def add(self, event, text, scope='dm', key=HUMAN, trust='trusted'):
        self.store.add_item(persona_id=AI, scope_kind=scope, scope_key=key, room_id='!r',
            counterpart=HUMAN, event_id=event, sender=HUMAN, role='user', text=text, trust=trust)
        return self.store.items(AI)[0]['id']
    def test_summary_threshold_and_no_silent_input_truncation(self):
        for n in range(40): self.add('$%d'%n, 'short')
        self.assertIsNone(summary_input(self.store, persona_id=AI, scope_kind='dm', scope_key=HUMAN))
        self.add('$long', 'x'*7990 + 'TAILMARK')
        request = summary_input(self.store, persona_id=AI, scope_kind='dm', scope_key=HUMAN)
        self.assertIn('TAILMARK', ''.join(m['content'] for m in request['messages']))
        self.add('$group', '\u4e2d'*4001, 'group', '!group')
        self.assertIsNotNone(summary_input(self.store, persona_id=AI, scope_kind='group', scope_key='!group'))
    def test_vector_priority_dedupe_scope_and_budget(self):
        item = self.add('$a', 'semantic unique text')
        vectors = PythonVectorStore(self.store)
        vectors.upsert(persona_id=AI, scope_kind='dm', scope_key=HUMAN,
                       item_id=item, vector=[1,0], model='m')
        adapter = VectorRecall(vectors, [1,0], model='m', dim=2)
        messages = assemble_recall(self.store, persona_id=AI, scope_kind='dm', scope_key=HUMAN,
                                   counterpart=HUMAN, vector_recall=adapter, budget=200)
        joined = ''.join(m['content'] for m in messages)
        self.assertEqual(joined.count('semantic unique text'), 1)
        self.assertIn('layer=vector', joined)
        self.assertLessEqual(sum(estimate_tokens(m['content']) for m in messages), 200)
        self.assertEqual(adapter.recall(persona_id=AI, scope_kind='group', scope_key='!group'), [])
    def test_summary_untrusted_recall_and_redaction_exclusion(self):
        self.add('$bad', 'REDACT_THIS', trust='untrusted')
        for n in range(40): self.add('$s%d'%n, 'ordinary')
        client = SimpleNamespace(complete=lambda **kw: 'summary-data')
        generate_summary(self.store, client, persona_id=AI, scope_kind='dm', scope_key=HUMAN)
        self.assertEqual(self.store.summary(AI,'dm',HUMAN)['trust'], 'untrusted')
        messages = assemble_recall(self.store, persona_id=AI, scope_kind='dm', scope_key=HUMAN,
            counterpart=HUMAN, summary_provider=StoreSummaryProvider(self.store))
        self.assertTrue(any('summary-data' in m['content'] and '<untrusted id=' in m['content'] for m in messages))
        self.store.redact_event('$bad')
        request = summary_input(self.store, persona_id=AI, scope_kind='dm', scope_key=HUMAN)
        self.assertNotIn('REDACT_THIS', str(request['messages']))
        self.assertNotIn('summary-data', str(request['messages']))
    def test_embedding_failure_does_not_prevent_real_human_reply(self):
        transport = InMemoryMatrixTransport()
        config = Config(dev_mode=True, matrix_server_name='local', silent_hours_enabled=False,
                        hmac_secret=b'x'*64)
        service = GatewayService(config, transport)
        embedding = SimpleNamespace(config=SimpleNamespace(model='m'), dimension=2,
            embed=lambda text: (_ for _ in ()).throw(TransientUnavailable()))
        client = ModelClient(ModelConfig('http://example.invalid','fake'))
        hooks = EngineHooks(personas={AI:Persona(AI,'fixed')}, client=client, service=service,
            memory_store=self.store, memory_embedding=embedding, memory_vectors=PythonVectorStore(self.store))
        context = ConsumerContext(event_type='message_text', event_id='$human', room_id='!r',
            sender=HUMAN, text='hello', trust='trusted', persona_user_id=AI,
            target_user_ids=(AI,), room_ai_user_ids=(AI,), room_members_confirmed=True,
            room_is_dm=True)
        with patch.object(client, 'complete', return_value='answer'):
            hooks.on_human_text(context)
        self.assertEqual(len(transport.sent), 1)
        self.assertEqual(hooks.stats.snapshot()['memory_vector_skipped'], 1)

    def test_embedding_budget_denial_makes_no_request_and_still_replies(self):
        transport = InMemoryMatrixTransport()
        config = Config(dev_mode=True, matrix_server_name='local', silent_hours_enabled=False,
                        platform_token_budget=1000, hmac_secret=b'x'*64)
        service = GatewayService(config, transport)
        requests = []
        class FakeEmbedding(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(self.path)
                self.send_response(200)
                self.end_headers()
            def log_message(self, format, *args): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), FakeEmbedding)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(lambda: server.shutdown())
        embedding = EmbeddingClient(EmbeddingConfig(
            'http://127.0.0.1:%d' % server.server_port, 'fake-embedding', 2))
        # Leave enough budget for the short reply, but not the 20% reply reserve
        # required before an embedding call.  This is a real policy denial.
        service.record_memory_usage(persona_id=AI, tokens=800, conversation_id='!r')
        client = ModelClient(ModelConfig('http://example.invalid', 'fake'))
        hooks = EngineHooks(personas={AI: Persona(AI, 'fixed', max_reply_chars=10)}, client=client,
            service=service, memory_store=self.store, memory_embedding=embedding,
            memory_vectors=PythonVectorStore(self.store))
        context = ConsumerContext(event_type='message_text', event_id='$budget', room_id='!r',
            sender=HUMAN, text='hello', trust='trusted', persona_user_id=AI,
            target_user_ids=(AI,), room_ai_user_ids=(AI,), room_members_confirmed=True,
            room_is_dm=True)
        with patch.object(client, 'complete', return_value='answer'):
            hooks.on_human_text(context)
        self.assertEqual(requests, [])
        self.assertEqual(len(transport.sent), 1)
        self.assertEqual(hooks.stats.snapshot()['memory_vector_skipped'], 1)
    def test_memory_usage_only_consumes_tokens_not_message_quota(self):
        service = GatewayService(Config(dev_mode=True, silent_hours_enabled=False,
            platform_token_budget=100000, rate_limit_messages=1,
            hmac_secret=b'x'*64), InMemoryMatrixTransport())
        with patch.object(service, 'evaluate_policy', wraps=service.evaluate_policy) as evaluate:
            self.assertTrue(service.authorize_memory_call(AI, 100))
            self.assertEqual(evaluate.call_args.args[0]['kind'], 'memory')
        decision = service.evaluate_policy({'persona_id':AI, 'kind':'message', 'token_estimate':1})
        self.assertEqual(decision['decision'], 'allow')
        usage = service.get_usage(persona_id=AI)
        self.assertEqual(usage['totals']['messages_sent'], 0)
        self.assertEqual(usage['totals']['tokens_total'], 100)
        self.assertEqual(usage['breakdown'][0]['key'], AI)
        self.assertEqual(service.list_usage_events(persona_id=AI)['items'], [])

    def test_memory_usage_is_bucketed_by_reply_conversation(self):
        service = GatewayService(Config(dev_mode=True, silent_hours_enabled=False,
            platform_token_budget=100000, hmac_secret=b'x'*64), InMemoryMatrixTransport())
        self.assertTrue(service.authorize_memory_call(AI, 100, conversation_id='!room:local'))
        usage = service.get_usage(persona_id=AI)
        self.assertEqual(usage['breakdown'][0]['key'], '!room:local')

    def test_extractor_rejects_duplicate_and_non_string_keys(self):
        for answer in ('[{"key":[],"value":"bad"}]',
                       '[{"key":"name","key":"notes","value":"bad"}]'):
            self.assertEqual(list(ModelExtractor(SimpleNamespace(complete=lambda **kw: answer)).extract('text')), [])

if __name__ == '__main__': unittest.main()
