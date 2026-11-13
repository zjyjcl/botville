# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""Focused regression coverage for PR4a memory boundaries."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'src'))
from botville_gateway.memory.owner import OwnerMemoryService
from botville_gateway.memory.store import MemoryStore
from botville_gateway.memory.vector import VectorRecall
from botville_gateway.memory.worker import IdleSignal, MemoryIdleWorker
from botville_gateway.as_consumer import TransientUnavailable


class _Vectors:
    def search(self, **kwargs):
        return [dict(persona_id='p', scope_kind='dm', scope_key='u', visibility='stored', text='one'),
                dict(persona_id='p', scope_kind='group', scope_key='other', visibility='wrong', text='two')]


class _Service:
    def authorize_memory_call(self, *args, **kwargs): return True


class Pr4aMemoryTests(unittest.TestCase):
    def test_vector_recall_query_is_immutable_and_scope_safe(self):
        base = VectorRecall(_Vectors())
        source = [1]
        left, right = base.with_query(source), base.with_query([2])
        source[0] = 9
        self.assertEqual((1,), left._query); self.assertEqual((2,), right._query)
        self.assertIsNone(base._query)
        with self.assertRaises((AttributeError, TypeError)):
            left._query = (3,)
        rows = left.recall(persona_id='p', scope_kind='dm', scope_key='u')
        self.assertEqual(1, len(rows)); self.assertEqual('stored', rows[0]['visibility'])

    def test_worker_stop_is_bounded(self):
        store = MemoryStore(':memory:'); self.addCleanup(store.close)
        worker = MemoryIdleWorker(store, _Service(), {}, idle_signal=IdleSignal())
        blocker = threading.Event()
        worker._thread = threading.Thread(target=lambda: blocker.wait(10), daemon=True); worker._thread.start()
        from unittest.mock import patch
        with patch('botville_gateway.memory.worker.MEMORY_WORKER_STOP_TIMEOUT_SECONDS', .01):
            started = time.monotonic(); worker.stop()
        self.assertLess(time.monotonic() - started, .5)
        blocker.set(); worker._thread.join(1)

    def test_worker_permanent_scope_failure_does_not_globally_backoff(self):
        store = MemoryStore(':memory:'); self.addCleanup(store.close)
        for key in ('a', 'b'):
            store.add_item(persona_id='p', scope_kind='dm', scope_key=key, room_id=None, counterpart=key, event_id='$'+key, sender=key, role='user', trust='trusted', text='x')
        clock = [240]
        signal = IdleSignal(clock=lambda: clock[0]); clock[0] += 121
        worker = MemoryIdleWorker(store, _Service(), {'p': object()}, idle_signal=signal, clock=lambda: clock[0])
        seen = []
        def process(scope, persona):
            seen.append(scope['scope_key'])
            if scope['scope_key'] == 'a': raise ValueError()
        worker._process_scope = process
        worker.run_once()
        self.assertEqual(['a', 'b'], seen); self.assertEqual(0, worker._retry_at)

    def test_worker_transient_backoff_caps(self):
        store = MemoryStore(':memory:'); self.addCleanup(store.close)
        store.add_item(persona_id='p', scope_kind='dm', scope_key='a', room_id=None, counterpart='a', event_id='$a', sender='a', role='user', trust='trusted', text='x')
        clock = [240]
        signal = IdleSignal(clock=lambda: clock[0]); clock[0] += 121
        worker = MemoryIdleWorker(store, _Service(), {'p': object()}, idle_signal=signal, clock=lambda: clock[0])
        worker._process_scope = lambda scope, persona: (_ for _ in ()).throw(TransientUnavailable())
        expected = (30, 60, 120, 240, 480, 960, 1920, 3600, 3600)
        for delay in expected:
            worker._cursor = ('', '', '')
            worker.run_once(); self.assertEqual(clock[0] + delay, worker._retry_at)
            clock[0] = worker._retry_at

    def test_purge_boundary_and_expiry_index(self):
        now = datetime(2026, 1, 2, tzinfo=timezone.utc)
        cutoff = (now - timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
        old = (now - timedelta(days=1, seconds=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
        store = MemoryStore(':memory:', retention_days=1); self.addCleanup(store.close)
        for index, stamp in enumerate((cutoff, old)):
            store._db.execute("INSERT INTO memory_items(id,persona_id,scope_kind,scope_key,role,trust,text,created_at,updated_at,pinned,source,visibility) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (100+index, 'p', 'dm', 'u', 'user', 'trusted', 'x', stamp, stamp, 0, 'x', 'dm_only'))
            store._db.execute("INSERT INTO memory_facts(id,persona_id,counterpart,key,value,visibility,source,trust,pinned,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (100+index, 'p', 'u', 'k'+str(index), 'x', 'dm_only', 'owner', 'owner', 0, stamp, stamp))
            store._db.execute("INSERT INTO memory_summaries(id,persona_id,scope_kind,scope_key,text,trust,covers_until_item_id,dirty,updated_at,model,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (100+index, 'p', 'dm', 'u'+str(index), 'x', 'derived', 1, 0, stamp, 'm', stamp))
        store._db.commit(); store.purge(now)
        for table in ('memory_items', 'memory_facts', 'memory_summaries'):
            self.assertEqual([100], [row[0] for row in store._db.execute('SELECT id FROM ' + table)])
        self.assertIsNotNone(store._db.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_memory_summaries_expiry'").fetchone())

    def test_tombstone_and_audit_purge_keep_exact_cutoff(self):
        now = datetime(2026, 1, 2, tzinfo=timezone.utc)
        cutoff = now - timedelta(days=1)
        store = MemoryStore(':memory:', retention_days=1)
        self.addCleanup(store.close)
        for label, point in (('old', cutoff - timedelta(seconds=1)),
                             ('equal', cutoff), ('new', cutoff + timedelta(seconds=1))):
            stamp = point.strftime('%Y-%m-%dT%H:%M:%SZ')
            store._db.execute('INSERT INTO memory_tombstones(event_id,created_at) VALUES(?,?)',
                              (label, stamp))
            store._db.execute("INSERT INTO memory_audit "
                "(occurred_at,request_id,caller,persona_id,action,result_code) "
                "VALUES(?,?,'owner','p','list','200')", (stamp, label))
        store._db.commit()
        self.assertEqual(store.purge(now), 2)
        for table, column in (('memory_tombstones', 'event_id'), ('memory_audit', 'request_id')):
            with self.subTest(table=table):
                self.assertEqual([row[0] for row in store._db.execute(
                    'SELECT ' + column + ' FROM ' + table + ' ORDER BY ' + column)], ['equal', 'new'])

    def test_v7_upgrade_adds_expiry_index_idempotently(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'memory.db')
            first = MemoryStore(path); first.close()
            # This represents a complete v7 database before the additive v8 DDL.
            import sqlite3
            db = sqlite3.connect(path)
            db.execute('DROP INDEX idx_memory_summaries_expiry')
            db.execute('UPDATE memory_schema SET version=7 WHERE id=1')
            db.commit(); db.close()
            upgraded = MemoryStore(path)
            self.assertIsNotNone(upgraded._db.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_memory_summaries_expiry'").fetchone())
            upgraded.close()
            repeated = MemoryStore(path)
            self.assertIsNotNone(repeated._db.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_memory_summaries_expiry'").fetchone())
            repeated.close()

    def test_owner_list_sql_filters_and_paginates(self):
        store = MemoryStore(':memory:', hmac_secret=b'k'); self.addCleanup(store.close)
        service = OwnerMemoryService(store, b'k')
        for index in range(120):
            dm = index % 2
            store.add_item(persona_id='p', scope_kind='dm' if dm else 'group', scope_key='u' if dm else 'r', room_id=None if dm else 'r', counterpart='u' if dm else None, event_id='$'+str(index), sender='u', role='user', trust='trusted', text='x', pinned=index % 3 == 0)
        seen, cursor = [], None
        while True:
            page, cursor = service.list('p', scope_kind='dm', pinned=False, cursor=cursor, limit=7)
            seen.extend(item['id'] for item in page)
            if not cursor: break
        self.assertEqual(len(seen), len(set(seen))); self.assertEqual(40, len(seen))

    def test_owner_list_5000_rows_matches_previous_reference(self):
        store = MemoryStore(':memory:', hmac_secret=b'k'); self.addCleanup(store.close)
        service = OwnerMemoryService(store, b'k')
        # Populate all three layers and mixed scopes without relying on their
        # insertion timestamps for identity.
        with store._lock, store._db:
            for index in range(5000):
                stamp = '2026-01-%02dT00:00:%02dZ' % (1 + index % 28, index % 60)
                if index % 3 == 0:
                    store._db.execute("INSERT INTO memory_items(id,persona_id,scope_kind,scope_key,room_id,counterpart,event_id,sender,role,trust,text,created_at,updated_at,pinned,source,visibility,public_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (10000+index, 'p', 'dm' if index % 2 else 'group', 'u' if index % 2 else 'r', None if index % 2 else 'r', 'u' if index % 2 else None, '$x'+str(index), 'u', 'user', 'trusted', 'x', stamp, stamp, int(index % 5 == 0), 'x', 'dm_only' if index % 2 else 'shared', 'mr_%016x' % index))
                elif index % 3 == 1:
                    store._db.execute("INSERT INTO memory_summaries(id,persona_id,scope_kind,scope_key,text,trust,covers_until_item_id,dirty,updated_at,model,created_at,public_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (10000+index, 'p', 'dm' if index % 2 else 'group', 'u' if index == 1 else ('du' if index % 2 else 'gr') + str(index), 'x', 'derived', 1, 0, stamp, 'm', stamp, 'ms_%016x' % index))
                else:
                    store._db.execute("INSERT INTO memory_facts(id,persona_id,counterpart,key,value,visibility,source,trust,pinned,created_at,updated_at,public_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (10000+index, 'p', 'u' if index % 2 else 'other', 'k'+str(index), 'x', 'dm_only', 'owner', 'owner', int(index % 5 == 0), stamp, stamp, 'mf_%016x' % index))

        def old_reference(**filters):
            wanted = set(filters.get('layers', ())) or {'recent', 'summary', 'profile'}
            output = []
            for layer, row in service._rows('p'):
                if layer not in wanted: continue
                item = service._memory(layer, row); scope = item['scope']
                if filters.get('scope_kind') and scope['kind'] != filters['scope_kind']: continue
                if filters.get('counterpart') and scope.get('counterpart') != filters['counterpart']: continue
                if filters.get('room_id') and scope.get('room_id') != filters['room_id']: continue
                if filters.get('pinned') is not None and item['pinned'] != filters['pinned']: continue
                output.append(item)
            return sorted(output, key=lambda item: (item['updated_at'], item['id']), reverse=True)

        for filters in ({}, {'scope_kind': 'dm'}, {'scope_kind': 'group'},
                        {'counterpart': 'u'}, {'room_id': 'r'}, {'pinned': False},
                        {'pinned': True}, {'layers': ('summary',)},
                        {'layers': ('recent', 'profile'), 'scope_kind': 'dm', 'counterpart': 'u', 'pinned': False}):
            expected = old_reference(**filters)
            actual, cursor = [], None
            started = time.monotonic()
            while True:
                page, cursor = service.list('p', cursor=cursor, limit=37, **filters)
                actual.extend(page)
                if not cursor: break
            self.assertEqual(expected, actual)
            self.assertEqual(len(actual), len({item['id'] for item in actual}))
            self.assertLess(time.monotonic() - started, 15)

        # Cursor payload predates this SQL implementation; a valid old-format
        # cursor must continue from the same public ID boundary.
        filters = {'scope_kind': 'dm'}
        expected = old_reference(**filters)
        tag = hashlib.sha256(json.dumps(['p', ['profile', 'recent', 'summary'], 'dm', None, None, None], separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()[:16]
        boundary = expected[36]
        encoded = json.dumps([tag, boundary['updated_at'], '', boundary['id']], separators=(',', ':'))
        raw = base64.urlsafe_b64encode(encoded.encode()).decode().rstrip('=')
        old_cursor = raw + '.' + hmac.new(service._secret, raw.encode('ascii'), hashlib.sha256).hexdigest()[:16]
        page, _ = service.list('p', cursor=old_cursor, limit=37, **filters)
        self.assertEqual(expected[37:74], page)


if __name__ == '__main__':
    unittest.main()
