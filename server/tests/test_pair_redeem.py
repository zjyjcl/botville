#!/usr/bin/env python3
"""Unit tests for POST /_botville/pair/redeem (issue #128, first cut).

All Synapse admin calls are mocked. No Docker, no live homeserver, no
edge, no federation /v1/pairing/redeem. Examples use example.org only.

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
        discover -s server/tests -p test_pair_redeem.py -v
"""

import base64
import json
import logging
import os
import socket
import shutil
import ssl
import subprocess
import tempfile
from unittest import mock
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_network
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_DIR.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from pair_redeem.service import (  # noqa: E402
    AUTH_EXPIRES_IN_MS,
    ENV_BIND,
    ENV_GLOBAL_LIMIT,
    ENV_IP_LIMIT,
    ENV_ISSUER_TIMEOUT,
    ENV_SYNAPSE_ADMIN_TOKEN,
    ENV_SYNAPSE_URL,
    ENV_TRUSTED_PROXIES,
    ISSUER_TIMEOUT_SECONDS,
    allow_bind_host,
    require_admin_token,
    validate_synapse_admin_url,
    ERROR_STATUS,
    ERROR_TEXT,
    MAX_BODY_BYTES,
    REDEEM_PATH,
    ConfigError,
    IssuerError,
    PairRedeemApp,
    PairRedeemConfig,
    PairRedeemError,
    SynapseRegistrationTokenIssuer,
    client_ip,
    parse_redeem_request,
    start_pruner,
)
from pair_redeem.store import PairRedeemStore, code_sha256  # noqa: E402
import pair_redeem.service as redeem_service  # noqa: E402
import pairing_qr_contract as contract  # noqa: E402

HS = "https://hs.example.org"
GW = "https://gw.example.org"
DEVICE = "Alice 的 iPhone 17"
PLACEHOLDER_TOKEN = "<redacted-example>"


def make_code(seed=1):
    """Canonical 43-char base64url (32 bytes). Deterministic for a seed."""
    raw = bytes((seed + i) % 256 for i in range(32))
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class FakeIssuer:
    def __init__(self, token=PLACEHOLDER_TOKEN, expires_in_ms=AUTH_EXPIRES_IN_MS,
                 fail=False, hook=None):
        self.token = token
        self.expires_in_ms = expires_in_ms
        self.fail = fail
        self.hook = hook
        self.calls = 0

    def issue(self, expires_in_ms, now=None):
        self.calls += 1
        if self.hook:
            self.hook(self, expires_in_ms, now)
        if self.fail:
            raise IssuerError("http_unavailable")
        return {
            "type": "registration_token",
            "token": self.token,
            "expires_in_ms": int(expires_in_ms if expires_in_ms is not None
                                 else self.expires_in_ms),
        }


