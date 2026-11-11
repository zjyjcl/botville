#!/usr/bin/env python3
"""Smoke the compose gateway against a real homeserver (A2.2).

Verifies, against an already-running stack (`make up GATEWAY=1` on synapse
or tuwunel):

  1. Gateway GET /health is 200, `status=ok`, and the `matrix` check is ok.
     In appservice mode that check is the startup whoami with
     GATEWAY_AS_TOKEN (the process exits if whoami fails).
  2. OpenID userinfo: register a throwaway user on the homeserver, request
     an OpenID token, then GET /v1/moments on the gateway with
     `Authorization: MatrixOpenID ...`. HTTP 200 means the gateway validated
     the token via in-stack userinfo (GATEWAY_HOMESERVER_FEDERATION_URL is
     empty, so it falls back to GATEWAY_HOMESERVER_URL — the compose service
     name, not fedgate or a public address).

Tokens, passwords and registration secrets are never printed: failures
report only the HTTP status and a path, never a body or header value.
First failure exits non-zero.

  python3 server/tests/gateway_hs_smoke.py \\
      --hs-url http://127.0.0.1:8008 \\
      --gateway-url http://127.0.0.1:8020 \\
      --env-file server/compose/.env
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REQUEST_TIMEOUT = 15.0
SUPPORTED_STAGES = ("m.login.registration_token", "m.login.dummy")


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


def request_json(method, url, body=None, headers=None, timeout=REQUEST_TIMEOUT):
    """Return (status, decoded body or None). HTTP errors are not raised.

    The URL and headers passed in must already be safe to log (no tokens in
    the URL). This function never prints the request or the body.
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
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return 0, None


def fail(message):
    raise SystemExit("FAIL: " + message)


def path_of(base, path, query=None):
    url = base.rstrip("/") + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    return url


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
                print("PASS: gateway /health matrix check is ok (startup whoami)")
                return
        time.sleep(2)
    fail(
        "gateway /health did not become ok with matrix=ok within {}s (last HTTP {})".format(
            timeout, last_status
        )
    )


def register_user(hs_url, registration_token):
    """Generic UIA register; returns (user_id, access_token) and never prints either."""
    username = "gwsmoke_" + secrets.token_hex(4)
    password = secrets.token_hex(16)
    body = {"username": username, "password": password}
    flow = None
    register_url = path_of(hs_url, "/_matrix/client/v3/register")
    for _ in range(12):
        status, resp = request_json("POST", register_url, body=body)
        if status == 200 and isinstance(resp, dict):
            user_id = resp.get("user_id")
            access_token = resp.get("access_token")
            if not user_id or not access_token:
                fail("register 200 missing user_id or access_token")
            return user_id, access_token
        if status != 401 or not isinstance(resp, dict):
            fail("register returned HTTP {}".format(status or "unreachable"))
        session = resp.get("session")
        if not session:
            fail("register 401 missing session")
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
                fail("register 401 has no supported UIA flow")
            flow = candidates[0]
        remaining = [s for s in flow if s not in completed]
        if not remaining:
            fail("UIA flow completed but register still 401")
        stage = remaining[0]
        auth = {"session": session, "type": stage}
        if stage == "m.login.registration_token":
            auth["token"] = registration_token
        body["auth"] = auth
    fail("register did not complete")


def request_openid_token(hs_url, user_id, access_token):
    # user_id is a Matrix id; quote so @ and : are safe. Token goes in the
    # header, never the query string (query strings show up in error URLs).
    path = "/_matrix/client/v3/user/{}/openid/request_token".format(
        urllib.parse.quote(user_id, safe="")
    )
    status, resp = request_json(
        "POST",
        path_of(hs_url, path),
        body={},
        headers={"Authorization": "Bearer " + access_token},
    )
    if status != 200 or not isinstance(resp, dict) or not resp.get("access_token"):
        fail("openid/request_token returned HTTP {}".format(status or "unreachable"))
    return resp["access_token"]


def check_moments_via_openid(gateway_url, openid_token):
    status, resp = request_json(
        "GET",
        path_of(gateway_url, "/v1/moments"),
        headers={"Authorization": "MatrixOpenID " + openid_token},
    )
    if status != 200:
        fail("GET /v1/moments with MatrixOpenID returned HTTP {}".format(status or "unreachable"))
    if not isinstance(resp, dict):
        fail("GET /v1/moments 200 but the body was not an object")
    print("PASS: OpenID userinfo via in-stack homeserver URL (GET /v1/moments)")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hs-url", required=True, help="homeserver Client-Server base, e.g. http://127.0.0.1:8008")
    parser.add_argument("--gateway-url", required=True, help="gateway HTTP base, e.g. http://127.0.0.1:8020")
    parser.add_argument("--env-file", help="reads BOTVILLE_REGISTRATION_TOKEN (never printed)")
    parser.add_argument(
        "--registration-token",
        default="",
        help="optional override; otherwise taken from --env-file",
    )
    parser.add_argument("--timeout", type=int, default=120, help="seconds to wait for /health")
    args = parser.parse_args(argv)

    token = args.registration_token
    if not token and args.env_file:
        token = env_value(args.env_file, "BOTVILLE_REGISTRATION_TOKEN")
    if not token or token == "<change-me>":
        fail("BOTVILLE_REGISTRATION_TOKEN is missing or still the placeholder")

    wait_for_health(args.gateway_url, args.timeout)
    user_id, access_token = register_user(args.hs_url, token)
    openid_token = request_openid_token(args.hs_url, user_id, access_token)
    check_moments_via_openid(args.gateway_url, openid_token)
    print("PASS: gateway homeserver smoke")
    return 0


if __name__ == "__main__":
    sys.exit(main())
