#!/usr/bin/env python3
"""Botville homeserver smoke test (A0.2).

Exercises the Matrix Client-Server API against a running stack using only the
Python standard library:

  1. wait for GET /_matrix/client/versions -> 200
  2. register a random user through generic UIA
     (completes m.login.registration_token and m.login.dummy stages)
  3. login (m.login.password)
  4. GET /account/whoami and assert the user_id
  5. POST /createRoom with room_version "10"
  6. send an m.room.message event
  7. GET /rooms/.../messages?dir=b and find the event
  8. GET /sync and find the event in the room timeline

No /_synapse/admin endpoints, no admin-room commands, no server-specific APIs.
Each step prints PASS/FAIL; the first failure exits non-zero.
"""

import argparse
import json
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

POLL_INTERVAL = 2.0
REQUEST_TIMEOUT = 30.0
SUPPORTED_STAGES = ("m.login.registration_token", "m.login.dummy")


def request_json(method, url, body=None, token=None, timeout=REQUEST_TIMEOUT):
    """HTTP request returning (status, decoded body or None). HTTP errors are
    returned as (error code, body) instead of raised."""
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
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


class Failure(Exception):
    pass


def expect(condition, message):
    if not condition:
        raise Failure(message)


class Smoke:
    def __init__(self, base_url, registration_token):
        self.base = base_url.rstrip("/")
        self.token_arg = registration_token
        self.username = "smoke_" + secrets.token_hex(4)
        self.password = secrets.token_hex(24)

    def url(self, path, query=None):
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        return url

    def run(self, name, fn):
        try:
            detail = fn()
            print("PASS {}{}".format(name, " ({})".format(detail) if detail else ""))
        except Exception as exc:  # noqa: BLE001 - report anything and stop
            print("FAIL {}: {}".format(name, exc))
            raise

    # steps -------------------------------------------------------------

    def wait_for_versions(self, timeout):
        deadline = time.monotonic() + timeout
        last = "no response"
        while True:
            try:
                status, body = request_json(
                    "GET", self.url("/_matrix/client/versions"), timeout=10
                )
                if status == 200:
                    versions = (body or {}).get("versions") or []
                    shown = ",".join(versions[:3])
                    if len(versions) > 3:
                        shown += ",..."
                    return "versions=" + (shown or "none")
                last = "HTTP {}".format(status)
            except Exception as exc:  # noqa: BLE001 - keep polling
                last = str(exc)
            if time.monotonic() >= deadline:
                raise Failure(
                    "GET /_matrix/client/versions not 200 within {}s (last: {})".format(
                        timeout, last
                    )
                )
            time.sleep(POLL_INTERVAL)

    def register(self):
        body = {"username": self.username, "password": self.password, "auth": {}}
        flow = None
        for _ in range(12):
            status, resp = request_json(
                "POST", self.url("/_matrix/client/v3/register"), body=body
            )
            if status == 200:
                user_id = (resp or {}).get("user_id")
                access_token = (resp or {}).get("access_token")
                expect(user_id, "register 200 without user_id: {!r}".format(resp))
                expect(access_token, "register 200 without access_token: {!r}".format(resp))
                self.user_id = user_id
                self.access_token = access_token
                return user_id
            expect(status == 401, "register returned HTTP {}: {!r}".format(status, resp))
            session = (resp or {}).get("session")
            expect(session, "register 401 without session: {!r}".format(resp))
            completed = set((resp or {}).get("completed") or [])
            if flow is None:
                flows = (resp or {}).get("flows") or []
                candidates = [
                    f["stages"]
                    for f in flows
                    if f.get("stages") and all(s in SUPPORTED_STAGES for s in f["stages"])
                ]
                # Prefer a flow that spends the registration token.
                candidates.sort(key=lambda stages: "m.login.registration_token" not in stages)
                expect(candidates, "no registrable flow among: {!r}".format(flows))
                flow = candidates[0]
            remaining = [s for s in flow if s not in completed]
            expect(remaining, "UIA flow {} completed but registration still 401".format(flow))
            stage = remaining[0]
            auth = {"session": session, "type": stage}
            if stage == "m.login.registration_token":
                auth["token"] = self.token_arg
            body["auth"] = auth
        raise Failure("registration did not complete for flow {}".format(flow))

    def login(self):
        status, resp = request_json(
            "POST",
            self.url("/_matrix/client/v3/login"),
            body={
                "type": "m.login.password",
                "identifier": {"type": "m.id.user", "user": self.username},
                "password": self.password,
            },
        )
        expect(status == 200, "login returned HTTP {}: {!r}".format(status, resp))
        token = (resp or {}).get("access_token")
        expect(token, "login 200 without access_token: {!r}".format(resp))
        self.access_token = token
        return self.username

    def whoami(self):
        status, resp = request_json(
            "GET",
            self.url("/_matrix/client/v3/account/whoami"),
            token=self.access_token,
        )
        expect(status == 200, "whoami returned HTTP {}: {!r}".format(status, resp))
        got = (resp or {}).get("user_id")
        expect(got == self.user_id, "whoami user_id {} != {}".format(got, self.user_id))
        return got

    def create_room(self):
        status, resp = request_json(
            "POST",
            self.url("/_matrix/client/v3/createRoom"),
            body={"room_version": "10"},
            token=self.access_token,
        )
        expect(status == 201 or status == 200, "createRoom returned HTTP {}: {!r}".format(status, resp))
        room_id = (resp or {}).get("room_id")
        expect(room_id, "createRoom without room_id: {!r}".format(resp))
        self.room_id = room_id
        return room_id

    def send_message(self):
        txn = "smoke" + secrets.token_hex(4)
        self.message_body = "botville smoke " + txn
        path = "/_matrix/client/v3/rooms/{}/send/m.room.message/{}".format(
            urllib.parse.quote(self.room_id, safe=""), txn
        )
        status, resp = request_json(
            "PUT",
            self.url(path),
            body={"msgtype": "m.text", "body": self.message_body},
            token=self.access_token,
        )
        expect(status == 200, "send returned HTTP {}: {!r}".format(status, resp))
        event_id = (resp or {}).get("event_id")
        expect(event_id, "send 200 without event_id: {!r}".format(resp))
        self.event_id = event_id
        return event_id

    def messages(self):
        path = "/_matrix/client/v3/rooms/{}/messages".format(
            urllib.parse.quote(self.room_id, safe="")
        )
        status, resp = request_json(
            "GET", self.url(path, query={"dir": "b", "limit": 50}), token=self.access_token
        )
        expect(status == 200, "messages returned HTTP {}: {!r}".format(status, resp))
        chunk = (resp or {}).get("chunk") or []
        found = any(e.get("event_id") == self.event_id for e in chunk)
        expect(found, "event {} not in /messages chunk ({})".format(self.event_id, [e.get("event_id") for e in chunk]))
        return self.event_id

    def sync(self):
        status, resp = request_json(
            "GET",
            self.url("/_matrix/client/v3/sync", query={"timeout": 0}),
            token=self.access_token,
        )
        expect(status == 200, "sync returned HTTP {}: {!r}".format(status, resp))
        next_batch = (resp or {}).get("next_batch")
        expect(next_batch, "sync without next_batch")
        rooms = ((resp or {}).get("rooms") or {}).get("join") or {}
        timeline = (rooms.get(self.room_id) or {}).get("timeline") or {}
        events = timeline.get("events") or []
        found = any(e.get("event_id") == self.event_id for e in events)
        expect(
            found,
            "event {} not in sync timeline of {} ({})".format(
                self.event_id, self.room_id, [e.get("event_id") for e in events]
            ),
        )
        return next_batch


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", required=True, help="e.g. http://127.0.0.1:8008")
    parser.add_argument("--registration-token", required=True)
    parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="max seconds to wait for /_matrix/client/versions (default 180)",
    )
    args = parser.parse_args()

    smoke = Smoke(args.base_url, args.registration_token)
    steps = [
        ("versions", lambda: smoke.wait_for_versions(args.timeout)),
        ("register (UIA)", smoke.register),
        ("login", smoke.login),
        ("whoami", smoke.whoami),
        ("createRoom v10", smoke.create_room),
        ("send m.room.message", smoke.send_message),
        ("messages dir=b", smoke.messages),
        ("sync", smoke.sync),
    ]
    for name, fn in steps:
        try:
            smoke.run(name, fn)
        except Exception:
            return 1
    print("SMOKE OK: {} @ {}".format(smoke.user_id, args.base_url))
    return 0


if __name__ == "__main__":
    sys.exit(main())
