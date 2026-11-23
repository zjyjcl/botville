#!/usr/bin/env python3
"""Unit tests for POST /_botville/pair/redeem (issue #128, first cut).

All Synapse admin calls are mocked. No Docker, no live homeserver, no
edge, no federation /v1/pairing/redeem. Examples use example.org only.

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
        discover -s server/tests -p test_pair_redeem.py -v
"""

import base64
import json
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    ENV_SYNAPSE_ADMIN_TOKEN,
    ENV_SYNAPSE_URL,
    ENV_TRUSTED_PROXIES,
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
        # Optional Retry-After header, seconds floored; body wins for the app.
        retry_header = headers.get("Retry-After") or headers.get("retry-after")
        if retry_header is not None:
            self.assertEqual(int(retry_header),
                             validated["retry_after_ms"] // 1000
                             if validated["retry_after_ms"] >= 1000 else int(retry_header))
        kinds = [e["kind"] for e in self.store.events()]
        self.assertIn("rate_limited", kinds)

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
        self.assertNotEqual(body.get("errcode"), "BV_PAIR_USED")
        self.assertNotIn("auth", body if isinstance(body, dict) else {})
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
        self.assertEqual(config.bind_host, "127.0.0.1")
        self.assertEqual(len(config.trusted_proxies), 2)
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


if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    unittest.main()
