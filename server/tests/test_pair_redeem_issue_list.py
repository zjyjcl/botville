#!/usr/bin/env python3
"""Unit tests for admin issuance list / filter / bulk-revoke (A-R10 / #128).

No Docker, no live homeserver, no public edge, no phone scan. Examples
use example.org only. Tokens and codes below are obvious fakes.

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
        discover -s server/tests -p test_pair_redeem_issue_list.py -v
"""

import json
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
from pair_redeem.issue import issue_code  # noqa: E402
from pair_redeem.issue_admin import (  # noqa: E402
    ISSUE_LIST_PATH,
    ISSUE_REVOKE_PATH,
    LIST_MAX_LIMIT,
    PUBLIC_RECORD_KEYS,
    parse_list_request,
    parse_revoke_request,
)
from pair_redeem.service import (  # noqa: E402
    PairRedeemApp,
    PairRedeemConfig,
)
from pair_redeem.store import PairRedeemStore  # noqa: E402

from test_pair_redeem import (  # noqa: E402
    DEVICE,
    FakeIssuer,
    GW,
    HS,
    PairRedeemTestCase,
    make_code,
)
from test_pair_redeem_issue import (  # noqa: E402
    ADMIN_TOKEN,
    ISSUE_TOKEN,
    OTHER_TOKEN,
    _EnabledIssueServer,
)

OTHER_HS = "https://other.example.org"
def _secrets_in(blob, codes=(), uris=()):
    """Return any leak tokens found in a JSON-ish blob."""
    text = blob if isinstance(blob, str) else json.dumps(blob, ensure_ascii=False)
    found = []
    if ISSUE_TOKEN in text:
        found.append("issue_token")
    if OTHER_TOKEN in text:
        found.append("other_token")
    if ADMIN_TOKEN in text:
        found.append("admin_token")
    for code in codes:
        if code and code in text:
            found.append("code")
    for uri in uris:
        if uri and uri in text:
            found.append("uri")
    if "botville://pair" in text:
        found.append("pair_uri")
    if "code_sha256" in text:
        found.append("code_sha256_field")
    return found


class IssueListParseTests(unittest.TestCase):
    def _list(self, body):
        return parse_list_request(
            json.dumps(body).encode("utf-8"), "application/json")

    def _revoke(self, body):
        return parse_revoke_request(
            json.dumps(body).encode("utf-8"), "application/json")

    def test_list_defaults_and_rejects_unknown_or_bad_fields(self):
        self.assertEqual(self._list({}), {
            "status": None, "hs": None, "expired": None,
            "limit": 50, "after_id": None,
        })
        from pair_redeem.issue import IssueError
        for body in (
            {"status": "unknown"},
            {"hs": "http://hs.example.org"},
            {"hs": "https://hs.example.org/x&gw=https://evil.example.org"},
            {"expired": "true"},
            {"limit": 0},
            {"limit": LIST_MAX_LIMIT + 1},
            {"limit": 1.5},
            {"limit": True},
            {"after_id": 0},
            {"after_id": -1},
            {"extra": 1},
        ):
            with self.subTest(body=body):
                with self.assertRaises(IssueError) as ctx:
                    self._list(body)
                self.assertEqual(ctx.exception.status, 400)

    def test_revoke_requires_positive_int_ids(self):
        self.assertEqual(self._revoke({"ids": [1, 2]}), {"ids": [1, 2]})
        from pair_redeem.issue import IssueError
        for body in (
            {},
            {"ids": []},
            {"ids": [0]},
            {"ids": [-1]},
            {"ids": ["1"]},
            {"ids": [True]},
            {"ids": [1] * 101},
            {"ids": [1], "extra": True},
        ):
            with self.subTest(body=body):
                with self.assertRaises(IssueError) as ctx:
                    self._revoke(body)
                self.assertEqual(ctx.exception.status, 400)


