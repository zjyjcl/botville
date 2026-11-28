"""Authenticated admin issuance for App QR pairing codes (A-R9 / #128).

Default off: an empty BOTVILLE_PAIR_REDEEM_ISSUE_TOKEN means
POST /_botville/pair/issue is not offered (404). When set, the
token must be at least 32 characters, distinct from the Synapse
admin token, and sent as Authorization: Bearer. This path must
never be added to the public HTTPS edge allowlist, and it must
never issue without authentication.

First cut: registration_token only (pairing-qr.md §11 B4). No
login_token, no web UI, no one-time CLI bootstrap token, no
list/filter/batch revoke. Those stay later work.

Plaintext code appears once, in the returned URI. Logs and audit
rows store sha256(code) only.
"""

import argparse
import base64
import hmac
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from .store import (
    CODE_TTL_SECONDS,
    require_code,
    require_fp,
    require_https_url,
)

ISSUE_PATH = "/_botville/pair/issue"
ENV_ISSUE_TOKEN = "BOTVILLE_PAIR_REDEEM_ISSUE_TOKEN"
ISSUE_TOKEN_MIN_LEN = 32
# Known JSON fields for the admin issue body. auth_type is optional and
# may only be registration_token in this cut.
ISSUE_FIELDS = frozenset({"hs", "gw", "fp", "auth_type"})


class IssueError(Exception):
    """Admin issue request failed. Message is safe to return in JSON."""

    def __init__(self, status, error):
        self.status = status
        self.error = error
        super().__init__(error)


class IssueConfigError(Exception):
    """Issue-token configuration is invalid; refuse to start.

    The message names the env key only — never the token value.
    """


def validate_issue_token(token, synapse_admin_token=""):
    """Return the token, or '' when issuance stays off.

    Non-empty values must be at least ISSUE_TOKEN_MIN_LEN characters,
    contain no whitespace or controls, and must not equal the Synapse
    admin token.
    """
    if token is None or token == "":
        return ""
    if not isinstance(token, str):
        raise IssueConfigError("%s is invalid" % ENV_ISSUE_TOKEN)
    if any(ch.isspace() or ord(ch) < 0x20 or ch == "\x7f" for ch in token):
        raise IssueConfigError(
            "%s contains whitespace or control characters" % ENV_ISSUE_TOKEN)
    if len(token) < ISSUE_TOKEN_MIN_LEN:
        raise IssueConfigError("%s is too short" % ENV_ISSUE_TOKEN)
    other = synapse_admin_token or ""
    if other and hmac.compare_digest(token.encode("utf-8"), other.encode("utf-8")):
        raise IssueConfigError(
            "%s must be distinct from the Synapse admin token" % ENV_ISSUE_TOKEN)
    return token


def bearer_token(headers):
    """Return the Bearer token or None. Never logs the value."""
    # BaseHTTPRequestHandler headers are case-insensitive.
    raw = headers.get("Authorization") if headers is not None else None
    if not raw or not isinstance(raw, str):
        return None
    if not raw.startswith("Bearer "):
        return None
    token = raw[7:]
    if not token or any(ch.isspace() or ord(ch) < 0x20 or ch == "\x7f" for ch in token):
        return None
    return token


def issue_authorized(provided, expected):
    if not expected or not provided:
        return False
    if len(provided) != len(expected):
        # compare_digest still works with different lengths in modern
        # Python, but keep the check obvious and avoid leaking length
        # via exception type.
        hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def generate_code():
    raw = secrets.token_bytes(32)
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def build_pair_uri(hs, code, exp, gw=None, fp=None):
    """Build a botville://pair URI. Values are already canonical (no %)."""
    require_https_url(hs, "hs")
    require_code(code)
    parts = ["v=1", "hs=%s" % hs]
    if gw is not None:
        require_https_url(gw, "gw")
        parts.append("gw=%s" % gw)
    if fp is not None:
        require_fp(fp)
        parts.append("fp=%s" % fp)
    parts.append("code=%s" % code)
    parts.append("exp=%s" % int(exp))
    return "botville://pair?" + "&".join(parts)


def parse_issue_request(raw, content_type):
    """Parse the admin issue JSON body. Raises IssueError(400, ...)."""
    if raw is None or len(raw) > 64 * 1024:
        raise IssueError(400, "invalid request")
    if not _json_media_type(content_type):
        raise IssueError(400, "invalid request")
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise IssueError(400, "invalid request") from None
    if not isinstance(body, dict) or set(body) - ISSUE_FIELDS:
        raise IssueError(400, "invalid request")
    hs = body.get("hs")
    if not isinstance(hs, str):
        raise IssueError(400, "invalid request")
    try:
        require_https_url(hs, "hs")
    except ValueError:
        raise IssueError(400, "invalid request") from None
    gw = body.get("gw", None)
    if "gw" in body:
        if not isinstance(gw, str):
            raise IssueError(400, "invalid request")
        try:
            require_https_url(gw, "gw")
        except ValueError:
            raise IssueError(400, "invalid request") from None
    fp = body.get("fp", None)
    if "fp" in body:
        if not isinstance(fp, str):
            raise IssueError(400, "invalid request")
        try:
            require_fp(fp)
        except ValueError:
            raise IssueError(400, "invalid request") from None
    auth_type = body.get("auth_type", "registration_token")
    if auth_type != "registration_token":
        # B4: this cut does not issue login_token, including for "self".
        raise IssueError(400, "invalid request")
    return {"hs": hs, "gw": gw, "fp": fp, "auth_type": "registration_token"}