class PairRedeemTestCase(unittest.TestCase):
    def setUp(self):
        self.store = PairRedeemStore(":memory:")
        self.issuer = FakeIssuer()
        self.now = 1_800_000_000
        self.config = PairRedeemConfig(
            ip_limit=10, global_limit=60, bind_host="127.0.0.1", bind_port=0,
        )
        self.app = PairRedeemApp(
            self.store, self.issuer, self.config, clock=lambda: self.now)
        self.server = self.app.make_server("127.0.0.1", 0)
        self.base = "http://127.0.0.1:%s" % self.server.server_address[1]
        self._thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True)
        self._thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.close()

    def issue(self, code=None, hs=HS, gw=GW, fp=None, ttl=300):
        code = make_code(1) if code is None else code
        self.store.issue(hs, code, gw=gw, fp=fp, now=self.now, ttl_seconds=ttl)
        return code

    def post(self, body, path=REDEEM_PATH, content_type="application/json",
             extra_headers=None, raw=None):
        data = raw if raw is not None else json.dumps(body).encode("utf-8")
        headers = {"Content-Type": content_type}
        if extra_headers:
            headers.update(extra_headers)
        request = urllib.request.Request(
            self.base + path, data=data, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8")), dict(response.headers)
        except urllib.error.HTTPError as exc:
            raw_body = exc.read()
            try:
                parsed = json.loads(raw_body.decode("utf-8"))
            except (ValueError, UnicodeError):
                parsed = raw_body.decode("utf-8", "replace")
            return exc.code, parsed, dict(exc.headers)

    def redeem_ok(self, code, name=DEVICE):
        return self.post({"code": code, "device_display_name": name})


class SuccessAndContractTests(PairRedeemTestCase):
    def test_registration_token_success_matches_contract(self):
        code = self.issue(make_code(2), gw=GW)
        status, body, _ = self.redeem_ok(code)
        self.assertEqual(status, 200)
        validated = contract.validate_redeem_response(body, HS)
        self.assertEqual(validated["v"], "1")
        self.assertEqual(validated["homeserver"], HS)
        self.assertEqual(validated["gateway"], GW)
        self.assertEqual(validated["auth"]["type"], "registration_token")
        self.assertEqual(validated["auth"]["token"], PLACEHOLDER_TOKEN)
        self.assertEqual(validated["auth"]["expires_in_ms"], AUTH_EXPIRES_IN_MS)
        self.assertEqual(validated["features_url"],
                         GW + "/v1/capabilities")

    def test_chat_only_omits_features_url_and_returns_null_gateway(self):
        code = self.issue(make_code(3), gw=None)
        status, body, _ = self.redeem_ok(code)
        self.assertEqual(status, 200)
        validated = contract.validate_redeem_response(body, HS)
        self.assertIsNone(validated["gateway"])
        self.assertNotIn("features_url", body)
        self.assertEqual(validated["auth"]["type"], "registration_token")

    def test_homeserver_is_stored_value_not_current_config(self):
        issued_hs = "https://old.example.org"
        code = self.issue(make_code(4), hs=issued_hs, gw=None)
        # "Current" config drifting must not rewrite the response.
        self.app.config.synapse_url = "https://new.example.org"
        status, body, _ = self.redeem_ok(code)
        self.assertEqual(status, 200)
        self.assertEqual(body["homeserver"], issued_hs)
        self.assertNotEqual(body["homeserver"], "https://new.example.org")
        with self.assertRaises(contract.PairRedeemError):
            contract.validate_redeem_response(body, HS)

    def test_features_url_uses_stored_gateway_path_prefix(self):
        gw = "https://hs.example.org/_botville/gateway"
        code = self.issue(make_code(5), gw=gw)
        status, body, _ = self.redeem_ok(code)
        self.assertEqual(status, 200)
        self.assertEqual(body["features_url"],
                         gw + "/v1/capabilities")

    def test_login_token_is_not_issued(self):
        with self.assertRaises(ValueError):
            self.store.issue(HS, make_code(6), now=self.now,
                             auth_type="login_token")

    def test_audit_omits_code_and_token(self):
        sentinel_code = make_code(7)
        self.issuer.token = PLACEHOLDER_TOKEN
        self.issue(sentinel_code)
        status, body, _ = self.redeem_ok(sentinel_code)
        self.assertEqual(status, 200)
        self.assertEqual(body["auth"]["token"], PLACEHOLDER_TOKEN)
        blob = json.dumps(self.store.events(), ensure_ascii=False)
        self.assertNotIn(sentinel_code, blob)
        self.assertNotIn(PLACEHOLDER_TOKEN, blob)
        kinds = [event["kind"] for event in self.store.events()]
        self.assertIn("issued", kinds)
        self.assertIn("redeemed", kinds)
        redeemed = [e for e in self.store.events() if e["kind"] == "redeemed"]
        self.assertEqual(redeemed[0]["detail"]["device_display_name"], DEVICE)
        self.assertEqual(redeemed[0]["detail"]["code_sha256"],
                         code_sha256(sentinel_code))


class ErrorCodeTests(PairRedeemTestCase):
    def assert_error(self, status, body, errcode, http=None):
        http = ERROR_STATUS[errcode] if http is None else http
        self.assertEqual(status, http)
        validated = contract.validate_error(body)
        self.assertEqual(validated["errcode"], errcode)
        self.assertEqual(validated["error"], ERROR_TEXT[errcode])

    def test_unknown_code_is_invalid_same_as_bad_format(self):
        unknown = make_code(20)
        status, body, _ = self.redeem_ok(unknown)
        self.assert_error(status, body, "BV_PAIR_INVALID")
        # Format error uses the same errcode and message (no oracle).
        status2, body2, _ = self.post({
            "code": "SHORT",
            "device_display_name": DEVICE,
        })
        self.assert_error(status2, body2, "BV_PAIR_INVALID")
        self.assertEqual(body["error"], body2["error"])

    def test_used_then_replay_is_409(self):
        code = self.issue(make_code(21))
        status, body, _ = self.redeem_ok(code)
        self.assertEqual(status, 200)
        status, body, _ = self.redeem_ok(code)
        self.assert_error(status, body, "BV_PAIR_USED")

    def test_expired_is_410_and_not_consumed(self):
        code = self.issue(make_code(22), ttl=300)
        self.now = self.now + 300  # now >= exp
        status, body, _ = self.redeem_ok(code)
        self.assert_error(status, body, "BV_PAIR_EXPIRED")
        self.assertEqual(self.issuer.calls, 0)
        self.assertEqual(self.store.get(code_sha256(code))["status"], "unused")

    def test_revoked_beats_expired(self):
        code = self.issue(make_code(23), ttl=300)
        self.store.revoke(code_sha256(code), now=self.now)
        self.now = self.now + 301
        status, body, _ = self.redeem_ok(code)
        self.assert_error(status, body, "BV_PAIR_REVOKED")

    def test_used_beats_expired_and_revoked_order(self):
        code = self.issue(make_code(24), ttl=300)
        self.assertEqual(self.redeem_ok(code)[0], 200)
        # Manually age the used row; used must still win over expired.
        self.now = self.now + 301
        status, body, _ = self.redeem_ok(code)
        self.assert_error(status, body, "BV_PAIR_USED")

    def test_missing_and_unknown_fields_and_oversize_and_content_type(self):
        # This case hammers the endpoint with many invalid bodies; keep
        # rate-limit tests separate so structure errors stay 400.
        self.app.ip_limiter._limit = 100
        self.app.global_limiter._limit = 100
        cases = [
            ({"code": make_code(25)}, "application/json"),
            ({"device_display_name": DEVICE}, "application/json"),
            ({"code": make_code(25), "device_display_name": DEVICE,
              "extra": "nope"}, "application/json"),
            ({"code": make_code(25), "device_display_name": ""},
             "application/json"),
            ({"code": make_code(25), "device_display_name": "x" * 129},
             "application/json"),
            ({"code": make_code(25), "device_display_name": DEVICE},
             "text/plain"),
            ({"code": make_code(25), "device_display_name": DEVICE},
             "application/xml"),
        ]
        for body, ctype in cases:
            status, payload, _ = self.post(body, content_type=ctype)
            self.assertEqual(status, 400, msg=(body, ctype, payload))
            self.assertEqual(payload["errcode"], "BV_PAIR_INVALID")

        status, payload, _ = self.post(
            {"code": make_code(25), "device_display_name": DEVICE},
            content_type="application/json; charset=utf-8",
        )
        # charset is ignored; but the code is unknown -> still 400 invalid
        self.assertEqual(status, 400)
        self.assertEqual(payload["errcode"], "BV_PAIR_INVALID")

        status, payload, _ = self.post(
            None, raw=b"{", content_type="application/json")
        self.assertEqual(status, 400)
        self.assertEqual(payload["errcode"], "BV_PAIR_INVALID")

        status, payload, _ = self.post(
            None, raw=b"[1]", content_type="application/json")
        self.assertEqual(status, 400)
        self.assertEqual(payload["errcode"], "BV_PAIR_INVALID")

        huge = b"{" + b"a" * (MAX_BODY_BYTES + 8) + b"}"
        status, payload, _ = self.post(None, raw=huge,
                                       content_type="application/json")
        self.assertEqual(status, 400)
        self.assertEqual(payload["errcode"], "BV_PAIR_INVALID")

    def test_non_post_is_405_without_pairing_error_requirement(self):
        request = urllib.request.Request(self.base + REDEEM_PATH, method="GET")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 405)

    def test_other_path_is_404(self):
        status, payload, _ = self.post(
            {"code": make_code(26), "device_display_name": DEVICE},
            path="/v1/pairing/redeem")
        self.assertEqual(status, 404)

    def test_rate_limited_has_retry_after_ms(self):
        self.app.ip_limiter._limit = 2
        code = self.issue(make_code(27))
        self.assertEqual(self.redeem_ok(code)[0], 200)
        # Two more attempts from the same IP (one replay + one unknown) trip the cap.
        self.assertEqual(self.redeem_ok(code)[0], 409)
        status, body, headers = self.redeem_ok(make_code(28))
        self.assertEqual(status, 429)
        validated = contract.validate_error(body)
        self.assertEqual(validated["errcode"], "BV_PAIR_RATE_LIMITED")
        self.assertGreaterEqual(validated["retry_after_ms"], 1)
        retry_header = headers.get("Retry-After") or headers.get("retry-after")
        if validated["retry_after_ms"] < 1000:
            self.assertIsNone(retry_header)
        else:
            self.assertIsNotNone(retry_header)
            self.assertEqual(int(retry_header),
                             validated["retry_after_ms"] // 1000)
        kinds = [e["kind"] for e in self.store.events()]
        self.assertIn("rate_limited", kinds)

    def test_retry_after_header_absent_when_under_one_second(self):
        self.app.ip_limiter._limit = 1
        self.app.ip_limiter._window = 0.4
        self.app.global_limiter._limit = 100
        self.issue(make_code(70))
        self.assertEqual(self.redeem_ok(make_code(70))[0], 200)
        status, body, headers = self.redeem_ok(make_code(71))
        self.assertEqual(status, 429)
        validated = contract.validate_error(body)
        self.assertLess(validated["retry_after_ms"], 1000)
        retry_header = headers.get("Retry-After") or headers.get("retry-after")
        self.assertIsNone(retry_header)

    def test_xff_ignored_unless_peer_is_trusted(self):
        self.app.ip_limiter._limit = 2
        # Direct client is the test process (127.0.0.1), not a trusted proxy.
        self.app.config.trusted_proxies = []
        self.issue(make_code(29))
        headers = {"X-Forwarded-For": "203.0.113.9"}
        self.assertEqual(self.post(
            {"code": make_code(29), "device_display_name": DEVICE},
            extra_headers=headers)[0], 200)
        self.assertEqual(self.post(
            {"code": make_code(29), "device_display_name": DEVICE},
            extra_headers=headers)[0], 409)
        status, body, _ = self.post(
            {"code": make_code(30), "device_display_name": DEVICE},
            extra_headers=headers)
        self.assertEqual(status, 429)
        self.assertEqual(body["errcode"], "BV_PAIR_RATE_LIMITED")


class AtomicityTests(PairRedeemTestCase):
    def test_issuer_failure_does_not_consume_code(self):
        code = self.issue(make_code(40))
        self.issuer.fail = True
        status, body, _ = self.redeem_ok(code)
        self.assertEqual(status, 503)
        self.assertIsInstance(body, dict)
        self.assertNotIn("errcode", body)
        self.assertNotIn("auth", body)
        rejected = [e for e in self.store.events(kind="rejected")
                    if e["detail"].get("reason") == "issuer_failed"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(self.store.get(code_sha256(code))["status"], "unused")
        self.issuer.fail = False
        status, body, _ = self.redeem_ok(code)
        self.assertEqual(status, 200)
        self.assertEqual(body["auth"]["type"], "registration_token")

    def test_issuer_error_text_has_no_body(self):
        exc = IssuerError("http_unavailable")
        self.assertEqual(str(exc), "http_unavailable")
        self.assertNotIn("token", str(exc).lower())

    def test_concurrent_redeem_only_one_succeeds(self):
        # Exercise the sqlite transaction directly: HTTP backlog on a
        # loopback ThreadingHTTPServer is not the atomicity contract.
        code = self.issue(make_code(41))
        results = []
        lock = threading.Lock()

        def worker():
            try:
                self.app.redeem(code, DEVICE, "127.0.0.1")
                outcome = "ok"
            except PairRedeemError as exc:
                outcome = exc.errcode
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(len(results), 8)
        self.assertEqual(results.count("ok"), 1)
        self.assertEqual(results.count("BV_PAIR_USED"), 7)
        self.assertEqual(self.issuer.calls, 1)
        self.assertEqual(self.store.get(code_sha256(code))["status"], "used")


class IssuerHttpTests(unittest.TestCase):
    def test_live_issuer_parses_admin_response_and_hides_body_on_error(self):
        seen = {}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                seen["path"] = self.path
                seen["auth"] = self.headers.get("Authorization")
                seen["body"] = json.loads(self.rfile.read(length).decode("utf-8"))
                raw = json.dumps({"token": PLACEHOLDER_TOKEN,
                                  "uses_allowed": 1}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            issuer = SynapseRegistrationTokenIssuer(
                "http://127.0.0.1:%s" % server.server_address[1],
                "fixture-admin-token-not-a-secret-example",
            )
            auth = issuer.issue(AUTH_EXPIRES_IN_MS, now=1_800_000_000)
            self.assertEqual(auth["type"], "registration_token")
            self.assertEqual(auth["token"], PLACEHOLDER_TOKEN)
            self.assertEqual(seen["path"],
                             "/_synapse/admin/v1/registration_tokens/new")
            self.assertEqual(seen["body"]["uses_allowed"], 1)
            self.assertEqual(seen["body"]["expiry_time"],
                             1_800_000_000 * 1000 + AUTH_EXPIRES_IN_MS)
            self.assertNotIn("device_display_name", seen["body"])
            self.assertNotIn("code", seen["body"])
            self.assertEqual(set(seen["body"]), {"uses_allowed", "expiry_time"})
        finally:
            server.shutdown()
            server.server_close()

    def test_live_issuer_maps_http_error_without_body_text(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                raw = b'{"error":"SENTINEL-BODY-SHOULD-NOT-ESCAPE"}'
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            issuer = SynapseRegistrationTokenIssuer(
                "http://127.0.0.1:%s" % server.server_address[1],
                "fixture-admin-token-not-a-secret-example",
            )
            with self.assertRaises(IssuerError) as caught:
                issuer.issue(1000, now=1_800_000_000)
            self.assertEqual(str(caught.exception), "unusable_result")
            self.assertNotIn("SENTINEL-BODY-SHOULD-NOT-ESCAPE",
                             str(caught.exception))
        finally:
            server.shutdown()
            server.server_close()


class IsolationAndConfigTests(unittest.TestCase):
    def test_package_does_not_import_federation_redeem(self):
        self.assertFalse(hasattr(redeem_service, "redeem_invite"))
        source = Path(redeem_service.__file__).read_text(encoding="utf-8")
        self.assertNotIn("v1/pairing/redeem", source)
        self.assertNotIn("pairing.service", source)
        self.assertNotIn("redeem_invite", source)
        store_src = Path(SERVER_DIR / "pair_redeem" / "store.py").read_text(encoding="utf-8")
        self.assertNotIn("redeem_invite", store_src)

    def test_trusted_proxy_uses_last_xff_only(self):
        from ipaddress import ip_network
        proxies = [ip_network("127.0.0.1/32")]
        self.assertEqual(
            client_ip("127.0.0.1",
                      {"X-Forwarded-For": "203.0.113.9, 198.51.100.2"},
                      proxies),
            "198.51.100.2")
        # Untrusted direct peer: ignore spoofed XFF.
        self.assertEqual(
            client_ip("192.0.2.1",
                      {"X-Forwarded-For": "203.0.113.9"},
                      proxies),
            "192.0.2.1")
        # Trusted but empty/malformed header falls back to the direct peer.
        self.assertEqual(
            client_ip("127.0.0.1", {"X-Forwarded-For": ""}, proxies),
            "127.0.0.1")

    def test_client_ip_unpacks_ipv4_mapped(self):
        from ipaddress import ip_network
        proxies = [ip_network("127.0.0.1/32")]
        self.assertEqual(
            client_ip("::ffff:127.0.0.1",
                      {"X-Forwarded-For": "198.51.100.2"},
                      proxies),
            "198.51.100.2")
        self.assertEqual(
            client_ip("::ffff:192.0.2.1",
                      {"X-Forwarded-For": "203.0.113.9"},
                      proxies),
            "192.0.2.1")

    def test_config_env_keys_and_defaults(self):
        config = PairRedeemConfig.from_env({
            ENV_BIND: "127.0.0.1:0",
            ENV_IP_LIMIT: "10",
            ENV_GLOBAL_LIMIT: "60",
            ENV_TRUSTED_PROXIES: "10.0.0.1/32, 192.0.2.0/24",
        })
        self.assertEqual(config.ip_limit, 10)
        self.assertEqual(config.global_limit, 60)
        self.assertEqual(config.auth_expires_in_ms, AUTH_EXPIRES_IN_MS)
        self.assertEqual(config.issuer_timeout, ISSUER_TIMEOUT_SECONDS)
        self.assertEqual(config.bind_host, "127.0.0.1")
        self.assertEqual(len(config.trusted_proxies), 2)
        config = PairRedeemConfig.from_env({
            ENV_BIND: "127.0.0.1:0",
            ENV_ISSUER_TIMEOUT: "8",
        })
        self.assertEqual(config.issuer_timeout, 8)
        with self.assertRaises(ConfigError):
            PairRedeemConfig.from_env({ENV_TRUSTED_PROXIES: "not-an-ip"})

    def test_parse_redeem_request_unicode_length(self):
        code = make_code(50)
        name = "设备" * 64  # 128 Unicode characters
        parsed_code, parsed_name = parse_redeem_request(
            json.dumps({"code": code, "device_display_name": name}).encode(),
            "application/json")
        self.assertEqual(parsed_name, name)
        with self.assertRaises(PairRedeemError):
            parse_redeem_request(
                json.dumps({"code": code,
                            "device_display_name": name + "超"}).encode(),
                "application/json")
        # Lone surrogates must be rejected at parse time (r2 §3.3).
        with self.assertRaises(PairRedeemError):
            parse_redeem_request(
                json.dumps({"code": code,
                            "device_display_name": "\ud800"}).encode(
                                "utf-8"),
                "application/json")


def _http_header_map(header_text):
    headers = {}
    for line in header_text.split("\r\n")[1:]:
        if not line or ":" not in line:
            continue
        name, value = line.split(":", 1)
        headers[name.strip().lower()] = value.strip()
    return headers


def read_http_response(sock):
    """Read one HTTP response from an already-connected socket."""
    chunks = []
    body = b""
    header_text = ""
    while True:
        piece = sock.recv(8192)
        if not piece:
            break
        chunks.append(piece)
        blob = b"".join(chunks)
        if b"\r\n\r\n" not in blob:
            continue
        raw_header, rest = blob.split(b"\r\n\r\n", 1)
        header_text = raw_header.decode("iso-8859-1")
        headers = _http_header_map(header_text)
        length = int(headers.get("content-length", "0") or 0)
        if len(rest) >= length:
            body = rest[:length]
            break
    return header_text, body


def raw_http(host, port, request, timeout=2.0):
    """Send one raw request and read one HTTP response; leave the socket open."""
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.sendall(request)
    header_text, body = read_http_response(sock)
    return header_text, body, sock


class ReviewFixTests(PairRedeemTestCase):
    def test_parse_failures_write_rejected_audit(self):
        # pairing-qr.md §4.6: illegal requests are audited as rejected (A1).
        before = len(self.store.events(kind="rejected"))
        cases = [
            (b"{", "application/json"),
            (b"[1]", "application/json"),
            (json.dumps({"code": make_code(80),
                         "device_display_name": DEVICE,
                         "extra": "nope"}).encode(), "application/json"),
            (json.dumps({"code": "SHORT",
                         "device_display_name": DEVICE}).encode(),
             "application/json"),
            (json.dumps({"code": make_code(80),
                         "device_display_name": DEVICE}).encode(),
             "text/plain"),
        ]
        for raw, ctype in cases:
            status, body, _ = self.post(None, raw=raw, content_type=ctype)
            self.assertEqual(status, 400, msg=(ctype, body))
            self.assertEqual(body["errcode"], "BV_PAIR_INVALID")
        rejected = self.store.events(kind="rejected")
        self.assertEqual(len(rejected), before + len(cases))
        self.assertTrue(all(e["detail"]["reason"] == "invalid_request"
                            for e in rejected[before:]))

    def test_global_layer_http_429(self):
        self.app.ip_limiter._limit = 100
        self.app.global_limiter._limit = 2
        self.issue(make_code(81))
        self.assertEqual(self.redeem_ok(make_code(81))[0], 200)
        self.assertEqual(self.redeem_ok(make_code(81))[0], 409)
        status, body, headers = self.redeem_ok(make_code(82))
        self.assertEqual(status, 429)
        validated = contract.validate_error(body)
        self.assertEqual(validated["errcode"], "BV_PAIR_RATE_LIMITED")
        self.assertGreaterEqual(validated["retry_after_ms"], 1)
        if validated["retry_after_ms"] < 1000:
            self.assertIsNone(headers.get("Retry-After") or headers.get("retry-after"))
        else:
            self.assertEqual(
                int(headers.get("Retry-After") or headers.get("retry-after")),
                validated["retry_after_ms"] // 1000)

    def test_both_layers_retry_after_is_max(self):
        now = time.monotonic()
        self.app.ip_limiter._limit = 1
        self.app.global_limiter._limit = 1
        self.app.ip_limiter._window = 1.0
        self.app.global_limiter._window = 5.0
        self.app.ip_limiter.commit("203.0.113.9", now=now - 0.4)
        self.app.global_limiter.commit("global", now=now - 0.1)
        with self.assertRaises(PairRedeemError) as caught:
            self.app.check_rate_limit("203.0.113.9", now_mono=now)
        # IP would allow in ~0.6s, global in ~4.9s; earliest both-clear is max.
        self.assertEqual(caught.exception.errcode, "BV_PAIR_RATE_LIMITED")
        self.assertGreaterEqual(caught.exception.retry_after_ms, 4000)
        self.assertLess(caught.exception.retry_after_ms, 5500)

    def test_trusted_proxy_xff_is_honoured_over_http(self):
        self.app.config.trusted_proxies = [ip_network("127.0.0.1/32")]
        self.app.ip_limiter._limit = 2
        self.app.global_limiter._limit = 100
        self.issue(make_code(83))
        spoofed = {"X-Forwarded-For": "203.0.113.9"}
        other = {"X-Forwarded-For": "198.51.100.2"}
        self.assertEqual(self.post(
            {"code": make_code(83), "device_display_name": DEVICE},
            extra_headers=spoofed)[0], 200)
        self.assertEqual(self.post(
            {"code": make_code(83), "device_display_name": DEVICE},
            extra_headers=spoofed)[0], 409)
        status, body, _ = self.post(
            {"code": make_code(84), "device_display_name": DEVICE},
            extra_headers=spoofed)
        self.assertEqual(status, 429)
        self.assertEqual(body["errcode"], "BV_PAIR_RATE_LIMITED")
        # A different forwarded IP still has budget.
        status, body, _ = self.post(
            {"code": make_code(85), "device_display_name": DEVICE},
            extra_headers=other)
        self.assertEqual(status, 400)
        self.assertEqual(body["errcode"], "BV_PAIR_INVALID")

    def test_rate_limited_audit_is_throttled_per_ip(self):
        self.app.ip_limiter._limit = 1
        self.app.global_limiter._limit = 100
        self.issue(make_code(86))
        self.assertEqual(self.redeem_ok(make_code(86))[0], 200)
        for _ in range(5):
            self.assertEqual(self.redeem_ok(make_code(87))[0], 429)
        limited = self.store.events(kind="rate_limited")
        self.assertEqual(len(limited), 1)
        # In-window repeats are counted in memory and not written yet.
        self.assertEqual(limited[0]["detail"]["suppressed"], 0)
        self.assertEqual(self.app._rate_audit["127.0.0.1"]["suppressed"], 4)
        # Rolling the window flushes the previous count and starts at 0.
        self.now = self.now + self.config.rate_window + 1
        self.app.ip_limiter._hits.clear()
        self.app.global_limiter._hits.clear()
        self.issue(make_code(87))
        self.assertEqual(self.redeem_ok(make_code(87))[0], 200)
        self.assertEqual(self.redeem_ok(make_code(93))[0], 429)
        limited = self.store.events(kind="rate_limited")
        self.assertEqual(len(limited), 2)
        self.assertEqual(limited[0]["detail"]["suppressed"], 4)
        self.assertEqual(limited[1]["detail"]["suppressed"], 0)

    def test_pruner_runs_at_start_and_deletes_old_rows(self):
        code = self.issue(make_code(88))
        self.assertEqual(self.redeem_ok(code)[0], 200)
        self.assertTrue(self.store.events())
        # Advance the clock past the 30-day retention, then start the pruner.
        self.now = self.now + 31 * 86400
        stop = threading.Event()
        start_pruner(self.app, interval=60, stop=stop)
        deadline = time.time() + 2
        while time.time() < deadline:
            if not self.store.events() and self.store.get(code_sha256(code)) is None:
                break
            time.sleep(0.02)
        stop.set()
        self.assertEqual(self.store.events(), [])
        self.assertIsNone(self.store.get(code_sha256(code)))

    def test_issue_rejects_non_https_empty_gateway_and_bad_code(self):
        with self.assertRaises(ValueError):
            self.store.issue("http://hs.example.org", make_code(89), now=self.now)
        with self.assertRaises(ValueError):
            self.store.issue(HS, make_code(89), gw="", now=self.now)
        with self.assertRaises(ValueError):
            self.store.issue(HS, "SHORT", now=self.now)
        with self.assertRaises(ValueError):
            self.store.issue(HS, make_code(89), gw="http://gw.example.org",
                             now=self.now)

    def test_oversized_keepalive_body_closes_and_does_not_poison(self):
        code = self.issue(make_code(90))
        host, port = "127.0.0.1", self.server.server_address[1]
        payload = json.dumps({"code": code, "device_display_name": DEVICE}).encode("utf-8")
        leftover = (
            "POST " + REDEEM_PATH + " HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: %d\r\n"
            "Connection: keep-alive\r\n"
            "\r\n"
        ).encode("ascii") % len(payload) + payload
        first = (
            "POST " + REDEEM_PATH + " HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: 70000\r\n"
            "Connection: keep-alive\r\n"
            "\r\n"
        ).encode("ascii") + leftover
        header, body, sock = raw_http(host, port, first)
        try:
            self.assertTrue(header.startswith("HTTP/1.1 400"), header)
            headers = _http_header_map(header)
            self.assertEqual(headers.get("connection"), "close")
            err = json.loads(body.decode("utf-8"))
            self.assertEqual(err["errcode"], "BV_PAIR_INVALID")
            sock.settimeout(0.4)
            try:
                rest = sock.recv(1024)
            except socket.timeout:
                rest = b""
            self.assertFalse(rest)
            self.assertEqual(self.store.get(code_sha256(code))["status"], "unused")
        finally:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        status, body, _ = self.redeem_ok(code)
        self.assertEqual(status, 200)

    def test_transfer_encoding_is_400_and_closes(self):
        host, port = "127.0.0.1", self.server.server_address[1]
        request = (
            "POST " + REDEEM_PATH + " HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            "Content-Type: application/json\r\n"
            "Transfer-Encoding: chunked\r\n"
            "Connection: keep-alive\r\n"
            "\r\n"
            "2\r\n[]\r\n0\r\n\r\n"
        ).encode("ascii")
        header, body, sock = raw_http(host, port, request)
        try:
            self.assertTrue(header.startswith("HTTP/1.1 400"), header)
            headers = _http_header_map(header)
            self.assertEqual(headers.get("connection"), "close")
            err = json.loads(body.decode("utf-8"))
            self.assertEqual(err["errcode"], "BV_PAIR_INVALID")
        finally:
            sock.close()

    def test_rate_limited_keepalive_closes_and_does_not_poison(self):
        # r2 S1: a 429 used to leave the unread body on a keep-alive
        # connection, so a smuggled follow-up (including a forged
        # X-Forwarded-For) could be parsed as the next request.
        self.app.config.trusted_proxies = [ip_network("127.0.0.1/32")]
        self.app.ip_limiter._limit = 1
        self.app.global_limiter._limit = 100
        first_code = self.issue(make_code(91))
        smuggled_code = self.issue(make_code(92))
        host, port = "127.0.0.1", self.server.server_address[1]
        first_payload = json.dumps({
            "code": first_code, "device_display_name": DEVICE,
        }).encode("utf-8")
        first = (
            "POST " + REDEEM_PATH + " HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: %d\r\n"
            "Connection: keep-alive\r\n"
            "\r\n"
        ).encode("ascii") % len(first_payload) + first_payload
        smuggled_payload = json.dumps({
            "code": smuggled_code, "device_display_name": DEVICE,
        }).encode("utf-8")
        smuggled = (
            "POST " + REDEEM_PATH + " HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: %d\r\n"
            "X-Forwarded-For: 203.0.113.99\r\n"
            "Connection: keep-alive\r\n"
            "\r\n"
        ).encode("ascii") % len(smuggled_payload) + smuggled_payload
        limited = (
            "POST " + REDEEM_PATH + " HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: %d\r\n"
            "Connection: keep-alive\r\n"
            "\r\n"
        ).encode("ascii") % len(smuggled) + smuggled
        header, body, sock = raw_http(host, port, first)
        try:
            self.assertTrue(header.startswith("HTTP/1.1 200"), header)
            sock.sendall(limited)
            header2, body2 = read_http_response(sock)
            self.assertTrue(header2.startswith("HTTP/1.1 429"), header2)
            headers = _http_header_map(header2)
            self.assertEqual(headers.get("connection"), "close")
            err = json.loads(body2.decode("utf-8"))
            self.assertEqual(err["errcode"], "BV_PAIR_RATE_LIMITED")
            sock.settimeout(0.4)
            try:
                rest = sock.recv(1024)
            except socket.timeout:
                rest = b""
            self.assertFalse(rest)
            self.assertEqual(
                self.store.get(code_sha256(smuggled_code))["status"], "unused")
        finally:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        # Same loopback IP is still at the per-IP cap from the first
        # redeem; clear the buckets so a fresh connection can prove the
        # smuggled code was not consumed.
        self.app.ip_limiter._hits.clear()
        self.app.global_limiter._hits.clear()
        status, body, _ = self.redeem_ok(smuggled_code)
        self.assertEqual(status, 200)

    def test_duplicate_content_length_is_400_and_closes(self):
        host, port = "127.0.0.1", self.server.server_address[1]
        payload = b"{}"
        request = (
            "POST " + REDEEM_PATH + " HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: %d\r\n"
            "Content-Length: %d\r\n"
            "Connection: keep-alive\r\n"
            "\r\n"
        ).encode("ascii") % (len(payload), len(payload)) + payload
        header, body, sock = raw_http(host, port, request)
        try:
            self.assertTrue(header.startswith("HTTP/1.1 400"), header)
            headers = _http_header_map(header)
            self.assertEqual(headers.get("connection"), "close")
            err = json.loads(body.decode("utf-8"))
            self.assertEqual(err["errcode"], "BV_PAIR_INVALID")
        finally:
            sock.close()

    def test_patch_and_options_are_405(self):
        host, port = "127.0.0.1", self.server.server_address[1]
        for method in ("PATCH", "OPTIONS", "HEAD"):
            request = (
                "%s %s HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
                % (method, REDEEM_PATH)
            ).encode("ascii")
            header, body, sock = raw_http(host, port, request)
            sock.close()
            self.assertTrue(header.startswith("HTTP/1.1 405"), (method, header))


class TimeoutTests(unittest.TestCase):
    def setUp(self):
        self.store = PairRedeemStore(":memory:")
        self.issuer = FakeIssuer()
        self.now = 1_800_000_000
        self.config = PairRedeemConfig(
            ip_limit=10, global_limit=60, bind_host="127.0.0.1", bind_port=0,
            request_timeout=0.3,
        )
        self.app = PairRedeemApp(
            self.store, self.issuer, self.config, clock=lambda: self.now)
        self.server = self.app.make_server("127.0.0.1", 0)
        self._thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True)
        self._thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.close()

    def test_incomplete_body_times_out_and_closes(self):
        host, port = "127.0.0.1", self.server.server_address[1]
        request = (
            "POST " + REDEEM_PATH + " HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: 40\r\n"
            "Connection: keep-alive\r\n"
            "\r\n"
            "{"
        ).encode("ascii")
        sock = socket.create_connection((host, port), timeout=2)
        try:
            sock.sendall(request)
            sock.settimeout(3.0)
            data = b""
            while True:
                try:
                    piece = sock.recv(4096)
                except socket.timeout:
                    piece = b""
                if not piece:
                    break
                data += piece
            # Connection closed after the handler timeout; may or may not
            # include a 400 if the timeout landed mid-read.
            if data:
                self.assertIn(b"Connection: close", data)
        finally:
            sock.close()





class HealthAndBindTests(PairRedeemTestCase):
    def test_health_is_200_and_other_gets_stay_405(self):
        url = self.base + "/health"
        with urllib.request.urlopen(url, timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"ok")
        status, body, _ = self.post(
            {"code": make_code(1), "device_display_name": DEVICE})
        # POST still works after a health check.
        self.assertIn(status, (400, 429, 200))
        host, port = "127.0.0.1", self.server.server_address[1]
        header, _, sock = raw_http(
            host, port,
            ("GET %s HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
             % REDEEM_PATH).encode("ascii"))
        sock.close()
        self.assertTrue(header.startswith("HTTP/1.1 405"), header)


class AdminUrlAndTokenTests(unittest.TestCase):
    def test_allow_bind_host(self):
        self.assertTrue(allow_bind_host("127.0.0.1"))
        self.assertTrue(allow_bind_host("localhost"))
        self.assertTrue(allow_bind_host("0.0.0.0"))
        self.assertTrue(allow_bind_host("10.0.0.4"))
        self.assertTrue(allow_bind_host("172.18.0.2"))
        self.assertTrue(allow_bind_host("::1"))
        self.assertTrue(allow_bind_host("::"))
        self.assertTrue(allow_bind_host("::ffff:10.0.0.4"))
        self.assertFalse(allow_bind_host("8.8.8.8"))
        self.assertFalse(allow_bind_host("example.org"))
        self.assertFalse(allow_bind_host("169.254.169.254"))
        self.assertFalse(allow_bind_host("::ffff:8.8.8.8"))
        self.assertFalse(allow_bind_host("::ffff:169.254.169.254"))

    def test_validate_http_url_allowlist(self):
        self.assertEqual(
            validate_synapse_admin_url("http://127.0.0.1:8008/"),
            "http://127.0.0.1:8008")
        self.assertEqual(
            validate_synapse_admin_url("http://synapse:8008"),
            "http://synapse:8008")
        self.assertEqual(
            validate_synapse_admin_url("https://hs.example.org"),
            "https://hs.example.org")
        with self.assertRaises(ConfigError):
            validate_synapse_admin_url("http://example.org")
        with self.assertRaises(ConfigError):
            validate_synapse_admin_url("http://8.8.8.8")
        with self.assertRaises(ConfigError):
            validate_synapse_admin_url("http://169.254.169.254")
        with self.assertRaises(ConfigError):
            validate_synapse_admin_url("http://[::ffff:8.8.8.8]:8008")
        with self.assertRaises(ConfigError):
            validate_synapse_admin_url("http://[::ffff:169.254.169.254]")
        with self.assertRaises(ConfigError):
            validate_synapse_admin_url("http://user:pass@127.0.0.1:8008")
        with self.assertRaises(ConfigError):
            validate_synapse_admin_url("")
        with self.assertRaises(ConfigError):
            validate_synapse_admin_url("ftp://127.0.0.1")

    def test_require_admin_token_rejects_blank_and_hides_value(self):
        self.assertEqual(require_admin_token("ok-token"), "ok-token")
        with self.assertRaises(ConfigError) as caught:
            require_admin_token("")
        self.assertNotIn("ok-token", str(caught.exception))
        with self.assertRaises(ConfigError) as caught:
            require_admin_token("has space")
        self.assertNotIn("has space", str(caught.exception))

    def test_issuer_rejects_http_public_url_before_network(self):
        with self.assertRaises(ConfigError):
            SynapseRegistrationTokenIssuer(
                "http://example.org", "fixture-admin-token-not-a-secret")

    def test_issuer_refuses_redirect_and_hides_token(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                self.send_response(302)
                self.send_header("Location", "https://evil.example.org/steal")
                self.send_header("Content-Length", "0")
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True)
        thread.start()
        token = "fixture-admin-token-SHOULD-NOT-LEAK"
        try:
            issuer = SynapseRegistrationTokenIssuer(
                "http://127.0.0.1:%s" % server.server_address[1],
                token)
            with self.assertRaises(IssuerError) as caught:
                issuer.issue(1000, now=1_800_000_000)
            self.assertEqual(str(caught.exception), "redirect_refused")
            self.assertNotIn(token, str(caught.exception))
            self.assertNotIn("evil.example.org", str(caught.exception))
        finally:
            server.shutdown()
            server.server_close()

    def test_issuer_pins_resolved_ip_and_keeps_host_header(self):
        seen = {}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                seen["host"] = self.headers.get("Host")
                body = b'{"token":"pinned-reg-token"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True)
        thread.start()
        port = server.server_address[1]
        alias = "synapse-e2e"
        real_getaddrinfo = socket.getaddrinfo

        def fake_getaddrinfo(host, svc, *args, **kwargs):
            if host == alias:
                return real_getaddrinfo("127.0.0.1", svc, *args, **kwargs)
            return real_getaddrinfo(host, svc, *args, **kwargs)

        try:
            socket.getaddrinfo = fake_getaddrinfo
            issuer = SynapseRegistrationTokenIssuer(
                "http://%s:%s" % (alias, port),
                "fixture-admin-token-not-a-secret")
            result = issuer.issue(1000, now=1_800_000_000)
            self.assertEqual(result["token"], "pinned-reg-token")
            self.assertTrue(
                seen.get("host") == alias or
                seen.get("host") == "%s:%s" % (alias, port),
                seen)
        finally:
            socket.getaddrinfo = real_getaddrinfo
            server.shutdown()
            server.server_close()

    def test_issuer_parses_chunked_admin_response(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                body = b'{"token":"chunked-reg-token"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                # Two chunks so a raw fp.read would not be valid JSON.
                self.wfile.write(b"%x\r\n" % 10)
                self.wfile.write(body[:10])
                self.wfile.write(b"\r\n%x\r\n" % (len(body) - 10))
                self.wfile.write(body[10:])
                self.wfile.write(b"\r\n0\r\n\r\n")

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True)
        thread.start()
        try:
            issuer = SynapseRegistrationTokenIssuer(
                "http://127.0.0.1:%s" % server.server_address[1],
                "fixture-admin-token-not-a-secret")
            result = issuer.issue(1000, now=1_800_000_000)
            self.assertEqual(result["token"], "chunked-reg-token")
        finally:
            server.shutdown()
            server.server_close()

    def test_issuer_total_deadline_bounds_slow_drip(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                payload = (
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: 20\r\n"
                    b"\r\n"
                    b'{"token":"0123456"}'
                )
                try:
                    for idx in range(len(payload)):
                        self.wfile.write(payload[idx:idx + 1])
                        self.wfile.flush()
                        time.sleep(0.8)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True)
        thread.start()
        try:
            issuer = SynapseRegistrationTokenIssuer(
                "http://127.0.0.1:%s" % server.server_address[1],
                "fixture-admin-token-not-a-secret",
                timeout=2)
            started = time.monotonic()
            with self.assertRaises(IssuerError):
                issuer.issue(1000, now=1_800_000_000)
            self.assertLess(time.monotonic() - started, 5.0)
        finally:
            server.shutdown()
            server.server_close()

    def test_issuer_rejects_http_mapped_public_at_resolve(self):
        real_getaddrinfo = socket.getaddrinfo

        def fake_getaddrinfo(host, svc, *args, **kwargs):
            if host == "synapse":
                return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "",
                         ("::ffff:8.8.8.8", 8008, 0, 0))]
            return real_getaddrinfo(host, svc, *args, **kwargs)

        try:
            socket.getaddrinfo = fake_getaddrinfo
            issuer = SynapseRegistrationTokenIssuer(
                "http://synapse:8008", "fixture-admin-token-not-a-secret")
            with self.assertRaises(IssuerError) as caught:
                issuer.issue(1000, now=1_800_000_000)
            self.assertEqual(str(caught.exception), "untrusted_destination")
        finally:
            socket.getaddrinfo = real_getaddrinfo

    def test_ipv6_loopback_bind_starts(self):
        store = PairRedeemStore(":memory:")
        try:
            app = PairRedeemApp(store, FakeIssuer(), PairRedeemConfig(
                ip_limit=10, global_limit=60, bind_host="::1", bind_port=0))
            try:
                server = app.make_server("::1", 0)
            except OSError as exc:
                self.skipTest("IPv6 loopback not available: %s" % exc)
            thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.05},
                daemon=True)
            thread.start()
            try:
                self.assertEqual(server.address_family, socket.AF_INET6)
                host, port = server.server_address[:2]
                sock = socket.create_connection((host, port), timeout=2)
                sock.sendall(
                    b"GET /health HTTP/1.1\r\nHost: localhost\r\n"
                    b"Connection: close\r\n\r\n")
                data = b""
                while True:
                    piece = sock.recv(4096)
                    if not piece:
                        break
                    data += piece
                sock.close()
                self.assertTrue(data.startswith(b"HTTP/1.1 200"), data)
            finally:
                server.shutdown()
                server.server_close()
        finally:
            store.close()


