# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""Storage, profile, and internal-owner API checks for PR3a."""
import os
import sqlite3
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from botville_gateway.memory.api import MemoryPermissionError, OwnerMemoryAPI
from botville_gateway.memory.profile import apply_extractions
from botville_gateway.memory.store import MemoryPurger, MemoryStore, SCHEMA_VERSION, _MIGRATIONS


def add_item(store, event_id, pinned=False):
    return store.add_item(persona_id="p", scope_kind="dm", scope_key="r",
        room_id="r", counterpart="@human:example.org", event_id=event_id,
        sender="@human:example.org", role="user", trust="trusted", text="safe", pinned=pinned)


class MemoryStoreTests(unittest.TestCase):
    def test_public_ids_do_not_reuse_after_delete_or_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'memory.db')
            first = MemoryStore(path, hmac_secret='test-owner-id-secret')
            add_item(first, '$first')
            old = first.items('p')[0]['public_id']
            first.delete_item('p', 1)
            first.close()
            second = MemoryStore(path, hmac_secret='test-owner-id-secret')
            add_item(second, '$second')
            self.assertNotEqual(old, second.items('p')[0]['public_id'])

    def test_deleted_extracted_fact_is_suppressed_until_owner_write(self):
        store = MemoryStore(':memory:', hmac_secret='test-owner-id-secret')
        store.upsert_fact('p', '@human:example.org', 'name', 'old', source='extraction',
                          source_event_ids=['$one'])
        self.assertTrue(store.delete_fact('p', 'name', '@human:example.org'))
        self.assertFalse(store.upsert_fact('p', '@human:example.org', 'name', 'new', source='extraction',
                                           source_event_ids=['$two']))
        self.assertTrue(store.upsert_fact('p', '@human:example.org', 'name', 'owner', source='owner'))
        self.assertFalse(store.upsert_fact('p', '@human:example.org', 'name', 'new', source='extraction',
                                           source_event_ids=['$three']))

    def test_private_reopen_and_future_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "memory.db")
            store = MemoryStore(path)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertTrue(add_item(store, "$one"))
            store.close()
            self.assertEqual(len(MemoryStore(path).items("p")), 1)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "future.db")
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE memory_schema(version INTEGER NOT NULL)")
            conn.execute("INSERT INTO memory_schema VALUES (999)")
            conn.commit(); conn.close()
            with self.assertRaises(RuntimeError): MemoryStore(path)

    def test_v1_migration_preserves_data_and_adds_visibility(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "old.db")
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE memory_schema(version INTEGER NOT NULL)")
            for statement in _MIGRATIONS[1]:
                conn.execute(statement)
            conn.execute("INSERT INTO memory_schema VALUES (1)")
            conn.execute("INSERT INTO memory_facts(persona_id,counterpart,key,value,visibility,source,source_event_ids,trust,pinned,created_at,updated_at) VALUES('p','u','name','saved','dm_only','owner','[]','trusted',0,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')")
            conn.commit(); conn.close()
            store = MemoryStore(path)
            self.assertEqual(store.facts("p", "u")[0]["value"], "saved")
            self.assertEqual(store.facts("p", "u")[0]["visibility"], "dm_only")
            self.assertEqual(store._db.execute("SELECT version FROM memory_schema").fetchone()[0], SCHEMA_VERSION)

    def test_retention_pinned_and_tombstone_expiry(self):
        store = MemoryStore(":memory:", retention_days=1)
        add_item(store, "$old"); add_item(store, "$pin", True)
        store.upsert_fact("p", "@human:example.org", "name", "old", source="extraction", source_event_ids=["$old"])
        store.upsert_fact("p", "@human:example.org", "notes", "kept", source="owner", pinned=True)
        store.redact_event("$gone")
        old = "2020-01-01T00:00:00Z"
        store._db.execute("UPDATE memory_items SET created_at=?", (old,))
        store._db.execute("UPDATE memory_facts SET updated_at=?", (old,))
        store._db.execute("UPDATE memory_tombstones SET created_at=?", (old,)); store._db.commit()
        store.purge(datetime.now(timezone.utc))
        self.assertEqual([x["event_id"] for x in store.items("p")], ["$pin"])
        self.assertEqual([x["key"] for x in store.facts("p")], ["notes"])
        self.assertFalse(store.is_tombstoned("$gone"))

    def test_redaction_sources_replay_and_precedence(self):
        store = MemoryStore(":memory:"); add_item(store, "$one")
        store.upsert_fact("p", "@human:example.org", "name", "one", source="extraction", source_event_ids=["$one"])
        store.upsert_fact("p", "@human:example.org", "notes", "two", source="extraction", source_event_ids=["$one", "$two"])
        store.redact_event("$one")
        facts = {x["key"]: x for x in store.facts("p")}
        self.assertNotIn("name", facts); self.assertEqual(facts["notes"]["source_event_ids"], '["$two"]')
        self.assertFalse(add_item(store, "$one"))
        store.upsert_fact("p", "@human:example.org", "name", "first", source="extraction", source_event_ids=["$first"])
        store.upsert_fact("p", "@human:example.org", "name", "later", source="extraction", source_event_ids=["$later"])
        store.upsert_fact("p", "@human:example.org", "name", "owner", source="owner")
        self.assertFalse(store.upsert_fact("p", "@human:example.org", "name", "model", source="extraction", source_event_ids=["$model"]))
        store.pin_fact("p", "name", "@human:example.org", True)
        self.assertFalse(store.upsert_fact("p", "@human:example.org", "name", "again", source="extraction", source_event_ids=["$again"]))


