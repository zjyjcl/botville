#!/usr/bin/env python3
"""Unit tests for authenticated App QR admin issuance (A-R9 / #128).

No Docker, no live homeserver, no public edge, no phone scan. Examples
use example.org only. Tokens and codes below are obvious fakes.

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
        discover -s server/tests -p test_pair_redeem_issue.py -v
"""

import io
import json
import logging
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import pairing_qr_contract as contract  # noqa: E402
from pair_redeem.issue import (  # noqa: E402
    ENV_ISSUE_TOKEN,
    ISSUE_PATH,
    ISSUE_TOKEN_MIN_LEN,
    build_pair_uri,
    cli_main,
    generate_code,
    issue_code,
    issue_via_http,
    require_loopback_issue_url,
    validate_issue_token,
)
from pair_redeem.service import (  # noqa: E402
    ConfigError,
    PairRedeemApp,
    PairRedeemConfig,
)
from pair_redeem.store import (  # noqa: E402
    PairRedeemStore, code_sha256, require_fp, require_https_url,
)

from test_pair_redeem import (  # noqa: E402
    DEVICE,
    FakeIssuer,
    HS,
    GW,
    PairRedeemTestCase,
    make_code,
)

ISSUE_TOKEN = "A" * 32
OTHER_TOKEN = "B" * 32
ADMIN_TOKEN = "S" * 32
FP = "sha256:ExampleSpkiSha256DigestForPairingQrContracA"


class IssueConfigTests(unittest.TestCase):
    def test_empty_token_keeps_issuance_off(self):
        self.assertEqual(validate_issue_token(""), "")
        self.assertEqual(validate_issue_token(None), "")
        config = PairRedeemConfig.from_env({"BOTVILLE_PAIR_REDEEM_BIND": "127.0.0.1:0"})
        self.assertEqual(config.issue_token, "")

    def test_short_or_whitespace_token_is_refused(self):
        with self.assertRaises(Exception):
            validate_issue_token("short")
        with self.assertRaises(ConfigError):
            PairRedeemConfig(issue_token="short")
        with self.assertRaises(ConfigError):
            PairRedeemConfig(issue_token="A" * 31)
        with self.assertRaises(ConfigError):
            PairRedeemConfig(issue_token="A" * 32 + "\n")
        with self.assertRaises(ConfigError):
            PairRedeemConfig(issue_token="A" * 16 + " " + "B" * 16)

    def test_issue_token_must_differ_from_synapse_admin_token(self):
        with self.assertRaises(ConfigError):
            PairRedeemConfig(issue_token=ADMIN_TOKEN, synapse_admin_token=ADMIN_TOKEN)
        config = PairRedeemConfig(issue_token=ISSUE_TOKEN, synapse_admin_token=ADMIN_TOKEN)
        self.assertEqual(config.issue_token, ISSUE_TOKEN)

    def test_from_env_reads_issue_token(self):
        config = PairRedeemConfig.from_env({
            "BOTVILLE_PAIR_REDEEM_BIND": "127.0.0.1:0",
            ENV_ISSUE_TOKEN: ISSUE_TOKEN,
            "BOTVILLE_PAIR_REDEEM_SYNAPSE_ADMIN_TOKEN": ADMIN_TOKEN,
        })
        self.assertEqual(config.issue_token, ISSUE_TOKEN)


