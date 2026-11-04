"""Unit tests for pairing.service (real HTTP on ephemeral ports, no Docker)."""

import ipaddress
import json
import os
import secrets
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
NOTIFY_SECRET = "ab" * 32  # 64-hex stand-in per-peer shared secret


def signed_notify_headers(secret, origin, body_raw, ts=None, nonce=None):
    """Valid X-Botville-* headers over `body_raw` (review B2 fixture)."""
    ts = str(int(time.time())) if ts is None else ts
    nonce = nonce or secrets.token_hex(16)
    return {
        "X-Botville-Origin": origin,
        "X-Botville-Ts": ts,
        "X-Botville-Nonce": nonce,
        "X-Botville-Sig": service.notify_signature(
            secret, origin, ts, nonce, body_raw),
    }


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
    so state never leaks between tests). `received` entries are dicts:
    {"method", "path", "body", "headers"} (headers lower-cased)."""

    redeem_status = 200
    redeem_shared_secret = None  # echoed in the /redeem response
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

    def _record(self, body):
        type(self).received.append({
            "method": self.command,
            "path": self.path,
            "body": body,
            "headers": {key.lower(): value for key, value in
                        self.headers.items()},
        })

    def do_GET(self):
        self._record(None)
        self._respond(200, {"server_name": type(self).key_server_name})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self._record(json.loads(self.rfile.read(length) or b"{}"))
        if self.path.endswith("/redeem"):
            payload = {"ok": type(self).redeem_status == 200}
            if type(self).redeem_shared_secret:
                payload["shared_secret"] = type(self).redeem_shared_secret
            self._respond(type(self).redeem_status, payload)
        else:
            self._respond(200, {"ok": True})

    def do_DELETE(self):
        self._respond(404, {})


def stub_peer(redeem_status=200, key_server_name="hs-b",
              redeem_shared_secret=None):
    """Serve a StubPeerHandler with the given behaviour; returns (url, cls)."""
    cls = type("StubPeerHandlerCase", (StubPeerHandler,), {
        "redeem_status": redeem_status,
        "key_server_name": key_server_name,
        "redeem_shared_secret": redeem_shared_secret,
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

    def test_redeem_rate_limited_per_invite(self):
        # 5 attempts per invite per minute (review B3): even malformed
        # secrets cannot be brute-forced faster than that.
        body = {"invite_id": "inv-x", "secret": "wrong",
                "server_name": "hs-b", "api_url": "https://x"}
        for _ in range(5):
            status, resp = self.redeem(**body)
            self.assertEqual(status, 404, resp)  # unknown invite, allowed
        status, resp = self.redeem(**body)
        self.assertEqual(status, 429)
        self.assertEqual(resp["errcode"], "M_LIMIT_EXCEEDED")

    def test_forged_xff_not_trusted_without_proxies(self):
        # Default config trusts no proxy: client-supplied X-Forwarded-For
        # must not split the rate-limit bucket (review B3).
        for i in range(10):
            status, _ = testutil.http_json(
                "POST", self.url("/v1/pairing/redeem"),
                body={"invite_id": "x"},
                headers={"X-Forwarded-For": "203.0.113.{}".format(i)})
            self.assertEqual(status, 400)
        status, _ = testutil.http_json(
            "POST", self.url("/v1/pairing/redeem"),
            body={"invite_id": "x"},
            headers={"X-Forwarded-For": "203.0.113.99"})
        self.assertEqual(status, 429)  # same direct-IP bucket

    def test_xff_used_when_direct_peer_is_trusted(self):
        # With 127.0.0.1 as a trusted proxy (tests connect from there),
        # the last XFF value becomes the rate-limit key.
        base, _ = self.start_service(
            trusted_proxies=[ipaddress.ip_network("127.0.0.1")])
        for _ in range(10):
            status, _ = testutil.http_json(
                "POST", base + "/v1/pairing/redeem", body={"invite_id": "x"},
                headers={"X-Forwarded-For": "203.0.113.7"})
            self.assertEqual(status, 400)
        status, _ = testutil.http_json(
            "POST", base + "/v1/pairing/redeem", body={"invite_id": "x"},
            headers={"X-Forwarded-For": "203.0.113.7"})
        self.assertEqual(status, 429)  # that forwarded IP is exhausted
        status, _ = testutil.http_json(
            "POST", base + "/v1/pairing/redeem", body={"invite_id": "x"},
            headers={"X-Forwarded-For": "198.51.100.7"})
        self.assertEqual(status, 400)  # a different forwarded IP is fine

    # --- peer identity verification (review B4 ordering) --------------------

    def test_redeem_verifies_peer_key(self):
        stub_url, cls = stub_peer(key_server_name="hs-b")
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
        self.assertEqual(len(cls.received), 1)  # exactly one key fetch

    def test_redeem_bad_secret_triggers_no_outbound_request(self):
        # Review B4: the invite is validated BEFORE the key fetch, so a
        # wrong secret must not produce any outbound traffic (no liveness
        # oracle, no SSRF-lite).
        stub_url, cls = stub_peer(key_server_name="hs-b")
        base, _ = self.start_service(
            verify_peer_key=True,
            peer_key_url_template=stub_url + "/_matrix/key/v2/server")
        status, invite = testutil.http_json(
            "POST", base + "/v1/pairing/invites", body={}, token=ADMIN)
        _, _, invite_id, secret = service.parse_qr_payload(invite["qr_payload"])

        status, resp = testutil.http_json(
            "POST", base + "/v1/pairing/redeem",
            body={"invite_id": invite_id, "secret": "wrong-secret",
                  "server_name": "hs-b",
                  "api_url": "https://hs-b.example:8448/_matrix/pairing"})
        self.assertEqual(status, 403)
        self.assertEqual(resp["errcode"], "M_FORBIDDEN")
        self.assertEqual(cls.received, [])  # no outbound request at all
        # The invite survives the failed attempt and redeems normally.
        status, resp = testutil.http_json(
            "POST", base + "/v1/pairing/redeem",
            body={"invite_id": invite_id, "secret": secret,
                  "server_name": "hs-b",
                  "api_url": "https://hs-b.example:8448/_matrix/pairing"})
        self.assertEqual(status, 200, resp)
        self.assertEqual(len(cls.received), 1)

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

    # --- server_name grammar + canonicalisation (review B4) ------------------

    def test_redeem_uppercase_server_name_lowercased(self):
        invite = self.create_invite()
        _, _, invite_id, secret = service.parse_qr_payload(invite["qr_payload"])
        status, resp = self.redeem(invite_id, secret, server_name="HS-B")
        self.assertEqual(status, 200, resp)
        status, resp = testutil.http_json(
            "GET", self.url("/v1/pairing/peers"), token=ADMIN)
        self.assertEqual([p["server_name"] for p in resp["peers"]], ["hs-b"])

    def test_redeem_rejects_non_dns_server_names(self):
        invite = self.create_invite()
        _, _, invite_id, secret = service.parse_qr_payload(invite["qr_payload"])
        # Eight rejections + one valid redeem below: the per-IP bucket
        # allows exactly 10 redeem attempts per minute.
        for name in (
            "127.0.0.1",          # IPv4 literal
            "::1", "[::1]",       # IPv6 literal
            "localhost",          # reserved
            "hs-b:8448",          # port
            "-bad",               # leading hyphen label
            "a" * 64,             # label > 63 chars
            "a" * 254,            # total > 253 chars
        ):
            status, resp = self.redeem(invite_id, secret, server_name=name)
            self.assertEqual(status, 400, name)
            self.assertEqual(resp["errcode"], "M_INVALID_PARAM", name)
        # The invite is still intact after all those rejections.
        status, _ = self.redeem(invite_id, secret)
        self.assertEqual(status, 200)

    def test_qr_payload_uppercase_name_normalised(self):
        payload = service.build_qr_payload(
            "HS-B", "https://hs-b.example:8448/_matrix/pairing", "inv", "sec")
        name, _url, invite_id, secret = service.parse_qr_payload(payload)
        self.assertEqual(name, "hs-b")
        self.assertEqual((invite_id, secret), ("inv", "sec"))

    def test_suspicious_paths_rejected(self):
        for path in (
            "/v1/pairing/redeem/../redeem",
            "/v1//pairing/redeem",
            "/v1/pairing/peers/%2e%2e",
            "/v1/pairing/%5cpeers",
        ):
            status, _ = testutil.http_json("POST", self.url(path), body={},
                                           token=ADMIN)
            self.assertEqual(status, 404, path)

    def test_trusted_proxies_env_parsing(self):
        env = {"PAIRING_ADMIN_TOKEN": "t", "PAIRING_SERVER_NAME": "hs-a",
               "PAIRING_API_URL": "https://hs-a.example/pairing",
               "PAIRING_TRUSTED_PROXIES": "172.30.81.10, 10.0.0.0/8"}
        config = service.PairingConfig.from_env(env)
        self.assertEqual(len(config.trusted_proxies), 2)
        self.assertEqual(str(config.trusted_proxies[0]), "172.30.81.10/32")
        self.assertEqual(str(config.trusted_proxies[1]), "10.0.0.0/8")
        # Empty default: trust nothing.
        env["PAIRING_TRUSTED_PROXIES"] = ""
        self.assertEqual(
            service.PairingConfig.from_env(env).trusted_proxies, [])
        # Garbage refuses to start.
        env["PAIRING_TRUSTED_PROXIES"] = "not-an-ip"
        with self.assertRaises(service.ConfigError):
            service.PairingConfig.from_env(env)

    def test_server_name_env_normalised(self):
        env = {"PAIRING_ADMIN_TOKEN": "t", "PAIRING_SERVER_NAME": "HS-A",
               "PAIRING_API_URL": "https://hs-a.example/pairing"}
        config = service.PairingConfig.from_env(env)
        self.assertEqual(config.server_name, "hs-a")
        env["PAIRING_SERVER_NAME"] = "127.0.0.1"
        with self.assertRaises(service.ConfigError):
            service.PairingConfig.from_env(env)

    # --- accept ---------------------------------------------------------------

    def test_accept_pairs_locally_and_calls_peer_redeem(self):
        stub_url, cls = stub_peer(redeem_shared_secret=NOTIFY_SECRET)
        invite_id, secret = "peer-invite", "peer-secret"
        qr = service.build_qr_payload("hs-b", stub_url, invite_id, secret)
        status, resp = testutil.http_json(
            "POST", self.url("/v1/pairing/accept"), body={"qr_payload": qr},
            token=ADMIN)
        self.assertEqual(status, 200, resp)
        self.assertEqual(resp["server_name"], "hs-b")
        # The peer saw our redeem with our own identity.
        posts = [r for r in cls.received if r["path"].endswith("/redeem")]
        self.assertEqual(len(posts), 1)
        body = posts[0]["body"]
        self.assertEqual(body["invite_id"], invite_id)
        self.assertEqual(body["server_name"], "hs-a")
        self.assertEqual(body["api_url"],
                         "https://hs-a.example:8448/_matrix/pairing")
        # Local state: the peer is active with its announced api_url AND
        # the shared secret from the redeem response (review B2).
        status, resp = testutil.http_json(
            "GET", self.url("/v1/pairing/peers"), token=ADMIN)
        self.assertEqual(resp["peers"][0]["server_name"], "hs-b")
        self.assertEqual(resp["peers"][0]["state"], "active")
        self.assertEqual(resp["peers"][0]["peer_api_url"], stub_url)
        self.assertEqual(
            self.httpd.app.store.get_peer("hs-b")["shared_secret"],
            NOTIFY_SECRET)

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

    def seed_peer(self, server_name="hs-b", api_url=None,
                  shared_secret=NOTIFY_SECRET):
        self.httpd.app.store.upsert_peer_active(
            server_name, api_url or "https://hs-b.example:8448/_matrix/pairing",
            shared_secret=shared_secret)

    def notify(self, secret=NOTIFY_SECRET, origin="hs-b", body=None,
               ts=None, nonce=None, headers=None, **header_overrides):
        """POST /notify-severed with (optionally valid) auth headers.

        ts/nonce flow into the signature first, so overriding them tests
        the handler's own window/length checks, not a broken signature."""
        body = {"server_name": origin} if body is None else body
        raw = json.dumps(body).encode("utf-8")
        hdrs = signed_notify_headers(secret, origin, raw, ts=ts, nonce=nonce)
        hdrs.update(header_overrides)
        if headers is not None:
            hdrs = headers
        return testutil.http_json("POST", self.url("/v1/pairing/notify-severed"),
                                  data=raw, headers=hdrs)

    def event_count(self):
        return self.httpd.app.store._conn.execute(
            "SELECT COUNT(*) FROM events").fetchone()[0]

    def test_sever_local_notifies_remote_signed(self):
        stub_url, cls = stub_peer()
        self.seed_peer(api_url=stub_url)
        status, resp = testutil.http_json(
            "DELETE", self.url("/v1/pairing/peers/hs-b"), token=ADMIN)
        self.assertEqual(status, 200, resp)
        self.assertTrue(resp["notified"])
        self.assertEqual(resp["severed_by"], "local")
        notifications = [r for r in cls.received
                         if r["path"].endswith("/notify-severed")]
        self.assertEqual(len(notifications), 1)
        sent = notifications[0]
        self.assertEqual(sent["body"], {"server_name": "hs-a"})
        headers = sent["headers"]
        self.assertEqual(headers.get("x-botville-origin"), "hs-a")
        # The signature covers origin/ts/nonce/body-hash with the shared
        # secret both sides stored at pairing time (review B2).
        raw = json.dumps(sent["body"]).encode("utf-8")
        self.assertEqual(
            headers.get("x-botville-sig"),
            service.notify_signature(
                NOTIFY_SECRET, "hs-a", headers["x-botville-ts"],
                headers["x-botville-nonce"], raw))
        status, resp = testutil.http_json(
            "GET", self.url("/v1/pairing/peers"), token=ADMIN)
        peer = resp["peers"][0]
        self.assertEqual(peer["state"], "severed")
        self.assertEqual(peer["severed_by"], "local")

    def test_sever_invalidates_unused_invites(self):
        invite = self.create_invite()
        _, _, invite_id, secret = service.parse_qr_payload(invite["qr_payload"])
        self.seed_peer()
        status, _ = testutil.http_json(
            "DELETE", self.url("/v1/pairing/peers/hs-b"), token=ADMIN)
        self.assertEqual(status, 200)
        # The outstanding invite must no longer be redeemable.
        status, resp = self.redeem(invite_id, secret)
        self.assertEqual(status, 403)
        self.assertIn("already used", resp["error"])

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

    # --- notify-severed authentication (review B2) ---------------------------

    def test_notify_severed_valid_signature_accepted(self):
        self.seed_peer()
        before = self.event_count()
        status, resp = self.notify()
        self.assertEqual(status, 200, resp)
        self.assertEqual(resp["severed_by"], "remote")
        status, resp = testutil.http_json(
            "GET", self.url("/v1/pairing/peers"), token=ADMIN)
        self.assertEqual(resp["peers"][0]["severed_by"], "remote")
        # One peer_severed event recorded for the accepted transition.
        self.assertEqual(self.event_count(), before + 1)

    def test_notify_severed_rejects_unsigned(self):
        self.seed_peer()
        before = self.event_count()
        for headers in ({}, {"X-Botville-Origin": "hs-b"},
                        {"X-Botville-Sig": "ff" * 32}):
            status, resp = testutil.http_json(
                "POST", self.url("/v1/pairing/notify-severed"),
                body={"server_name": "hs-b"}, headers=headers)
            self.assertEqual(status, 401, headers)
            self.assertEqual(resp["errcode"], "M_UNAUTHORIZED")
        # Peer untouched AND no events written (log counter only).
        status, resp = testutil.http_json(
            "GET", self.url("/v1/pairing/peers"), token=ADMIN)
        self.assertEqual(resp["peers"][0]["state"], "active")
        self.assertEqual(self.event_count(), before)

    def test_notify_severed_rejects_wrong_secret(self):
        self.seed_peer()
        before = self.event_count()
        status, _ = self.notify(secret="cd" * 32)
        self.assertEqual(status, 401)
        self.assertEqual(self.event_count(), before)

    def test_notify_severed_rejects_stale_and_future_ts(self):
        self.seed_peer()
        old_ts = str(int(time.time()) - 400)
        future_ts = str(int(time.time()) + 400)
        for ts in (old_ts, future_ts):
            status, _ = self.notify(ts=ts)
            self.assertEqual(status, 401, ts)
        status, _ = self.notify(ts="not-a-number")
        self.assertEqual(status, 401)

    def test_notify_severed_rejects_replay(self):
        self.seed_peer()
        raw = json.dumps({"server_name": "hs-b"}).encode("utf-8")
        headers = signed_notify_headers(NOTIFY_SECRET, "hs-b", raw)
        status, _ = testutil.http_json(
            "POST", self.url("/v1/pairing/notify-severed"),
            data=raw, headers=headers)
        self.assertEqual(status, 200)
        status, _ = testutil.http_json(
            "POST", self.url("/v1/pairing/notify-severed"),
            data=raw, headers=headers)
        self.assertEqual(status, 401)  # same nonce: replay

    def test_notify_severed_rejects_short_nonce(self):
        self.seed_peer()
        status, _ = self.notify(nonce="abcd")  # < 32 hex chars
        self.assertEqual(status, 401)

    def test_notify_severed_unknown_peer_401(self):
        # Unknown origins are 401 like every other auth failure (no
        # existence oracle, no events).
        before = self.event_count()
        raw = json.dumps({"server_name": "hs-z"}).encode("utf-8")
        headers = signed_notify_headers(NOTIFY_SECRET, "hs-z", raw)
        status, _ = testutil.http_json(
            "POST", self.url("/v1/pairing/notify-severed"),
            data=raw, headers=headers)
        self.assertEqual(status, 401)
        self.assertEqual(self.event_count(), before)

    def test_notify_severed_never_overwrites_local_sever(self):
        self.seed_peer()
        self.httpd.app.store.sever_peer("hs-b", "local")
        status, resp = self.notify()
        self.assertEqual(status, 200, resp)
        status, resp = testutil.http_json(
            "GET", self.url("/v1/pairing/peers"), token=ADMIN)
        peer = resp["peers"][0]
        self.assertEqual(peer["state"], "severed")
        self.assertEqual(peer["severed_by"], "local")

    def test_notify_severed_rate_limited(self):
        self.seed_peer()
        raw = json.dumps({"server_name": "hs-b"}).encode("utf-8")
        status = None
        for _ in range(31):
            status, _ = testutil.http_json(
                "POST", self.url("/v1/pairing/notify-severed"),
                body={"server_name": "hs-b"})
        self.assertEqual(status, 429)

    def test_allowed_endpoint_lists_active(self):
        self.seed_peer()
        status, resp = testutil.http_json(
            "GET", self.url("/v1/pairing/allowed"), token=ADMIN)
        self.assertEqual(status, 200)
        self.assertEqual(resp["allowed"], ["hs-b"])


if __name__ == "__main__":
    unittest.main()