class ProfileAndOwnerTests(unittest.TestCase):
    def test_extraction_trust_and_owner_permissions(self):
        store = MemoryStore(":memory:")
        common = dict(persona_id="p", counterpart="@human:example.org", facts=[{"key":"name", "value":"Ada"}], event_id="$e", sender="@human:example.org", server_name="example.org")
        self.assertEqual(apply_extractions(store, trusted_local=False, role="user", **common), 0)
        self.assertEqual(apply_extractions(store, trusted_local=True, role="assistant", **common), 0)
        self.assertEqual(apply_extractions(store, trusted_local=True, role="user", sender="@human:foreign", server_name="example.org", persona_id="p", counterpart="@human:example.org", facts=common["facts"], event_id="$foreign"), 0)
        self.assertEqual(apply_extractions(store, trusted_local=True, role="user", **common), 1)
        store.redact_event("$dead")
        self.assertEqual(apply_extractions(store, trusted_local=True, role="user", **{**common,"event_id":"$dead"}), 0)
        add_item(store, "$item")
        api = OwnerMemoryAPI(store, {"p":"@human:example.org", "noowner":None})
        self.assertTrue(api.list_items("p", "@human:example.org"))
        for persona, caller in (("p", "@other:example.org"), ("noowner", "@human:example.org")):
            with self.assertRaises(MemoryPermissionError): api.list_facts(persona, caller)
        with self.assertRaises(MemoryPermissionError): api.set_fact("p", "@other:example.org", "name", "x", "@human:example.org")

    def test_purger_is_quiet_when_disabled_and_logs_only_exception_type(self):
        class BrokenStore:
            def __init__(self): self.calls = 0
            def purge(self):
                self.calls += 1
                raise RuntimeError("sensitive memory body must not be logged")
        broken = BrokenStore()
        disabled = MemoryPurger(broken, 0)
        disabled.start()
        self.assertEqual(broken.calls, 0)
        enabled = MemoryPurger(broken, 60)
        with self.assertLogs("botville_gateway.memory.store", "WARNING") as logs:
            enabled.run_purge_safely()
        self.assertIn("RuntimeError", logs.output[0])
        self.assertNotIn("sensitive memory body", logs.output[0])