class IssueUriTests(unittest.TestCase):
    def test_built_uri_matches_frozen_contract(self):
        code = make_code(1)
        uri = build_pair_uri(HS, code, 4102444800, gw=GW)
        parsed = contract.parse_pair_uri(uri)
        self.assertEqual(parsed["v"], "1")
        self.assertEqual(parsed["hs"], HS)
        self.assertEqual(parsed["gw"], GW)
        self.assertEqual(parsed["code"], code)
        self.assertEqual(parsed["exp"], 4102444800)
        self.assertNotIn("fp", parsed)

    def test_built_uri_omits_gw_and_accepts_fp(self):
        code = make_code(2)
        uri = build_pair_uri(HS, code, 4102444800, fp=FP)
        parsed = contract.parse_pair_uri(uri)
        self.assertNotIn("gw", parsed)
        self.assertEqual(parsed["fp"], FP)

    def test_http_hs_is_rejected(self):
        with self.assertRaises(ValueError):
            build_pair_uri("http://hs.example.org", make_code(3), 1)
        with self.assertRaises(ValueError):
            require_fp("sha256:short")

    def test_ampersand_in_hs_or_gw_is_rejected(self):
        # Review F2: hs with &gw= injects a gateway into parse_pair_uri
        # while the store would have recorded gw=None.
        injected = "https://hs.example.org/x&gw=https://evil.example.org"
        with self.assertRaises(ValueError):
            require_https_url(injected, "hs")
        with self.assertRaises(ValueError):
            build_pair_uri(injected, make_code(3), 4102444800)
        with self.assertRaises(ValueError):
            require_https_url("https://gw.example.org/x&fp=sha256:x", "gw")
        # ? and # would also break the pair URI; already invalid, still rejected.
        with self.assertRaises(ValueError):
            require_https_url("https://hs.example.org/x?gw=https://evil.example.org", "hs")
        with self.assertRaises(ValueError):
            require_https_url("https://hs.example.org/x#gw=https://evil.example.org", "hs")
        # A lone = in the path is not a query separator and stays allowed.
        require_https_url("https://hs.example.org/x=y", "hs")

    def test_issue_code_rejects_ampersand_before_store(self):
        store = PairRedeemStore(":memory:")
        try:
            with self.assertRaises(ValueError):
                issue_code(
                    store, "https://hs.example.org/x&gw=https://evil.example.org",
                    now=1_800_000_000)
            self.assertEqual(
                store._conn.execute("SELECT count(*) FROM codes").fetchone()[0],
                0)
        finally:
            store.close()

    def test_issue_code_floors_float_now_for_matching_exp(self):
        store = PairRedeemStore(":memory:")
        try:
            result = issue_code(store, HS, gw=GW, now=1_800_000_000.9)
            self.assertEqual(result["exp"], 1_800_000_000 + 300)
            parsed = contract.parse_pair_uri(result["uri"])
            self.assertEqual(parsed["exp"], result["exp"])
            row = store.get(result["code_sha256"])
            self.assertEqual(int(row["exp"]), result["exp"])
        finally:
            store.close()

    def test_plus_in_hs_path_is_accepted(self):
        # r2 nit: the local URI check must not treat '+' as space, which
        # would reject a path the frozen contract still accepts.
        hs = "https://hs.example.org/x+y"
        require_https_url(hs, "hs")
        store = PairRedeemStore(":memory:")
        try:
            result = issue_code(store, hs, now=1_800_000_000)
            parsed = contract.parse_pair_uri(result["uri"])
            self.assertEqual(parsed["hs"], hs)
            self.assertEqual(store.get(result["code_sha256"])["hs"], hs)
        finally:
            store.close()

    def test_generate_code_is_43_char_base64url(self):
        code = generate_code()
        self.assertEqual(len(code), 43)
        contract.parse_pair_uri(build_pair_uri(HS, code, 4102444800))


class _EnabledIssueServer(PairRedeemTestCase):
    """Shared server fixture; no test_* methods of its own."""

    def setUp(self):
        self.store = PairRedeemStore(":memory:")
        self.issuer = FakeIssuer()
        self.now = 1_800_000_000
        self.config = PairRedeemConfig(
            ip_limit=10, global_limit=60, bind_host="127.0.0.1", bind_port=0,
            issue_token=ISSUE_TOKEN, synapse_admin_token=ADMIN_TOKEN,
        )
        self.app = PairRedeemApp(
            self.store, self.issuer, self.config, clock=lambda: self.now)
        self.server = self.app.make_server("127.0.0.1", 0)
        self.base = "http://127.0.0.1:%s" % self.server.server_address[1]
        self._thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True)
        self._thread.start()
        self._log = io.StringIO()
        self._handler = logging.StreamHandler(self._log)
        self._logger = logging.getLogger("pair_redeem")
        self._prev_level = self._logger.level
        self._logger.addHandler(self._handler)
        self._logger.setLevel(logging.INFO)

    def tearDown(self):
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._prev_level)
        super().tearDown()

    def issue_post(self, body, token=ISSUE_TOKEN, path=ISSUE_PATH, extra=None):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer %s" % token
        if extra:
            headers.update(extra)
        return self.post(body, path=path, extra_headers=headers)

    def logs(self):
        self._handler.flush()
        return self._log.getvalue()


