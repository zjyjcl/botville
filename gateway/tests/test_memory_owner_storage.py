# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""Persistent owner-management invariants across deletion and retention."""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'src'))
from botville_gateway.memory.owner import OwnerMemoryService
from botville_gateway.memory.store import MemoryStore


class OwnerStorageTests(unittest.TestCase):
    def test_audit_retention_and_private_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'memory.db')
            store = MemoryStore(path, retention_days=1)
            self.addCleanup(store.close)
            service = OwnerMemoryService(store, b'fake-owner-audit-key')
            service.audit(caller='@owner:test', request_id='req_test', action='list',
                          persona_id='@ai_test:test', memory_id=None, layer=None,
                          counterpart=None, result_code='200')
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(store._db.execute('SELECT counterpart_hmac FROM memory_audit').fetchone()[0], None)
            store.purge(datetime.now(timezone.utc) + timedelta(days=2))
            self.assertEqual(store._db.execute('SELECT count(*) FROM memory_audit').fetchone()[0], 0)

    def test_summary_delete_resets_coverage_without_forgetting_sources(self):
        store = MemoryStore(':memory:')
        self.addCleanup(store.close)
        service = OwnerMemoryService(store, b'fake-owner-summary-key')
        store.add_item(persona_id='p', scope_kind='dm', scope_key='@human:test',
                       room_id=None, counterpart='@human:test', event_id='$fake',
                       sender='@human:test', role='user', trust='trusted', text='fake text')
        raw = store.items('p')[0]
        args = dict(persona_id='p', scope_kind='dm', scope_key='@human:test',
                    text='fake summary', trust='derived', covers_until_item_id=raw['id'], model='fake')
        self.assertTrue(store.save_summary(**args))
        old = store.summary('p', 'dm', '@human:test')['public_id']
        service.delete('p', old)
        self.assertEqual(len(store.items('p')), 1)
        self.assertTrue(store.summary_scopes())
        self.assertTrue(store.save_summary(**args))
        self.assertNotEqual(store.summary('p', 'dm', '@human:test')['public_id'], old)
        self.assertIsNone(service.get(old))

    def test_deleted_fact_id_never_aliases_recreated_fact(self):
        store = MemoryStore(':memory:')
        self.addCleanup(store.close)
        service = OwnerMemoryService(store, b'fake-owner-fact-key')
        first, created = service.set_fact('p', '@human:test', 'likes', 'fake value')
        self.assertTrue(created)
        service.delete('p', first['id'])
        second, created = service.set_fact('p', '@human:test', 'likes', 'another fake value')
        self.assertTrue(created)
        self.assertNotEqual(first['id'], second['id'])
        self.assertIsNone(service.get(first['id']))


if __name__ == '__main__':
    unittest.main()