class IssueStoreListRevokeTests(unittest.TestCase):
    def setUp(self):
        self.store = PairRedeemStore(":memory:")
        self.now = 1_800_000_000

    def tearDown(self):
        self.store.close()

    def _issue(self, seed, hs=HS, gw=GW, now=None):
        result = issue_code(
            self.store, hs, gw=gw, now=now if now is not None else self.now,
            code=make_code(seed))
        return result

    def test_list_filters_and_pagination(self):
        a = self._issue(1, hs=HS)
        b = self._issue(2, hs=OTHER_HS, gw=None)
        c = self._issue(3, hs=HS)
        self.store.revoke_ids([a["id"]], now=self.now)
        rows = self.store.list_codes(status="revoked", now=self.now)
        self.assertEqual([row["id"] for row in rows], [a["id"]])
        rows = self.store.list_codes(hs=OTHER_HS, now=self.now)
        self.assertEqual([row["id"] for row in rows], [b["id"]])
        self.assertIsNone(rows[0]["gw"])
        self.assertNotIn("code_sha256", rows[0])
        page = self.store.list_codes(limit=2, now=self.now)
        self.assertEqual([row["id"] for row in page], [c["id"], b["id"]])
        rest = self.store.list_codes(
            limit=2, after_id=page[-1]["id"], now=self.now)
        self.assertEqual([row["id"] for row in rest], [a["id"]])

    def test_expired_filter_and_revoke_results(self):
        fresh = self._issue(4, now=self.now)
        old = self._issue(5, now=self.now - 400)
        later = self.now
        expired = self.store.list_codes(expired=True, now=later)
        self.assertEqual([row["id"] for row in expired], [old["id"]])
        active = self.store.list_codes(expired=False, now=later)
        self.assertEqual([row["id"] for row in active], [fresh["id"]])
        results = self.store.revoke_ids(
            [fresh["id"], fresh["id"], 999, old["id"]], now=later)
        self.assertEqual(results, [
            {"id": fresh["id"], "result": "revoked"},
            {"id": fresh["id"], "result": "already_revoked"},
            {"id": 999, "result": "not_found"},
            {"id": old["id"], "result": "revoked"},
        ])


