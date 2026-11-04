"""Unit tests for pairing.fedgate (X-Matrix parsing + gate matrix, no Docker).

Boots fedgate against fake upstreams (a homeserver stub and a pairing
stub) with a real SQLite pairing database, then walks the allow/deny
matrix for every path class.
"""

import http.client
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "..", ".."))  # server/ -> `pairing`
sys.path.insert(0, TESTS_DIR)  # this directory -> util.py helper

from pairing import fedgate  # noqa: E402
from pairing import store as store_mod  # noqa: E402
import util as testutil  # noqa: E402

BIG_BODY = b"x" * (300 * 1024)  # exercises the streaming copy loop


class FakeHomeserverHandler(BaseHTTPRequestHandler):
    """Records requests; /echo replies with what it saw, /big streams a
    large body, /nolength responds without Content-Length, others 200."""

    protocol_version = "HTTP/1.1"
    received = []

    def log_message(self, fmt, *args):
        pass

    def _record(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        type(self).received.append({
            "method": self.command,
            "path": self.path,
            "authorization": self.headers.get_all("Authorization"),
            "content_type": self.headers.get("Content-Type"),
            "custom": self.headers.get("X-Botville-Trace"),
            "xff": self.headers.get("X-Forwarded-For"),
            "body": body,
        })
        return body

    def _respond(self, status, payload, content_type="application/json"):
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload).encode("utf-8")
        else:
            body = payload
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._record()
        if self.path.endswith("/big"):
            self._respond(200, BIG_BODY, "application/octet-stream")
        elif self.path.endswith("/nolength"):
            # Stream without a length: forces fedgate's close-framing path.
            self.send_response(200)
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"streamed-until-close")
            self.close_connection = True
        else:
            self._respond(200, {"upstream": True})

    def do_PUT(self):
        body = self._record()
        self._respond(200, {"upstream": True, "len": len(body)})

    def do_POST(self):
        self._record()
        self._respond(200, {"upstream": True})

    def do_DELETE(self):
        self._record()
        self._respond(204, b"")


class FakePairingHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    received = []

    def log_message(self, fmt, *args):
        pass

    def _record(self, body):
        type(self).received.append({
            "method": self.command,
            "path": self.path,
            "body": body,
            "headers": {key.lower(): value for key, value in
                        self.headers.items()},
        })

    def _respond(self, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._record(None)
        self._respond({"via": "pairing"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self._record(self.rfile.read(length).decode())
        self._respond({"via": "pairing"})


class ParseXMatrixTest(unittest.TestCase):
    """B1: parsing mirrors Synapse but fails closed on any ambiguity."""

    def test_quoted_origin(self):
        headers = ['X-Matrix origin="hs-a",destination="hs-b",key="k",sig="s"']
        self.assertEqual(fedgate.parse_x_matrix_origin(headers), "hs-a")

    def test_unquoted_origin(self):
        self.assertEqual(
            fedgate.parse_x_matrix_origin(["X-Matrix origin=hs-a,key=k,sig=s"]),
            "hs-a")

    def test_mixed_case_param_names(self):
        self.assertEqual(
            fedgate.parse_x_matrix_origin(['X-Matrix Origin="hs-a",Sig="x"']),
            "hs-a")

    def test_backslash_escape_unfolded(self):
        # Synapse's strip_quotes unfolds '\x' -> 'x' inside quoted values.
        self.assertEqual(
            fedgate.parse_x_matrix_origin(
                [r'X-Matrix origin="hs\-a",key="k",sig="s"']),
            "hs-a")

    def test_bearer_plus_single_x_matrix_is_usable(self):
        self.assertEqual(
            fedgate.parse_x_matrix_origin([
                "Bearer sometoken",
                'X-Matrix origin="hs-a",key="k",sig="s"']),
            "hs-a")

    def test_two_x_matrix_headers_rejected(self):
        # The B1 attack: two headers would let Synapse authenticate a
        # different origin than the one the gate checks.
        self.assertIsNone(
            fedgate.parse_x_matrix_origin([
                'X-Matrix origin="hs-a",key="k",sig="s"',
                'X-Matrix origin="hs-evil",key="k",sig="s"']))

    def test_lowercase_x_matrix_scheme_alone_rejected(self):
        # Synapse only recognises the exact "X-Matrix" scheme; a header
        # with a lower-case scheme would be skipped by the homeserver but
        # parsed by a naive gate — refuse it here too.
        self.assertIsNone(
            fedgate.parse_x_matrix_origin(
                ['x-matrix origin="hs-a",key="k",sig="s"']))

    def test_duplicate_origin_param_rejected(self):
        self.assertIsNone(
            fedgate.parse_x_matrix_origin(
                ['X-Matrix origin="hs-a",origin="hs-evil",sig="s"']))

    def test_duplicate_origin_param_case_insensitive_rejected(self):
        self.assertIsNone(
            fedgate.parse_x_matrix_origin(
                ['X-Matrix origin="hs-a",Origin="hs-evil",sig="s"']))

    def test_duplicate_sig_param_rejected(self):
        self.assertIsNone(
            fedgate.parse_x_matrix_origin(
                ['X-Matrix origin="hs-a",sig="s1",sig="s2"']))

    def test_uppercase_origin_rejected(self):
        # Peer rows are keyed lower-case; a mixed-case origin must never
        # be silently normalised into an allow.
        self.assertIsNone(
            fedgate.parse_x_matrix_origin(
                ['X-Matrix origin="HS-A",key="k",sig="s"']))

    def test_origin_with_port_rejected(self):
        self.assertIsNone(
            fedgate.parse_x_matrix_origin(
                ['X-Matrix origin="hs-a:8448",key="k",sig="s"']))

    def test_ip_literal_origin_rejected(self):
        self.assertIsNone(
            fedgate.parse_x_matrix_origin(
                ['X-Matrix origin="127.0.0.1",key="k",sig="s"']))

    def test_missing_origin_is_none(self):
        self.assertIsNone(
            fedgate.parse_x_matrix_origin(['X-Matrix destination="hs-b"']))

    def test_missing_sig_is_none(self):
        self.assertIsNone(
            fedgate.parse_x_matrix_origin(['X-Matrix origin="hs-a"']))

    def test_malformed_quotes_are_none(self):
        for header in (
            'X-Matrix origin=",sig="s"',             # quote-only value
            'X-Matrix origin=,sig="s"',              # empty value
            'X-Matrix origin="",sig="s"',            # empty quoted value
            'X-Matrix origin="hs-a",nonsense',       # param without '='
            'X-Matrix origin="hs-a" sig="s"',        # missing comma
        ):
            self.assertIsNone(
                fedgate.parse_x_matrix_origin([header]), header)

    def test_synapse_quote_slicing_is_copied_exactly(self):
        # Synapse's strip_quotes blindly drops the first and last char of
        # a quote-starting value, so 'X-Matrix origin="hsa,key="k",...'
        # splits into params origin='"hsa' / key='"k' and yields origin
        # 'hs'. The gate must reach the SAME string (never a different
        # origin than the homeserver would authenticate) — here it is a
        # valid-but-unknown name, so the gate simply denies it. (Slice
        # '"hs-a' would give the invalid label 'hs-' and deny too.)
        self.assertEqual(
            fedgate.parse_x_matrix_origin(
                ['X-Matrix origin="hsa,key="k",sig="s"']),
            "hs")

    def test_malformed_scheme_prefix_is_none(self):
        for header in (
            'X-Matrix origin="hs-a",sig="s"',  # control (parseable)
            'X-Matrixorigin="hs-a",sig="s"',   # no space after scheme
            'X-Matrix',                        # no parameter section
        ):
            if header == 'X-Matrix origin="hs-a",sig="s"':
                self.assertEqual(fedgate.parse_x_matrix_origin([header]),
                                 "hs-a")
            else:
                self.assertIsNone(fedgate.parse_x_matrix_origin([header]),
                                  header)

    def test_non_matrix_authorization_skipped(self):
        self.assertIsNone(
            fedgate.parse_x_matrix_origin(["Bearer sometoken"]))

    def test_no_headers_is_none(self):
        self.assertIsNone(fedgate.parse_x_matrix_origin(None))
        self.assertIsNone(fedgate.parse_x_matrix_origin([]))


class AllowedPeersTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = os.path.join(self._tmp.name, "pairing.db")
        self.db = store_mod.PairingStore(self.db_path)
        self.addCleanup(self.db.close)

    def test_contains_active_only(self):
        allowed = fedgate.AllowedPeers(self.db_path, refresh_interval=0)
        self.db.upsert_peer_active("hs-b", "https://hs-b/x")
        self.assertTrue(allowed.contains("hs-b"))
        self.db.sever_peer("hs-b", "local")
        self.assertFalse(allowed.contains("hs-b"))

    def test_missing_db_is_unavailable_not_empty(self):
        # A missing database must not masquerade as "no peers allowed"
        # (silent 200-sink): it is reported as unavailable (503 path).
        allowed = fedgate.AllowedPeers("no-such-file.db", refresh_interval=0)
        ok, names = allowed.refresh_if_stale()
        self.assertFalse(ok)
        self.assertEqual(names, frozenset())
        self.assertFalse(allowed.contains("hs-b"))

    def test_transient_failure_serves_stale_snapshot(self):
        allowed = fedgate.AllowedPeers(self.db_path, refresh_interval=0)
        self.db.upsert_peer_active("hs-b", "https://hs-b/x")

        def broken_load():
            raise sqlite3.OperationalError("database is locked")

        self.assertTrue(allowed.refresh_if_stale()[0])  # one good load first
        allowed._load = broken_load
        # Within the stale-grace window the last good snapshot still
        # serves (ok=True), so a transient lock does not flap the gate.
        ok, names = allowed.refresh_if_stale()
        self.assertTrue(ok)
        self.assertEqual(names, frozenset(["hs-b"]))

    def test_failure_beyond_grace_window_is_unavailable(self):
        allowed = fedgate.AllowedPeers(self.db_path, refresh_interval=0,
                                       stale_ok_seconds=0)
        self.db.upsert_peer_active("hs-b", "https://hs-b/x")
        self.assertTrue(allowed.refresh_if_stale()[0])

        def broken_load():
            raise sqlite3.OperationalError("database is locked")

        allowed._load = broken_load
        ok, _names = allowed.refresh_if_stale()
        self.assertFalse(ok)


class FedgateMatrixTest(unittest.TestCase):
    """fedgate in front of a fake homeserver + fake pairing service."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = os.path.join(self._tmp.name, "pairing.db")
        self.db = store_mod.PairingStore(self.db_path)
        self.addCleanup(self.db.close)

        self.hs_cls = type("Hs", (FakeHomeserverHandler,), {"received": []})
        hs_httpd = testutil.serve(self.hs_cls)
        self.addCleanup(testutil.shutdown, hs_httpd)
        self.hs_url = testutil.base_url(hs_httpd)

        self.pairing_cls = type("Pair", (FakePairingHandler,), {"received": []})
        pairing_httpd = testutil.serve(self.pairing_cls)
        self.addCleanup(testutil.shutdown, pairing_httpd)
        self.pairing_url = testutil.base_url(pairing_httpd)

        self.httpd = fedgate.build_server(fedgate.FedgateConfig(
            host="127.0.0.1", port=0, upstream=self.hs_url,
            pairing_upstream=self.pairing_url, pairing_db=self.db_path))
        # Refresh the allowed set on every request except where a test
        # explicitly exercises the 1s cache window.
        self.httpd.allowed = fedgate.AllowedPeers(
            self.db_path, refresh_interval=0)
        self.addCleanup(testutil.shutdown, self.httpd)
        threading.Thread(
            target=self.httpd.serve_forever, daemon=True).start()
        self.port = self.httpd.server_address[1]

    def url(self, path):
        return "http://127.0.0.1:{}{}".format(self.port, path)

    def paired(self, server_name="hs-a"):
        self.db.upsert_peer_active(server_name, "https://peer/x")

    def get(self, path, headers=None):
        return testutil.http_json("GET", self.url(path), headers=headers)

    # --- open paths -----------------------------------------------------------

    def test_health(self):
        status, resp = self.get("/health")
        self.assertEqual(status, 200)
        self.assertTrue(resp["ok"])

    def test_server_key_open_without_authorization(self):
        status, resp = self.get("/_matrix/key/v2/server")
        self.assertEqual(status, 200)
        self.assertTrue(resp["upstream"])
        # ...and the versioned subpaths likewise.
        status, _ = self.get("/_matrix/key/v2/server/aKeyID")
        self.assertEqual(status, 200)

    # --- gated paths -------------------------------------------------------------

    def test_federation_allowed_with_paired_origin(self):
        self.paired()
        auth = 'X-Matrix origin="hs-a",destination="fedgate-test",sig="s"'
        status, resp = self.get(
            "/_matrix/federation/v1/event/abc", headers={"Authorization": auth})
        self.assertEqual(status, 200)
        self.assertTrue(resp["upstream"])
        # The Authorization header reached the homeserver untouched.
        sent = self.hs_cls.received[-1]
        self.assertEqual(sent["authorization"], [auth])

    def test_send_from_unpaired_origin_is_sunk(self):
        # /send from an unpaired origin: 200 {"pdus": {}} (no Synapse
        # backoff poisoning) and nothing reaches the homeserver.
        before = len(self.hs_cls.received)
        status, resp = testutil.http_json(
            "PUT", self.url("/_matrix/federation/v1/send/txn1"),
            body={"pdus": [{"x": 1}]},
            headers={"Authorization": 'X-Matrix origin="hs-z",sig="s"'})
        self.assertEqual(status, 200)
        self.assertEqual(resp, {"pdus": {}})
        self.assertEqual(len(self.hs_cls.received), before)

    def test_send_without_signature_still_403(self):
        status, _ = testutil.http_json(
            "PUT", self.url("/_matrix/federation/v1/send/txn1"), body={})
        self.assertEqual(status, 403)

    def test_well_known_delegation(self):
        status, _ = self.get("/.well-known/matrix/server")
        self.assertEqual(status, 404)
        self.httpd.config.well_known_server = "hs-a:443"
        status, resp = self.get("/.well-known/matrix/server")
        self.assertEqual(status, 200)
        self.assertEqual(resp, {"m.server": "hs-a:443"})

    def test_federation_denied_when_not_paired(self):
        auth = 'X-Matrix origin="hs-stranger",sig="s"'
        status, resp = self.get(
            "/_matrix/federation/v1/event/abc", headers={"Authorization": auth})
        self.assertEqual(status, 403)
        self.assertEqual(resp["errcode"], "M_FORBIDDEN")
        self.assertEqual(resp["error"], "server not paired")

    def test_federation_denied_without_authorization(self):
        status, resp = self.get("/_matrix/federation/v1/event/abc")
        self.assertEqual(status, 403)
        self.assertEqual(resp["error"], "server not paired")

    def test_federation_denied_with_bearer_token(self):
        status, resp = self.get("/_matrix/federation/v1/event/abc",
                                headers={"Authorization": "Bearer nope"})
        self.assertEqual(status, 403)

    def test_federation_denied_after_sever_within_cache_window(self):
        # This test needs the real 1s cache: rebuild the allowed set with
        # the production refresh interval.
        self.httpd.allowed = fedgate.AllowedPeers(self.db_path)
        self.paired()
        auth = 'X-Matrix origin="hs-a",sig="s"'
        status, _ = self.get("/_matrix/federation/v1/event/abc",
                             headers={"Authorization": auth})
        self.assertEqual(status, 200)
        self.db.sever_peer("hs-a", "local")
        time.sleep(1.2)
        status, resp = self.get("/_matrix/federation/v1/event/abc",
                                headers={"Authorization": auth})
        self.assertEqual(status, 403)
        self.assertEqual(resp["error"], "server not paired")

    def test_key_query_and_media_are_gated(self):
        for path in (
            "/_matrix/key/v2/query/server/hs-a",
            "/_matrix/key/v2/query",
            "/_matrix/media/v1/download/hs-a/abc",
            "/_matrix/media/r0/download/hs-a/abc",
        ):
            self.paired()
            auth = 'X-Matrix origin="hs-a",sig="s"'
            status, _ = self.get(path, headers={"Authorization": auth})
            self.assertEqual(status, 200, path)
            self.db.sever_peer("hs-a", "local")
            status, _ = self.get(path, headers={"Authorization": auth})
            self.assertEqual(status, 403, path)

    # --- proxying fidelity ---------------------------------------------------------

    def test_put_body_and_custom_header_forwarded(self):
        self.paired()
        status, resp, = testutil.http_json(
            "PUT", self.url("/_matrix/federation/v1/send/123"),
            body={"pdus": []},
            headers={"Authorization": 'X-Matrix origin="hs-a",sig="s"',
                     "X-Botville-Trace": "trace-1"})
        self.assertEqual(status, 200)
        self.assertEqual(resp["len"], len(json.dumps({"pdus": []}).encode()))
        sent = self.hs_cls.received[-1]
        self.assertEqual(sent["custom"], "trace-1")
        self.assertEqual(sent["content_type"], "application/json")

    def test_upstream_status_passed_through(self):
        # The fake homeserver answers DELETE with 204; fedgate must pass
        # the status (and the empty body) through untouched.
        self.paired()
        status, _reason, _headers, body = testutil.raw_request(
            self.port, "DELETE", "/_matrix/federation/v1/backfill/room",
            [("Authorization", 'X-Matrix origin="hs-a",sig="s"')])
        self.assertEqual(status, 204)
        self.assertEqual(body, b"")

    def test_large_response_streamed(self):
        self.paired()
        status, _reason, _headers, body = testutil.raw_request(
            self.port, "GET", "/_matrix/federation/v1/big",
            [("Authorization", 'X-Matrix origin="hs-a",sig="s"')])
        self.assertEqual(status, 200)
        self.assertEqual(len(body), len(BIG_BODY))
        self.assertEqual(body[:64], BIG_BODY[:64])

    def test_response_without_length_streamed_to_close(self):
        self.paired()
        status, _reason, _headers, body = testutil.raw_request(
            self.port, "GET", "/_matrix/federation/v1/nolength",
            [("Authorization", 'X-Matrix origin="hs-a",sig="s"')])
        self.assertEqual(status, 200)
        self.assertEqual(body, b"streamed-until-close")

    def test_chunked_request_body_dechunked(self):
        self.paired()
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest("PUT", "/_matrix/federation/v1/send/chunked")
            conn.putheader("Authorization", 'X-Matrix origin="hs-a",sig="s"')
            conn.putheader("Transfer-Encoding", "chunked")
            conn.endheaders()
            conn.send(b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n")
            resp = conn.getresponse()
            body = resp.read()
            self.assertEqual(resp.status, 200)
            self.assertEqual(json.loads(body)["len"], len(b"hello world"))
        finally:
            conn.close()
        self.assertEqual(self.hs_cls.received[-1]["body"], b"hello world")

    # --- pairing forwarding + catch-all ------------------------------------------

    def test_pairing_paths_forwarded(self):
        status, resp = testutil.http_json(
            "POST", self.url("/_matrix/pairing/redeem"),
            body={"invite_id": "i", "secret": "s", "server_name": "hs-b",
                  "api_url": "https://x"})
        self.assertEqual(status, 200)
        self.assertEqual(resp["via"], "pairing")
        posts = [r for r in self.pairing_cls.received if r["method"] == "POST"]
        self.assertEqual(posts[-1]["path"], "/_matrix/pairing/redeem")

    def test_pairing_notify_severed_forwarded(self):
        status, resp = testutil.http_json(
            "POST", self.url("/_matrix/pairing/notify-severed"),
            body={"server_name": "hs-a"})
        self.assertEqual(status, 200)
        self.assertEqual(resp["via"], "pairing")

    def test_pairing_admin_paths_not_forwarded(self):
        # Only redeem + notify-severed belong on the public port; admin
        # endpoints (invites/accept/peers/allowed/DELETE) must 404 at the
        # gate (review: pairing-surface).
        checks = [
            ("POST", "/_matrix/pairing/invites", {}),
            ("POST", "/_matrix/pairing/accept", {"qr_payload": "x"}),
            ("GET", "/_matrix/pairing/peers", None),
            ("GET", "/_matrix/pairing/allowed", None),
            ("DELETE", "/_matrix/pairing/peers/hs-b", None),
            ("POST", "/_matrix/pairing/redeem/", {}),   # trailing slash
        ]
        for method, path, body in checks:
            before = len(self.pairing_cls.received)
            status, resp = testutil.http_json(method, self.url(path),
                                              body=body)
            self.assertEqual(status, 404, path)
            self.assertEqual(resp["errcode"], "M_UNRECOGNIZED", path)
            self.assertEqual(len(self.pairing_cls.received), before, path)

    def test_pairing_forward_overwrites_forwarded_hints(self):
        # Client-supplied forwarding hints must be dropped and replaced by
        # the direct client IP (review B3); Content-Length is recomputed.
        status, _ = testutil.http_json(
            "POST", self.url("/_matrix/pairing/redeem"),
            body={"invite_id": "i", "secret": "s", "server_name": "hs-b",
                  "api_url": "https://x"},
            headers={"X-Forwarded-For": "6.6.6.6, 7.7.7.7",
                     "X-Real-IP": "6.6.6.6", "Forwarded": "for=6.6.6.6"})
        self.assertEqual(status, 200)
        sent = self.pairing_cls.received[-1]
        headers = sent["headers"]
        self.assertEqual(headers.get("x-forwarded-for"), "127.0.0.1")
        self.assertNotIn("x-real-ip", headers)
        self.assertNotIn("forwarded", headers)
        # The recomputed length matches the body the stub read.
        self.assertEqual(int(headers.get("content-length")),
                         len(sent["body"].encode("utf-8")))

    def test_gated_proxy_forwards_xff_verbatim_to_homeserver(self):
        # Toward the homeserver the forwarding hints pass through verbatim
        # (the overwrite only applies to the pairing upstream); end-to-end
        # headers like X-Matrix are untouched either way.
        self.paired()
        auth = 'X-Matrix origin="hs-a",sig="s"'
        status, _ = testutil.http_json(
            "GET", self.url("/_matrix/federation/v1/event/abc"),
            headers={"Authorization": auth,
                     "X-Forwarded-For": "6.6.6.6"})
        self.assertEqual(status, 200)
        sent = self.hs_cls.received[-1]
        self.assertEqual(sent["xff"], "6.6.6.6")
        self.assertEqual(sent["authorization"], [auth])

    def test_unknown_path_404(self):
        for path in ("/", "/_matrix/client/v3/sync", "/admin", "/_matrix/media"):
            status, resp = self.get(path)
            self.assertEqual(status, 404, path)
            self.assertEqual(resp["errcode"], "M_UNRECOGNIZED")

    # --- path hygiene (review: bypass-via-path) ---------------------------------

    def test_suspicious_paths_rejected(self):
        for path in (
            "/_matrix/federation/v1/event/../x",
            "/_matrix/key/v2/server/../server",
            "/_matrix/key//v2/server",
            "/_matrix/key/v2/server//aKeyID",
            "/_matrix/media/v1/download/%2e%2e/x",
            "/_matrix/federation/v1/send/%2f",
            "/_matrix/key/v2/server%5c",
            "/_matrix/pairing/%2fredeem",
        ):
            status, resp = self.get(path)
            self.assertEqual(status, 404, path)
            self.assertEqual(resp["errcode"], "M_UNRECOGNIZED", path)

    # --- ambiguous X-Matrix is denied at the gate (review B1) -------------------

    def test_send_with_two_x_matrix_headers_403(self):
        # The B1 attack shape: gate must refuse (and must NOT sink) —
        # nothing reaches the homeserver either way.
        self.paired()
        before = len(self.hs_cls.received)
        status, _reason, _hdrs, body = testutil.raw_request(
            self.port, "PUT", "/_matrix/federation/v1/send/txn1",
            [("Authorization", 'X-Matrix origin="hs-a",key="k",sig="s"'),
             ("Authorization", 'X-Matrix origin="hs-evil",key="k",sig="s"'),
             ("Content-Type", "application/json")],
            body=b'{"pdus":[]}')
        self.assertEqual(status, 403)
        self.assertIn("not paired", json.loads(body)["error"])
        self.assertEqual(len(self.hs_cls.received), before)

    def test_send_with_duplicate_origin_param_403(self):
        self.paired()
        before = len(self.hs_cls.received)
        status, _reason, _headers, _body = testutil.raw_request(
            self.port, "PUT", "/_matrix/federation/v1/send/txn2",
            [("Authorization",
              'X-Matrix origin="hs-a",origin="hs-evil",key="k",sig="s"'),
             ("Content-Type", "application/json")],
            body=b'{"pdus":[]}')
        self.assertEqual(status, 403)
        self.assertEqual(len(self.hs_cls.received), before)

    def test_send_with_duplicate_sig_param_403(self):
        self.paired()
        before = len(self.hs_cls.received)
        status, _reason, _headers, _body = testutil.raw_request(
            self.port, "PUT", "/_matrix/federation/v1/send/txn3",
            [("Authorization",
              'X-Matrix origin="hs-a",key="k",sig="s1",sig="s2"'),
             ("Content-Type", "application/json")],
            body=b'{"pdus":[]}')
        self.assertEqual(status, 403)
        self.assertEqual(len(self.hs_cls.received), before)

    def test_send_with_malformed_quotes_403(self):
        self.paired()
        before = len(self.hs_cls.received)
        for auth in ('X-Matrix origin=",sig="s"',   # quote-only origin
                     'X-Matrix origin=,sig="s"',    # empty origin
                     'X-Matrix origin="hs-a",junk'):  # param without '='
            status, _reason, _headers, _body = testutil.raw_request(
                self.port, "PUT", "/_matrix/federation/v1/send/txn4",
                [("Authorization", auth),
                 ("Content-Type", "application/json")],
                body=b'{"pdus":[]}')
            self.assertEqual(status, 403, auth)
        self.assertEqual(len(self.hs_cls.received), before)

    def test_federation_with_uppercase_origin_403(self):
        self.paired()
        status, resp = self.get(
            "/_matrix/federation/v1/event/abc",
            headers={"Authorization": 'X-Matrix origin="HS-A",sig="s"'})
        self.assertEqual(status, 403)

    def test_federation_with_missing_sig_403(self):
        self.paired()
        status, _ = self.get(
            "/_matrix/federation/v1/event/abc",
            headers={"Authorization": 'X-Matrix origin="hs-a"'})
        self.assertEqual(status, 403)

    # --- pairing DB unavailable => 503, never sink ------------------------------

    def test_gated_503_when_db_unavailable(self):
        class UnavailableAllowed:
            def refresh_if_stale(self):
                return False, frozenset()

            def contains(self, server_name):
                return False

        self.paired()
        self.httpd.allowed = UnavailableAllowed()
        auth = {"Authorization": 'X-Matrix origin="hs-a",sig="s"'}

        status, resp = self.get("/_matrix/federation/v1/event/abc",
                                headers=auth)
        self.assertEqual(status, 503)
        self.assertEqual(resp["errcode"], "M_UNAVAILABLE")

        # /send is NOT sunk while the DB is unreadable...
        before = len(self.hs_cls.received)
        status, resp = testutil.http_json(
            "PUT", self.url("/_matrix/federation/v1/send/txn1"),
            body={"pdus": []}, headers=auth)
        self.assertEqual(status, 503)
        self.assertEqual(resp["errcode"], "M_UNAVAILABLE")
        self.assertEqual(len(self.hs_cls.received), before)

        # ...and /health reports the failure loudly.
        status, resp = self.get("/health")
        self.assertEqual(status, 503)
        self.assertEqual(resp, {"ok": False, "db": False})

    def test_health_ok_when_db_readable(self):
        status, resp = self.get("/health")
        self.assertEqual(status, 200)
        self.assertTrue(resp["ok"])

    # --- sink body cap (review: unauthenticated resource use) -------------------

    def test_sink_body_over_1mib_closes_connection(self):
        oversized = b"x" * (fedgate.SINK_MAX_BODY_BYTES + 1)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        with self.assertRaises((http.client.HTTPException, OSError)):
            try:
                conn.putrequest("PUT", "/_matrix/federation/v1/send/big")
                conn.putheader(
                    "Authorization", 'X-Matrix origin="hs-z",sig="s"')
                conn.putheader("Content-Type", "application/json")
                conn.putheader("Content-Length", str(len(oversized)))
                conn.endheaders()
                conn.send(oversized)
                conn.getresponse().read()
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
