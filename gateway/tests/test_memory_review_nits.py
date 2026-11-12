# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""Regression coverage for PR #205 review boundaries; fake data only."""
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from botville_gateway.as_consumer import MemoryDBUnavailable
from botville_gateway.engine.engine import estimate_tokens
from botville_gateway.memory.api import OwnerMemoryAPI
from botville_gateway.memory.__main__ import main
from botville_gateway.memory.recall import assemble_recall, RECALL_ITEMS_LIMIT, RECALL_FACTS_LIMIT
from botville_gateway.memory.store import MemoryStore
from test_memory_integration import AI, HUMAN, SERVER, config, context
from botville_gateway.engine import EngineHooks, ModelClient, ModelConfig, Persona
from botville_gateway.matrix import InMemoryMatrixTransport
from botville_gateway.service import GatewayService
from test_as_consumer import _TmpDb


def add(store, event='$one', **extra):
    args = dict(persona_id=AI, scope_kind='dm', scope_key=HUMAN,
                room_id='!dm', counterpart=HUMAN, event_id=event, sender=HUMAN,
                role='user', trust='trusted', text='fake fixture')
    args.update(extra)
    return store.add_item(**args)


class StoreNits(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore(':memory:')
        self.addCleanup(self.store.close)
        self.api = OwnerMemoryAPI(self.store, {AI: HUMAN}, server_name=SERVER)

    def test_owner_delete_tombstones_survive_purge_and_block_replay(self):
        add(self.store)
        item = self.store.items(AI)[0]
        self.assertTrue(self.api.delete_item(AI, HUMAN, item['id']))
        self.store.upsert_fact(AI, HUMAN, 'name', 'fake', source='extraction', source_event_ids=['$a', '$b'])
        self.assertTrue(self.api.delete_fact(AI, HUMAN, 'name', HUMAN))
        self.store.purge(datetime.now(timezone.utc) + timedelta(days=1000))
        for event in ('$one', '$a', '$b'):
            self.assertTrue(self.store.is_tombstoned(event))
            self.assertFalse(add(self.store, event))
            self.assertFalse(self.store.upsert_fact(AI, HUMAN, 'name', 'fake', source='extraction', source_event_ids=[event]))
        self.assertFalse(self.api.delete_item(AI, HUMAN, item['id']))
        self.assertTrue(self.api.set_fact(AI, HUMAN, 'name', 'fake', HUMAN))
        self.assertTrue(self.api.delete_fact(AI, HUMAN, 'name', HUMAN))

    def test_counterpart_validation(self):
        for value in ('@x:elsewhere.test', '@AI_fake:example.test', '@bad user:example.test',
                      'x:example.test', '@:example.test', '@x:', '@x:example.test:extra', '', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.api.set_fact(AI, HUMAN, 'name', 'fake', value, 'shared')
        self.assertEqual(self.store.facts(AI), [])
        self.assertTrue(self.api.set_fact(AI, HUMAN, 'name', 'fake', '@person:EXAMPLE.TEST'))

    def test_recall_sql_limits_and_pinned_priority(self):
        add(self.store, '$pinned', pinned=True, text='PINNED')
        for i in range(RECALL_ITEMS_LIMIT + 10):
            add(self.store, '$event' + str(i))
        # Seed many keys to exercise the SQL cap independently of fact-key allowlist.
        self.store.upsert_fact(AI, HUMAN, 'name', 'fake')
        for i in range(RECALL_FACTS_LIMIT + 10):
            self.store._db.execute('''INSERT INTO memory_facts
                (persona_id,counterpart,key,value,visibility,source,trust,pinned,created_at,updated_at)
                VALUES(?,?,?,'fake','dm_only','owner','trusted',0,'2000','2000')''', (AI,HUMAN,'fixture'+str(i)))
        self.store._db.commit()
        self.assertEqual(len(self.store.items(AI, limit=RECALL_ITEMS_LIMIT)), RECALL_ITEMS_LIMIT)
        self.assertEqual(len(self.store.facts(AI, HUMAN, limit=RECALL_FACTS_LIMIT)), RECALL_FACTS_LIMIT)
        queries = []
        self.store._db.set_trace_callback(queries.append)
        result = assemble_recall(self.store, persona_id=AI, scope_kind='dm', scope_key=HUMAN, counterpart=HUMAN)
        self.store._db.set_trace_callback(None)
        self.assertIn('PINNED', result[0]['content'])
        selects = [q for q in queries if q.startswith('SELECT * FROM memory_')]
        self.assertEqual(len(selects), 2)
        self.assertTrue(all(' LIMIT ' in q for q in selects))

    def test_cjk_budget_and_ranges(self):
        for char in ('中', '\U00020000', '\U00030000', 'あ', 'ア', '한', 'Ａ', '。', '\uff76'):
            self.assertEqual(estimate_tokens(char * 100), 101)
        from botville_gateway.policy import estimate_tokens as policy_estimate
        self.assertIs(policy_estimate, estimate_tokens)
        self.assertEqual(estimate_tokens('abcd'), 2)
        self.assertEqual(estimate_tokens('中文abcd'), 4)
        add(self.store, text='中文' * 4000)
        result = assemble_recall(self.store, persona_id=AI, scope_kind='dm', scope_key=HUMAN, counterpart=HUMAN)
        self.assertLessEqual(sum(estimate_tokens(m['content']) for m in result), 2000)
        self.assertLess(len(result[0]['content']), 2500)

    def test_existing_permissions_and_schema_duplicate_migration(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'memory.db')
            first = MemoryStore(path)
            first._db.execute('DROP TABLE memory_schema')
            first._db.execute('CREATE TABLE memory_schema(version INTEGER NOT NULL)')
            first._db.executemany('INSERT INTO memory_schema VALUES(?)', [(2,), (2,)])
            first._db.commit()
            for name in (path, path+'-wal', path+'-shm'):
                os.chmod(name, 0o644)
            second = MemoryStore(path)
            try:
                for name in (path, path+'-wal', path+'-shm'):
                    self.assertEqual(os.stat(name).st_mode & 0o777, 0o600)
                self.assertEqual([tuple(r) for r in second._db.execute('SELECT * FROM memory_schema')], [(1, 4)])
                with self.assertRaises(sqlite3.IntegrityError):
                    second._db.execute('INSERT INTO memory_schema VALUES(2,2)')
                second._db.rollback()
            finally:
                second.close()
                first.close()
            reopened = MemoryStore(path)
            self.assertEqual(reopened._db.execute('SELECT count(*) FROM memory_schema').fetchone()[0], 1)
            reopened.close()

    def test_concurrent_first_open(self):
        for _ in range(10):
            with tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, 'memory.db')
                barrier = threading.Barrier(2)
                def open_store(_):
                    barrier.wait()
                    return MemoryStore(path)
                with ThreadPoolExecutor(2) as pool:
                    stores = list(pool.map(open_store, range(2)))
                try:
                    for store in stores:
                        self.assertEqual(store._db.execute('SELECT count(*) FROM memory_schema').fetchone()[0], 1)
                finally:
                    for store in stores:
                        store.close()

    def test_cli_delete_and_invalid_counterpart(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, 'memory.db')
            personas = os.path.join(d, 'personas.json')
            Path(personas).write_text(json.dumps({'personas': [{'user_id': AI, 'system_prompt': 'fake', 'owner_user_id': HUMAN}]}))
            store = MemoryStore(db)
            add(store)
            store.upsert_fact(AI, HUMAN, 'name', 'fake', source='extraction', source_event_ids=['$fact'])
            item_id = store.items(AI)[0]['id']
            store.close()
            common = ['--db', db, '--persona', AI, '--as-owner', HUMAN, '--personas-file', personas, '--server-name', SERVER]
            def invoke(args):
                with patch('botville_gateway.memory.__main__._emit'), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    return main(args + common)
            self.assertEqual(invoke(['delete', '--id', str(item_id)]), 0)
            self.assertEqual(invoke(['delete', '--kind', 'fact', '--key', 'name', '--counterpart', HUMAN]), 0)
            for counterpart in ('@ai_bad:example.test', '@x:elsewhere', 'bad'):
                self.assertNotEqual(invoke(['set-fact', '--key', 'name', '--value', 'fake', '--counterpart', counterpart]), 0)
            store = MemoryStore(db)
            try:
                self.assertTrue(store.is_tombstoned('$one'))
                self.assertTrue(store.is_tombstoned('$fact'))
                self.assertEqual(store.facts(AI), [])
            finally:
                store.close()


class MemoryFailureNits(_TmpDb):
    def test_reads_defer_and_backoff_caps_alerts_and_resets(self):
        store = MemoryStore(':memory:')
        self.addCleanup(store.close)
        transport = InMemoryMatrixTransport()
        hooks = EngineHooks(personas={AI: Persona(AI, 'fake')},
            client=ModelClient(ModelConfig('https://example.invalid', 'fake', 1)),
            service=GatewayService(config(), transport), memory_store=store)
        hooks._client.complete = Mock(return_value='fake')
        self._put_txn('t1')
        consumer = self._make_consumer(hooks=hooks)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        with patch.object(store, 'is_tombstoned', side_effect=sqlite3.OperationalError('private sentinel')):
            with self.assertLogs('botville_gateway.engine.engine', level='WARNING') as logs:
                for i in range(10):
                    self.conn.execute("UPDATE as_transactions SET next_attempt_at=NULL WHERE txn_id='t1'")
                    self.conn.commit()
                    consumer.run_once(now=now)
                    row = self.conn.execute("SELECT attempts,failed_at,processed_at,next_attempt_at FROM as_transactions WHERE txn_id='t1'").fetchone()
                    self.assertEqual(tuple(row[:3]), (0, None, None))
                    retry = datetime.fromisoformat(row[3].replace('Z', '+00:00'))
                    self.assertEqual((retry-now).total_seconds(), min(3600, 30*2**i))
            self.assertEqual([r.getMessage() for r in logs.records], ['OperationalError'])
            self.assertTrue(all(r.exc_info is None for r in logs.records))
        self.assertEqual(hooks.stats.snapshot()['memory_db_alerts'], 1)
        self.assertEqual(hooks.stats.snapshot()['memory_db_unavailable_consecutive'], 10)
        self.assertEqual(transport.sent, [])
        hooks._client.complete.assert_not_called()
        hooks.on_human_text(context())
        self.assertEqual(hooks.stats.snapshot()['memory_db_unavailable_consecutive'], 0)
        for operation in ('is_tombstoned', 'items', 'facts', 'add_item'):
            with patch.object(store, operation, side_effect=sqlite3.OperationalError('private sentinel')):
                with self.assertRaises(MemoryDBUnavailable) as raised:
                    hooks.on_human_text(context(event='$next'))
                self.assertEqual(raised.exception.retry_seconds, 30)
            hooks.on_human_text(context(event='$recovered'))