class IssueListHttpTests(_EnabledIssueServer):
    def admin_post(self, path, body, token=ISSUE_TOKEN, extra=None):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer %s" % token
        if extra:
            headers.update(extra)
        return self.post(body, path=path, extra_headers=headers)

    def admin_other(self, method, path, token=ISSUE_TOKEN):
        headers = {}
        if token is not None:
            headers["Authorization"] = "Bearer %s" % token
        request = urllib.request.Request(
            self.base + path, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read().decode("utf-8"), dict(
                    response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace"), dict(
                exc.headers)

    def _disable_issue_token(self):
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

    def _issue(self, hs=HS, gw=GW, fp=None):
        payload = {"hs": hs}
        if gw is not None:
            payload["gw"] = gw
        if fp is not None:
            payload["fp"] = fp
        status, body, headers = self.issue_post(payload)
        self.assertEqual(status, 200)
        parsed = contract.parse_pair_uri(body["uri"])
        return body, parsed, headers

    def test_default_off_is_404_even_with_a_bearer_header(self):
        self._disable_issue_token()
        for path, body in (
            (ISSUE_LIST_PATH, {}),
            (ISSUE_REVOKE_PATH, {"ids": [1]}),
        ):
            with self.subTest(path=path):
                status, _, _ = self.admin_post(path, body)
                self.assertEqual(status, 404)
                status, _, _ = self.admin_other("GET", path)
                self.assertEqual(status, 404)

    def test_missing_or_wrong_token_is_401(self):
        for path, body, token in (
            (ISSUE_LIST_PATH, {}, None),
            (ISSUE_LIST_PATH, {}, OTHER_TOKEN),
            (ISSUE_REVOKE_PATH, {"ids": [1]}, None),
            (ISSUE_REVOKE_PATH, {"ids": [1]}, OTHER_TOKEN),
        ):
            with self.subTest(path=path, token=token):
                status, payload, _ = self.admin_post(path, body, token=token)
                self.assertEqual(status, 401)
                self.assertEqual(payload, {"error": "unauthorized"})
        events = self.store.events(kind="rejected")
        self.assertGreaterEqual(len(events), 4)
        for event in events:
            self.assertEqual(event["detail"].get("reason"), "unauthorized")
            leaks = _secrets_in(event)
            self.assertEqual(leaks, [], leaks)
        self.assertEqual(self.store.list_codes(now=self.now), [])

    def test_list_success_filters_and_omits_secrets(self):
        first, parsed_first, _ = self._issue(hs=HS, gw=GW)
        second, parsed_second, _ = self._issue(hs=OTHER_HS, gw=None)
        status, payload, headers = self.admin_post(ISSUE_LIST_PATH, {})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(set(payload), {"items"})
        self.assertEqual(len(payload["items"]), 2)
        ids = [item["id"] for item in payload["items"]]
        self.assertEqual(ids, sorted(ids, reverse=True))
        for item in payload["items"]:
            self.assertEqual(set(item), set(PUBLIC_RECORD_KEYS))
        leaks = _secrets_in(
            payload, codes=(parsed_first["code"], parsed_second["code"]),
            uris=(first["uri"], second["uri"]))
        self.assertEqual(leaks, [], leaks)

        status, by_hs, _ = self.admin_post(ISSUE_LIST_PATH, {"hs": OTHER_HS})
        self.assertEqual(status, 200)
        self.assertEqual(len(by_hs["items"]), 1)
        self.assertEqual(by_hs["items"][0]["hs"], OTHER_HS)
        self.assertIsNone(by_hs["items"][0]["gw"])
        self.assertFalse(by_hs["items"][0]["expired"])

        status, page, _ = self.admin_post(ISSUE_LIST_PATH, {"limit": 1})
        self.assertEqual(status, 200)
        self.assertEqual(len(page["items"]), 1)
        self.assertIn("next_after_id", page)
        status, rest, _ = self.admin_post(
            ISSUE_LIST_PATH, {"limit": 1, "after_id": page["next_after_id"]})
        self.assertEqual(status, 200)
        self.assertEqual(len(rest["items"]), 1)
        self.assertNotEqual(rest["items"][0]["id"], page["items"][0]["id"])

    def test_expired_filter_and_derived_field(self):
        body, parsed, _ = self._issue()
        self.now += 400
        status, expired, _ = self.admin_post(
            ISSUE_LIST_PATH, {"expired": True})
        self.assertEqual(status, 200)
        self.assertEqual(len(expired["items"]), 1)
        self.assertTrue(expired["items"][0]["expired"])
        self.assertEqual(expired["items"][0]["status"], "unused")
        status, active, _ = self.admin_post(
            ISSUE_LIST_PATH, {"expired": False})
        self.assertEqual(status, 200)
        self.assertEqual(active["items"], [])
        leaks = _secrets_in(
            expired, codes=(parsed["code"],), uris=(body["uri"],))
        self.assertEqual(leaks, [], leaks)

    def test_bulk_revoke_and_redeem_rejected(self):
        unused, parsed_unused, _ = self._issue()
        used, parsed_used, _ = self._issue()
        status, redeem_body, _ = self.post({
            "code": parsed_used["code"],
            "device_display_name": DEVICE,
        })
        self.assertEqual(status, 200)
        status, listed, _ = self.admin_post(ISSUE_LIST_PATH, {})
        self.assertEqual(status, 200)
        by_code = {}
        # Map via exp/hs order: look up by issued listing vs known URIs
        # is unnecessary — use store get on sha256 only in this test
        # helper path through list fields (no digest in HTTP).
        unused_id = None
        used_id = None
        for item in listed["items"]:
            if item["status"] == "used":
                used_id = item["id"]
                self.assertEqual(item["device_display_name"], DEVICE)
            elif item["status"] == "unused":
                unused_id = item["id"]
        self.assertIsNotNone(unused_id)
        self.assertIsNotNone(used_id)

        status, payload, headers = self.admin_post(
            ISSUE_REVOKE_PATH,
            {"ids": [unused_id, used_id, unused_id, 999999]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(payload, {"results": [
            {"id": unused_id, "result": "revoked"},
            {"id": used_id, "result": "used"},
            {"id": unused_id, "result": "already_revoked"},
            {"id": 999999, "result": "not_found"},
        ]})
        leaks = _secrets_in(
            payload,
            codes=(parsed_unused["code"], parsed_used["code"]),
            uris=(unused["uri"], used["uri"]),
        )
        self.assertEqual(leaks, [], leaks)

        status, redeem, _ = self.post({
            "code": parsed_unused["code"],
            "device_display_name": DEVICE,
        })
        self.assertEqual(status, 410)
        self.assertEqual(redeem["errcode"], "BV_PAIR_REVOKED")

        events = self.store.events(kind="revoked")
        self.assertTrue(any(ev["detail"].get("id") == unused_id for ev in events))
        for ev in events:
            blob = json.dumps(ev, ensure_ascii=False)
            self.assertNotIn(ISSUE_TOKEN, blob)
            self.assertNotIn(parsed_unused["code"], blob)
            self.assertNotIn(unused["uri"], blob)
            self.assertNotIn("botville://pair", blob)

        self.assertNotIn(ISSUE_TOKEN, self.logs())
        self.assertNotIn(parsed_unused["code"], self.logs())
        self.assertNotIn(unused["uri"], self.logs())

    def test_query_string_and_bad_bodies_are_rejected(self):
        status, payload, _ = self.post(
            {}, path=ISSUE_LIST_PATH + "?hs=https://evil.example.org",
            extra_headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer %s" % ISSUE_TOKEN,
            })
        self.assertEqual(status, 400)
        self.assertEqual(payload, {"error": "invalid request"})
        status, payload, _ = self.admin_post(
            ISSUE_LIST_PATH, {"status": "nope"})
        self.assertEqual(status, 400)
        status, payload, _ = self.admin_post(
            ISSUE_REVOKE_PATH, {"ids": []})
        self.assertEqual(status, 400)
        self.assertEqual(self.store.list_codes(now=self.now), [])
        for ev in self.store.events(kind="rejected"):
            leaks = _secrets_in(ev)
            self.assertEqual(leaks, [], leaks)

    def test_wrong_methods_are_405_when_enabled(self):
        for path in (ISSUE_LIST_PATH, ISSUE_REVOKE_PATH):
            status, _, _ = self.admin_other("GET", path)
            self.assertEqual(status, 405)
            status, _, _ = self.admin_other("PUT", path)
            self.assertEqual(status, 405)