class DeadlineAndConcurrencyTests(unittest.TestCase):
    def _serve(self, **config_kw):
        store = PairRedeemStore(":memory:")
        issuer = FakeIssuer()
        now = 1_800_000_000
        config = PairRedeemConfig(
            ip_limit=100, global_limit=100, bind_host="127.0.0.1",
            bind_port=0, **config_kw)
        app = PairRedeemApp(store, issuer, config, clock=lambda: now)
        server = app.make_server("127.0.0.1", 0)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True)
        thread.start()
        return store, server

    def test_total_deadline_bounds_slow_drip(self):
        header = (
            "POST " + REDEEM_PATH + " HTTP/1.1\r\n"
            "Host: localhost\r\nContent-Type: application/json\r\n"
            "Content-Length: 40\r\nConnection: keep-alive\r\n\r\n"
        ).encode("ascii")
        self._slow_drip_and_expect_timeout([header] + [b"{"] * 8)

    def test_over_capacity_returns_503_and_closes(self):
        store, server = self._serve(max_concurrent=1, request_timeout=5,
                                    request_deadline=5)
        host, port = "127.0.0.1", server.server_address[1]
        holder = socket.create_connection((host, port), timeout=5)
        try:
            holder.sendall((
                "POST " + REDEEM_PATH + " HTTP/1.1\r\n"
                "Host: 127.0.0.1\r\n"
                "Content-Type: application/json\r\n"
                "Content-Length: 40\r\n"
                "Connection: keep-alive\r\n"
                "\r\n"
                "{"
            ).encode("ascii"))
            self.assertTrue(
                server.handler_started.wait(2),
                "holder must occupy the concurrency slot before the probe")
            header, body, sock = raw_http(
                host, port,
                ("GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                 "Connection: close\r\n\r\n").encode("ascii"),
                timeout=2)
            try:
                self.assertTrue(header.startswith("HTTP/1.1 503"), header)
                headers = _http_header_map(header)
                self.assertEqual(headers.get("connection"), "close")
                err = json.loads(body.decode("utf-8"))
                self.assertNotIn("errcode", err)
                self.assertEqual(err.get("error"), "temporarily unavailable")
            finally:
                sock.close()
        finally:
            holder.close()
            server.shutdown()
            server.server_close()
            store.close()

    def _slow_drip_and_expect_timeout(self, chunks, request_deadline=1,
                                      per_byte=0.4):
        store, server = self._serve(request_timeout=5,
                                    request_deadline=request_deadline,
                                    max_concurrent=1)
        host, port = "127.0.0.1", server.server_address[1]
        sock = socket.create_connection((host, port), timeout=5)
        try:
            for chunk in chunks:
                try:
                    sock.sendall(chunk)
                except OSError:
                    break
                time.sleep(per_byte)
            sock.settimeout(2)
            data = b""
            closed = False
            while True:
                try:
                    piece = sock.recv(4096)
                except socket.timeout:
                    self.fail("slow-drip connection was not closed")
                except ConnectionResetError:
                    closed = True
                    piece = b""
                if not piece:
                    closed = True
                    break
                data += piece
            self.assertTrue(closed, "deadline must close the connection")
            if data:
                self.assertIn(b"Connection: close", data)
        finally:
            sock.close()
        # Concurrency slot must be released after the drip is aborted.
        try:
            self.assertTrue(server._pair_sema.acquire(timeout=2),
                            "deadline must release the concurrency slot")
            server._pair_sema.release()
            header, body, probe = raw_http(
                host, port,
                ("GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                 "Connection: close\r\n\r\n").encode("ascii"),
                timeout=2)
            try:
                self.assertTrue(header.startswith("HTTP/1.1 200"), header)
            finally:
                probe.close()
        finally:
            server.shutdown()
            server.server_close()
            store.close()

    def test_total_deadline_bounds_slow_drip_request_line(self):
        line = ("POST " + REDEEM_PATH + " HTTP/1.1\r\n").encode("ascii")
        self._slow_drip_and_expect_timeout([line[i:i + 1] for i in range(len(line))])

    def test_total_deadline_bounds_slow_drip_headers(self):
        prefix = ("POST " + REDEEM_PATH + " HTTP/1.1\r\n").encode("ascii")
        headers = (
            "Host: 127.0.0.1\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: 40\r\n"
            "Connection: keep-alive\r\n"
        ).encode("ascii")
        chunks = [prefix] + [headers[i:i + 1] for i in range(len(headers))]
        self._slow_drip_and_expect_timeout(chunks)

    def test_request_line_query_is_rejected_and_not_logged(self):
        store, server = self._serve(request_timeout=5, request_deadline=5)
        host, port = "127.0.0.1", server.server_address[1]
        secret = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        handler = logging.Handler()
        records = []

        def emit(record):
            records.append(record.getMessage())

        handler.emit = emit
        log = logging.getLogger("pair_redeem")
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        try:
            header, body, sock = raw_http(
                host, port,
                ("POST %s?code=%s HTTP/1.1\r\n"
                 "Host: 127.0.0.1\r\n"
                 "Content-Type: application/json\r\n"
                 "Content-Length: 2\r\n"
                 "Connection: close\r\n\r\n{}"
                 % (REDEEM_PATH, secret)).encode("ascii"),
                timeout=2)
            try:
                self.assertTrue(header.startswith("HTTP/1.1 400"), header)
                joined = "\n".join(records)
                self.assertNotIn(secret, joined)
                self.assertNotIn("code=", joined)
            finally:
                sock.close()
        finally:
            log.removeHandler(handler)
            server.shutdown()
            server.server_close()
            store.close()



class R2RegressionTests(unittest.TestCase):
    def test_https_issuer_real_tls(self):
        """Real TLS constructor+handshake+verified cert, including on 3.12."""
        seen = {}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                seen['host'] = self.headers['Host']
                seen['auth'] = self.headers['Authorization']
                raw = b'{"token":"tls-test-registration-token"}'
                self.send_response(200)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'openssl.cnf'
            cert, key = root / 'cert.pem', root / 'key.pem'
            config.write_text('[req]\ndistinguished_name=dn\nx509_extensions=ext\n'
                              'prompt=no\n[dn]\nCN=localhost\n[ext]\n'
                              'subjectAltName=DNS:localhost\nbasicConstraints=CA:TRUE\n'.replace('\n', '\n'))
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048',
                            '-nodes', '-days', '1', '-config', str(config),
                            '-keyout', str(key), '-out', str(cert)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(str(cert), str(key))
            context.set_servername_callback(lambda sock, name, ctx: seen.update(sni=name))
            client_context = ssl.create_default_context(cafile=str(cert))
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            worker = threading.Thread(target=server.serve_forever,
                                      kwargs={'poll_interval': .01}, daemon=True)
            worker.start()
            try:
                with mock.patch('ssl._create_default_https_context',
                                return_value=client_context):
                    issuer = SynapseRegistrationTokenIssuer(
                        'https://localhost:%s' % server.server_port,
                        'fixture-admin-not-a-secret')
                    result = issuer.issue(1000)
                self.assertEqual(result['token'], 'tls-test-registration-token')
                self.assertEqual(seen['sni'], 'localhost')
                self.assertEqual(seen['host'], 'localhost:%s' % server.server_port)
                self.assertEqual(seen['auth'], 'Bearer fixture-admin-not-a-secret')
            finally:
                server.shutdown()
                server.server_close()

    def test_main_ipv6_address_tuple(self):
        import pair_redeem.__main__ as entry
        server = mock.Mock(server_address=('::1', 8092, 0, 0))
        with mock.patch.object(entry, 'PairRedeemStore'), \
                mock.patch.object(entry, 'SynapseRegistrationTokenIssuer'), \
                mock.patch.object(entry, 'start_pruner'), \
                mock.patch.object(entry.PairRedeemApp, 'make_server', return_value=server):
            entry.main({ENV_BIND: '[::1]:8092'})
        server.serve_forever.assert_called_once()
        server.server_close.assert_called_once()

    def test_xff_mapped_and_mapped_proxy_network(self):
        proxies = redeem_service.parse_trusted_proxies('::ffff:172.18.0.0/108')
        self.assertEqual(str(proxies[0]), '172.16.0.0/12')
        self.assertEqual(client_ip('::ffff:172.18.0.1',
                                  {'X-Forwarded-For': '::ffff:1.2.3.4'}, proxies),
                         '1.2.3.4')

    def test_unspecified_admin_target_refused(self):
        for host in ('0.0.0.0', '[::]', '[::ffff:0.0.0.0]'):
            with self.subTest(host=host), self.assertRaises(ConfigError):
                validate_synapse_admin_url('http://' + host)
        self.assertTrue(allow_bind_host('0.0.0.0'))

    def test_new_config_limits(self):
        keys = (redeem_service.ENV_REQUEST_TIMEOUT,
                redeem_service.ENV_REQUEST_DEADLINE,
                redeem_service.ENV_MAX_CONCURRENT)
        config = PairRedeemConfig.from_env(dict(zip(keys, ('7', '11', '3'))))
        self.assertEqual((config.request_timeout, config.request_deadline,
                          config.max_concurrent), (7, 11, 3))
        for key in keys:
            for invalid in ('0', '-1', 'nope'):
                with self.subTest(key=key, value=invalid), self.assertRaises(ConfigError):
                    PairRedeemConfig.from_env({key: invalid})

    @unittest.skipUnless(shutil.which("make"), "make executable required")
    def test_make_non_synapse_guard(self):
        proc = subprocess.run(['make', '-n', 'up', 'HS=tuwunel', 'PAIR_REDEEM=1'],
                              cwd=str(REPO_ROOT), stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(b'Synapse-only', proc.stdout)

    def test_expired_transaction_rolls_back(self):
        store = PairRedeemStore(':memory:')
        code = make_code(74)
        store.issue(HS, code)
        calls = []
        def issuer(expires):
            calls.append(1)
            time.sleep(.15)
            return {'type': 'registration_token', 'token': 'fixture', 'expires_in_ms': 1000}
        try:
            with store.request_scope(time.monotonic() + .05), self.assertRaises(TimeoutError):
                store.redeem_atomic(code_sha256(code), DEVICE, '127.0.0.1',
                                    issuer=issuer, expires_in_ms=1000)
            self.assertEqual(len(calls), 1)
            self.assertEqual(store.get(code_sha256(code))['status'], 'unused')
            self.assertEqual(store.events(kind='redeemed'), [])
        finally:
            store.close()

    def test_live_issuer_uses_remaining_request_budget(self):
        stop = threading.Event()
        contacted = threading.Event()

        class SlowIssuer(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                contacted.set()
                self.send_response(200)
                self.send_header('Content-Length', '100')
                self.end_headers()
                try:
                    while not stop.wait(.05):
                        self.wfile.write(b'x')
                        self.wfile.flush()
                except OSError:
                    pass

        backend = ThreadingHTTPServer(('127.0.0.1', 0), SlowIssuer)
        threading.Thread(target=backend.serve_forever,
                         kwargs={'poll_interval': .01}, daemon=True).start()
        store = PairRedeemStore(':memory:')
        code = make_code(77)
        store.issue(HS, code)
        issuer = SynapseRegistrationTokenIssuer(
            'http://127.0.0.1:%s' % backend.server_port, 'fixture-admin', timeout=5)
        app = PairRedeemApp(store, issuer, PairRedeemConfig(
            request_deadline=.5, max_concurrent=1))
        server = app.make_server('127.0.0.1', 0)
        threading.Thread(target=server.serve_forever,
                         kwargs={'poll_interval': .01}, daemon=True).start()
        sock = socket.create_connection(server.server_address[:2], timeout=3)
        raw = json.dumps({'code': code, 'device_display_name': DEVICE}).encode()
        try:
            sock.sendall(('POST %s HTTP/1.1\r\nHost: localhost\r\n'
                          'Content-Type: application/json\r\nContent-Length: %s\r\n'
                          'Connection: close\r\n\r\n' % (REDEEM_PATH, len(raw))).encode() + raw)
            self.assertTrue(contacted.wait(2))
            try:
                self.assertEqual(sock.recv(4096), b'')
            except ConnectionResetError:
                pass
            self.assertTrue(server._pair_sema.acquire(timeout=2))
            server._pair_sema.release()
            self.assertEqual(store.get(code_sha256(code))['status'], 'unused')
            self.assertEqual(store.events(kind='redeemed'), [])
        finally:
            stop.set()
            sock.close()
            server.shutdown()
            server.server_close()
            backend.shutdown()
            backend.server_close()
            store.close()

    def test_queued_request_deadline_releases_slot_without_issuer(self):
        store = PairRedeemStore(':memory:')
        code = make_code(75)
        store.issue(HS, code)
        issuer = FakeIssuer()
        app = PairRedeemApp(store, issuer, PairRedeemConfig(
            request_deadline=.3, max_concurrent=1))
        server = app.make_server('127.0.0.1', 0)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        sock = socket.create_connection(server.server_address[:2], timeout=3)
        raw = json.dumps({'code': code, 'device_display_name': DEVICE}).encode()
        store._lock.acquire()
        try:
            sock.sendall(('POST %s HTTP/1.1\r\nHost: localhost\r\n'
                          'Content-Type: application/json\r\nContent-Length: %s\r\n'
                          'Connection: close\r\n\r\n' % (REDEEM_PATH, len(raw))).encode() + raw)
            self.assertTrue(server.handler_started.wait(2))
            try:
                self.assertEqual(sock.recv(4096), b'')
            except ConnectionResetError:
                pass
            # The store is STILL locked here: old implementation kept its
            # handler/slot blocked until the lock was released.
            self.assertTrue(server._pair_sema.acquire(timeout=2))
            server._pair_sema.release()
            self.assertEqual(issuer.calls, 0)
        finally:
            store._lock.release()
            sock.close()
            server.shutdown()
            server.server_close()
        try:
            self.assertEqual(store.get(code_sha256(code))['status'], 'unused')
        finally:
            store.close()


class R2HttpRegressionTests(PairRedeemTestCase):
    def test_query_rejection_counts_toward_rate_limit(self):
        self.app.config.ip_limit = 1
        self.app.ip_limiter = redeem_service.RateLimiter(1, 60)
        host, port = self.server.server_address[:2]
        request = ('POST %s?code=fixture HTTP/1.1\r\nHost: localhost\r\n'
                   'Content-Length: 2\r\nConnection: close\r\n\r\n{}' % REDEEM_PATH).encode()
        statuses = []
        for _ in range(3):
            header, _, sock = raw_http(host, port, request)
            sock.close()
            statuses.append(header.split()[1])
        self.assertEqual(statuses, ['400', '429', '429'])
        self.assertEqual(len(self.store.events(kind='rejected')), 1)
        self.assertEqual(len(self.store.events(kind='rate_limited')), 1)

    def test_unknown_sensitive_path_is_not_logged(self):
        secret = make_code(76)
        host, port = self.server.server_address[:2]
        with self.assertLogs('pair_redeem', level='INFO') as captured:
            for path in (REDEEM_PATH + '/' + secret, '/user:password@' + secret):
                header, _, sock = raw_http(host, port,
                    ('POST %s HTTP/1.1\r\nHost: localhost\r\n'
                     'Content-Length: 0\r\nConnection: close\r\n\r\n' % path).encode())
                sock.close()
                self.assertTrue(header.startswith('HTTP/1.1 404'))
        output = '\n'.join(captured.output)
        self.assertNotIn(secret, output)
        self.assertNotIn('password', output)
        self.assertIn('<unknown>', output)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    unittest.main()
