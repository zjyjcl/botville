# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""Integration checks for PR3b's bounded internal idle worker."""
from __future__ import annotations
import os
import sys
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'src'))
from botville_gateway.as_consumer import TransientUnavailable
from botville_gateway.config import Config
from botville_gateway.matrix import InMemoryMatrixTransport
from botville_gateway.memory.store import MemoryStore
from botville_gateway.memory.worker import BudgetedModel, IdleSignal, MemoryIdleWorker
from botville_gateway.service import GatewayService, ValidationError

class Clock:
    def __init__(self): self.value = 0
    def __call__(self): return self.value

class Service:
    def __init__(self): self.calls = []
    def authorize_memory_call(self, persona_id, estimate): self.calls.append((persona_id, estimate)); return True
    def record_memory_usage(self, **kwargs): pass

class Client:
    def __init__(self): self.calls=[]; self.config=SimpleNamespace()
    def complete(self, **kwargs): self.calls.append(kwargs); return '[{"key":"name","value":"Ada"}]'

def add(store, key='@alice:example.test', event='$e', sender='@alice:example.test', role='user', trust='trusted'):
    store.add_item(persona_id='p', scope_kind='dm', scope_key=key, room_id='!r', counterpart=sender,
      event_id=event, sender=sender, role=role, trust=trust, text='ordinary text')

class IdleWorkerTests(unittest.TestCase):
    def setUp(self):
        self.store=MemoryStore(':memory:'); self.addCleanup(self.store.close)
        self.clock=Clock(); self.pending=False
        self.signal=IdleSignal(clock=self.clock, pending=lambda: self.pending)
        self.service=Service(); self.personas={'p':SimpleNamespace(system_prompt='fixed')}
    def worker(self, client=None):
        return MemoryIdleWorker(self.store, self.service, self.personas, idle_signal=self.signal,
          model_client=client, server_name='example.test', clock=self.clock)
    def test_idle_pending_and_round_cap(self):
        for i in range(9): add(self.store, '@u%d:example.test'%i, '$%d'%i)
        worker=self.worker(); seen=[]; worker._process_scope=lambda scope, persona: seen.append(scope)
        self.assertEqual(worker.run_once(), 0)
        self.clock.value=120; self.pending=True; self.assertEqual(worker.run_once(), 0)
        self.pending=False; self.assertEqual(worker.run_once(), 8); self.assertEqual(len(seen), 8)
    def test_transient_backoff_doubles(self):
        add(self.store); self.clock.value=120; worker=self.worker()
        worker._process_scope=lambda scope, persona: (_ for _ in ()).throw(TransientUnavailable())
        worker.run_once(); self.assertEqual(worker._retry_at, 150)
        # The worker deliberately rotates its bounded cursor; restart its round
        # to exercise a second failure rather than treating an empty tail as one.
        worker._cursor=('', '', '')
        self.clock.value=150; worker.run_once(); self.assertEqual(worker._retry_at, 210)
    def test_trusted_local_user_extracts_once(self):
        add(self.store, event='$trusted')
        add(self.store, event='$foreign', sender='@bob:foreign.test', trust='untrusted')
        add(self.store, event='$ai', sender='@ai_x:example.test', role='assistant')
        self.clock.value=120; client=Client(); self.worker(client).run_once()
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self.store.facts('p','@alice:example.test')[0]['value'], 'Ada')
        self.assertTrue(all(row['extracted'] for row in self.store.items('p')))
        self.worker(client).run_once(); self.assertEqual(len(client.calls), 1)
    def test_real_policy_reserve_quiet_and_no_public_memory_kind(self):
        now=datetime(2026,11,13,12,tzinfo=timezone.utc)
        reserve=GatewayService(Config(dev_mode=True, matrix_server_name='example.test', silent_hours_enabled=False, platform_token_budget=100), InMemoryMatrixTransport(), now_fn=lambda:now)
        self.assertTrue(reserve.authorize_memory_call('p',80)); self.assertFalse(reserve.authorize_memory_call('p',1))
        quiet=GatewayService(Config(dev_mode=True, matrix_server_name='example.test', silent_hours_enabled=True, silent_hours_start='00:00', silent_hours_end='23:59'), InMemoryMatrixTransport(), now_fn=lambda:now)
        client=Client()
        with self.assertRaises(Exception): BudgetedModel(client,quiet,'p').complete(system_text='fixed',messages=[{'role':'user','content':'data'}])
        self.assertFalse(client.calls)
        with self.assertRaises(ValidationError): quiet.evaluate_policy({'persona_id':'p','kind':'memory'})

if __name__ == '__main__': unittest.main()
