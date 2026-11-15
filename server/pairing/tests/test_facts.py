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
from unittest import mock

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
        # #243 review 1: resync at the WATERMARK (last pruned id), not at
        # the newest id -- the retained hs-c fact must not be skipped.
        self.assertEqual(resync, events[1]["cursor"])
        self.assertEqual(self.db.fact_events(resync, 100)[0], events[2:])

    def test_resync_after_prune_returns_every_retained_event(self):
        now = time.time()
        old = now - 40 * 86400
        for i in range(3):
            self.db.upsert_peer_active("old{}".format(i), PEER_URL, now=old + i)
        for i in range(7):
            self.db.upsert_peer_active("new{}".format(i), PEER_URL, now=now + i)
        everything, _, _ = self.db.fact_events(0, 100)
        self.db.prune(now=now)
        with self.assertRaises(store.CursorExpired) as ctx:
            self.db.fact_events(everything[0]["cursor"], 2)
        cursor = ctx.exception.resync_cursor
        self.assertEqual(cursor, everything[2]["cursor"])
        seen = []
        while True:  # page with a small limit, as a gateway would
            events, cursor, has_more = self.db.fact_events(cursor, 2)
            seen.extend(events)
            if not has_more:
                break
        self.assertEqual([e["server_name"] for e in seen],
                         ["new{}".format(i) for i in range(7)])
        self.assertEqual(seen, everything[3:])

    def test_cursor_ahead_of_log_is_expired(self):
        self.db.upsert_peer_active("hs-b", PEER_URL)
        _, cursor, _ = self.db.fact_events(0, 100)
        with self.assertRaises(store.CursorExpired) as ctx:
            self.db.fact_events(cursor + 5, 100)
        self.assertEqual(ctx.exception.reason, "cursor_ahead_of_log")
        # Nothing pruned yet: watermark 0, so the resync re-reads the
        # whole (possibly rebuilt) log instead of skipping it.
        self.assertEqual(ctx.exception.resync_cursor, 0)
        self.assertEqual(len(self.db.fact_events(0, 100)[0]), 1)

    def test_cursor_ahead_after_prune_resyncs_at_watermark(self):
        now = time.time()
        self.db.upsert_peer_active("hs-old", PEER_URL, now=now - 40 * 86400)
        self.db.upsert_peer_active("hs-b", PEER_URL, now=now)
        self.db.prune(now=now)
        events, latest, _ = self.db.fact_events(1, 100)
        with self.assertRaises(store.CursorExpired) as ctx:
            self.db.fact_events(latest + 1, 100)
        self.assertEqual(ctx.exception.resync_cursor, 1)
        self.assertEqual(self.db.fact_events(1, 100)[0], events)

    def test_limit_equal_to_remaining(self):
        for name in ("hs-b", "hs-c", "hs-d"):
            self.db.upsert_peer_active(name, PEER_URL)
        self.db.record_event("invite_created", invite_id="x")  # non-fact tail
        latest = self.db._latest_event_id()
        events, next_cursor, has_more = self.db.fact_events(0, 3)
        self.assertEqual(len(events), 3)
        self.assertFalse(has_more)
        self.assertEqual(next_cursor, latest)  # jumps past the non-fact tail
        self.assertEqual(self.db.fact_events(next_cursor, 3),
                         ([], latest, False))
        # One fewer than remaining: has_more and the cursor stops on a fact.
        events, next_cursor, has_more = self.db.fact_events(0, 2)
        self.assertTrue(has_more)
        self.assertEqual(next_cursor, events[-1]["cursor"])

    def test_repaired_when_upserting_an_active_peer_again(self):
        self.db.upsert_peer_active("hs-b", PEER_URL)
        self.db.upsert_peer_active("hs-b", PEER_URL)  # still active
        events, _, _ = self.db.fact_events(0, 100)
        self.assertEqual([(e["kind"], e["repaired"]) for e in events],
                         [("peer_paired", False), ("peer_paired", True)])

    # --- same-transaction guarantees (#243 review) -------------------------

    def fail_on_event(self, kind):
        """SQLite trigger that aborts any INSERT of an event of `kind`."""
        self.db._conn.execute(
            "CREATE TRIGGER fail_{0} BEFORE INSERT ON events"
            " WHEN NEW.kind = '{0}'"
            " BEGIN SELECT RAISE(ABORT, 'injected failure'); END".format(kind))
        self.db._conn.commit()

    def committed_peer_state(self, name):
        reader = sqlite3.connect(self.path)
        try:
            row = reader.execute("SELECT state FROM peers WHERE server_name=?",
                                 (name,)).fetchone()
        finally:
            reader.close()
        return row[0] if row else None

    def test_upsert_active_and_peer_paired_commit_together(self):
        self.fail_on_event("peer_paired")
        with self.assertRaises(sqlite3.Error):
            self.db.upsert_peer_active("hs-b", PEER_URL)
        # An unrelated later commit must not publish the half-done change.
        self.db.record_event("invite_created", invite_id="x")
        self.assertIsNone(self.committed_peer_state("hs-b"))
        self.assertIsNone(self.db.get_peer("hs-b"))

    def test_upsert_over_severed_rolls_back_to_severed(self):
        self.db.upsert_peer_active("hs-b", PEER_URL)
        self.db.sever_peer("hs-b", "local")
        self.fail_on_event("peer_paired")
        with self.assertRaises(sqlite3.Error):
            self.db.upsert_peer_active("hs-b", PEER_URL)
        self.db.record_event("invite_created", invite_id="x")
        self.assertEqual(self.committed_peer_state("hs-b"), "severed")

    def test_redeem_active_and_peer_paired_commit_together(self):
        self.db.create_invite("inv", "sec", 600)
        self.fail_on_event("peer_paired")
        with self.assertRaises(sqlite3.Error):
            self.db.redeem_invite("inv", "sec", "hs-b", PEER_URL)
        self.db.record_event("invite_created", invite_id="y")
        self.assertIsNone(self.committed_peer_state("hs-b"))
        # The invite was not consumed either (whole transaction rolled back).
        self.db._conn.execute("DROP TRIGGER fail_peer_paired")
        self.db._conn.commit()
        ok, reason = self.db.redeem_invite("inv", "sec", "hs-b", PEER_URL)
        self.assertTrue(ok, reason)

    def test_sever_and_peer_severed_commit_together(self):
        self.db.upsert_peer_active("hs-b", PEER_URL)
        self.fail_on_event("peer_severed")
        with self.assertRaises(sqlite3.Error):
            self.db.sever_peer("hs-b", "local")
        self.db.record_event("invite_created", invite_id="x")
        self.assertEqual(self.committed_peer_state("hs-b"), "active")

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
        base, httpd = self.start(service_token=None)
        httpd.app.store.upsert_peer_active("hs-b", PEER_URL)
        for path in ("/v1/pairing/internal/events",
                     "/v1/pairing/internal/peers/hs-b",
                     "/v1/pairing/internal/peers/hs-unknown"):
            for token in (None, "", SERVICE, ADMIN):
                with self.subTest(path=path, token=token and token[:6]):
                    status, resp = self.get(base, path, token=token)
                    self.assertEqual(status, 401)
                    self.assertEqual(resp["errcode"], "M_UNKNOWN_TOKEN")
                    self.assertNotIn("active", json.dumps(resp))

    def test_bare_internal_root_needs_auth_then_404(self):
        base, _ = self.start()
        for path in ("/v1/pairing/internal", "/v1/pairing/internal/"):
            with self.subTest(path=path):
                self.assertEqual(self.get(base, path, token=None)[0], 401)
                self.assertEqual(self.get(base, path)[0], 404)

    def test_internal_rejections_are_counted_and_log_throttled(self):
        base, httpd = self.start()
        app = httpd.app
        with self.assertLogs("pairing.service", "WARNING") as logs:
            for token in (None, ADMIN, "wrong-" + SERVICE, None, ADMIN):
                self.assertEqual(
                    self.get(base, "/v1/pairing/internal/events",
                             token=token)[0], 401)
        self.assertEqual(app.internal_rejected, 5)
        refused = [r for r in logs.output if "internal facts request refused" in r]
        self.assertEqual(len(refused), 1, logs.output)  # throttled
        self.assertIn("1 refused in total; 0 similar warnings suppressed", refused[0])
        # Next interval: one more warning that reports the swallowed ones.
        with mock.patch.object(service.time, "monotonic",
                               return_value=time.monotonic()
                               + service.INTERNAL_REJECT_LOG_INTERVAL + 1):
            with self.assertLogs("pairing.service", "WARNING") as logs:
                self.get(base, "/v1/pairing/internal/events", token=None)
        self.assertEqual(app.internal_rejected, 6)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("6 refused in total; 4 similar warnings suppressed",
                      logs.output[0])
        for line in logs.output:
            self.assertNotIn(SERVICE, line)
            self.assertNotIn(ADMIN, line)
        # A valid request is not counted.
        self.assertEqual(self.get(base, "/v1/pairing/internal/events")[0], 200)
        self.assertEqual(app.internal_rejected, 6)

    def test_log_throttle_unit(self):
        throttle = service.LogThrottle(10)
        self.assertEqual(throttle.allow(now=100), (True, 0))
        self.assertEqual(throttle.allow(now=105), (False, 0))
        self.assertEqual(throttle.allow(now=109.9), (False, 0))
        self.assertEqual(throttle.allow(now=110), (True, 2))
        self.assertEqual(throttle.allow(now=125), (True, 0))

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
        base, httpd = self.start()
        httpd.app.store.upsert_peer_active("hs-b", PEER_URL)
        port = httpd.server_address[1]
        # Decision: the alias forms of the internal namespace are 404
        # before auth, whatever the token (see _dispatch) -- never 200 and
        # never a 401 that hints at an authenticated route.
        alias = ("/_matrix/pairing/internal",
                 "/_matrix/pairing/internal/",
                 "/_matrix/pairing/internal/events",
                 "/_matrix/pairing/internal/events?since=0&limit=1",
                 "/_matrix/pairing/internal/peers/hs-b",
                 "/_matrix/pairing/internal/nope",
                 # refused by the path-hygiene check before routing
                 "/_matrix/pairing//internal/events",
                 "/_matrix/pairing/x/../internal/events",
                 "/_matrix/pairing/internal%2Fevents",
                 "/v1/pairing/internal/../internal/events",
                 "/v1/pairing//internal/events")
        # Spellings the router never normalises into the internal namespace
        # (case, percent-encoding, "." segment): they land on the ordinary
        # admin routing (401 without the admin token, 404 with it), so the
        # service token opens nothing there either.
        lookalike = ("/_matrix/pairing/./internal/events",
                     "/_matrix/pairing/%69nternal/events",
                     "/_matrix/pairing/INTERNAL/events",
                     "/_MATRIX/pairing/internal/events")
        for paths, allowed in ((alias, {404}), (lookalike, {401, 404})):
            for path in paths:
                for token in (SERVICE, ADMIN, None):
                    headers = ([("Authorization", "Bearer " + token)]
                               if token else [])
                    with self.subTest(path=path, token=token and token[:6]):
                        status, _, _, body = testutil.raw_request(
                            port, "GET", path, headers)
                        self.assertIn(status, allowed)
                        self.assertNotIn(b"active", body)
                        self.assertNotIn(b"hs-b", body)
                        if token == SERVICE:
                            self.assertIn(status, {401, 404})

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
        # The retained hs-c fact comes back (#243 review 1).
        self.assertEqual(status, 200)
        self.assertEqual([(e["kind"], e["server_name"]) for e in resp2["events"]],
                         [("peer_paired", "hs-c")])
        status, resp3 = self.get(
            base, "/v1/pairing/internal/events?since={}".format(
                resp["resync_cursor"] + 100))
        self.assertEqual((status, resp3["reason"]), (410, "cursor_ahead_of_log"))


if __name__ == "__main__":
    unittest.main()
