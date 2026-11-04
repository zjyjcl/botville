"""Unit tests for pairing.fedgate (X-Matrix parsing + gate matrix, no Docker).

Boots fedgate against fake upstreams (a homeserver stub and a pairing
stub) with a real SQLite pairing database, then walks the allow/deny
matrix for every path class.
"""

import json
import os
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

    def _respond(self, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        type(self).received.append(("GET", self.path))
        self._respond({"via": "pairing"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        type(self).received.append(("POST", self.path,
                                    self.rfile.read(length).decode()))
        self._respond({"via": "pairing"})


class ParseXMatrixTest(unittest.TestCase):
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

    def test_first_x_matrix_header_wins(self):
        self.assertEqual(
            fedgate.parse_x_matrix_origin([
                'X-Matrix origin="hs-a",key="k",sig="s"',
                'X-Matrix origin="hs-b",key="k",sig="s"',
            ]),
            "hs-a")

    def test_non_matrix_authorization_skipped(self):
        self.assertIsNone(
            fedgate.parse_x_matrix_origin(["Bearer sometoken"]))

    def test_missing_origin_is_none(self):
        self.assertIsNone(
            fedgate.parse_x_matrix_origin(['X-Matrix destination="hs-b"']))

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
        # A missing database means "allow nobody" (default deny).
        allowed = fedgate.AllowedPeers("no-such-file.db", refresh_interval=0)
        self.assertEqual(allowed.refresh_if_stale(), frozenset())

        allowed = fedgate.AllowedPeers(self.db_path, refresh_interval=0)
        self.db.upsert_peer_active("hs-b", "https://hs-b/x")
        self.assertTrue(allowed.contains("hs-b"))
        self.db.sever_peer("hs-b", "local")
        self.assertFalse(allowed.contains("hs-b"))


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
            [("Authorization", 'X-Matrix origin="hs-a"')])
        self.assertEqual(status, 200)
        self.assertEqual(len(body), len(BIG_BODY))
        self.assertEqual(body[:64], BIG_BODY[:64])

    def test_response_without_length_streamed_to_close(self):
        self.paired()
        status, _reason, _headers, body = testutil.raw_request(
            self.port, "GET", "/_matrix/federation/v1/nolength",
            [("Authorization", 'X-Matrix origin="hs-a"')])
        self.assertEqual(status, 200)
        self.assertEqual(body, b"streamed-until-close")

    def test_chunked_request_body_dechunked(self):
        self.paired()
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest("PUT", "/_matrix/federation/v1/send/chunked")
            conn.putheader("Authorization", 'X-Matrix origin="hs-a"')
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
        posts = [r for r in self.pairing_cls.received if r[0] == "POST"]
        self.assertEqual(posts[-1][1], "/_matrix/pairing/redeem")

    def test_unknown_path_404(self):
        for path in ("/", "/_matrix/client/v3/sync", "/admin", "/_matrix/media"):
            status, resp = self.get(path)
            self.assertEqual(status, 404, path)
            self.assertEqual(resp["errcode"], "M_UNRECOGNIZED")


if __name__ == "__main__":
    unittest.main()
