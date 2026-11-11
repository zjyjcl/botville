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
  2. OpenID userinfo: register a throwaway user on the homeserver, request
     an OpenID token, then GET /v1/moments on the gateway with
     `Authorization: MatrixOpenID ...`. HTTP 200 means the gateway validated
     the token via in-stack userinfo (GATEWAY_HOMESERVER_FEDERATION_URL is
     empty, so it falls back to GATEWAY_HOMESERVER_URL).
  3. (unless --skip-as-txn) AS transactions:
     a. HS -> gateway ping: POST /_matrix/client/v1/appservice/<id>/ping
        with the as_token; the homeserver calls the gateway's
        /_matrix/app/v1/ping (registration url http://gateway:8080).
     b. an @ai_* user joins a room the human user created and the human
        sends a message; the homeserver pushes a transaction and the gateway
        stores it (checked in GATEWAY_AS_TXN_DB_PATH via --gateway-exec).
     c. restart the gateway (--gateway-restart), wait for /health, check the
        row survived, then replay the same txnId with the hs_token: HTTP 200
        and still exactly one stored row (idempotent by txnId). A wrong
        hs_token must get 403.

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
import json
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
    proc = subprocess.run(
        argv, input=input_text, capture_output=True, text=True, timeout=180
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


def register_user(hs_url, registration_token):
    """Generic UIA register; returns (user_id, access_token) and never prints either.

    Retries HTTP 429/503: Synapse's default rc_registration burst is small, so a
    smoke run right after `make e2e` seed can be rate-limited for a few seconds.
    """
    register_url = path_of(hs_url, "/_matrix/client/v3/register")
    deadline = time.monotonic() + 60
    last_status = 0
    while time.monotonic() < deadline:
        username = "gwsmoke_" + secrets.token_hex(4)
        password = secrets.token_hex(16)
        body = {"username": username, "password": password}
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
                return user_id, access_token
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
        # 502/504: the HS could not reach the AS yet (e.g. just restarted).
        time.sleep(3)
    http_fail("POST", url, status, "appservice ping")


def as_register_ai_user(hs_url, as_token):
    url = path_of(hs_url, "/_matrix/client/v3/register")
    localpart = "ai_gwsmoke_" + secrets.token_hex(4)
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
    if status != 200 or not isinstance(resp, dict) or not resp.get("user_id"):
        http_fail("POST", url, status, "m.login.application_service")
    return resp["user_id"]


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


def check_as_transactions(args, hs_url, access_token, as_token, hs_token):
    hs_ping(hs_url, as_token)

    ai_user = as_register_ai_user(hs_url, as_token)
    room_id = create_room_with_ai(hs_url, access_token, ai_user)
    ai_join(hs_url, as_token, ai_user, room_id)
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
    user_id, access_token = register_user(args.hs_url, token)
    openid_token = request_openid_token(args.hs_url, user_id, access_token)
    check_moments_via_openid(args.gateway_url, openid_token)

    if not args.skip_as_txn:
        if not (args.env_file and args.gateway_exec and args.gateway_restart):
            fail("AS transaction checks need --env-file, --gateway-exec and --gateway-restart (or --skip-as-txn)")
        as_token = env_value(args.env_file, "APPSERVICE_AS_TOKEN")
        hs_token = env_value(args.env_file, "APPSERVICE_HS_TOKEN")
        if not as_token or not hs_token or "<change-me>" in (as_token, hs_token):
            fail("APPSERVICE_AS_TOKEN / APPSERVICE_HS_TOKEN missing or placeholder in the env file")
        check_as_transactions(args, args.hs_url, access_token, as_token, hs_token)

    print("PASS: gateway homeserver smoke")
    return 0


if __name__ == "__main__":
    sys.exit(main())
