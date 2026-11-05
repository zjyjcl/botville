#!/usr/bin/env python3
"""Tests for server/pairing_qr_contract.py (issue #128 PR-a, contract-only).

Checks the reference implementation against the contract documents
(docs/contracts/pairing-qr.md + pairing-redeem.openapi.yaml) and the mock
examples (docs/contracts/examples/pairing-qr/):

- every example URI classifies as valid/invalid exactly as its filename says;
- duplicate / missing / unknown parameters are rejected, fp format rules hold,
  hs/gw accept https:// only;
- redeem 200 responses must echo the QR's hs byte-for-byte (no normalisation);
- BV_PAIR_RATE_LIMITED error bodies must carry retry_after_ms;
- the OpenAPI fragment lists all five BV_PAIR_* error codes;
- the examples directory contains nothing that looks like a real secret
  (PEM blocks, long hex runs, non-placeholder token-ish fields).

No Docker; standard library only.

Run:  python3 -m unittest discover -s server/tests -p "test_pairing_qr_contract.py" -v
"""

import importlib.util
import json
import re
import unittest
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_DIR.parent
CONTRACT_DIR = REPO_ROOT / "docs" / "contracts"
EXAMPLES_DIR = CONTRACT_DIR / "examples" / "pairing-qr"
OPENAPI = CONTRACT_DIR / "pairing-redeem.openapi.yaml"
MODULE = SERVER_DIR / "pairing_qr_contract.py"

HS = "https://hs.example.org"
GW = "https://gw.example.org"
CODE = "QRPAIREXAMPLECODE" + "0" * 25 + "A"  # 43 chars, canonical base64url
EXP = "1791000000"
FP = "sha256:ExampleSpkiSha256DigestForPairingQrContracA"

ERROR_STATUS = {
    "BV_PAIR_INVALID": 400,
    "BV_PAIR_EXPIRED": 410,
    "BV_PAIR_USED": 409,
    "BV_PAIR_REVOKED": 410,
    "BV_PAIR_RATE_LIMITED": 429,
}

SECRETISH_KEYS = {
    "token", "access_token", "password", "secret", "client_secret",
    "api_key", "apikey", "private_key",
}
PLACEHOLDER_MARKS = ("example", "redacted", "change-me", "mock", "placeholder")