class IssueHttpTests(_EnabledIssueServer):
    def test_default_off_is_404_even_with_a_bearer_header(self):
        # Rebuild the server with issuance left off.
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self.store = PairRedeemStore(":memory:")
        self.config = PairRedeemConfig(
            ip_limit=10, global_limit=60, bind_host="127.0.0.1", bind_port=0)
        self.app = PairRedeemApp(
            self.store, self.issuer, self.config, clock=lambda: self.now)
        self.server = self.app.make_server("127.0.0.1", 0)
        self.base = "http://127.0.0.1:%s" % self.server.server_address[1]
        self._thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True)
        self._thread.start()
        status, body, _ = self.issue_post({"hs": HS})
        self.assertEqual(status, 404)
        get = urllib.request.Request(self.base + ISSUE_PATH, method="GET")
        try:
            with urllib.request.urlopen(get, timeout=5) as resp:
                get_status = resp.status
        except urllib.error.HTTPError as exc:
            get_status = exc.code
        self.assertEqual(get_status, 404)
        # Redeem still works when issuance is off.
        code = make_code(90)
        self.store.issue(HS, code, gw=GW, now=self.now)
        self.assertEqual(self.redeem_ok(code)[0], 200)

    def test_missing_or_wrong_token_is_401(self):
        status, body, _ = self.issue_post({"hs": HS}, token=None)
        self.assertEqual(status, 401)
        self.assertEqual(body.get("error"), "unauthorized")
        status, body, _ = self.issue_post({"hs": HS}, token=OTHER_TOKEN)
        self.assertEqual(status, 401)
        status, body, _ = self.issue_post({"hs": HS}, token=ADMIN_TOKEN)
        self.assertEqual(status, 401)
        self.assertEqual(self.store._conn.execute("SELECT count(*) FROM codes").fetchone()[0], 0)

    def test_valid_issue_returns_contract_uri_once(self):
        status, body, headers = self.issue_post({"hs": HS, "gw": GW})
        self.assertEqual(status, 200)
        cache = headers.get("Cache-Control") or headers.get("cache-control")
        self.assertEqual(cache, "no-store")
        self.assertEqual(set(body), {"uri", "exp", "auth_type"})
        self.assertEqual(body["auth_type"], "registration_token")
        self.assertEqual(body["exp"], self.now + 300)
        parsed = contract.parse_pair_uri(body["uri"])
        self.assertEqual(parsed["hs"], HS)
        self.assertEqual(parsed["gw"], GW)
        self.assertEqual(parsed["exp"], body["exp"])
        self.assertNotIn("code", body)
        digest = code_sha256(parsed["code"])
        row = self.store.get(digest)
        self.assertEqual(row["status"], "unused")
        self.assertEqual(row["hs"], HS)
        self.assertEqual(row["gw"], GW)
        self.assertEqual(row["auth_type"], "registration_token")
        # The minted code can be redeemed through the existing route.
        status, redeem, _ = self.redeem_ok(parsed["code"])
        self.assertEqual(status, 200)
        self.assertEqual(redeem["homeserver"], HS)

    def test_issue_with_fp_and_without_gw(self):
        status, body, _ = self.issue_post({"hs": HS, "fp": FP})
        self.assertEqual(status, 200)
        parsed = contract.parse_pair_uri(body["uri"])
        self.assertNotIn("gw", parsed)
        self.assertEqual(parsed["fp"], FP)

    def test_rejects_http_hs_login_token_and_unknown_fields(self):
        cases = (
            {"hs": "http://hs.example.org"},
            {"hs": "https://hs.example.org/x&gw=https://evil.example.org"},
            {"hs": HS, "gw": "https://gw.example.org/x&fp=other"},
            {"hs": HS, "auth_type": "login_token"},
            {"hs": HS, "extra": "nope"},
            {"hs": HS + "//"},
            {},
        )
        for body in cases:
            status, payload, _ = self.issue_post(body)
            self.assertEqual(status, 400, body)
            self.assertEqual(payload.get("error"), "invalid request")
        self.assertEqual(self.store._conn.execute("SELECT count(*) FROM codes").fetchone()[0], 0)

    def test_get_and_other_methods_are_405_when_enabled(self):
        get = urllib.request.Request(self.base + ISSUE_PATH, method="GET")
        try:
            with urllib.request.urlopen(get, timeout=5) as resp:
                status = resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        self.assertEqual(status, 405)
        put = urllib.request.Request(self.base + ISSUE_PATH, data=b"{}", method="PUT")
        try:
            with urllib.request.urlopen(put, timeout=5) as resp:
                status = resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        self.assertEqual(status, 405)

    def test_query_string_is_rejected_and_not_processed(self):
        status, body, _ = self.issue_post({"hs": HS}, path=ISSUE_PATH + "?code=should-not-issue")
        self.assertEqual(status, 400)
        self.assertEqual(self.store._conn.execute("SELECT count(*) FROM codes").fetchone()[0], 0)

    def test_logs_and_audit_omit_token_code_and_uri(self):
        status, body, _ = self.issue_post({"hs": HS, "gw": GW})
        self.assertEqual(status, 200)
        parsed = contract.parse_pair_uri(body["uri"])
        blob = self.logs()
        self.assertNotIn(ISSUE_TOKEN, blob)
        self.assertNotIn(ADMIN_TOKEN, blob)
        self.assertNotIn(parsed["code"], blob)
        self.assertNotIn(body["uri"], blob)
        # Access log may name the path, but not a query.
        self.assertIn(ISSUE_PATH, blob)
        self.assertNotIn("?code=", blob)
        events = self.store._conn.execute("SELECT kind, detail FROM events").fetchall()
        self.assertTrue(any(row[0] == "issued" for row in events))
        for _kind, detail in events:
            self.assertNotIn(parsed["code"], detail)
            self.assertNotIn(ISSUE_TOKEN, detail)
            self.assertNotIn(body["uri"], detail)


