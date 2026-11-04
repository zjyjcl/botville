"""Unit tests for pairing.service (real HTTP on ephemeral ports, no Docker)."""

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

from pairing import service  # noqa: E402
import util as testutil  # noqa: E402

ADMIN = "test-admin-token"


class ServiceTestBase(unittest.TestCase):
    """Boots one pairing service against a temp SQLite database."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = os.path.join(self._tmp.name, "pairing.db")

    def start_service(self, **overrides):
        kwargs = dict(
            db_path=self.db_path,
            admin_token=ADMIN,
            server_name="hs-a",
            api_url="https://hs-a.example:8448/_matrix/pairing",
            verify_peer_key=False,
            host="127.0.0.1",
            port=0,
        )
        kwargs.update(overrides)
        httpd = service.build_server(service.PairingConfig(**kwargs))
        self.addCleanup(testutil.shutdown, httpd)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        return testutil.base_url(httpd), httpd


class StubPeerHandler(BaseHTTPRequestHandler):
    """Stand-in for the *other* server's pairing endpoint / key endpoint.

    Class attributes are set per-test (a fresh class is created in setUp
    so state never leaks between tests)."""

    redeem_status = 200
    key_server_name = "hs-b"
    received = []

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _respond(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        type(self).received.append(("GET", self.path, None))
        self._respond(200, {"server_name": type(self).key_server_name})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).received.append(("POST", self.path, body))
        if self.path.endswith("/redeem"):
            self._respond(type(self).redeem_status,
                          {"ok": type(self).redeem_status == 200})
        else:
            self._respond(200, {"ok": True})

    def do_DELETE(self):
        self._respond(404, {})


def stub_peer(redeem_status=200, key_server_name="hs-b"):
    """Serve a StubPeerHandler with the given behaviour; returns (url, cls)."""
    cls = type("StubPeerHandlerCase", (StubPeerHandler,), {
        "redeem_status": redeem_status,
        "key_server_name": key_server_name,
        "received": [],
    })
    httpd = testutil.serve(cls)
    return testutil.base_url(httpd), cls


class ConfigTest(ServiceTestBase):
    def test_admin_token_required_to_start(self):
        with self.assertRaises(service.ConfigError):
            service.PairingConfig.from_env({})
        with self.assertRaises(service.ConfigError):
            service.PairingConfig.from_env({"PAIRING_ADMIN_TOKEN": "x"})

    def test_server_name_and_api_url_required(self):
        env = {"PAIRING_ADMIN_TOKEN": "t"}
        with self.assertRaises(service.ConfigError):
            service.PairingConfig.from_env(env)
        env["PAIRING_SERVER_NAME"] = "hs-a"
        with self.assertRaises(service.ConfigError):
            service.PairingConfig.from_env(env)
        env["PAIRING_API_URL"] = "not-a-url"
        with self.assertRaises(service.ConfigError):
            service.PairingConfig.from_env(env)
        env["PAIRING_API_URL"] = "https://hs-a.example/pairing"
        config = service.PairingConfig.from_env(env)
        self.assertTrue(config.verify_peer_key)  # default on

    def test_qr_payload_roundtrip(self):
        payload = service.build_qr_payload(
            "hs-a", "https://hs-a.example:8448/_matrix/pairing", "inv1", "sec1")
        name, url, invite, secret = service.parse_qr_payload(payload)
        self.assertEqual(
            (name, url, invite, secret),
            ("hs-a", "https://hs-a.example:8448/_matrix/pairing", "inv1", "sec1"))

    def test_qr_payload_rejects_garbage(self):
        bad = [
            "",
            "botville-pair:v2:hs-a:aaaa:inv:sec",   # wrong version
            "other-pair:v1:hs-a:aaaa:inv:sec",      # wrong prefix
            "botville-pair:v1:hs-a:inv:sec",        # too few fields
            "botville-pair:v1:hs-a:%_%:inv:sec",    # bad base64
            "botville-pair:v1:hs-a:YQ:inv:sec",     # b64("a") not a URL
        ]
        for payload in bad:
            with self.assertRaises(service.QrPayloadError, msg=payload):
                service.parse_qr_payload(payload)


class ApiTest(ServiceTestBase):
    def setUp(self):
        super().setUp()
        self.base, self.httpd = self.start_service()

    def url(self, path):
        return self.base + path

    def create_invite(self):
        status, resp = testutil.http_json(
            "POST", self.url("/v1/pairing/invites"), body={}, token=ADMIN)
        self.assertEqual(status, 200, resp)
        return resp

    def redeem(self, invite_id, secret, server_name="hs-b",
               api_url="https://hs-b.example:8448/_matrix/pairing"):
        return testutil.http_json(
            "POST", self.url("/v1/pairing/redeem"),
            body={"invite_id": invite_id, "secret": secret,
                  "server_name": server_name, "api_url": api_url})

    # --- auth ---------------------------------------------------------------

    def test_health_open(self):
        status, resp = testutil.http_json("GET", self.url("/health"))
        self.assertEqual(status, 200)
        self.assertTrue(resp["ok"])

    def test_admin_requires_token(self):
        for token in (None, "wrong-token"):
            status, resp = testutil.http_json(
                "POST", self.url("/v1/pairing/invites"), body={}, token=token)
            self.assertEqual(status, 401, resp)
            self.assertEqual(resp["errcode"], "M_UNKNOWN_TOKEN")
        status, _ = testutil.http_json(
            "POST", self.url("/v1/pairing/invites"), body={}, token=ADMIN)
        self.assertEqual(status, 200)

    # --- invites / redeem ----------------------------------------------------

    def test_invite_response_shape(self):
        resp = self.create_invite()
        self.assertIn("invite_id", resp)
        self.assertIn("expires_at", resp)
        expected_prefix = "botville-pair:v1:hs-a:"
        self.assertTrue(resp["qr_payload"].startswith(expected_prefix),
                        resp["qr_payload"])
        name, url, invite_id, secret = service.parse_qr_payload(
            resp["qr_payload"])
        self.assertEqual(name, "hs-a")
        self.assertEqual(url, "https://hs-a.example:8448/_matrix/pairing")
        self.assertEqual(invite_id, resp["invite_id"])
        self.assertTrue(secret)

    def test_redeem_success_and_peers_listing(self):
        invite = self.create_invite()
        _, _, invite_id, secret = service.parse_qr_payload(invite["qr_payload"])
        status, resp = self.redeem(invite_id, secret)
        self.assertEqual(status, 200, resp)
        self.assertEqual(resp["server_name"], "hs-a")

        status, resp = testutil.http_json(
            "GET", self.url("/v1/pairing/peers"), token=ADMIN)
        self.assertEqual(status, 200)
        self.assertEqual([p["server_name"] for p in resp["peers"]], ["hs-b"])
        peer = resp["peers"][0]
        self.assertEqual(peer["state"], "active")
        self.assertEqual(peer["severed_by"], None)

    def test_redeem_wrong_secret_then_correct(self):
        invite = self.create_invite()
        _, _, invite_id, secret = service.parse_qr_payload(invite["qr_payload"])
        status, resp = self.redeem(invite_id, "nope")
        self.assertEqual(status, 403)
        self.assertEqual(resp["errcode"], "M_FORBIDDEN")
        status, resp = self.redeem(invite_id, secret)
        self.assertEqual(status, 200)

    def test_redeem_replay_rejected(self):
        invite = self.create_invite()
        _, _, invite_id, secret = service.parse_qr_payload(invite["qr_payload"])
        self.assertEqual(self.redeem(invite_id, secret)[0], 200)
        status, resp = self.redeem(invite_id, secret)
        self.assertEqual(status, 403)
        self.assertIn("already used", resp["error"])

    def test_redeem_expired(self):
        base, _ = self.start_service(invite_ttl=0.2)
        status, invite = testutil.http_json(
            "POST", base + "/v1/pairing/invites", body={}, token=ADMIN)
        _, _, invite_id, secret = service.parse_qr_payload(invite["qr_payload"])
        time.sleep(0.3)
        status, resp = testutil.http_json(
            "POST", base + "/v1/pairing/redeem",
            body={"invite_id": invite_id, "secret": secret,
                  "server_name": "hs-b",
                  "api_url": "https://hs-b.example:8448/_matrix/pairing"})
        self.assertEqual(status, 403)
        self.assertIn("expired", resp["error"])

    def test_redeem_unknown_invite_404(self):
        status, resp = self.redeem("nope", "nope")
        self.assertEqual(status, 404)
        self.assertEqual(resp["errcode"], "M_NOT_FOUND")

    def test_redeem_requires_valid_body(self):
        for body in (
            {},
            {"invite_id": "x", "secret": "y", "server_name": "hs-b"},  # no url
            {"invite_id": "x", "secret": "y", "server_name": "",
             "api_url": "https://x"},
            {"invite_id": "x", "secret": "y", "server_name": "hs:b",
             "api_url": "https://x"},
        ):
            status, resp = testutil.http_json(
                "POST", self.url("/v1/pairing/redeem"), body=body)
            self.assertEqual(status, 400, body)

    def test_matrix_pairing_path_alias(self):
        invite = self.create_invite()
        _, _, invite_id, secret = service.parse_qr_payload(invite["qr_payload"])
        status, resp = testutil.http_json(
            "POST", self.url("/_matrix/pairing/redeem"),
            body={"invite_id": invite_id, "secret": secret,
                  "server_name": "hs-b",
                  "api_url": "https://hs-b.example:8448/_matrix/pairing"})
        self.assertEqual(status, 200, resp)

    def test_redeem_rate_limited_per_ip(self):
        # 10 hits per sliding minute from one IP: the 11th is refused even
        # though the earlier ones failed validation (bodies missing fields).
        for _ in range(10):
            status, _ = testutil.http_json(
                "POST", self.url("/v1/pairing/redeem"),
                body={"invite_id": "x"})
            self.assertEqual(status, 400)
        status, resp = testutil.http_json(
            "POST", self.url("/v1/pairing/redeem"),
            body={"invite_id": "x", "secret": "y", "server_name": "hs-b",
                  "api_url": "https://x"})
        self.assertEqual(status, 429)
        self.assertEqual(resp["errcode"], "M_LIMIT_EXCEEDED")

    # --- peer identity verification -----------------------------------------

    def test_redeem_verifies_peer_key(self):
        stub_url, _cls = stub_peer(key_server_name="hs-b")
        base, _ = self.start_service(
            verify_peer_key=True,
            peer_key_url_template=stub_url + "/_matrix/key/v2/server")
        status, invite = testutil.http_json(
            "POST", base + "/v1/pairing/invites", body={}, token=ADMIN)
        _, _, invite_id, secret = service.parse_qr_payload(invite["qr_payload"])

        status, resp = testutil.http_json(
            "POST", base + "/v1/pairing/redeem",
            body={"invite_id": invite_id, "secret": secret,
                  "server_name": "hs-b",
                  "api_url": "https://hs-b.example:8448/_matrix/pairing"})
        self.assertEqual(status, 200, resp)

    def test_redeem_rejects_name_not_matching_key(self):
        stub_url, _cls = stub_peer(key_server_name="hs-b")
        base, _ = self.start_service(
            verify_peer_key=True,
            peer_key_url_template=stub_url + "/_matrix/key/v2/server")
        status, invite = testutil.http_json(
            "POST", base + "/v1/pairing/invites", body={}, token=ADMIN)
        _, _, invite_id, secret = service.parse_qr_payload(invite["qr_payload"])

        # Claims hs-c but publishes hs-b's key: impersonation, rejected.
        status, resp = testutil.http_json(
            "POST", base + "/v1/pairing/redeem",
            body={"invite_id": invite_id, "secret": secret,
                  "server_name": "hs-c",
                  "api_url": "https://hs-c.example:8448/_matrix/pairing"})
        self.assertEqual(status, 403)
        self.assertIn("does not match", resp["error"])
        # And the invite is still consumable by the genuine server.
        status, _ = testutil.http_json(
            "POST", base + "/v1/pairing/redeem",
            body={"invite_id": invite_id, "secret": secret,
                  "server_name": "hs-b",
                  "api_url": "https://hs-b.example:8448/_matrix/pairing"})
        self.assertEqual(status, 200)

    def test_redeem_rejects_unreachable_peer_key(self):
        base, _ = self.start_service(
            verify_peer_key=True,
            peer_key_url_template="http://127.0.0.1:1/_matrix/key/v2/server")
        status, invite = testutil.http_json(
            "POST", base + "/v1/pairing/invites", body={}, token=ADMIN)
        _, _, invite_id, secret = service.parse_qr_payload(invite["qr_payload"])
        status, resp = testutil.http_json(
            "POST", base + "/v1/pairing/redeem",
            body={"invite_id": invite_id, "secret": secret,
                  "server_name": "hs-b",
                  "api_url": "https://hs-b.example:8448/_matrix/pairing"})
        self.assertEqual(status, 403)
        self.assertIn("could not verify", resp["error"])

    # --- accept ---------------------------------------------------------------

    def test_accept_pairs_locally_and_calls_peer_redeem(self):
        stub_url, cls = stub_peer()
        invite_id, secret = "peer-invite", "peer-secret"
        qr = service.build_qr_payload("hs-b", stub_url, invite_id, secret)
        status, resp = testutil.http_json(
            "POST", self.url("/v1/pairing/accept"), body={"qr_payload": qr},
            token=ADMIN)
        self.assertEqual(status, 200, resp)
        self.assertEqual(resp["server_name"], "hs-b")
        # The peer saw our redeem with our own identity.
        posts = [r for r in cls.received if r[1].endswith("/redeem")]
        self.assertEqual(len(posts), 1)
        method, path, body = posts[0]
        self.assertEqual(body["invite_id"], invite_id)
        self.assertEqual(body["server_name"], "hs-a")
        self.assertEqual(body["api_url"],
                         "https://hs-a.example:8448/_matrix/pairing")
        # Local state: the peer is active with its announced api_url.
        status, resp = testutil.http_json(
            "GET", self.url("/v1/pairing/peers"), token=ADMIN)
        self.assertEqual(resp["peers"][0]["server_name"], "hs-b")
        self.assertEqual(resp["peers"][0]["state"], "active")
        self.assertEqual(resp["peers"][0]["peer_api_url"], stub_url)

    def test_accept_surfaces_peer_rejection(self):
        stub_url, _cls = stub_peer(redeem_status=403)
        qr = service.build_qr_payload("hs-b", stub_url, "i", "s")
        status, resp = testutil.http_json(
            "POST", self.url("/v1/pairing/accept"), body={"qr_payload": qr},
            token=ADMIN)
        self.assertEqual(status, 403)
        self.assertIn("peer rejected", resp["error"])
        # Nothing was written locally.
        status, resp = testutil.http_json(
            "GET", self.url("/v1/pairing/peers"), token=ADMIN)
        self.assertEqual(resp["peers"], [])

    def test_accept_rejects_bad_qr(self):
        for payload in ("", "garbage", "botville-pair:v9:a:b:c:d"):
            status, resp = testutil.http_json(
                "POST", self.url("/v1/pairing/accept"),
                body={"qr_payload": payload}, token=ADMIN)
            self.assertEqual(status, 400, payload)

    # --- severing ---------------------------------------------------------------

    def seed_peer(self, server_name="hs-b", api_url=None):
        self.httpd.app.store.upsert_peer_active(
            server_name, api_url or "https://hs-b.example:8448/_matrix/pairing")

    def test_sever_local_notifies_remote(self):
        stub_url, cls = stub_peer()
        self.seed_peer(api_url=stub_url)
        status, resp = testutil.http_json(
            "DELETE", self.url("/v1/pairing/peers/hs-b"), token=ADMIN)
        self.assertEqual(status, 200, resp)
        self.assertTrue(resp["notified"])
        self.assertEqual(resp["severed_by"], "local")
        notifications = [r for r in cls.received
                         if r[1].endswith("/notify-severed")]
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0][2], {"server_name": "hs-a"})
        status, resp = testutil.http_json(
            "GET", self.url("/v1/pairing/peers"), token=ADMIN)
        peer = resp["peers"][0]
        self.assertEqual(peer["state"], "severed")
        self.assertEqual(peer["severed_by"], "local")

    def test_sever_survives_unreachable_peer(self):
        # Peer api_url points at a closed port: severing must still succeed
        # (notify is best-effort).
        self.seed_peer(api_url="http://127.0.0.1:1/pairing")
        status, resp = testutil.http_json(
            "DELETE", self.url("/v1/pairing/peers/hs-b"), token=ADMIN)
        self.assertEqual(status, 200)
        self.assertFalse(resp["notified"])
        status, resp = testutil.http_json(
            "GET", self.url("/v1/pairing/peers"), token=ADMIN)
        self.assertEqual(resp["peers"][0]["state"], "severed")

    def test_sever_unknown_peer_404(self):
        status, resp = testutil.http_json(
            "DELETE", self.url("/v1/pairing/peers/hs-z"), token=ADMIN)
        self.assertEqual(status, 404)

    def test_notify_severed_marks_remote(self):
        self.seed_peer()
        status, resp = testutil.http_json(
            "POST", self.url("/v1/pairing/notify-severed"),
            body={"server_name": "hs-b"})
        self.assertEqual(status, 200, resp)
        self.assertEqual(resp["severed_by"], "remote")
        status, resp = testutil.http_json(
            "GET", self.url("/v1/pairing/peers"), token=ADMIN)
        self.assertEqual(resp["peers"][0]["severed_by"], "remote")

    def test_notify_severed_unknown_peer_404(self):
        status, _ = testutil.http_json(
            "POST", self.url("/v1/pairing/notify-severed"),
            body={"server_name": "hs-z"})
        self.assertEqual(status, 404)

    def test_allowed_endpoint_lists_active(self):
        self.seed_peer()
        status, resp = testutil.http_json(
            "GET", self.url("/v1/pairing/allowed"), token=ADMIN)
        self.assertEqual(status, 200)
        self.assertEqual(resp["allowed"], ["hs-b"])


if __name__ == "__main__":
    unittest.main()