def issue_code(store, hs, gw=None, fp=None, now=None, code=None):
    """Persist sha256(code) and return the one-time URI payload.

    ``code`` is only injectable for tests. Production callers pass None
    so a fresh CSPRNG value is used. The plaintext is not stored.
    """
    now = int(time.time() if now is None else now)
    if code is None:
        code = generate_code()
    record_id, digest, exp = store.issue(
        hs, code, gw=gw, fp=fp, now=now, ttl_seconds=CODE_TTL_SECONDS,
        auth_type="registration_token")
    exp_int = int(exp)
    uri = build_pair_uri(hs, code, exp_int, gw=gw, fp=fp)
    return {
        "uri": uri,
        "exp": exp_int,
        "auth_type": "registration_token",
        "code_sha256": digest,
        "id": record_id,
    }


def public_issue_payload(result):
    """HTTP/CLI response: URI + exp + type. No separate code field."""
    return {
        "uri": result["uri"],
        "exp": result["exp"],
        "auth_type": result["auth_type"],
    }


def _json_media_type(content_type):
    if not content_type:
        return False
    media = content_type.split(";", 1)[0].strip().lower()
    return media == "application/json"


def require_loopback_issue_url(url):
    """CLI may only POST to a loopback HTTP URL (never a public host)."""
    if not isinstance(url, str) or not url:
        raise IssueError(400, "invalid issue url")
    try:
        parts = urllib.parse.urlsplit(url)
        parts.port
    except ValueError as exc:
        raise IssueError(400, "invalid issue url") from exc
    if parts.scheme != "http":
        raise IssueError(400, "invalid issue url")
    if parts.username is not None or parts.password is not None:
        raise IssueError(400, "invalid issue url")
    host = parts.hostname
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise IssueError(400, "invalid issue url")
    if parts.path in ("", "/"):
        path = ISSUE_PATH
    else:
        path = parts.path
    if path != ISSUE_PATH:
        raise IssueError(400, "invalid issue url")
    return urllib.parse.urlunsplit((
        parts.scheme, parts.netloc, ISSUE_PATH, "", "",
    ))


def issue_via_http(url, token, hs, gw=None, fp=None, timeout=10):
    target = require_loopback_issue_url(url)
    body = {"hs": hs, "auth_type": "registration_token"}
    if gw is not None:
        body["gw"] = gw
    if fp is not None:
        body["fp"] = fp
    raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        target, data=raw, method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer %s" % token,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            return resp.status, payload
    except urllib.error.HTTPError as exc:
        raw_body = exc.read()
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (ValueError, UnicodeError):
            payload = {}
        return exc.code, payload


def cli_main(argv, env=None, stdout=None, stderr=None):
    """python -m pair_redeem issue --hs https://hs.example.org

    The issue token is read from BOTVILLE_PAIR_REDEEM_ISSUE_TOKEN only
    (never from argv, which would show up in process listings).
    """
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    env = os.environ if env is None else env
    parser = argparse.ArgumentParser(
        prog="python -m pair_redeem issue",
        description=(
            "Issue one App QR pairing URI on the loopback admin API. "
            "Default off until BOTVILLE_PAIR_REDEEM_ISSUE_TOKEN is set. "
            "Not a public HTTPS edge and not a phone-scan test. "
            "Examples use example.org only."
        ),
    )
    parser.add_argument("--hs", required=True, help="https:// homeserver base URL")
    parser.add_argument("--gw", default=None, help="optional https:// gateway base URL")
    parser.add_argument("--fp", default=None, help="optional sha256:<SPKI digest>")
    parser.add_argument(
        "--url", default="http://127.0.0.1:8092/_botville/pair/issue",
        help="loopback issue URL (default http://127.0.0.1:8092/_botville/pair/issue)",
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    token = env.get(ENV_ISSUE_TOKEN, "")
    if not token:
        stderr.write("issuance is disabled: %s is empty\n" % ENV_ISSUE_TOKEN)
        return 2
    try:
        validate_issue_token(token)
        require_https_url(args.hs, "hs")
        if args.gw is not None:
            require_https_url(args.gw, "gw")
        if args.fp is not None:
            require_fp(args.fp)
        status, payload = issue_via_http(
            args.url, token, args.hs, gw=args.gw, fp=args.fp)
    except (IssueConfigError, IssueError, ValueError,
            urllib.error.URLError, TimeoutError, OSError):
        # Do not print token, URI, or request bodies.
        stderr.write("issue failed\n")
        return 2
    if status != 200 or not isinstance(payload, dict) or "uri" not in payload:
        stderr.write("issue failed\n")
        return 1
    # The URI (which contains the one-time code) goes to stdout once.
    # Operators should not pipe this into a log file.
    stdout.write(payload["uri"] + "\n")
    return 0
