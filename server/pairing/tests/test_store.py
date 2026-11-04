"""Unit tests for pairing.store (SQLite, no network, no Docker)."""

import os
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
        # Remote notification after a local sever overwrites severed_by.
        self.assertTrue(self.db.sever_peer("hs-b", "remote"))
        self.assertEqual(self.db.get_peer("hs-b")["severed_by"], "remote")

    def test_sever_unknown_peer(self):
        self.assertFalse(self.db.sever_peer("hs-b", "local"))

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


if __name__ == "__main__":
    unittest.main()