class IssueCliTests(PairRedeemTestCase):
    def setUp(self):
        self.store = PairRedeemStore(":memory:")
        self.issuer = FakeIssuer()
        self.now = 1_800_000_000
        self.config = PairRedeemConfig(
            ip_limit=10, global_limit=60, bind_host="127.0.0.1", bind_port=0,
            issue_token=ISSUE_TOKEN, synapse_admin_token=ADMIN_TOKEN,
        )
        self.app = PairRedeemApp(
            self.store, self.issuer, self.config, clock=lambda: self.now)
        self.server = self.app.make_server("127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.base = "http://127.0.0.1:%s" % self.port
        self._thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True)
        self._thread.start()

    def test_cli_prints_uri_to_stdout_only(self):
        out, err = io.StringIO(), io.StringIO()
        url = self.base + ISSUE_PATH
        rc = cli_main(
            ["--hs", HS, "--gw", GW, "--url", url],
            env={ENV_ISSUE_TOKEN: ISSUE_TOKEN},
            stdout=out, stderr=err,
        )
        self.assertEqual(rc, 0)
        uri = out.getvalue().strip()
        parsed = contract.parse_pair_uri(uri)
        self.assertEqual(parsed["hs"], HS)
        self.assertEqual(parsed["gw"], GW)
        self.assertNotIn(ISSUE_TOKEN, out.getvalue())
        self.assertNotIn(ISSUE_TOKEN, err.getvalue())
        self.assertNotIn(uri, err.getvalue())

    def test_cli_disabled_without_token(self):
        out, err = io.StringIO(), io.StringIO()
        rc = cli_main(
            ["--hs", HS, "--url", self.base + ISSUE_PATH],
            env={},
            stdout=out, stderr=err,
        )
        self.assertEqual(rc, 2)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("disabled", err.getvalue())

    def test_cli_refuses_non_loopback_url(self):
        with self.assertRaises(Exception):
            require_loopback_issue_url("http://hs.example.org/_botville/pair/issue")
        with self.assertRaises(Exception):
            require_loopback_issue_url("https://127.0.0.1/_botville/pair/issue")
        out, err = io.StringIO(), io.StringIO()
        rc = cli_main(
            ["--hs", HS, "--url", "http://192.0.2.1:8092/_botville/pair/issue"],
            env={ENV_ISSUE_TOKEN: ISSUE_TOKEN},
            stdout=out, stderr=err,
        )
        self.assertEqual(rc, 2)
        self.assertEqual(out.getvalue(), "")
        self.assertNotIn(ISSUE_TOKEN, err.getvalue())


