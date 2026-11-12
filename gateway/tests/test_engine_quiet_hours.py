# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""Default quiet-hours integration, including send boundary races and replay."""
import json
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from botville_gateway.as_consumer import AsTransactionConsumer, speech_idempotency_key
from botville_gateway.as_txn_store import AsTransactionStore, PersistentSpeechDedupe, connect
from botville_gateway.config import Config
from botville_gateway.engine import EngineHooks, Persona
from botville_gateway.matrix import InMemoryMatrixTransport
from botville_gateway.service import GatewayService

AI = '@ai_a:example.test'
OTHER = '@ai_b:example.test'
ROOM = '!room:example.test'

class QuietHoursTests(unittest.TestCase):
    def setUp(self):
        self.clock = datetime(2026, 11, 13, 15, 5, tzinfo=timezone.utc)
        self.end = datetime(2026, 11, 13, 23, 30, tzinfo=timezone.utc)
        self.conn = connect(':memory:')
        self.addCleanup(self.conn.close)
        self.store = AsTransactionStore(self.conn)
        self.dedupe = PersistentSpeechDedupe(self.store)
        # Keep all policy defaults, especially 23:00–07:30 Asia/Shanghai.
        self.config = Config(dev_mode=True, matrix_server_name='example.test')
        self.transport = InMemoryMatrixTransport()
        self.service = GatewayService(self.config, self.transport, now_fn=lambda: self.clock)
        self.client = mock.Mock()
        self.client.complete.return_value = 'morning reply'
        self.hooks = EngineHooks(personas={uid: Persona(user_id=uid, system_prompt='Be nice.')
            for uid in (AI, OTHER)}, client=self.client, service=self.service, dedupe=self.dedupe)
        self.consumer = AsTransactionConsumer(self.store, hooks=self.hooks,
            service=self.service, server_name='example.test', dedupe=self.dedupe,
            now=lambda: self.clock)

    def enqueue(self, txn='txn', targets=(AI,)):
        self.store.store_transaction(txn, json.dumps({'events': [{
            'type': 'm.room.message', 'event_id': '$night', 'room_id': ROOM,
            'sender': '@alice:example.test', 'content': {'msgtype': 'm.text',
            'body': 'hello', 'm.mentions': {'user_ids': list(targets)}}}]}))

    def assert_deferred(self):
        row = self.conn.execute('SELECT * FROM as_transactions WHERE txn_id=?', ('txn',)).fetchone()
        self.assertIsNone(row['processed_at'])
        self.assertIsNone(row['failed_at'])
        self.assertEqual(row['attempts'], 0)
        self.assertEqual(row['next_attempt_at'], '2026-11-13T23:30:00Z')
        self.assertEqual(self.consumer.stats['deferred'], 1)

    def test_default_quiet_hours_resume_and_replay(self):
        self.enqueue()
        self.assertEqual(self.consumer.run_once(), 0)
        self.client.complete.assert_not_called()
        self.assertEqual(self.transport.sent, [])
        self.assertFalse(self.dedupe.seen(speech_idempotency_key(AI, '$night')))
        self.assert_deferred()
        self.assertEqual(self.consumer.run_once(), 0)
        self.clock = self.end
        self.assertEqual(self.consumer.run_once(), 1)
        self.assertEqual(self.client.complete.call_count, 1)
        self.enqueue('replay')
        self.consumer.run_once()
        self.assertEqual(len(self.transport.sent), 1)
        self.assertEqual(self.client.complete.call_count, 1)

    def test_send_boundary_race_and_partial_personas(self):
        self.clock = datetime(2026, 11, 13, 14, 59, tzinfo=timezone.utc)
        def reply(**kwargs):
            if kwargs['persona'].user_id == OTHER:
                self.clock = datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc)
            return 'reply'
        self.client.complete.side_effect = reply
        self.enqueue(targets=(AI, OTHER))
        self.consumer.run_once()
        self.assert_deferred()
        self.assertEqual(len(self.transport.sent), 1)
        self.assertTrue(self.dedupe.seen(speech_idempotency_key(AI, '$night')))
        self.assertFalse(self.dedupe.seen(speech_idempotency_key(OTHER, '$night')))
        self.clock = self.end
        self.client.complete.side_effect = None
        self.consumer.run_once()
        self.assertEqual([e.sender_id for e in self.transport.sent], [AI, OTHER])
        self.assertEqual(self.client.complete.call_count, 3)
        self.enqueue('replay', targets=(AI, OTHER))
        self.consumer.run_once()
        self.assertEqual(len(self.transport.sent), 2)

    def test_missing_decision_time_uses_config_window(self):
        with mock.patch.object(self.service, 'evaluate_policy', return_value={'decision': 'defer'}):
            self.enqueue()
            self.consumer.run_once()
        self.assert_deferred()
        self.client.complete.assert_not_called()

    def test_non_allow_precheck_never_marks_speech(self):
        self.clock = self.end
        for decision in ('block', 'unknown'):
            with self.subTest(decision=decision), mock.patch.object(
                    self.service, 'evaluate_policy', return_value={'decision': decision}):
                self.enqueue(decision)
                self.assertEqual(self.consumer.run_once(), 1)
                self.assertFalse(self.dedupe.seen(speech_idempotency_key(AI, '$night')))
        self.client.complete.assert_not_called()
        self.assertEqual(self.transport.sent, [])

    def test_room_buffers_lru_bound(self):
        with mock.patch('botville_gateway.engine.engine.MAX_ROOM_BUFFERS', 2):
            first = self.hooks._get_buffer('first')
            self.hooks._get_buffer('second')
            self.assertIs(self.hooks._get_buffer('first'), first)
            self.hooks._get_buffer('third')
            self.assertEqual(list(self.hooks._buffers), ['first', 'third'])
