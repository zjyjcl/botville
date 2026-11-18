"""A-R3 cross-server relation transport (federation side only).

Real HTTP on ephemeral ports, temp SQLite, no Docker. Secrets and tokens
below are test-only literals, never deployment values. The gateway is a
fake (the real gateway client is deliberately not part of this build).
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "..", ".."))  # server/ -> `pairing`
sys.path.insert(0, TESTS_DIR)

from pairing import relations, service  # noqa: E402
import util as testutil  # noqa: E402

ADMIN = "test-admin-token"
SERVICE = "test-service-token-" + "s" * 24   # >= 32 chars
SEND = "test-relations-send-token-" + "x" * 24   # >= 32 chars, != SERVICE
SECRET = "ab" * 32
RID = "01JREQUESTID000000000000AB"


class FakeGateway(relations.RelationsGatewayClient):
    def __init__(self, result=None, exc=None):
        self.calls = []
        self.result = result or relations.GatewayResult(
            202, {"accepted": True})
        self.exc = exc

    def deliver(self, kind, envelope, meta):
        self.calls.append((kind, envelope, meta))
        if self.exc is not None:
            raise self.exc
        return self.result


# --- pure helpers ---------------------------------------------------------------

class SigningTest(unittest.TestCase):
    def test_signature_round_trip_and_binding(self):
        body = b'{"x":1}'
        h = relations.build_relation_headers(SECRET, "request", "hs-b", body)
        ts, nonce, sig = h["X-Botville-Ts"], h["X-Botville-Nonce"], \
            h["X-Botville-Sig"]
        self.assertTrue(relations.signature_ok(
            SECRET, "request", "hs-b", ts, nonce, body, sig))
        self.assertTrue(relations.signature_ok(
            SECRET, "request", "hs-b", ts, nonce, body, sig.upper()))
        # Bound to kind, origin, ts, nonce, body and key.
        for args in (
                (SECRET, "respond", "hs-b", ts, nonce, body),
                (SECRET, "request", "hs-c", ts, nonce, body),
                (SECRET, "request", "hs-b", str(int(ts) + 1), nonce, body),
                (SECRET, "request", "hs-b", ts, "0" * 32, body),
                (SECRET, "request", "hs-b", ts, nonce, b'{"x":2}'),
                ("cd" * 32, "request", "hs-b", ts, nonce, body)):
            self.assertFalse(relations.signature_ok(*args, sig), args[1:3])

    def test_notify_signature_is_not_a_relation_signature(self):
        body = b"{}"
        ts, nonce = str(int(time.time())), "0" * 32
        notify = service.notify_signature(SECRET, "hs-b", ts, nonce, body)
        for kind in relations.RELATION_KINDS:
            self.assertFalse(relations.signature_ok(
                SECRET, kind, "hs-b", ts, nonce, body, notify))

    def test_signature_shape_and_corrupt_secret_fail_closed(self):
        for sig in (None, "", "zz" * 32, "a" * 63, "é" * 64):
            self.assertFalse(relations.signature_ok(
                SECRET, "request", "hs-b", "1", "0" * 32, b"", sig))
        sig = "a" * 64
        self.assertFalse(relations.signature_ok(
            "not-hex", "request", "hs-b", "1", "0" * 32, b"", sig))

    def test_timestamp_and_nonce_shapes(self):
        now = 1_800_000_000
        self.assertTrue(relations.timestamp_ok(str(now - 300), now))
        self.assertFalse(relations.timestamp_ok(str(now - 301), now))
        self.assertFalse(relations.timestamp_ok(str(now + 301), now))
        for ts in ("", "-1", "1.5", "１２３", " 1", "9" * 13, None):
            self.assertFalse(relations.timestamp_ok(ts, now), ts)
        self.assertTrue(relations.nonce_ok("0f" * 16))
        for nonce in ("", "0f" * 15 + "0", "AB" * 16, "0" * 129, None):
            self.assertFalse(relations.nonce_ok(nonce), nonce)


class StrictNonceGuardTest(unittest.TestCase):
    def test_seen_fresh_full_and_expiry(self):
        guard = relations.StrictNonceGuard(window=10, capacity=2)
        S = relations.StrictNonceGuard
        self.assertEqual(guard.check_and_mark("a", now=0), S.FRESH)
        self.assertEqual(guard.check_and_mark("a", now=1), S.SEEN)
        self.assertEqual(guard.check_and_mark("b", now=1), S.FRESH)
        # Full of unexpired entries: refuse instead of evicting (an
        # evicted nonce could be replayed inside the window).
        self.assertEqual(guard.check_and_mark("c", now=2), S.FULL)
        self.assertEqual(guard.check_and_mark("a", now=2), S.SEEN)
        # After the window, entries expire and space frees up.
        self.assertEqual(guard.check_and_mark("c", now=20), S.FRESH)


class EnvelopeTest(unittest.TestCase):
    def good(self, **over):
        env = {"v": 1, "kind": "request", "origin": "hs-b",
               "destination": "hs-a", "request_id": RID, "payload": {}}
        env.update(over)
        return env

    def test_valid_and_canonical(self):
        env = relations.build_envelope("request", "hs-b", "hs-a", RID,
                                       {"b": 1, "a": "中"})
        raw = relations.encode_envelope(env)
        self.assertEqual(raw, json.dumps(env, sort_keys=True,
                                         separators=(",", ":"),
                                         ensure_ascii=False).encode("utf-8"))
        self.assertEqual(relations.parse_envelope_bytes(
            raw, "request", "hs-b", "hs-a"), env)

    def test_rejections(self):
        bad = [
            self.good(v=2), self.good(v=True), self.good(v="1"),
            self.good(kind="respond"), self.good(origin="hs-c"),
            self.good(destination="hs-c"), self.good(request_id="short"),
            self.good(request_id="x" * 65), self.good(request_id="a b" * 8),
            self.good(request_id=12345678901234567), self.good(payload=[]),
            self.good(payload="x"), self.good(extra=1), [], "x",
        ]
        missing = self.good()
        del missing["payload"]
        bad.append(missing)
        for env in bad:
            with self.assertRaises(relations.EnvelopeError, msg=repr(env)):
                relations.validate_envelope(env, "request", "hs-b", "hs-a")
        for raw in (b"not json", b"\xff\xfe", b"[]",
                    b"x" * (relations.MAX_ENVELOPE_BYTES + 1)):
            with self.assertRaises(relations.EnvelopeError):
                relations.parse_envelope_bytes(raw, "request", "hs-b", "hs-a")
        with self.assertRaises(relations.EnvelopeError):
            relations.build_envelope("request", "hs-b", "hs-a", RID,
                                     {"x": "y" * relations.MAX_ENVELOPE_BYTES})

    def test_outbound_request_shape(self):
        relations.validate_outbound_request(
            {"destination": "hs-b", "request_id": RID, "payload": {}})
        for obj in ({}, {"destination": "hs-b", "request_id": RID},
                    {"destination": "hs-b", "request_id": RID, "payload": {},
                     "origin": "hs-x"},
                    {"destination": 1, "request_id": RID, "payload": {}},
                    {"destination": "hs-b", "request_id": "x", "payload": {}},
                    {"destination": "hs-b", "request_id": RID, "payload": 1},
                    []):
            with self.assertRaises(relations.EnvelopeError):
                relations.validate_outbound_request(obj)


class GatewayResultTest(unittest.TestCase):
    def test_checked_result(self):
        R = relations.GatewayResult
        self.assertEqual(relations.checked_gateway_result(R(409, {"e": 1})),
                         (409, {"e": 1}))
        for result in (None, R(500, {}), R(302, {}), R(200, []),
                       R("200", {}), R(True, {}), R(200, {"x": object()}),
                       R(200, {"x": "y" * relations.MAX_GATEWAY_BODY_BYTES})):
            with self.assertRaises(relations.GatewayUnavailable):
                relations.checked_gateway_result(result)

    def test_unconfigured_and_recorder(self):
        with self.assertRaises(relations.GatewayUnavailable):
            relations.UnconfiguredRelationsGateway().deliver("request", {}, {})
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "rec.jsonl")
            gw = relations.build_gateway_client("test-recorder", path)
            res = gw.deliver("revoke", {"request_id": RID}, {"origin": "hs-b"})
            self.assertEqual((res.status, res.body),
                             (202, {"accepted": True, "request_id": RID}))
            with open(path, encoding="utf-8") as handle:
                line = json.loads(handle.readline())
            self.assertEqual(line["kind"], "revoke")
            broken = relations.RecordingRelationsGateway(
                os.path.join(tmp, "no", "such", "dir.jsonl"))
            with self.assertRaises(relations.GatewayUnavailable):
                broken.deliver("revoke", {"request_id": RID}, {})


class ConfigTest(unittest.TestCase):
    BASE = {"PAIRING_ADMIN_TOKEN": ADMIN, "PAIRING_SERVER_NAME": "hs-a",
            "PAIRING_API_URL": "https://hs-a/_matrix/pairing",
            "PAIRING_DB_PATH": "/tmp/x.db"}

    def test_modes(self):
        cfg = service.PairingConfig.from_env(dict(self.BASE))
        self.assertEqual(cfg.relations_gateway, "none")
        for mode in ("http", "HTTP", "recorder", "true"):
            env = dict(self.BASE, PAIRING_RELATIONS_GATEWAY=mode)
            with self.assertRaises(service.ConfigError, msg=mode):
                service.PairingConfig.from_env(env)
        for path in (None, "", "relative/rec.jsonl"):
            env = dict(self.BASE, PAIRING_RELATIONS_GATEWAY="test-recorder")
            if path is not None:
                env["PAIRING_RELATIONS_TEST_RECORD"] = path
            with self.assertRaises(service.ConfigError, msg=path):
                service.PairingConfig.from_env(env)
        env = dict(self.BASE, PAIRING_RELATIONS_GATEWAY="test-recorder",
                   PAIRING_RELATIONS_TEST_RECORD="/data/rec.jsonl")
        cfg = service.PairingConfig.from_env(env)
        self.assertEqual(cfg.relations_test_record, "/data/rec.jsonl")

    def test_send_token_rules(self):
        # A-R4: separate send token, same rules as the facts token.
        cfg = service.PairingConfig.from_env(dict(self.BASE))
        self.assertIsNone(cfg.relations_send_token)
        self.assertFalse(cfg.relations_allow_private)
        cfg = service.PairingConfig.from_env(dict(
            self.BASE, PAIRING_SERVICE_TOKEN=SERVICE,
            PAIRING_RELATIONS_SEND_TOKEN=SEND,
            PAIRING_RELATIONS_ALLOW_PRIVATE="true"))
        self.assertEqual(cfg.relations_send_token, SEND)
        self.assertTrue(cfg.relations_allow_private)
        for extra in ({"PAIRING_RELATIONS_SEND_TOKEN": "short"},
                      {"PAIRING_RELATIONS_SEND_TOKEN": "y" * 31},
                      {"PAIRING_ADMIN_TOKEN": "a" * 40,
                       "PAIRING_RELATIONS_SEND_TOKEN": "a" * 40},
                      {"PAIRING_SERVICE_TOKEN": SERVICE,
                       "PAIRING_RELATIONS_SEND_TOKEN": SERVICE}):
            with self.subTest(extra=sorted(extra)):
                with self.assertRaises(service.ConfigError) as ctx:
                    service.PairingConfig.from_env(dict(self.BASE, **extra))
                for value in extra.values():
                    if len(value) >= 8:
                        self.assertNotIn(value, str(ctx.exception))


class TargetPolicyTest(unittest.TestCase):
    """#249 review 2: https + public only, unless explicitly allowed."""

    @staticmethod
    def resolver(*addresses):
        def resolve(host, port, proto=0):
            if not addresses:
                raise OSError("nxdomain")
            return [(2, 1, 6, "", (a, port)) for a in addresses]
        return resolve

    def check(self, url, allow, *addresses):
        return relations.target_policy(url, allow, self.resolver(*addresses))

    def test_production_rule(self):
        self.assertEqual(self.check("https://hs-b.example/x", False,
                                    "93.184.216.34")[0], relations.TARGET_OK)
        for url, addrs in (
                ("http://hs-b.example/x", ("93.184.216.34",)),
                ("https://hs-b/x", ("172.30.81.11",)),
                ("https://hs-b/x", ("127.0.0.1",)),
                ("https://hs-b/x", ("10.0.0.5",)),
                ("https://hs-b/x", ("169.254.169.254",)),
                ("https://hs-b/x", ("::1",)),
                ("https://hs-b/x", ("::ffff:127.0.0.1",)),
                ("https://hs-b/x", ("fd00::1",)),
                ("https://hs-b/x", ("93.184.216.34", "192.168.1.2")),
                ("https://hs-b/x", ()),               # does not resolve
                ("ftp://hs-b/x", ("93.184.216.34",)),
                ("https:///x", ("93.184.216.34",))):
            with self.subTest(url=url, addrs=addrs):
                self.assertEqual(self.check(url, False, *addrs)[0],
                                 relations.TARGET_REFUSED)

    def test_switch_allows_but_audits(self):
        for url, addrs in (("http://127.0.0.1:9/x", ("127.0.0.1",)),
                           ("https://hs-b/x", ("172.30.81.11",))):
            verdict, detail = self.check(url, True, *addrs)
            self.assertEqual(verdict, relations.TARGET_AUDIT)
            self.assertTrue(detail)
        # Unresolvable / bad scheme stay refused even with the switch.
        self.assertEqual(self.check("https://hs-b/x", True)[0],
                         relations.TARGET_REFUSED)
        self.assertEqual(self.check("file:///etc/x", True, "127.0.0.1")[0],
                         relations.TARGET_REFUSED)


