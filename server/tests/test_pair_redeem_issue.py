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
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

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
    require_loopback_issue_url,
    validate_issue_token,
)
from pair_redeem.service import (  # noqa: E402
    ConfigError,
    PairRedeemApp,
    PairRedeemConfig,
)
from pair_redeem.store import PairRedeemStore, code_sha256, require_fp  # noqa: E402

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

    def test_generate_code_is_43_char_base64url(self):
        code = generate_code()
        self.assertEqual(len(code), 43)
        contract.parse_pair_uri(build_pair_uri(HS, code, 4102444800))


class IssueHttpTests(PairRedeemTestCase):
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
        status, body, _ = self.issue_post({"hs": HS, "gw": GW})
        self.assertEqual(status, 200)
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


if __name__ == "__main__":
    unittest.main()