def load_contract():
    spec = importlib.util.spec_from_file_location("pairing_qr_contract", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


contract = load_contract()


def make_uri(**overrides):
    """A canonical botville://pair URI; pass None to omit a parameter."""
    params = {"v": "1", "hs": HS, "gw": GW, "code": CODE, "exp": EXP}
    for key, value in overrides.items():
        if value is None:
            params.pop(key, None)
        else:
            params[key] = value
    order = ("v", "hs", "gw", "fp", "code", "exp")
    parts = ["%s=%s" % (key, params[key]) for key in order if key in params]
    return "botville://pair?" + "&".join(parts)


def ok_response(**overrides):
    """A canonical 200 redeem response; overrides replace / add fields."""
    resp = {
        "v": "1",
        "homeserver": HS,
        "gateway": GW,
        "auth": {
            "type": "registration_token",
            "token": "<redacted-example>",
            "expires_in_ms": 600000,
        },
        "features_url": "https://gw.example.org/features.json",
    }
    resp.update(overrides)
    return resp


class UriParseTest(unittest.TestCase):
    def test_valid_minimal(self):
        parsed = contract.parse_pair_uri(make_uri())
        self.assertEqual(parsed, {
            "v": "1", "hs": HS, "gw": GW, "code": CODE, "exp": int(EXP),
        })
        self.assertIsInstance(parsed["exp"], int)
        self.assertNotIn("fp", parsed)  # fp optional, absent here

    def test_valid_with_fp(self):
        parsed = contract.parse_pair_uri(make_uri(fp=FP))
        self.assertEqual(parsed["fp"], FP)

    def test_valid_param_order_and_outer_whitespace(self):
        shuffled = ("botville://pair?exp=%s&code=%s&gw=%s&fp=%s&hs=%s&v=1"
                    % (EXP, CODE, GW, FP, HS))
        self.assertEqual(contract.parse_pair_uri("  \n" + shuffled + "\t\n"),
                         contract.parse_pair_uri(make_uri(fp=FP)))

    def test_missing_required_rejected(self):
        for key in ("v", "hs", "gw", "code", "exp"):
            with self.assertRaises(contract.PairUriError, msg=key):
                contract.parse_pair_uri(make_uri(**{key: None}))
            with self.assertRaises(contract.PairUriError, msg=key + "="):
                contract.parse_pair_uri(make_uri(**{key: ""}))

    def test_duplicate_param_rejected(self):
        for key, value in (("v", "1"), ("hs", HS), ("code", CODE), ("exp", EXP)):
            with self.assertRaises(contract.PairUriError, msg=key):
                contract.parse_pair_uri(make_uri() + "&%s=%s" % (key, value))

    def test_unknown_param_rejected(self):
        for extra in ("&foo=1", "&user=@alice:hs.example.org", "&V=1", "&fp2=" + FP):
            with self.assertRaises(contract.PairUriError, msg=extra):
                contract.parse_pair_uri(make_uri() + extra)

    def test_unknown_v_rejected(self):
        for value in ("2", "01", "1.0", " 1", "one", "v1"):
            with self.assertRaises(contract.PairUriError, msg=value):
                contract.parse_pair_uri(make_uri(v=value))

    def test_https_only(self):
        for value in ("http://hs.example.org", "HTTPS://hs.example.org",
                      "//hs.example.org", "ftp://hs.example.org", "https://",
                      "hs.example.org"):
            with self.assertRaises(contract.PairUriError, msg=value):
                contract.parse_pair_uri(make_uri(hs=value))
            with self.assertRaises(contract.PairUriError, msg=value):
                contract.parse_pair_uri(make_uri(gw=value))

    def test_url_shape_rules(self):
        for value in ("https://user@hs.example.org", "https://:pw@hs.example.org",
                      "https://hs.example.org/?x=1", "https://hs.example.org/#f",
                      "https://hs.example.org:99999", "https://hs.example.org/%41",
                      "https://hs.example.org/a b"):
            with self.assertRaises(contract.PairUriError, msg=value):
                contract.parse_pair_uri(make_uri(hs=value))
        # port and path are fine; trailing slash is a different string but valid
        parsed = contract.parse_pair_uri(
            make_uri(gw="https://gw.example.org:8448/botville"))
        self.assertEqual(parsed["gw"], "https://gw.example.org:8448/botville")
        self.assertEqual(contract.parse_pair_uri(make_uri(hs=HS + "/"))["hs"], HS + "/")

    def test_code_format(self):
        for value in (CODE[:-1], CODE + "0", CODE.replace("Q", "."), CODE[:-1] + "B",
                      CODE.lower()):
            with self.assertRaises(contract.PairUriError, msg=value):
                contract.parse_pair_uri(make_uri(code=value))
        # 'B' as the last char decodes fine but is a non-canonical encoding
        # (low bits non-zero); 'A' is canonical — both differ only in that bit.
        self.assertEqual(contract.parse_pair_uri(make_uri(code=CODE))["code"], CODE)

    def test_fp_format(self):
        digest = FP[len("sha256:"):]
        for value in (digest,  # missing prefix
                      "sha256:" + digest[:-1],  # 42 chars
                      "sha256:" + digest + "0",  # 44 chars
                      "sha256:" + digest.replace("E", "#"),
                      "sha256:" + "A" * 42 + "B",  # non-canonical encoding
                      "sha256:" + "0" * 64,  # hex sha256, wrong encoding
                      "SHA256:" + digest,  # prefix is case-sensitive
                      "sha256:"):
            with self.assertRaises(contract.PairUriError, msg=value):
                contract.parse_pair_uri(make_uri(fp=value))

    def test_exp_format(self):
        for value in ("0", "00", "-1", "1.0", "abc", "", " 1", "1 1",
                      "9" * 11, "0" + EXP):
            with self.assertRaises(contract.PairUriError, msg=value):
                contract.parse_pair_uri(make_uri(exp=value))

    def test_shape_rejected(self):
        federation = ("botville-pair:v1:hs.example.org:aHR0cHM6Ly9ocy5leGFtcGxl"
                      ".b3Jn:invite-1:<redacted-example>")
        for uri in ("", "botville://pair", "botville://pair?", "botville://pairing?v=1",
                    "BOTVILLE://pair?v=1", "https://example.org/?v=1",
                    make_uri() + "#f", make_uri() + "&", make_uri().replace("v=1", "v1"),
                    make_uri().replace("https", "https%", 1) + "%41",
                    "botville://pair?v=1&&hs=" + HS,
                    federation):
            with self.assertRaises(contract.PairUriError, msg=uri):
                contract.parse_pair_uri(uri)

    def test_error_hierarchy(self):
        self.assertTrue(issubclass(contract.PairUriError, contract.PairContractError))
        self.assertTrue(issubclass(contract.PairRedeemError, contract.PairContractError))
        self.assertTrue(issubclass(contract.PairContractError, ValueError))


class ExamplesTest(unittest.TestCase):
    def test_uri_examples_classification(self):
        files = sorted(EXAMPLES_DIR.glob("uri-*.txt"))
        self.assertGreaterEqual(len(files), 9, "expected 2 valid + 7 invalid examples")
        names = " ".join(path.name for path in files)
        for category in ("duplicate", "missing", "http", "fp"):
            self.assertIn(category, names, "missing invalid-example category: " + category)
        valid = invalid = 0
        for path in files:
            text = path.read_text(encoding="utf-8").strip()
            self.assertTrue(text, "empty example: %s" % path)
            self.assertNotIn("\n", text, "one URI per file: %s" % path)
            if path.name.startswith("uri-valid"):
                parsed = contract.parse_pair_uri(text)  # must not raise
                self.assertEqual((parsed["hs"], parsed["gw"], parsed["code"]),
                                 (HS, GW, CODE), path.name)
                valid += 1
            else:
                self.assertTrue(path.name.startswith("uri-invalid"), path.name)
                with self.assertRaises(contract.PairUriError, msg=path.name):
                    contract.parse_pair_uri(text)
                invalid += 1
        self.assertGreaterEqual(valid, 2)
        self.assertGreaterEqual(invalid, 7)

    def test_valid_fp_example_carries_fp(self):
        text = (EXAMPLES_DIR / "uri-valid-with-fp.txt").read_text(encoding="utf-8")
        parsed = contract.parse_pair_uri(text)
        self.assertEqual(parsed["fp"], FP)

    def test_redeem_request_example(self):
        data = json.loads((EXAMPLES_DIR / "redeem-request.json").read_text(encoding="utf-8"))
        self.assertEqual(set(data), {"code", "device_display_name"})
        self.assertEqual(data["code"], CODE)
        self.assertTrue(1 <= len(data["device_display_name"]) <= 128)

    def test_redeem_200_examples(self):
        for name in ("redeem-200-registration-token.json", "redeem-200-login-token.json"):
            data = json.loads((EXAMPLES_DIR / name).read_text(encoding="utf-8"))
            result = contract.validate_redeem_response(data, HS)  # must not raise
            self.assertIn(result["auth"]["type"], contract.AUTH_TYPES)
            self.assertEqual(result["homeserver"], HS)
            self.assertEqual(data["auth"]["token"], "<redacted-example>")

    def test_error_examples_cover_all_codes(self):
        files = sorted(EXAMPLES_DIR.glob("redeem-*-bv-pair-*.json"))
        found = {}
        for path in files:
            parts = path.stem.split("-")  # redeem-<status>-bv-pair-<code>
            self.assertEqual(parts[0], "redeem", path)
            status = int(parts[1])
            errcode = "_".join(parts[2:]).upper()  # bv-pair-invalid -> BV_PAIR_INVALID
            data = json.loads(path.read_text(encoding="utf-8"))
            validated = contract.validate_error(data)  # must not raise
            self.assertEqual(validated["errcode"], errcode, path)
            self.assertEqual(ERROR_STATUS[errcode], status, path)
            found[errcode] = validated
        self.assertEqual(set(found), set(ERROR_STATUS),
                         "exactly one example per error code")
        self.assertIn("retry_after_ms", found["BV_PAIR_RATE_LIMITED"])
        for errcode, validated in found.items():
            if errcode != "BV_PAIR_RATE_LIMITED":
                self.assertNotIn("retry_after_ms", validated, errcode)


class RedeemResponseTest(unittest.TestCase):
    def test_valid_responses(self):
        result = contract.validate_redeem_response(ok_response(), HS)
        self.assertEqual(result, {
            "v": "1", "homeserver": HS, "gateway": GW,
            "auth": {"type": "registration_token", "token": "<redacted-example>",
                     "expires_in_ms": 600000},
            "features_url": "https://gw.example.org/features.json",
        })
        for auth_type in contract.AUTH_TYPES:
            resp = ok_response(auth={"type": auth_type, "token": "<redacted-example>",
                                     "expires_in_ms": 120000})
            self.assertEqual(
                contract.validate_redeem_response(resp, HS)["auth"]["type"], auth_type)

    def test_homeserver_mismatch_rejected(self):
        for bad in ("https://hs.example.org/",  # trailing slash = different string
                    "https://evil.example.org", "https://hs2.example.org",
                    "https://hs.example.org:8448", "https://HS.EXAMPLE.ORG",
                    None, 123):
            resp = ok_response(homeserver=bad)
            with self.assertRaises(contract.PairRedeemError, msg=repr(bad)):
                contract.validate_redeem_response(resp, HS)

    def test_missing_and_bad_fields_rejected(self):
        for key in ("v", "homeserver", "gateway", "auth", "features_url"):
            resp = ok_response()
            del resp[key]
            with self.assertRaises(contract.PairRedeemError, msg=key):
                contract.validate_redeem_response(resp, HS)
        with self.assertRaises(contract.PairRedeemError):
            contract.validate_redeem_response(ok_response(v="2"), HS)
        for url in ("http://gw.example.org", "https://", "https://g@w.example.org"):
            with self.assertRaises(contract.PairRedeemError, msg=url):
                contract.validate_redeem_response(ok_response(gateway=url), HS)
            with self.assertRaises(contract.PairRedeemError, msg=url):
                contract.validate_redeem_response(ok_response(features_url=url), HS)

    def test_auth_validation(self):
        bad_auths = (
            "registration_token", [], None,
            {"token": "<redacted-example>", "expires_in_ms": 60000},
            {"type": "magic_token", "token": "<redacted-example>", "expires_in_ms": 1},
            {"type": "registration_token", "token": "", "expires_in_ms": 1},
            {"type": "registration_token", "token": None, "expires_in_ms": 1},
            {"type": "registration_token", "token": "<redacted-example>"},
            {"type": "registration_token", "token": "<redacted-example>", "expires_in_ms": 0},
            {"type": "registration_token", "token": "<redacted-example>", "expires_in_ms": -5},
            {"type": "registration_token", "token": "<redacted-example>", "expires_in_ms": True},
            {"type": "registration_token", "token": "<redacted-example>", "expires_in_ms": "600000"},
        )
        for bad in bad_auths:
            with self.assertRaises(contract.PairRedeemError, msg=repr(bad)):
                contract.validate_redeem_response(ok_response(auth=bad), HS)

    def test_unknown_fields_ignored(self):
        resp = ok_response(extra_field={"nested": True}, auth={
            "type": "login_token", "token": "<redacted-example>",
            "expires_in_ms": 120000, "junk": 1})
        result = contract.validate_redeem_response(resp, HS)
        self.assertNotIn("extra_field", result)
        self.assertEqual(set(result["auth"]), {"type", "token", "expires_in_ms"})


class ErrorBodyTest(unittest.TestCase):
    def test_all_codes_valid(self):
        for errcode in ERROR_STATUS:
            body = {"errcode": errcode, "error": "msg"}
            if errcode == "BV_PAIR_RATE_LIMITED":
                body["retry_after_ms"] = 60000
            validated = contract.validate_error(body)
            self.assertEqual(validated["errcode"], errcode)

    def test_rate_limited_requires_retry_after_ms(self):
        with self.assertRaises(contract.PairRedeemError):
            contract.validate_error({"errcode": "BV_PAIR_RATE_LIMITED",
                                     "error": "slow down"})
        for bad in (0, -1, True, "60000", 1.5, None):
            with self.assertRaises(contract.PairRedeemError, msg=repr(bad)):
                contract.validate_error({"errcode": "BV_PAIR_RATE_LIMITED",
                                         "error": "slow down", "retry_after_ms": bad})

    def test_optional_retry_after_ms_on_other_codes(self):
        validated = contract.validate_error({"errcode": "BV_PAIR_INVALID",
                                             "error": "bad", "retry_after_ms": 1000})
        self.assertEqual(validated["retry_after_ms"], 1000)

    def test_bad_error_bodies_rejected(self):
        for body in (None, [], {}, {"errcode": "BV_PAIR_NOPE", "error": "x"},
                     {"errcode": "BV_PAIR_USED"},
                     {"errcode": "BV_PAIR_USED", "error": ""},
                     {"errcode": "BV_PAIR_USED", "error": 42},
                     {"errcode": None, "error": "x"},
                     {"errcode": "bv_pair_used", "error": "lowercase"}):
            with self.assertRaises(contract.PairRedeemError, msg=repr(body)):
                contract.validate_error(body)

    def test_error_status_mapping_pinned(self):
        self.assertEqual(contract.ERROR_STATUS, ERROR_STATUS)


class OpenApiCoverageTest(unittest.TestCase):
    def test_all_error_codes_present(self):
        text = OPENAPI.read_text(encoding="utf-8")
        for errcode in ERROR_STATUS:
            self.assertIn(errcode, text, errcode)
            self.assertIn(str(ERROR_STATUS[errcode]), text, errcode)
        for needed in ("/_botville/pair/redeem", "retry_after_ms",
                       "registration_token", "login_token", "homeserver",
                       "BV_PAIR_RATE_LIMITED"):
            self.assertIn(needed, text)

    def test_no_real_hosts_in_openapi(self):
        text = OPENAPI.read_text(encoding="utf-8")
        self.assertIn("hs.example.org", text)  # placeholders only
        for banned in ("https://matrix.org", "https://api.openai.com"):
            self.assertNotIn(banned, text)


class ExamplesSecretScanTest(unittest.TestCase):
    """Simple scan: nothing in the examples may look like a real secret."""

    LONG_HEX = re.compile(r"[0-9a-fA-F]{32,}")

    def _placeholder(self, value):
        text = value if isinstance(value, str) else str(value)
        lowered = text.lower()
        return any(mark in lowered for mark in PLACEHOLDER_MARKS) or text.startswith("<")

    def test_examples_contain_no_secrets(self):
        files = sorted(p for p in EXAMPLES_DIR.iterdir() if p.is_file())
        self.assertGreaterEqual(len(files), 16)
        for path in files:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("-----BEGIN", path.name + ": PEM block")
            self.assertIsNone(self.LONG_HEX.search(text),
                              "%s: long hex run (possible key/hash secret)" % path)
            self.assertNotIn("Bearer ", text, path)
            if path.suffix == ".json":
                data = json.loads(text)
                stack = [data]
                while stack:
                    node = stack.pop()
                    if isinstance(node, dict):
                        for key, value in node.items():
                            if key.lower() in SECRETISH_KEYS:
                                self.assertTrue(
                                    self._placeholder(value),
                                    "%s: %s must be an obvious placeholder, got %r"
                                    % (path, key, value))
                            stack.append(value)
                    elif isinstance(node, list):
                        stack.extend(node)


if __name__ == "__main__":
    unittest.main()