# --- HTTP: inbound ----------------------------------------------------------------

def start_service(test, db_path, server_name, gateway=None,
                  allow_private=True):
    config = service.PairingConfig(
        db_path=db_path, admin_token=ADMIN, server_name=server_name,
        api_url="https://{}.example/_matrix/pairing".format(server_name),
        verify_peer_key=False, host="127.0.0.1", port=0,
        service_token=SERVICE, relations_send_token=SEND,
        relations_allow_private=allow_private)
    httpd = service.build_server(config)
    if gateway is not None:
        httpd.app.relations_gateway = gateway
    test.addCleanup(testutil.shutdown, httpd)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return testutil.base_url(httpd), httpd


class InboundTest(unittest.TestCase):
    """hs-a receives relation messages from its peer hs-b."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.gw = FakeGateway()
        self.base, self.httpd = start_service(
            self, os.path.join(self._tmp.name, "a.db"), "hs-a", self.gw)
        self.app = self.httpd.app
        self.app.store.upsert_peer_active(
            "hs-b", "https://hs-b.example/_matrix/pairing",
            shared_secret=SECRET)

    def envelope(self, kind="request", **over):
        env = {"v": 1, "kind": kind, "origin": "hs-b", "destination": "hs-a",
               "request_id": RID, "payload": {"note": "hi"}}
        env.update(over)
        return relations.encode_envelope(env)

    def send(self, kind="request", body=None, headers=None, sign_kind=None,
             origin="hs-b", secret=SECRET, path_prefix="/v1/pairing",
             method="POST", now=None):
        body = self.envelope(kind) if body is None else body
        hdrs = relations.build_relation_headers(
            secret, sign_kind or kind, origin, body, now=now)
        hdrs.update(headers or {})
        return testutil.http_json(
            method, "{}{}/relations/{}".format(self.base, path_prefix, kind),
            data=body if method == "POST" else None, headers=hdrs)

    def events(self):
        return len(self.app.store.fact_events(0, 1000))

    def test_valid_message_reaches_gateway_and_answer_is_relayed(self):
        self.gw.result = relations.GatewayResult(
            409, {"errcode": "relation.state_conflict"})
        for prefix in ("/v1/pairing", "/_matrix/pairing"):
            for kind in relations.RELATION_KINDS:
                with self.subTest(prefix=prefix, kind=kind):
                    before = time.time()
                    status, resp = self.send(kind, path_prefix=prefix)
                    self.assertEqual(status, 409)
                    self.assertEqual(resp,
                                     {"errcode": "relation.state_conflict"})
                    got_kind, env, meta = self.gw.calls[-1]
                    self.assertEqual(got_kind, kind)
                    self.assertEqual(env["kind"], kind)
                    self.assertEqual(env["payload"], {"note": "hi"})
                    self.assertEqual(meta["origin"], "hs-b")
                    self.assertGreaterEqual(meta["received_at"],
                                            int(before) - 1)
                    self.assertIsInstance(meta["sent_at"], int)
        self.assertEqual(self.app.relations_rejected, 0)

    def test_authentication_failures_are_uniform_401(self):
        events = self.events()
        body = self.envelope()
        old = time.time() - 400
        cases = {
            "unknown origin": dict(origin="hs-x"),
            "bad origin": dict(headers={"X-Botville-Origin": "HS B!"}),
            "wrong secret": dict(secret="cd" * 32),
            "signed for another kind": dict(sign_kind="revoke"),
            "stale ts": dict(now=old),
            "future ts": dict(now=time.time() + 400),
            "bad nonce": dict(headers={"X-Botville-Nonce": "nope"}),
            "no sig": dict(headers={"X-Botville-Sig": ""}),
            "tampered body": dict(headers={"X-Botville-Sig": "0" * 64}),
        }
        for name, kwargs in cases.items():
            with self.subTest(name):
                status, resp = self.send(body=body, **kwargs)
                self.assertEqual(status, 401)
                self.assertEqual(resp["errcode"], "M_UNAUTHORIZED")
                self.assertEqual(resp["error"],
                                 "invalid relations authentication")
        self.assertEqual(self.gw.calls, [])
        self.assertEqual(self.app.relations_rejected, len(cases))
        self.assertEqual(self.events(), events)  # no fact events written

    def test_rejection_log_is_throttled_but_always_counted(self):
        # Same LogThrottle as the internal 401s (#243 review 2): one line
        # per interval, the swallowed count reported in the next line.
        with self.assertLogs("pairing.service", "WARNING") as logs:
            for _ in range(5):
                self.assertEqual(self.send(origin="hs-x")[0], 401)
        lines = [r for r in logs.output if "relation message rejected" in r]
        self.assertEqual(len(lines), 1)
        self.assertIn("1 rejected in total; 0 similar", lines[0])
        self.assertEqual(self.app.relations_rejected, 5)
        # Next interval: one line again, carrying the 4 suppressed ones.
        self.app.relations_reject_log._next_at = 0.0
        with self.assertLogs("pairing.service", "WARNING") as logs:
            self.send(origin="hs-x")
        self.assertIn("6 rejected in total; 4 similar warnings suppressed",
                      logs.output[-1])
        self.assertNotIn(SECRET, "".join(logs.output))

    def test_notify_severed_signature_cannot_be_replayed_here(self):
        body = self.envelope()
        ts, nonce = str(int(time.time())), "1f" * 16
        sig = service.notify_signature(SECRET, "hs-b", ts, nonce, body)
        status, _ = testutil.http_json(
            "POST", self.base + "/v1/pairing/relations/request", data=body,
            headers={"X-Botville-Origin": "hs-b", "X-Botville-Ts": ts,
                     "X-Botville-Nonce": nonce, "X-Botville-Sig": sig})
        self.assertEqual(status, 401)
        self.assertEqual(self.gw.calls, [])

    def test_replay_refused(self):
        body = self.envelope()
        hdrs = relations.build_relation_headers(SECRET, "request", "hs-b",
                                                body)
        url = self.base + "/v1/pairing/relations/request"
        self.assertEqual(testutil.http_json("POST", url, data=body,
                                            headers=hdrs)[0], 202)
        status, resp = testutil.http_json("POST", url, data=body, headers=hdrs)
        self.assertEqual(status, 401)
        self.assertEqual(len(self.gw.calls), 1)
        # Same nonce on another endpoint is also a replay (key is per
        # origin+nonce), even though that signature would not verify anyway.
        status, _ = testutil.http_json(
            "POST", self.base + "/_matrix/pairing/relations/request",
            data=body, headers=hdrs)
        self.assertEqual(status, 401)

    def test_inactive_peer_refused_403(self):
        self.app.store.sever_peer("hs-b", "remote")
        status, resp = self.send()
        self.assertEqual(status, 403)
        self.assertEqual(resp["errcode"], "BV_PEER_INACTIVE")
        self.assertEqual(self.gw.calls, [])

    def test_bad_envelope_400_without_echo(self):
        cases = [
            self.envelope(destination="hs-c"),
            self.envelope(origin="hs-c"),
            self.envelope(kind="revoke"),       # sent to /request
            self.envelope(request_id="<script>"),
            self.envelope(payload=["x"]),
            b"not json at all",
        ]
        for body in cases:
            with self.subTest(body=body[:40]):
                status, resp = self.send(body=body)
                self.assertEqual(status, 400)
                self.assertEqual(resp["errcode"], "BV_BAD_ENVELOPE")
                self.assertNotIn("<script>", resp["error"])
        self.assertEqual(self.gw.calls, [])

    def test_oversized_body_413(self):
        body = b"{" + b" " * relations.MAX_ENVELOPE_BYTES + b"}"
        status, _ = self.send(body=body)
        self.assertEqual(status, 413)
        self.assertEqual(self.gw.calls, [])

    def test_gateway_unconfigured_or_broken_is_503(self):
        self.app.relations_gateway = relations.UnconfiguredRelationsGateway()
        status, resp = self.send()
        self.assertEqual(status, 503)
        self.assertEqual(resp["errcode"], "BV_RELATIONS_UNAVAILABLE")
        for gw in (FakeGateway(exc=RuntimeError("boom")),
                   FakeGateway(exc=relations.GatewayUnavailable("down")),
                   FakeGateway(result=relations.GatewayResult(500, {})),
                   FakeGateway(result=relations.GatewayResult(200, None)),
                   FakeGateway(result="garbage")):
            self.app.relations_gateway = gw
            status, resp = self.send()
            self.assertEqual(status, 503)
            self.assertEqual(resp["errcode"], "BV_RELATIONS_UNAVAILABLE")
            self.assertNotIn("boom", json.dumps(resp))

    def test_gateway_exception_text_never_logged(self):
        # #249 review 4: a client may stuff bodies into exception text;
        # pairing logs only a fixed reason code / the exception type.
        leak = "LEAKED-BODY-" + "q" * 20
        cases = [
            (FakeGateway(exc=RuntimeError(leak)), "RuntimeError"),
            (FakeGateway(exc=ValueError({"body": leak})), "ValueError"),
            (FakeGateway(exc=relations.GatewayUnavailable(leak)),
             "unavailable"),
            (FakeGateway(exc=relations.GatewayUnavailable(
                "response body: " + leak)), "unavailable"),
            (FakeGateway(exc=relations.GatewayUnavailable("timeout")),
             "timeout"),
        ]
        for gw, shown in cases:
            with self.subTest(shown=shown):
                self.app.relations_gateway = gw
                with self.assertLogs("pairing.service", "WARNING") as logs:
                    status, resp = self.send()
                self.assertEqual(status, 503)
                text = "\n".join(logs.output)
                self.assertNotIn(leak, text)
                self.assertNotIn("note", text)  # envelope payload {"note": "hi"}
                self.assertNotIn("Traceback", text)
                self.assertIn(shown, text)
                self.assertNotIn(leak, json.dumps(resp))

    def test_safe_reason(self):
        G = relations.GatewayUnavailable
        self.assertEqual(relations.safe_reason(G("not_configured")),
                         "not_configured")
        for exc in (G(), G(""), G("Has Caps"), G("a" * 41), G({"x": 1}),
                    G("body: {...}")):
            self.assertEqual(relations.safe_reason(exc), "unavailable")

    def test_default_build_is_fail_closed(self):
        httpd = service.build_server(service.PairingConfig(
            db_path=os.path.join(self._tmp.name, "c.db"),
            admin_token=ADMIN, server_name="hs-c",
            api_url="https://hs-c/_matrix/pairing", host="127.0.0.1", port=0))
        self.addCleanup(httpd.server_close)
        self.assertIsInstance(httpd.app.relations_gateway,
                              relations.UnconfiguredRelationsGateway)

    def test_replay_guard_full_is_503(self):
        self.app.relation_nonces = relations.StrictNonceGuard(capacity=1)
        self.assertEqual(self.send()[0], 202)
        status, resp = self.send()
        self.assertEqual(status, 503)
        self.assertEqual(len(self.gw.calls), 1)

    def test_rate_limits(self):
        self.app.relations_origin_limiter = service.RateLimiter(limit=2)
        self.assertEqual(self.send()[0], 202)
        self.assertEqual(self.send()[0], 202)
        status, resp = self.send()
        self.assertEqual(status, 429)
        self.assertEqual(resp["errcode"], "M_LIMIT_EXCEEDED")
        self.app.relations_ip_limiter = service.RateLimiter(limit=1)
        self.send(origin="hs-x")  # consumes the IP budget (and is refused)
        status, _ = self.send()
        self.assertEqual(status, 429)

    def test_method_and_unknown_kind(self):
        status, _ = testutil.http_json(
            "GET", self.base + "/v1/pairing/relations/request")
        self.assertEqual(status, 405)
        for kind in ("block", "", "request/", "REQUEST"):
            status, _ = testutil.http_json(
                "POST", self.base + "/v1/pairing/relations/" + kind,
                data=b"{}")
            self.assertEqual(status, 404, kind)
        self.assertEqual(self.gw.calls, [])


# --- HTTP: outbound (two real services) ------------------------------------------------

class SlowPeerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    delay = 2.0
    status = 200
    extra_headers = ()

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        time.sleep(type(self).delay)
        body = b'{"ok":true}'
        self.send_response(type(self).status)
        for key, value in type(self).extra_headers:
            self.send_header(key, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class OutboundTest(unittest.TestCase):
    """hs-a's gateway asks its pairing service to send to hs-b."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.gw_b = FakeGateway()
        self.base_b, self.httpd_b = start_service(
            self, os.path.join(self._tmp.name, "b.db"), "hs-b", self.gw_b)
        self.base_a, self.httpd_a = start_service(
            self, os.path.join(self._tmp.name, "a.db"), "hs-a", FakeGateway())
        self.httpd_a.app.store.upsert_peer_active(
            "hs-b", self.base_b + "/_matrix/pairing", shared_secret=SECRET)
        self.httpd_b.app.store.upsert_peer_active(
            "hs-a", self.base_a + "/_matrix/pairing", shared_secret=SECRET)

    def send(self, kind="request", body=None, token=SEND, method="POST"):
        if body is None:
            body = {"destination": "hs-b", "request_id": RID,
                    "payload": {"note": "hi"}}
        return testutil.http_json(
            method, self.base_a + "/v1/pairing/internal/relations/" + kind,
            body=body if method == "POST" else None, token=token)

    def test_signed_delivery_end_to_end(self):
        for kind in relations.RELATION_KINDS:
            status, resp = self.send(kind)
            self.assertEqual(status, 200, resp)
            self.assertEqual(resp, {"delivered": True, "peer_status": 202,
                                    "peer_body": {"accepted": True}})
            got_kind, env, meta = self.gw_b.calls[-1]
            self.assertEqual(got_kind, kind)
            self.assertEqual(env, {"v": 1, "kind": kind, "origin": "hs-a",
                                   "destination": "hs-b", "request_id": RID,
                                   "payload": {"note": "hi"}})
            self.assertEqual(meta["origin"], "hs-a")

    def test_peer_business_refusal_is_wrapped_not_raised(self):
        self.httpd_b.app.store.sever_peer("hs-a", "local")
        status, resp = self.send()
        self.assertEqual(status, 200)
        self.assertFalse(resp["delivered"])
        self.assertEqual(resp["peer_status"], 403)
        self.assertEqual(resp["peer_body"]["errcode"], "BV_PEER_INACTIVE")

    def test_only_active_peers(self):
        self.httpd_a.app.store.sever_peer("hs-b", "local")
        status, resp = self.send()
        self.assertEqual(status, 409)
        self.assertEqual(resp["errcode"], "BV_PEER_INACTIVE")
        for dest in ("hs-unknown",):
            status, _ = self.send(body={"destination": dest,
                                        "request_id": RID, "payload": {}})
            self.assertEqual(status, 409)
        # Active but no shared secret (legacy row) -> refused too.
        self.httpd_a.app.store.upsert_peer_active("hs-c", "https://hs-c/x")
        status, _ = self.send(body={"destination": "hs-c", "request_id": RID,
                                    "payload": {}})
        self.assertEqual(status, 409)
        self.assertEqual(self.gw_b.calls, [])

    def test_bad_bodies_400(self):
        for body in ({}, {"destination": "hs-a", "request_id": RID,
                          "payload": {}},   # ourselves
                     {"destination": "Not A Name!", "request_id": RID,
                      "payload": {}},
                     {"destination": "hs-b", "request_id": "x",
                      "payload": {}}):
            status, resp = self.send(body=body)
            self.assertEqual(status, 400, body)
        self.assertEqual(self.gw_b.calls, [])

    def test_auth_method_kind(self):
        # A-R4: only the send token opens the send endpoint -- the
        # read-only facts token (SERVICE) is refused like any other.
        for token in (None, ADMIN, SERVICE, "wrong-" + SEND, SEND + "x"):
            self.assertEqual(self.send(token=token)[0], 401)
        self.assertEqual(self.send(method="GET")[0], 405)
        self.assertEqual(self.send(kind="block")[0], 404)
        # The public alias never reaches the internal namespace.
        status, _ = testutil.http_json(
            "POST", self.base_a + "/_matrix/pairing/internal/relations/request",
            body={"destination": "hs-b", "request_id": RID, "payload": {}},
            token=SEND)
        self.assertEqual(status, 404)
        self.assertEqual(self.gw_b.calls, [])

    def test_private_target_refused_without_switch_and_audited_with(self):
        self.httpd_a.app.config.relations_allow_private = False
        with self.assertLogs("pairing.service", "WARNING") as logs:
            status, resp = self.send()
        self.assertEqual(status, 409)
        self.assertEqual(resp["errcode"], "BV_PEER_TARGET_REFUSED")
        self.assertIn("PAIRING_RELATIONS_ALLOW_PRIVATE", logs.output[-1])
        self.assertEqual(self.gw_b.calls, [])
        self.httpd_a.app.config.relations_allow_private = True
        with self.assertLogs("pairing.service", "WARNING") as logs:
            status, resp = self.send()
        self.assertEqual(status, 200)
        self.assertTrue(any("AUDIT relation request" in line and RID in line
                            for line in logs.output))

    def test_m1_boundaries_and_token_namespaces(self):
        # #249 review 5 (M1): bare internal root and bare relations prefix
        # are 404 once authenticated; each token opens only its namespace.
        for path in ("/v1/pairing/internal", "/v1/pairing/internal/",
                     "/v1/pairing/internal/relations"):
            with self.subTest(path=path):
                status, resp = testutil.http_json(
                    "GET", self.base_a + path, token=SERVICE)
                self.assertEqual(status, 404)
                self.assertEqual(resp["errcode"], "M_UNRECOGNIZED")
                status, _ = testutil.http_json(
                    "GET", self.base_a + path, token=SEND)
                self.assertEqual(status, 401)   # send token: facts side
        status, _ = testutil.http_json(
            "GET", self.base_a + "/v1/pairing/internal/events", token=SEND)
        self.assertEqual(status, 401)
        status, _ = testutil.http_json(
            "GET", self.base_a + "/v1/pairing/internal/events",
            token=SERVICE)
        self.assertEqual(status, 200)
        status, _ = testutil.http_json(
            "POST", self.base_a + "/v1/pairing/internal/relations/",
            body={}, token=SEND)
        self.assertEqual(status, 404)

    def test_health_reports_gateway_mode(self):
        # #249 review 3
        status, resp = testutil.http_json("GET", self.base_a + "/health")
        self.assertEqual((status, resp),
                         (200, {"ok": True, "relations_gateway": "none"}))

    def test_outbound_rate_limit(self):
        self.httpd_a.app.relations_out_limiter = service.RateLimiter(limit=1)
        self.assertEqual(self.send()[0], 200)
        self.assertEqual(self.send()[0], 429)

    def test_unreachable_502_and_timeout_504(self):
        self.httpd_a.app.store.upsert_peer_active(
            "hs-b", "http://127.0.0.1:9/_matrix/pairing", shared_secret=SECRET)
        status, resp = self.send()
        self.assertEqual(status, 502)
        self.assertEqual(resp["errcode"], "BV_PEER_UNREACHABLE")

        slow = type("Slow", (SlowPeerHandler,), {"delay": 2.0})
        slow_httpd = testutil.serve(slow)
        self.addCleanup(testutil.shutdown, slow_httpd)
        self.httpd_a.app.store.upsert_peer_active(
            "hs-b", testutil.base_url(slow_httpd) + "/x", shared_secret=SECRET)
        with mock.patch.object(relations, "PEER_TIMEOUT_SECONDS", 0.3):
            status, resp = self.send()
        self.assertEqual(status, 504)
        self.assertEqual(resp["errcode"], "BV_PEER_TIMEOUT")

    def test_redirect_not_followed(self):
        redirect = type("Redir", (SlowPeerHandler,), {
            "delay": 0, "status": 307,
            "extra_headers": (("Location", self.base_b + "/elsewhere"),)})
        httpd = testutil.serve(redirect)
        self.addCleanup(testutil.shutdown, httpd)
        self.httpd_a.app.store.upsert_peer_active(
            "hs-b", testutil.base_url(httpd) + "/x", shared_secret=SECRET)
        status, resp = self.send()
        self.assertEqual(status, 200)
        self.assertFalse(resp["delivered"])
        self.assertEqual(resp["peer_status"], 307)
        self.assertEqual(self.gw_b.calls, [])


if __name__ == "__main__":
    unittest.main()