class IssueAuditAndLimitTests(_EnabledIssueServer):
    def test_unauthorized_is_audited_without_token_and_is_rate_limited(self):
        status, body, _ = self.issue_post({"hs": HS}, token=OTHER_TOKEN)
        self.assertEqual(status, 401)
        events = self.store._conn.execute("SELECT kind, detail FROM events").fetchall()
        self.assertTrue(any(row[0] == "rejected" for row in events))
        for _kind, detail in events:
            self.assertNotIn(ISSUE_TOKEN, detail)
            self.assertNotIn(OTHER_TOKEN, detail)
            self.assertNotIn(ADMIN_TOKEN, detail)
        # Same redeem limiter: the 11th failed attempt (ip_limit=10) is 429.
        last_status = None
        last_body = None
        for _ in range(10):
            last_status, last_body, _ = self.issue_post({"hs": HS}, token=OTHER_TOKEN)
        self.assertEqual(last_status, 429)
        self.assertEqual(last_body.get("errcode"), "BV_PAIR_RATE_LIMITED")
        self.assertEqual(
            self.store._conn.execute("SELECT count(*) FROM codes").fetchone()[0],
            0)


class IssueCliHttpSafetyTests(_EnabledIssueServer):
    def _serve(self, handler_cls):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True)
        thread.start()
        return server

    def test_issue_via_http_ignores_http_proxy(self):
        seen = {"requests": 0, "authorization": []}

        class ProxyHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_POST(self):
                seen["requests"] += 1
                auth = self.headers.get("Authorization")
                if auth:
                    seen["authorization"].append(auth)
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                self.send_response(502)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                self.do_POST()

        proxy = self._serve(ProxyHandler)
        old_env = {
            key: os.environ.get(key)
            for key in ("http_proxy", "HTTP_PROXY", "https_proxy",
                        "HTTPS_PROXY", "all_proxy", "ALL_PROXY",
                        "no_proxy", "NO_PROXY")
        }
        try:
            os.environ["http_proxy"] = "http://127.0.0.1:%s" % proxy.server_address[1]
            os.environ["HTTP_PROXY"] = os.environ["http_proxy"]
            for key in ("no_proxy", "NO_PROXY", "https_proxy", "HTTPS_PROXY",
                        "all_proxy", "ALL_PROXY"):
                os.environ.pop(key, None)
            status, body = issue_via_http(
                self.base + ISSUE_PATH, ISSUE_TOKEN, HS, gw=GW)
            self.assertEqual(status, 200)
            self.assertIn("uri", body)
            self.assertEqual(seen["requests"], 0)
            self.assertEqual(seen["authorization"], [])
        finally:
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            proxy.shutdown()
            proxy.server_close()

    def test_issue_via_http_does_not_follow_redirect_with_authorization(self):
        stolen = {"authorization": []}

        class SinkHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_POST(self):
                stolen["authorization"].append(self.headers.get("Authorization"))
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                self.send_response(200)
                raw = b'{"uri":"stolen"}'
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self.do_POST()

        sink = self._serve(SinkHandler)
        location = "http://127.0.0.1:%s/steal" % sink.server_address[1]

        class RedirectHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                self.send_response(302)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()

        start = self._serve(RedirectHandler)
        try:
            url = "http://127.0.0.1:%s%s" % (start.server_address[1], ISSUE_PATH)
            status, payload = issue_via_http(url, ISSUE_TOKEN, HS)
            self.assertNotEqual(status, 200)
            self.assertIn(status, (302, 301, 303, 307, 308))
            self.assertNotEqual(payload.get("uri"), "stolen")
            self.assertEqual(stolen["authorization"], [])
            # The token must not have been forwarded to the redirect target.
            self.assertNotIn("Bearer %s" % ISSUE_TOKEN, stolen["authorization"])
        finally:
            start.shutdown()
            start.server_close()
            sink.shutdown()
            sink.server_close()


class IssueMainDispatchTests(unittest.TestCase):
    def test_main_dispatches_issue_subcommand(self):
        import pair_redeem.__main__ as entry
        with mock.patch.object(entry, "issue_cli", return_value=0) as cli:
            with self.assertRaises(SystemExit) as caught:
                entry.main(
                    env={ENV_ISSUE_TOKEN: ISSUE_TOKEN},
                    argv=["issue", "--hs", HS, "--gw", GW],
                )
            self.assertEqual(caught.exception.code, 0)
            cli.assert_called_once()
            self.assertEqual(cli.call_args[0][0], ["--hs", HS, "--gw", GW])
            self.assertEqual(cli.call_args[1]["env"][ENV_ISSUE_TOKEN], ISSUE_TOKEN)


if __name__ == "__main__":
    unittest.main()
