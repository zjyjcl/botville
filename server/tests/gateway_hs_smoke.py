#!/usr/bin/env python3
"""Smoke the compose gateway against a real homeserver (A2.2, AS transactions #192).

Verifies, against an already-running stack (`make up GATEWAY=1` on synapse
or tuwunel):

  0. (optional, --expect-transport) the gateway container really runs the
     transport `make up` would select. A gateway recreated with plain
     `docker compose up` falls back to the compose default (memory).
  1. Gateway GET /health is 200, `status=ok`, and the `matrix` check is ok.
     In appservice mode that check is the startup whoami with
     GATEWAY_AS_TOKEN (the process exits if whoami fails).
  2. OpenID userinfo: reuse (or, the first time, register) the fixture user
     on the homeserver, request an OpenID token, then GET /v1/moments on the gateway with
     `Authorization: MatrixOpenID ...`. HTTP 200 means the gateway validated
     the token via in-stack userinfo (GATEWAY_HOMESERVER_FEDERATION_URL is
     empty, so it falls back to GATEWAY_HOMESERVER_URL).
  3. (unless --skip-as-txn) AS transactions:
     a. HS -> gateway ping: POST /_matrix/client/v1/appservice/<id>/ping
        with the as_token; the homeserver calls the gateway's
        /_matrix/app/v1/ping (registration url http://gateway:8080).
     b. the fixed @ai_gwsmoke user is in the fixture room of the human user
        and the human sends a message; the homeserver pushes a transaction and the gateway
        stores it (checked in GATEWAY_AS_TXN_DB_PATH via --gateway-exec).
     c. restart the gateway (--gateway-restart), wait for /health, check the
        row survived, then replay the same txnId with the hs_token: HTTP 200
        and still exactly one stored row (idempotent by txnId). A wrong
        hs_token must get 403.

Fixtures do not pile up: the human user @gwsmoke (one fixed device, password
derived from the local registration token so even a deleted fixture file
logs back into the same account), @ai_gwsmoke and the room are reused across
runs. Their ids and the
human's password / access token live in --fixture-file (default
<env-file dir>/generated/gateway-hs-smoke.json, gitignored, mode 0600), keyed
by --hs-url. They are re-created only when the homeserver no longer knows
them (e.g. after `down -v`); a replaced room is left and forgotten.

Tokens, passwords and registration secrets are never printed: failures
report only the HTTP status and the full URL (scheme://host:port/path, never
a query string, body or header value). First failure exits non-zero.

  python3 server/tests/gateway_hs_smoke.py \\
      --hs-url http://127.0.0.1:8008 \\
      --gateway-url http://127.0.0.1:8020 \\
      --env-file server/compose/.env \\
      --expect-transport appservice \\
      --gateway-exec 'docker compose ... exec -T gateway' \\
      --gateway-restart 'docker compose ... restart gateway'
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REQUEST_TIMEOUT = 15.0
SUPPORTED_STAGES = ("m.login.registration_token", "m.login.dummy")
APPSERVICE_ID = "botville-gateway"
AI_FIXTURE_LOCALPART = "ai_gwsmoke"
HUMAN_FIXTURE_LOCALPART = "gwsmoke"
DEVICE_ID = "GWSMOKE"
AS_TXN_DB_PATH = "/var/lib/botville-gateway/as_transactions.db"

# Runs inside the gateway container (python:3.12-slim). Prints only counts
# and txn ids (not secret), never a stored body.
_DB_PROBE = r"""
import json, sqlite3, sys
db, needle = sys.argv[1], sys.argv[2]
conn = sqlite3.connect("file:" + db + "?mode=ro", uri=True)
rows = conn.execute(
    "SELECT txn_id FROM as_transactions WHERE instr(body, ?) > 0 ORDER BY received_at",
    (needle,),
).fetchall()
total = conn.execute("SELECT count(*) FROM as_transactions").fetchone()[0]
print(json.dumps({"txn_ids": [r[0] for r in rows], "total": total}))
"""


def env_value(path, key, default=""):
    """Last assignment of KEY in an env file (same rule as docker compose)."""
    value = default
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, val = line.split("=", 1)
                if name == key:
                    value = val
    except FileNotFoundError:
        pass
    return value


def safe_url(url):
    """URL without query string or fragment, for failure messages."""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def request_json(method, url, body=None, headers=None, timeout=REQUEST_TIMEOUT):
    """Return (status, decoded body or None). HTTP errors are not raised.

    Tokens only ever travel in headers passed by the caller. This function
    never prints the request or the body.
    """
    data = None
    req_headers = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return exc.code, None
    except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError):
        return 0, None


def fail(message):
    raise SystemExit("FAIL: " + message)


def http_fail(method, url, status, what=""):
    fail(
        "{} {} returned HTTP {}{}".format(
            method,
            safe_url(url),
            status or "unreachable",
            " ({})".format(what) if what else "",
        )
    )


def path_of(base, path, query=None):
    url = base.rstrip("/") + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    return url


def run_cmd(prefix, extra, what, input_text=None):
    """Run `prefix + extra` as an argv list (no shell). Returns stdout.

    On failure only the exit code and the step name are reported (the
    command output is not echoed: compose may print environment details).
    """
    argv = shlex.split(prefix) + list(extra)
    # Never hand our stdin to `docker compose exec` (it would swallow the
    # rest of a calling script fed through stdin, e.g. a heredoc).
    stdin_kw = {"input": input_text} if input_text is not None else {"stdin": subprocess.DEVNULL}
    proc = subprocess.run(
        argv, capture_output=True, text=True, timeout=180, **stdin_kw
    )
    if proc.returncode != 0:
        fail("{} failed (exit {})".format(what, proc.returncode))
    return proc.stdout


def check_transport(gateway_exec, expected):
    out = run_cmd(
        gateway_exec, ["printenv", "GATEWAY_MATRIX_TRANSPORT"], "printenv GATEWAY_MATRIX_TRANSPORT"
    ).strip()
    if out != expected:
        fail(
            "gateway runs GATEWAY_MATRIX_TRANSPORT={} but {} was expected; it was "
            "probably recreated outside `make up` (plain `docker compose up` falls "
            "back to the compose default memory). Run `make up HS=<hs> GATEWAY=1`.".format(
                out or "<unset>", expected
            )
        )
    print("PASS: gateway transport is {}".format(expected))


def wait_for_health(gateway_url, timeout):
    """Wait until GET /health is ok and the matrix check is ok."""
    url = path_of(gateway_url, "/health")
    deadline = time.monotonic() + timeout
    last_status = 0
    while time.monotonic() < deadline:
        status, body = request_json("GET", url)
        last_status = status
        if status == 200 and isinstance(body, dict) and body.get("status") == "ok":
            checks = {
                c.get("name"): c.get("status")
                for c in (body.get("checks") or [])
                if isinstance(c, dict)
            }
            if checks.get("matrix") == "ok":
                return
        time.sleep(2)
    fail(
        "GET {} did not report status=ok with matrix=ok within {}s (last HTTP {})".format(
            safe_url(url), timeout, last_status or "unreachable"
        )
    )


def register_user(hs_url, registration_token, username=None, password=None):
    """Generic UIA register; returns (user_id, access_token, password), never printed.

    With a fixed `username` (and its `password`), returns None when the
    homeserver says the name is taken (M_USER_IN_USE) so the caller can fall
    back. Without one, a random gwsmoke_<hex> name is used.

    Retries HTTP 429/503: Synapse's default rc_registration burst is small, so a
    smoke run right after `make e2e` seed can be rate-limited for a few seconds.
    """
    register_url = path_of(hs_url, "/_matrix/client/v3/register")
    deadline = time.monotonic() + 60
    last_status = 0
    while time.monotonic() < deadline:
        name = username or "gwsmoke_" + secrets.token_hex(4)
        secret = password if username else secrets.token_hex(16)
        body = {"username": name, "password": secret, "device_id": DEVICE_ID}
        flow = None
        rate_limited = False
        for _ in range(12):
            status, resp = request_json("POST", register_url, body=body)
            last_status = status
            if status == 200 and isinstance(resp, dict):
                user_id = resp.get("user_id")
                access_token = resp.get("access_token")
                if not user_id or not access_token:
                    http_fail("POST", register_url, status, "missing user_id or access_token")
                return user_id, access_token, secret
            if (
                username
                and status == 400
                and isinstance(resp, dict)
                and resp.get("errcode") == "M_USER_IN_USE"
            ):
                return None
            if status in (429, 503):
                rate_limited = True
                break
            if status != 401 or not isinstance(resp, dict):
                http_fail("POST", register_url, status)
            session = resp.get("session")
            if not session:
                http_fail("POST", register_url, status, "401 without UIA session")
            completed = set(resp.get("completed") or [])
            if flow is None:
                flows = resp.get("flows") or []
                candidates = [
                    f["stages"]
                    for f in flows
                    if f.get("stages") and all(s in SUPPORTED_STAGES for s in f["stages"])
                ]
                candidates.sort(key=lambda stages: "m.login.registration_token" not in stages)
                if not candidates:
                    http_fail("POST", register_url, status, "no supported UIA flow")
                flow = candidates[0]
            remaining = [s for s in flow if s not in completed]
            if not remaining:
                http_fail("POST", register_url, status, "UIA flow completed but still 401")
            stage = remaining[0]
            auth = {"session": session, "type": stage}
            if stage == "m.login.registration_token":
                auth["token"] = registration_token
            body["auth"] = auth
        else:
            http_fail("POST", register_url, last_status, "UIA did not complete")
        if not rate_limited:
            http_fail("POST", register_url, last_status)
        time.sleep(5)
    http_fail("POST", register_url, last_status, "still rate-limited after 60s")


# --- reusable fixtures -------------------------------------------------------


def default_fixture_file(env_file):
    base = os.path.dirname(os.path.abspath(env_file)) if env_file else os.getcwd()
    return os.path.join(base, "generated", "gateway-hs-smoke.json")


def load_fixtures(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_fixtures(path, data):
    """Write atomically with mode 0600 (holds a password and a token)."""
    if not path:
        return
    parent = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(parent):
        # Created 0700 explicitly (makedirs' mode is masked by the umask and
        # only applies to the leaf); init.sh keeps generated/ 0700 as well.
        os.makedirs(parent, mode=0o700, exist_ok=True)
        os.chmod(parent, 0o700)
    tmp = "{}.tmp.{}".format(path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# Login/whoami answers that may heal by waiting: rate limit, HS errors,
# unreachable (status 0). Anything else is definitive.
AUTH_RETRIES = 5
AUTH_RETRY_SLEEP = 2


def _transient(status):
    return status == 0 or status == 429 or status >= 500


def whoami_status(hs_url, access_token):
    """(status, user_id or None); bounded retries on transient answers."""
    url = path_of(hs_url, "/_matrix/client/v3/account/whoami")
    status = 0
    for attempt in range(AUTH_RETRIES):
        status, resp = request_json("GET", url, headers={"Authorization": "Bearer " + access_token})
        if status == 200 and isinstance(resp, dict) and resp.get("user_id"):
            return status, resp["user_id"]
        if not _transient(status):
            return status, None
        if attempt + 1 < AUTH_RETRIES:
            time.sleep(AUTH_RETRY_SLEEP)
    return status, None


def whoami_after_login(hs_url, access_token, expected=None):
    """whoami right after a successful login: must work. A failure here is a
    homeserver problem, not a password mismatch, so it never falls through to
    creating a random user -- it fails (after bounded retries)."""
    url = path_of(hs_url, "/_matrix/client/v3/account/whoami")
    status, user_id = whoami_status(hs_url, access_token)
    if not user_id:
        http_fail("GET", url, status, "whoami failed right after a successful login")
    if expected and user_id != expected:
        fail("whoami after login returned another user than the one logged in")
    return user_id


def password_login(hs_url, user_id, password):
    """Log in on the fixed device (reused, so no new devices pile up).

    Returns the access token, or None only when the homeserver definitively
    rejects the credentials (403, e.g. M_FORBIDDEN: wrong password or no such
    user). Transient answers are retried a bounded number of times, then fail;
    any other status fails at once.
    """
    url = path_of(hs_url, "/_matrix/client/v3/login")
    status = 0
    for attempt in range(AUTH_RETRIES):
        status, resp = request_json(
            "POST",
            url,
            body={
                "type": "m.login.password",
                "identifier": {"type": "m.id.user", "user": user_id},
                "password": password,
                "device_id": DEVICE_ID,
            },
        )
        if status == 200 and isinstance(resp, dict) and resp.get("access_token"):
            return resp["access_token"]
        if status == 403:
            return None
        if not _transient(status):
            http_fail("POST", url, status, "password login")
        if attempt + 1 < AUTH_RETRIES:
            time.sleep(AUTH_RETRY_SLEEP)
    http_fail("POST", url, status, "password login: still failing after retries")


def fixture_password(registration_token):
    """Deterministic password of the fixed @gwsmoke user.

    HMAC of the local BOTVILLE_REGISTRATION_TOKEN, so a lost fixture file does
    not force a new user: the same @gwsmoke account is logged into again.
    Never printed. Anyone holding the registration token could already
    register accounts, so this grants nothing extra (a throwaway test user).
    """
    return hmac.new(
        registration_token.encode("utf-8"), b"botville.gwsmoke.fixture.v1", hashlib.sha256
    ).hexdigest()


def ensure_human(hs_url, registration_token, fixture):
    """Reuse the fixture user; never pile up new users.

    Order: the stored token (whoami) -> the stored password (login on the
    fixed device) -> the fixed @gwsmoke user with its derived password (login,
    else register). Only when the password mismatch is CONFIRMED -- login
    rejected with 403 and registering @gwsmoke says M_USER_IN_USE (e.g. the
    registration token was rotated) -- is a random gwsmoke_<hex> user created,
    once, and then reused through the fixture file. A login that succeeds but
    whose whoami then fails, or a homeserver that keeps answering 429/5xx,
    fails the smoke instead (bounded retries).

    Returns (user_id, access_token); updates `fixture` in place.
    """
    user_id = fixture.get("user_id")
    token = fixture.get("access_token")
    if user_id and token:
        status, current = whoami_status(hs_url, token)
        if current == user_id:
            return user_id, token
        if _transient(status):
            http_fail("GET", path_of(hs_url, "/_matrix/client/v3/account/whoami"), status,
                      "whoami with the stored token")
        # 401 (token expired/logged out) or another user: log in again below.
    password = fixture.get("password")
    if user_id and password:
        token = password_login(hs_url, user_id, password)
        if token:
            whoami_after_login(hs_url, token, expected=user_id)
            fixture["access_token"] = token
            return user_id, token
    password = fixture_password(registration_token)
    token = password_login(hs_url, HUMAN_FIXTURE_LOCALPART, password)
    if token:
        user_id = whoami_after_login(hs_url, token)
    else:
        created = register_user(hs_url, registration_token, HUMAN_FIXTURE_LOCALPART, password)
        if created is None:
            # Confirmed: @gwsmoke exists but the derived password is rejected.
            created = register_user(hs_url, registration_token)
        user_id, token, password = created
    fixture.clear()
    fixture.update({"user_id": user_id, "password": password, "access_token": token})
    return user_id, token


def request_openid_token(hs_url, user_id, access_token):
    # Token goes in the header, never the query string.
    url = path_of(
        hs_url,
        "/_matrix/client/v3/user/{}/openid/request_token".format(
            urllib.parse.quote(user_id, safe="")
        ),
    )
    status, resp = request_json(
        "POST", url, body={}, headers={"Authorization": "Bearer " + access_token}
    )
    if status != 200 or not isinstance(resp, dict) or not resp.get("access_token"):
        http_fail("POST", url, status)
    return resp["access_token"]


def check_moments_via_openid(gateway_url, openid_token):
    url = path_of(gateway_url, "/v1/moments")
    status, resp = request_json(
        "GET", url, headers={"Authorization": "MatrixOpenID " + openid_token}
    )
    if status != 200:
        http_fail("GET", url, status, "Authorization: MatrixOpenID")
    if not isinstance(resp, dict):
        http_fail("GET", url, status, "body was not a JSON object")
    print("PASS: OpenID userinfo via in-stack homeserver URL (GET /v1/moments)")


# --- AS transactions (#192) -------------------------------------------------


def hs_ping(hs_url, as_token):
    url = path_of(
        hs_url,
        "/_matrix/client/v1/appservice/{}/ping".format(urllib.parse.quote(APPSERVICE_ID)),
    )
    deadline = time.monotonic() + 90
    status = 0
    while time.monotonic() < deadline:
        status, resp = request_json(
            "POST",
            url,
            body={"transaction_id": "gwsmoke-ping-" + secrets.token_hex(4)},
            headers={"Authorization": "Bearer " + as_token},
        )
        if status == 200 and isinstance(resp, dict):
            print("PASS: homeserver -> gateway ping (/_matrix/app/v1/ping)")
            return
        if 400 <= status < 500 and status != 429:
            # 4xx other than 429 never heals by waiting (401/403: wrong or
            # unknown as_token; 400/404: bad request or AS id): fail now.
            http_fail("POST", url, status, "appservice ping: client error, not retried")
        # 429 / 5xx (502/504: the HS could not reach the AS yet, e.g. just
        # restarted) / unreachable (status 0): transient, retry until the
        # deadline.
        time.sleep(3)
    http_fail("POST", url, status, "appservice ping")


def as_register_ai_user(hs_url, as_token, server_name):
    """Register the fixed @ai_gwsmoke user; already registered is fine."""
    url = path_of(hs_url, "/_matrix/client/v3/register")
    # Synapse applies the per-IP rc_registration limit before the AS branch,
    # so a smoke run right after other registrations can see 429 for a while.
    deadline = time.monotonic() + 90
    while True:
        localpart = AI_FIXTURE_LOCALPART
        status, resp = request_json(
            "POST",
            url,
            body={
                "type": "m.login.application_service",
                "username": localpart,
                "inhibit_login": True,
            },
            headers={"Authorization": "Bearer " + as_token},
        )
        if status not in (429, 503) or time.monotonic() >= deadline:
            break
        wait_ms = resp.get("retry_after_ms") if isinstance(resp, dict) else None
        wait = wait_ms / 1000.0 if isinstance(wait_ms, (int, float)) and wait_ms > 0 else 5.0
        time.sleep(min(max(wait, 1.0), 15.0))
    if status == 400 and isinstance(resp, dict) and resp.get("errcode") == "M_USER_IN_USE":
        return "@{}:{}".format(AI_FIXTURE_LOCALPART, server_name)
    if status != 200 or not isinstance(resp, dict) or not resp.get("user_id"):
        http_fail("POST", url, status, "m.login.application_service")
    return resp["user_id"]


def joined_members(hs_url, access_token, room_id):
    """Set of joined user ids, or None when the room is not usable."""
    url = path_of(
        hs_url,
        "/_matrix/client/v3/rooms/{}/joined_members".format(urllib.parse.quote(room_id, safe="")),
    )
    status, resp = request_json("GET", url, headers={"Authorization": "Bearer " + access_token})
    if status == 200 and isinstance(resp, dict) and isinstance(resp.get("joined"), dict):
        return set(resp["joined"])
    return None


def invite(hs_url, access_token, room_id, user_id):
    url = path_of(
        hs_url, "/_matrix/client/v3/rooms/{}/invite".format(urllib.parse.quote(room_id, safe=""))
    )
    status, _ = request_json(
        "POST", url, body={"user_id": user_id}, headers={"Authorization": "Bearer " + access_token}
    )
    if status != 200:
        http_fail("POST", url, status, "invite the @ai_* user")


def leave_and_forget(hs_url, access_token, room_id):
    """Best effort: drop a room the fixture no longer uses."""
    quoted = urllib.parse.quote(room_id, safe="")
    for action in ("leave", "forget"):
        request_json(
            "POST",
            path_of(hs_url, "/_matrix/client/v3/rooms/{}/{}".format(quoted, action)),
            body={},
            headers={"Authorization": "Bearer " + access_token},
        )


def ensure_room(hs_url, access_token, as_token, user_id, ai_user, fixture):
    """Reuse the fixture room (human joined) or create one; make sure the AI
    is joined. Updates `fixture` in place and returns the room id."""
    room_id = fixture.get("room_id")
    members = joined_members(hs_url, access_token, room_id) if room_id else None
    if members is None or user_id not in members:
        if room_id:
            leave_and_forget(hs_url, access_token, room_id)
        room_id = create_room_with_ai(hs_url, access_token, ai_user)
        fixture["room_id"] = room_id
        members = {user_id}
    elif ai_user not in members:
        invite(hs_url, access_token, room_id, ai_user)
    if ai_user not in members:
        ai_join(hs_url, as_token, ai_user, room_id)
    return room_id


def create_room_with_ai(hs_url, access_token, ai_user):
    url = path_of(hs_url, "/_matrix/client/v3/createRoom")
    status, resp = request_json(
        "POST",
        url,
        body={"preset": "private_chat", "invite": [ai_user]},
        headers={"Authorization": "Bearer " + access_token},
    )
    if status != 200 or not isinstance(resp, dict) or not resp.get("room_id"):
        http_fail("POST", url, status, "createRoom")
    return resp["room_id"]


def ai_join(hs_url, as_token, ai_user, room_id):
    url = path_of(
        hs_url, "/_matrix/client/v3/join/{}".format(urllib.parse.quote(room_id, safe=""))
    )
    # user_id (masquerade) is not a secret; the as_token stays in the header.
    status, _ = request_json(
        "POST",
        url + "?" + urllib.parse.urlencode({"user_id": ai_user}),
        body={},
        headers={"Authorization": "Bearer " + as_token},
    )
    if status != 200:
        http_fail("POST", url, status, "join as the @ai_* user")


def send_message(hs_url, access_token, room_id, text):
    url = path_of(
        hs_url,
        "/_matrix/client/v3/rooms/{}/send/m.room.message/{}".format(
            urllib.parse.quote(room_id, safe=""), "gwsmoke" + secrets.token_hex(6)
        ),
    )
    status, resp = request_json(
        "PUT",
        url,
        body={"msgtype": "m.text", "body": text},
        headers={"Authorization": "Bearer " + access_token},
    )
    if status != 200 or not isinstance(resp, dict) or not resp.get("event_id"):
        http_fail("PUT", url, status, "send m.room.message")
    return resp["event_id"]


def probe_db(gateway_exec, needle):
    out = run_cmd(
        gateway_exec,
        ["python", "-c", _DB_PROBE, AS_TXN_DB_PATH, needle],
        "query {} in the gateway container".format(AS_TXN_DB_PATH),
    )
    try:
        return json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        fail("could not parse the {} probe output".format(AS_TXN_DB_PATH))


def wait_for_stored(gateway_exec, event_id, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = probe_db(gateway_exec, event_id)
        if result["txn_ids"]:
            return result
        time.sleep(2)
    fail(
        "event {} was not stored in {} within {}s (homeserver did not push the "
        "transaction to http://gateway:8080/_matrix/app/v1/transactions/<txnId>)".format(
            event_id, AS_TXN_DB_PATH, timeout
        )
    )


def put_transaction(gateway_url, txn_id, token):
    url = path_of(
        gateway_url,
        "/_matrix/app/v1/transactions/{}".format(urllib.parse.quote(txn_id, safe="")),
    )
    status, _ = request_json(
        "PUT", url, body={"events": []}, headers={"Authorization": "Bearer " + token}
    )
    return url, status


def check_as_transactions(args, hs_url, user_id, access_token, as_token, hs_token,
                          fixture, save):
    hs_ping(hs_url, as_token)

    server_name = user_id.split(":", 1)[1]
    ai_user = as_register_ai_user(hs_url, as_token, server_name)
    room_id = ensure_room(hs_url, access_token, as_token, user_id, ai_user, fixture)
    save()
    event_id = send_message(hs_url, access_token, room_id, "gwsmoke " + secrets.token_hex(4))
    stored = wait_for_stored(args.gateway_exec, event_id)
    if len(stored["txn_ids"]) != 1:
        fail("event {} stored in {} transactions, expected 1".format(event_id, len(stored["txn_ids"])))
    txn_id = stored["txn_ids"][0]
    print("PASS: homeserver pushed a transaction; gateway stored it (txnId {})".format(txn_id))

    run_cmd(args.gateway_restart, [], "restart the gateway")
    wait_for_health(args.gateway_url, args.timeout)
    after_restart = probe_db(args.gateway_exec, event_id)
    if after_restart["txn_ids"] != [txn_id]:
        fail("after the gateway restart event {} is in txnIds {}, expected [{}]".format(
            event_id, after_restart["txn_ids"], txn_id))
    total_before = after_restart["total"]

    url, status = put_transaction(args.gateway_url, txn_id, "wrong-" + secrets.token_hex(16))
    if status != 403:
        http_fail("PUT", url, status, "wrong hs_token must be 403")
    url, status = put_transaction(args.gateway_url, txn_id, hs_token)
    if status != 200:
        http_fail("PUT", url, status, "replay with hs_token")
    replayed = probe_db(args.gateway_exec, event_id)
    if replayed["txn_ids"] != [txn_id] or replayed["total"] != total_before:
        fail(
            "replaying txnId {} changed the store (txnIds {}, rows {} -> {})".format(
                txn_id, replayed["txn_ids"], total_before, replayed["total"]
            )
        )
    print("PASS: after a gateway restart the replayed txnId is stored once (idempotent)")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hs-url", required=True, help="homeserver Client-Server base, e.g. http://127.0.0.1:8008")
    parser.add_argument("--gateway-url", required=True, help="gateway HTTP base, e.g. http://127.0.0.1:8020")
    parser.add_argument("--env-file", help="reads BOTVILLE_REGISTRATION_TOKEN and APPSERVICE_*_TOKEN (never printed)")
    parser.add_argument("--registration-token", default="", help="optional override; otherwise taken from --env-file")
    parser.add_argument("--timeout", type=int, default=120, help="seconds to wait for /health")
    parser.add_argument("--expect-transport", default="", help="assert GATEWAY_MATRIX_TRANSPORT in the container (needs --gateway-exec)")
    parser.add_argument("--gateway-exec", default="", help="command prefix that runs a command in the gateway container")
    parser.add_argument("--gateway-restart", default="", help="command that restarts the gateway container")
    parser.add_argument("--skip-as-txn", action="store_true", help="skip the AS transaction checks")
    parser.add_argument("--fixture-file", default=None,
                        help="reused fixture state (default <env-file dir>/generated/gateway-hs-smoke.json; never printed)")
    args = parser.parse_args(argv)

    token = args.registration_token
    if not token and args.env_file:
        token = env_value(args.env_file, "BOTVILLE_REGISTRATION_TOKEN")
    if not token or token == "<change-me>":
        fail("BOTVILLE_REGISTRATION_TOKEN is missing or still the placeholder")

    if args.expect_transport:
        if not args.gateway_exec:
            fail("--expect-transport needs --gateway-exec")
        check_transport(args.gateway_exec, args.expect_transport)

    wait_for_health(args.gateway_url, args.timeout)
    print("PASS: gateway /health matrix check is ok (startup whoami)")
    fixture_file = args.fixture_file
    if fixture_file is None:
        fixture_file = default_fixture_file(args.env_file)
    fixtures = load_fixtures(fixture_file) if fixture_file else {}
    fixture = fixtures.get(args.hs_url)
    if not isinstance(fixture, dict):
        fixture = {}
    fixtures[args.hs_url] = fixture

    def save():
        save_fixtures(fixture_file, fixtures)

    user_id, access_token = ensure_human(args.hs_url, token, fixture)
    save()
    openid_token = request_openid_token(args.hs_url, user_id, access_token)
    check_moments_via_openid(args.gateway_url, openid_token)

    if not args.skip_as_txn:
        if not (args.env_file and args.gateway_exec and args.gateway_restart):
            fail("AS transaction checks need --env-file, --gateway-exec and --gateway-restart (or --skip-as-txn)")
        as_token = env_value(args.env_file, "APPSERVICE_AS_TOKEN")
        hs_token = env_value(args.env_file, "APPSERVICE_HS_TOKEN")
        if not as_token or not hs_token or "<change-me>" in (as_token, hs_token):
            fail("APPSERVICE_AS_TOKEN / APPSERVICE_HS_TOKEN missing or placeholder in the env file")
        check_as_transactions(args, args.hs_url, user_id, access_token, as_token, hs_token,
                              fixture, save)

    print("PASS: gateway homeserver smoke")
    return 0


if __name__ == "__main__":
    sys.exit(main())
