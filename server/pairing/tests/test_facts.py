"""Pairing facts for the gateway (A-R1): store feed + internal HTTP API.

Real HTTP on ephemeral ports, temp SQLite, no Docker. Tokens below are
test-only literals, never deployment values.
"""

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "..", ".."))  # server/ -> `pairing`
sys.path.insert(0, TESTS_DIR)

from pairing import service, store  # noqa: E402
import util as testutil  # noqa: E402

ADMIN = "test-admin-token"
SERVICE = "test-service-token-" + "s" * 24   # >= 32 chars
PEER_URL = "https://hs-b.example:8448/_matrix/pairing"


class FactsStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "pairing.db")
        self.db = store.PairingStore(self.path)
        self.addCleanup(self.db.close)

    def pair_by_redeem(self, name="hs-b", invite="inv", now=None):
        self.db.create_invite(invite, "sec", 600, now=now)
        ok, reason = self.db.redeem_invite(invite, "sec", name, PEER_URL, now=now)
        self.assertTrue(ok, reason)

    def test_peer_fact_states(self):
        self.assertEqual(self.db.peer_fact("hs-b"),
                         {"state": "unpaired", "changed_at": None})
        self.db.upsert_peer_active("hs-b", PEER_URL, now=100.0)
        self.assertEqual(self.db.peer_fact("HS-B"),
                         {"state": "active", "changed_at": 100.0})
        self.db.sever_peer("hs-b", "remote", now=200.0)
        self.assertEqual(self.db.peer_fact("hs-b"),
                         {"state": "severed", "changed_at": 200.0})

    def test_paired_event_on_both_sides_and_repair(self):
        self.pair_by_redeem("hs-b", "inv1")           # redeeming side
        self.db.upsert_peer_active("hs-c", PEER_URL)  # accepting side
        self.db.sever_peer("hs-b", "local")
        self.pair_by_redeem("hs-b", "inv2")           # re-pair (fresh invite)
        self.db.sever_peer("hs-c", "remote")
        self.db.upsert_peer_active("hs-c", PEER_URL)  # re-pair, accepting side
        events, _, has_more = self.db.fact_events(0, 100)
        self.assertFalse(has_more)
        self.assertEqual(
            [(e["kind"], e["server_name"], e.get("repaired"), e.get("severed_by"))
             for e in events],
            [("peer_paired", "hs-b", False, None),
             ("peer_paired", "hs-c", False, None),
             ("peer_severed", "hs-b", None, "local"),
             ("peer_paired", "hs-b", True, None),
             ("peer_severed", "hs-c", None, "remote"),
             ("peer_paired", "hs-c", True, None)])
        cursors = [e["cursor"] for e in events]
        self.assertEqual(cursors, sorted(set(cursors)))  # strictly increasing

    def test_non_fact_events_are_skipped_but_cursor_advances(self):
        self.db.upsert_peer_active("hs-b", PEER_URL)
        events, cursor, _ = self.db.fact_events(0, 100)
        self.assertEqual(len(events), 1)
        # Only audit noise afterwards: no facts, but the cursor moves past it.
        self.db.create_invite("noise", "sec", 600)
        self.db.record_event("redeem_rejected", reason="x")
        events, cursor2, has_more = self.db.fact_events(cursor, 100)
        self.assertEqual(events, [])
        self.assertFalse(has_more)
        self.assertGreater(cursor2, cursor)
        self.assertEqual(self.db.fact_events(cursor2, 100), ([], cursor2, False))

    def test_pagination_is_gap_free_and_stable(self):
        for i in range(7):
            self.db.upsert_peer_active("hs-{}".format(i), PEER_URL)
            self.db.record_event("invite_created", invite_id=str(i))
        seen, cursor = [], 0
        while True:
            events, cursor, has_more = self.db.fact_events(cursor, 3)
            seen += [e["server_name"] for e in events]
            if not has_more:
                break
        self.assertEqual(seen, ["hs-{}".format(i) for i in range(7)])
        # Replaying an old cursor returns the same page (stable).
        first = self.db.fact_events(0, 3)
        self.assertEqual(first, self.db.fact_events(0, 3))

    def test_cursor_expired_after_prune(self):
        now = time.time()
        old = now - 40 * 86400
        self.db.upsert_peer_active("hs-b", PEER_URL, now=old)
        self.db.sever_peer("hs-b", "local", now=old + 1)
        self.db.upsert_peer_active("hs-c", PEER_URL, now=now)
        events, _, _ = self.db.fact_events(0, 100)
        self.assertEqual(len(events), 3)
        counts = self.db.prune(now=now)
        self.assertEqual(counts["events"], 2)
        with self.assertRaises(store.CursorExpired) as ctx:
            self.db.fact_events(0, 100)
        self.assertEqual(ctx.exception.reason, "cursor_pruned")
        resync = ctx.exception.resync_cursor
        self.assertEqual(resync, events[-1]["cursor"])
        # A cursor at the watermark (it saw everything pruned) still works.
        self.assertEqual(self.db.fact_events(events[1]["cursor"], 100)[0],
                         events[2:])
        self.assertEqual(self.db.fact_events(resync, 100), ([], resync, False))

    def test_cursor_ahead_of_log_is_expired(self):
        self.db.upsert_peer_active("hs-b", PEER_URL)
        _, cursor, _ = self.db.fact_events(0, 100)
        with self.assertRaises(store.CursorExpired) as ctx:
            self.db.fact_events(cursor + 5, 100)
        self.assertEqual(ctx.exception.reason, "cursor_ahead_of_log")
        self.assertEqual(ctx.exception.resync_cursor, cursor)

    def test_ids_survive_pruning_everything(self):
        now = time.time()
        self.db.upsert_peer_active("hs-b", PEER_URL, now=now - 40 * 86400)
        _, cursor, _ = self.db.fact_events(0, 100)
        self.db.prune(now=now)
        self.assertEqual(self.db.fact_events(cursor, 100), ([], cursor, False))
        self.db.upsert_peer_active("hs-c", PEER_URL)
        events, _, _ = self.db.fact_events(cursor, 100)
        self.assertGreater(events[0]["cursor"], cursor)  # AUTOINCREMENT, no reuse

    def test_severed_row_and_event_commit_together(self):
        self.db.upsert_peer_active("hs-b", PEER_URL)
        self.assertTrue(self.db.sever_peer("hs-b", "local"))
        reader = sqlite3.connect(self.path)
        self.addCleanup(reader.close)
        state = reader.execute(
            "SELECT state FROM peers WHERE server_name='hs-b'").fetchone()[0]
        kinds = [r[0] for r in reader.execute(
            "SELECT kind FROM events WHERE kind='peer_severed'")]
        self.assertEqual((state, kinds), ("severed", ["peer_severed"]))
        # A remote notice on an already-severed row changes nothing.
        self.assertFalse(self.db.sever_peer("hs-b", "remote", only_if_active=True))
        self.assertEqual(len(self.db.fact_events(0, 100)[0]), 2)

    def test_feed_never_carries_secrets_or_urls(self):
        self.db.upsert_peer_active("hs-b", PEER_URL, shared_secret="ab" * 32)
        self.db.sever_peer("hs-b", "local")
        raw = json.dumps(self.db.fact_events(0, 100))
        self.assertNotIn("ab" * 32, raw)
        self.assertNotIn(PEER_URL, raw)


class FactsApiTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = os.path.join(self._tmp.name, "pairing.db")

    def start(self, service_token=SERVICE):
        config = service.PairingConfig(
            db_path=self.db_path, admin_token=ADMIN, server_name="hs-a",
            api_url="https://hs-a.example:8448/_matrix/pairing",
            verify_peer_key=False, host="127.0.0.1", port=0,
            service_token=service_token)
        httpd = service.build_server(config)
        self.addCleanup(testutil.shutdown, httpd)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return testutil.base_url(httpd), httpd

    def get(self, base, path, token=SERVICE):
        return testutil.http_json("GET", base + path, token=token)

    # --- auth ---------------------------------------------------------------

    def test_auth_rejections_are_uniform(self):
        base, httpd = self.start()
        httpd.app.store.upsert_peer_active("hs-b", PEER_URL)
        for path in ("/v1/pairing/internal/events",
                     "/v1/pairing/internal/peers/hs-b",
                     "/v1/pairing/internal/nope"):
            for token in (None, "wrong-" + SERVICE, ADMIN, SERVICE + "x",
                          SERVICE[:-1]):
                with self.subTest(path=path, token=token and token[:6]):
                    status, resp = self.get(base, path, token=token)
                    self.assertEqual(status, 401)
                    self.assertEqual(resp["errcode"], "M_UNKNOWN_TOKEN")
                    self.assertNotIn("hs-b", json.dumps(resp))
        # Lower-case scheme / raw token without "Bearer " also refused.
        for header in ("bearer " + SERVICE, SERVICE, "Basic " + SERVICE):
            status, _, _, _ = testutil.raw_request(
                httpd.server_address[1], "GET", "/v1/pairing/internal/events",
                [("Authorization", header)])
            self.assertEqual(status, 401, header[:8])

    def test_unconfigured_token_refuses_everything(self):
        base, _ = self.start(service_token=None)
        for token in (None, "", SERVICE, ADMIN):
            status, resp = self.get(base, "/v1/pairing/internal/events",
                                    token=token)
            self.assertEqual(status, 401)
            self.assertEqual(resp["errcode"], "M_UNKNOWN_TOKEN")

    def test_service_token_never_opens_admin_endpoints(self):
        base, _ = self.start()
        for method, path in (("GET", "/v1/pairing/peers"),
                             ("GET", "/v1/pairing/allowed"),
                             ("POST", "/v1/pairing/invites"),
                             ("DELETE", "/v1/pairing/peers/hs-b")):
            status, _ = testutil.http_json(method, base + path, token=SERVICE,
                                           body={} if method == "POST" else None)
            self.assertEqual(status, 401, path)

    def test_internal_not_reachable_through_matrix_alias(self):
        base, _ = self.start()
        for path in ("/_matrix/pairing/internal/events",
                     "/_matrix/pairing/internal/peers/hs-b"):
            status, _ = self.get(base, path)
            self.assertEqual(status, 404, path)

    def test_config_rules_for_service_token(self):
        env = {"PAIRING_ADMIN_TOKEN": ADMIN, "PAIRING_SERVER_NAME": "hs-a",
               "PAIRING_API_URL": "https://hs-a.example/pairing"}
        self.assertIsNone(service.PairingConfig.from_env(env).service_token)
        env["PAIRING_SERVICE_TOKEN"] = ""
        self.assertIsNone(service.PairingConfig.from_env(env).service_token)
        env["PAIRING_SERVICE_TOKEN"] = "short"
        with self.assertRaises(service.ConfigError) as ctx:
            service.PairingConfig.from_env(env)
        self.assertNotIn("short", str(ctx.exception))
        env["PAIRING_ADMIN_TOKEN"] = SERVICE
        env["PAIRING_SERVICE_TOKEN"] = SERVICE
        with self.assertRaises(service.ConfigError) as ctx:
            service.PairingConfig.from_env(env)
        self.assertNotIn(SERVICE, str(ctx.exception))
        env["PAIRING_ADMIN_TOKEN"] = ADMIN
        self.assertEqual(service.PairingConfig.from_env(env).service_token,
                         SERVICE)

    # --- peers ----------------------------------------------------------------

    def test_peer_state_changes(self):
        base, httpd = self.start()
        status, resp = self.get(base, "/v1/pairing/internal/peers/hs-b")
        self.assertEqual((status, resp), (200, {"state": "unpaired",
                                                "changed_at": None}))
        httpd.app.store.upsert_peer_active("hs-b", PEER_URL)
        status, resp = self.get(base, "/v1/pairing/internal/peers/HS-B")
        self.assertEqual((status, resp["state"]), (200, "active"))
        self.assertTrue(resp["changed_at"].endswith("+00:00"))
        self.assertEqual(set(resp), {"state", "changed_at"})
        status, admin = testutil.http_json(
            "DELETE", base + "/v1/pairing/peers/hs-b", token=ADMIN)
        self.assertEqual(status, 200, admin)
        status, resp = self.get(base, "/v1/pairing/internal/peers/hs-b")
        self.assertEqual(resp["state"], "severed")
        for bad in ("bad_name!", "hs-b:8448", "127.0.0.1", "localhost"):
            status, resp = self.get(base, "/v1/pairing/internal/peers/" + bad)
            self.assertEqual(status, 400, bad)
            self.assertNotIn(bad, json.dumps(resp))

    def test_wrong_method_is_405_after_auth(self):
        base, _ = self.start()
        status, _ = testutil.http_json("POST", base + "/v1/pairing/internal/events",
                                       body={}, token=SERVICE)
        self.assertEqual(status, 405)
        status, _ = testutil.http_json("POST", base + "/v1/pairing/internal/events",
                                       body={}, token=ADMIN)
        self.assertEqual(status, 401)

    # --- events ---------------------------------------------------------------

    def test_events_feed_over_http_with_repair(self):
        base, httpd = self.start()
        st = httpd.app.store
        st.create_invite("inv1", "sec", 600)
        status, _ = testutil.http_json(
            "POST", base + "/v1/pairing/redeem",
            body={"invite_id": "inv1", "secret": "sec", "server_name": "hs-b",
                  "api_url": PEER_URL})
        self.assertEqual(status, 200)
        st.sever_peer("hs-b", "local")
        st.create_invite("inv2", "sec", 600)
        status, _ = testutil.http_json(
            "POST", base + "/v1/pairing/redeem",
            body={"invite_id": "inv2", "secret": "sec", "server_name": "hs-b",
                  "api_url": PEER_URL})
        self.assertEqual(status, 200)
        status, resp = self.get(base, "/v1/pairing/internal/events?limit=2")
        self.assertEqual(status, 200)
        self.assertTrue(resp["has_more"])
        self.assertEqual([e["kind"] for e in resp["events"]],
                         ["peer_paired", "peer_severed"])
        self.assertEqual(resp["events"][0]["repaired"], False)
        self.assertEqual(resp["events"][1]["severed_by"], "local")
        status, resp2 = self.get(
            base, "/v1/pairing/internal/events?since={}&limit=2".format(
                resp["next_cursor"]))
        self.assertEqual([(e["kind"], e["repaired"]) for e in resp2["events"]],
                         [("peer_paired", True)])
        self.assertFalse(resp2["has_more"])
        for event in resp["events"] + resp2["events"]:
            self.assertTrue(event["at"].endswith("+00:00"))
            self.assertNotIn("shared_secret", event)
            self.assertNotIn("peer_api_url", event)

    def test_events_bad_query_is_400(self):
        base, _ = self.start()
        for query in ("since=-1", "since=abc", "since=1&since=2", "limit=0",
                      "limit=501", "foo=1", "since", "since=" + "9" * 19,
                      "since=%31", "since=%D9%A1", "limit=1&limit=2"):
            with self.subTest(query=query):
                status, resp = self.get(
                    base, "/v1/pairing/internal/events?" + query)
                self.assertEqual(status, 400)

    def test_events_cursor_expired_is_410(self):
        base, httpd = self.start()
        st = httpd.app.store
        now = time.time()
        st.upsert_peer_active("hs-b", PEER_URL, now=now - 40 * 86400)
        st.upsert_peer_active("hs-c", PEER_URL, now=now)
        st.prune(now=now)
        status, resp = self.get(base, "/v1/pairing/internal/events?since=0")
        self.assertEqual(status, 410)
        self.assertEqual(resp["errcode"], "BV_CURSOR_EXPIRED")
        self.assertEqual(resp["reason"], "cursor_pruned")
        status, resp2 = self.get(
            base, "/v1/pairing/internal/events?since={}".format(
                resp["resync_cursor"]))
        self.assertEqual((status, resp2["events"]), (200, []))
        status, resp3 = self.get(
            base, "/v1/pairing/internal/events?since={}".format(
                resp["resync_cursor"] + 100))
        self.assertEqual((status, resp3["reason"]), (410, "cursor_ahead_of_log"))


if __name__ == "__main__":
    unittest.main()
