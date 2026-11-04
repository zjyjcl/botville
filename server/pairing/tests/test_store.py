"""Unit tests for pairing.store (SQLite, no network, no Docker)."""

import os
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from pairing import store  # noqa: E402


class StoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = store.PairingStore(os.path.join(self._tmp.name, "pairing.db"))
        self.addCleanup(self.db.close)

    def test_create_and_redeem_roundtrip(self):
        expires = self.db.create_invite("inv1", "sec1", 600)
        self.assertGreater(expires, time.time())
        ok, reason = self.db.redeem_invite("inv1", "sec1", "hs-b",
                                           "https://hs-b:8448/_matrix/pairing")
        self.assertEqual((ok, reason), (True, None))
        peer = self.db.get_peer("hs-b")
        self.assertEqual(peer["state"], "active")
        self.assertEqual(peer["peer_api_url"],
                         "https://hs-b:8448/_matrix/pairing")
        self.assertIsNone(peer["severed_at"])
        self.assertIsNone(peer["severed_by"])
        self.assertEqual(self.db.active_server_names(), ["hs-b"])

    def test_redeem_unknown_invite(self):
        ok, reason = self.db.redeem_invite("nope", "sec", "hs-b", "https://x")
        self.assertEqual((ok, reason), (False, store.REDEEM_UNKNOWN))

    def test_redeem_wrong_secret_leaves_invite_usable(self):
        self.db.create_invite("inv1", "sec1", 600)
        ok, reason = self.db.redeem_invite("inv1", "wrong", "hs-b", "https://x")
        self.assertEqual((ok, reason), (False, store.REDEEM_BAD_SECRET))
        # A wrong attempt must not consume the invite.
        ok, _ = self.db.redeem_invite("inv1", "sec1", "hs-b", "https://x")
        self.assertTrue(ok)

    def test_redeem_expired(self):
        self.db.create_invite("inv1", "sec1", 600)
        later = time.time() + 601
        ok, reason = self.db.redeem_invite("inv1", "sec1", "hs-b", "https://x",
                                           now=later)
        self.assertEqual((ok, reason), (False, store.REDEEM_EXPIRED))

    def test_redeem_single_use_replay_rejected(self):
        self.db.create_invite("inv1", "sec1", 600)
        ok, _ = self.db.redeem_invite("inv1", "sec1", "hs-b", "https://x")
        self.assertTrue(ok)
        ok, reason = self.db.redeem_invite("inv1", "sec1", "hs-b", "https://x")
        self.assertEqual((ok, reason), (False, store.REDEEM_USED))

    def test_sever_local_then_remote(self):
        self.db.create_invite("inv1", "sec1", 600)
        self.db.redeem_invite("inv1", "sec1", "hs-b", "https://hs-b/x")
        self.assertTrue(self.db.sever_peer("hs-b", "local"))
        peer = self.db.get_peer("hs-b")
        self.assertEqual(peer["state"], "severed")
        self.assertEqual(peer["severed_by"], "local")
        self.assertIsNotNone(peer["severed_at"])
        self.assertEqual(self.db.active_server_names(), [])
        # A remote notify must NOT overwrite an explicit local sever
        # (review B2): only_if_active leaves the row untouched.
        self.assertFalse(
            self.db.sever_peer("hs-b", "remote", only_if_active=True))
        peer = self.db.get_peer("hs-b")
        self.assertEqual(peer["state"], "severed")
        self.assertEqual(peer["severed_by"], "local")

    def test_sever_remote_from_active(self):
        self.db.upsert_peer_active("hs-b", "https://hs-b/x")
        self.assertTrue(
            self.db.sever_peer("hs-b", "remote", only_if_active=True))
        peer = self.db.get_peer("hs-b")
        self.assertEqual(peer["state"], "severed")
        self.assertEqual(peer["severed_by"], "remote")

    def test_sever_unknown_peer(self):
        self.assertFalse(self.db.sever_peer("hs-b", "local"))
        self.assertFalse(
            self.db.sever_peer("hs-b", "remote", only_if_active=True))

    def test_repair_resets_severed_state(self):
        self.db.create_invite("inv1", "sec1", 600)
        self.db.redeem_invite("inv1", "sec1", "hs-b", "https://old")
        self.db.sever_peer("hs-b", "local")
        # A fresh invite re-pairs the same server: severed_* must clear.
        self.db.create_invite("inv2", "sec2", 600)
        ok, _ = self.db.redeem_invite("inv2", "sec2", "hs-b", "https://new")
        self.assertTrue(ok)
        peer = self.db.get_peer("hs-b")
        self.assertEqual(peer["state"], "active")
        self.assertEqual(peer["peer_api_url"], "https://new")
        self.assertIsNone(peer["severed_at"])
        self.assertEqual(self.db.active_server_names(), ["hs-b"])

    def test_upsert_peer_active_without_local_invite(self):
        # The accepting side of a pairing has no invite row of its own.
        self.db.upsert_peer_active("hs-a", "https://hs-a:8448/_matrix/pairing")
        peer = self.db.get_peer("hs-a")
        self.assertEqual(peer["state"], "active")
        # Re-pairing through upsert also clears a previous sever.
        self.db.sever_peer("hs-a", "remote")
        self.db.upsert_peer_active("hs-a", "https://hs-a:8448/_matrix/pairing")
        self.assertEqual(self.db.get_peer("hs-a")["state"], "active")

    def test_events_recorded(self):
        self.db.create_invite("inv1", "sec1", 600)
        self.db.redeem_invite("inv1", "sec1", "hs-b", "https://x")
        self.db.redeem_invite("inv1", "sec1", "hs-b", "https://x")  # replay
        self.db.sever_peer("hs-b", "local")
        rows = self.db._conn.execute(
            "SELECT kind FROM events ORDER BY id").fetchall()
        kinds = [row["kind"] for row in rows]
        self.assertIn("invite_created", kinds)
        self.assertIn("invite_redeemed", kinds)
        self.assertIn("redeem_rejected", kinds)
        self.assertIn("peer_severed", kinds)

    def test_secret_never_stored_in_plaintext(self):
        self.db.create_invite("inv1", "topsecret-value", 600)
        row = self.db._conn.execute(
            "SELECT secret_hash FROM invites WHERE id = 'inv1'").fetchone()
        self.assertEqual(row["secret_hash"],
                         store.hash_secret("topsecret-value"))
        self.assertNotIn("topsecret-value", row["secret_hash"])

    # --- check_invite (read-only pre-check, review B4) ----------------------

    def test_check_invite_mirrors_redeem_rules(self):
        self.assertFalse(self.db.check_invite("nope", "sec")[0])
        self.db.create_invite("inv1", "sec1", 600)
        ok, reason = self.db.check_invite("inv1", "wrong")
        self.assertEqual((ok, reason), (False, store.REDEEM_BAD_SECRET))
        ok, reason = self.db.check_invite(
            "inv1", "sec1", now=time.time() + 601)
        self.assertEqual((ok, reason), (False, store.REDEEM_EXPIRED))
        self.assertEqual(self.db.check_invite("inv1", "sec1"), (True, None))
        # Read-only: the invite is still consumable afterwards.
        self.assertTrue(self.db.redeem_invite(
            "inv1", "sec1", "hs-b", "https://x")[0])
        ok, reason = self.db.check_invite("inv1", "sec1")
        self.assertEqual((ok, reason), (False, store.REDEEM_USED))

    # --- shared secrets + server_name canonicalisation (review B2) ----------

    def test_shared_secret_stored_on_both_sides(self):
        self.db.create_invite("inv1", "sec1", 600)
        self.assertTrue(self.db.redeem_invite(
            "inv1", "sec1", "hs-b", "https://x", shared_secret="ab" * 32))
        self.assertEqual(self.db.get_peer("hs-b")["shared_secret"], "ab" * 32)
        # Accepting side (no local invite) stores it via the upsert.
        self.db.upsert_peer_active("hs-a", "https://y",
                                   shared_secret="cd" * 32)
        self.assertEqual(self.db.get_peer("hs-a")["shared_secret"], "cd" * 32)
        # A legacy re-pair without a secret clears it (peer is refused
        # for notify until re-paired with a current server).
        self.db.upsert_peer_active("hs-a", "https://y")
        self.assertIsNone(self.db.get_peer("hs-a")["shared_secret"])

    def test_server_names_lowercased_on_write_and_lookup(self):
        self.db.create_invite("inv1", "sec1", 600)
        self.assertTrue(self.db.redeem_invite(
            "inv1", "sec1", "HS-B", "https://x"))
        self.assertIsNotNone(self.db.get_peer("hs-b"))
        self.assertIsNotNone(self.db.get_peer("HS-B"))
        self.assertTrue(self.db.sever_peer("HS-B", "local"))
        self.assertEqual(self.db.get_peer("hs-b")["state"], "severed")

    def test_migration_adds_shared_secret_and_lowercases(self):
        # A pre-B2 database in a *fresh* file (setUp's store already
        # created the current schema in pairing.db): peers table without
        # shared_secret, with a mixed-case row.
        legacy_path = os.path.join(self._tmp.name, "legacy.db")
        legacy = sqlite3.connect(legacy_path)
        try:
            legacy.executescript("""
                CREATE TABLE peers (
                    server_name TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    paired_at REAL NOT NULL,
                    severed_at REAL,
                    severed_by TEXT,
                    peer_api_url TEXT NOT NULL
                );
                INSERT INTO peers VALUES ('HS-B', 'active', 1, NULL, NULL,
                                          'https://hs-b/x');
            """)
            legacy.commit()
        finally:
            legacy.close()
        # Opening the legacy file applies the migration.
        migrated = store.PairingStore(legacy_path)
        self.addCleanup(migrated.close)
        columns = {row[1] for row in
                   migrated._conn.execute("PRAGMA table_info(peers)")}
        self.assertIn("shared_secret", columns)
        peer = migrated.get_peer("hs-b")
        self.assertIsNotNone(peer)
        self.assertEqual(peer["server_name"], "hs-b")
        self.assertIsNone(peer["shared_secret"])

    # --- sever invalidates invites + retention ------------------------------

    def test_invalidate_unused_invites(self):
        self.db.create_invite("used", "sec1", 600)
        self.db.create_invite("fresh", "sec2", 600)
        self.db.redeem_invite("used", "sec1", "hs-b", "https://x")
        count = self.db.invalidate_unused_invites()
        self.assertEqual(count, 1)  # only the unused one
        ok, reason = self.db.redeem_invite("fresh", "sec2", "hs-b", "https://x")
        self.assertEqual((ok, reason), (False, store.REDEEM_USED))

    def test_prune_keeps_live_rows(self):
        now = time.time()
        old = now - 40 * 86400  # == prune cutoff for older_than_days=30
        self.db.create_invite("gone", "s", 600, now=old - 700)   # long expired
        self.db.create_invite("used-old", "s", 600, now=old - 700)
        self.db.redeem_invite("used-old", "s", "hs-b", "https://x",
                              now=old - 600)                      # used long ago
        self.db.create_invite("live", "s", 600)  # still redeemable
        self.db.record_event("ancient", now=old - 1)
        self.db.record_event("recent")
        counts = self.db.prune(now=now)
        self.assertEqual(counts["invites"], 2)  # "gone" + "used-old"
        self.assertGreaterEqual(counts["events"], 1)  # "ancient" + old audit rows
        kinds = [row["kind"] for row in self.db._conn.execute(
            "SELECT kind FROM events ORDER BY id")]
        self.assertNotIn("ancient", kinds)
        self.assertIn("recent", kinds)
        self.assertIsNotNone(self.db.get_peer("hs-b"))  # peers are never pruned
        self.assertEqual(self.db.check_invite("live", "s"), (True, None))


if __name__ == "__main__":
    unittest.main()
