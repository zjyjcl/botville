#!/usr/bin/env python3
"""Botville push gateway e2e acceptance test (A1.2, issue #4).

Exercises the homeserver -> Sygnal half of the push path with the standard
Matrix APIs only (Client-Server + Push Gateway), against a stack started
with `make up HS=<hs> PUSH=1`:

  1. wait for the homeserver (GET /_matrix/client/versions) and for Sygnal:
     POST /_matrix/push/v1/notify with an unknown app_id must return 200 and
     reject the pushkey — the Push Gateway spec's rejection semantics double
     as the readiness probe (the compose healthcheck only proves /health;
     this proves the notify path works);
  2. register alice/bob through generic UIA (reuses smoke.py helpers);
  3. alice opens a private chat and invites bob; bob joins;
  4. bob sets an http pusher: app_id org.botville.mock (the mock pushkin
     inside Sygnal, which records every dispatch to a JSONL file instead of
     calling APNs), data.url http://sygnal:5000/_matrix/push/v1/notify,
     format event_id_only;
  5. alice sends a message; the test records the wall-clock send time and
     polls the mock JSONL (--pushes-cmd `docker compose ... exec -T sygnal
     cat /data/mock-pushes.jsonl`, or --pushes-file on the host) until the
     event_id shows up, then asserts the record's has_content is empty (an
     event_id_only pusher must carry no message content — the A1.2 privacy
     guardrail) and that 0 <= HS -> Sygnal latency < --max-latency (default
     5s, the A1.2 acceptance budget) and prints it.

Real APNs delivery (Sygnal -> device) is NOT covered here: it needs a real
Apple developer account, a .p8 key and a physical device — see
docs/deploy/push-sygnal.md for the manual procedure. Containers share the
host clock, so received_at_ms - send-time ms is a valid latency measure.
"""

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import smoke  # noqa: E402  (same directory as this file)
from smoke import request_json  # noqa: E402

MOCK_APP_ID = "org.botville.mock"
POLL_INTERVAL = 0.25
PROBE_APP_ID = "org.botville.probe.does-not-exist"


def run_step(name, fn):
    """Run one step; print PASS/FAIL; exit non-zero on the first failure."""
    try:
        detail = fn()
    except Exception as exc:  # noqa: BLE001 - report anything and stop
        print("FAIL {}: {}".format(name, exc))
        sys.exit(1)
    print("PASS {}{}".format(name, " ({})".format(detail) if detail else ""))


def register_user(base_url, registration_token, name):
    """Register a random-suffix user through generic UIA via smoke.Smoke."""
    client = smoke.Smoke(base_url, registration_token)
    client.username = "push_{}_{}".format(name, secrets.token_hex(4))
    client.password = secrets.token_hex(16)
    client.register()  # sets .user_id and .access_token
    return client


def join_room(client, room_id):
    path = "/_matrix/client/v3/rooms/{}/join".format(urllib.parse.quote(room_id, safe=""))
    status, resp = request_json(
        "POST", client.url(path), body={}, token=client.access_token
    )
    if status not in (200, 201):
        raise smoke.Failure("join returned HTTP {}: {!r}".format(status, resp))
    return room_id


def unknown_app_probe_body():
    """One Push Gateway notify for an app_id sygnal does not configure."""
    return {
        "notification": {
            "event_id": "$botville-probe-" + secrets.token_hex(4),
            "room_id": "!push-e2e-probe:invalid",
            "type": "m.room.message",
            "sender": "@push-e2e-probe:invalid",
            "counts": {"unread": 1},
            "devices": [
                {
                    "app_id": PROBE_APP_ID,
                    "pushkey": "probe-pushkey-" + secrets.token_hex(4),
                    "pushkey_ts": int(time.time() * 1000),
                    "data": {},
                    "tweaks": {},
                }
            ],
        }
    }


def wait_for_sygnal(sygnal_base, timeout):
    """Sygnal is up iff POST notify answers 200 for an unknown app_id with
    that pushkey in `rejected` (spec: rejected = 'not valid' pushkeys)."""
    deadline = time.monotonic() + timeout
    last = "no response"
    while True:
        body = unknown_app_probe_body()
        try:
            status, resp = request_json(
                "POST",
                sygnal_base.rstrip("/") + "/_matrix/push/v1/notify",
                body=body,
                timeout=10,
            )
            pushkey = body["notification"]["devices"][0]["pushkey"]
            if status == 200 and pushkey in ((resp or {}).get("rejected") or []):
                return "rejected=[{}<cut>]".format(pushkey[:12])
            last = "HTTP {} {!r}".format(status, resp)
        except Exception as exc:  # noqa: BLE001 - keep polling
            last = str(exc)
        if time.monotonic() >= deadline:
            raise smoke.Failure(
                "sygnal rejection probe failed within {}s (last: {})".format(timeout, last)
            )
        time.sleep(smoke.POLL_INTERVAL)


def set_pusher(bob, sygnal_internal_url):
    pushkey = "mock-" + secrets.token_hex(16)
    status, resp = request_json(
        "POST",
        bob.url("/_matrix/client/v3/pushers/set"),
        body={
            "app_id": MOCK_APP_ID,
            "pushkey": pushkey,
            "kind": "http",
            "app_display_name": "botville push e2e",
            "device_display_name": "botville test device",
            "lang": "en",
            # data.url is fetched by the homeserver, hence the in-network
            # service name; event_id_only keeps content off the gateway.
            "data": {"url": sygnal_internal_url, "format": "event_id_only"},
        },
        token=bob.access_token,
    )
    if status != 200:
        raise smoke.Failure("pushers/set returned HTTP {}: {!r}".format(status, resp))
    return pushkey


def read_push_records(args):
    """Current JSONL records as a list of dicts; empty when none/unreadable."""
    if args.pushes_file:
        try:
            with open(args.pushes_file, encoding="utf-8") as f:
                text = f.read()
        except FileNotFoundError:
            return []
    else:
        proc = subprocess.run(
            args.pushes_cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode != 0:  # e.g. file not created yet
            return []
        text = proc.stdout
    records = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a torn final line between write and cat; skip it
    return records


def wait_for_push(args, event_id, pushkey):
    """Poll the mock JSONL until (event_id, pushkey) shows up; return record."""
    deadline = time.monotonic() + args.push_wait
    last_count = 0
    while True:
        records = read_push_records(args)
        last_count = len(records)
        for record in records:
            if record.get("event_id") == event_id and record.get("pushkey") == pushkey:
                return record
        if time.monotonic() >= deadline:
            sample = json.dumps(records[-3:], sort_keys=True)
            raise smoke.Failure(
                "event {} not in mock pushes within {}s ({} records so far; "
                "last 3: {})".format(event_id, args.push_wait, last_count, sample)
            )
        time.sleep(POLL_INTERVAL)


def _create_chat(alice, bob):
    status, resp = request_json(
        "POST",
        alice.url("/_matrix/client/v3/createRoom"),
        body={
            "room_version": "10",
            "is_direct": True,
            "invite": [bob.user_id],
            "preset": "trusted_private_chat",
        },
        token=alice.access_token,
    )
    if status not in (200, 201):
        raise smoke.Failure("createRoom returned HTTP {}: {!r}".format(status, resp))
    room_id = (resp or {}).get("room_id")
    if not room_id:
        raise smoke.Failure("createRoom without room_id: {!r}".format(resp))
    return room_id


def _send(alice, room_id):
    txn = "push" + secrets.token_hex(6)
    path = "/_matrix/client/v3/rooms/{}/send/m.room.message/{}".format(
        urllib.parse.quote(room_id, safe=""), txn
    )
    status, resp = request_json(
        "PUT",
        alice.url(path),
        body={"msgtype": "m.text", "body": "push e2e " + txn},
        token=alice.access_token,
    )
    if status != 200:
        raise smoke.Failure("send returned HTTP {}: {!r}".format(status, resp))
    event_id = (resp or {}).get("event_id")
    if not event_id:
        raise smoke.Failure("send 200 without event_id: {!r}".format(resp))
    return event_id


def _assert_latency(latency_s, budget_s):
    # Containers share the host clock, so a sane latency is >= 0; a negative
    # one means clock skew and must not pass by just being far under budget.
    if latency_s < 0:
        raise smoke.Failure(
            "latency {:.2f}s is negative (clock skew between send time and "
            "received_at_ms?); refusing to pass".format(latency_s)
        )
    if latency_s >= budget_s:
        raise smoke.Failure("latency {:.2f}s >= budget {:.1f}s".format(latency_s, budget_s))
    return "{:.2f}s".format(latency_s)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", required=True, help="homeserver, e.g. http://127.0.0.1:8008")
    parser.add_argument(
        "--sygnal-base-url",
        required=True,
        help="sygnal as reachable from this script, e.g. http://127.0.0.1:8019",
    )
    parser.add_argument(
        "--sygnal-notify-url",
        default="http://sygnal:5000/_matrix/push/v1/notify",
        help="sygnal notify URL as reachable from the homeserver container "
        "(pusher data.url; default: http://sygnal:5000/_matrix/push/v1/notify)",
    )
    pushes = parser.add_mutually_exclusive_group(required=True)
    pushes.add_argument(
        "--pushes-cmd",
        help="shell command printing the mock JSONL, e.g. "
        '"docker compose -p botville -f server/compose/docker-compose.yml '
        '--env-file server/compose/.env exec -T sygnal cat /data/mock-pushes.jsonl"',
    )
    pushes.add_argument(
        "--pushes-file",
        help="host path of the mock JSONL (server/compose/generated/"
        "sygnal-data/mock-pushes.jsonl with the default bind mount)",
    )
    parser.add_argument("--registration-token", default=None, help="UIA token when token-gated")
    parser.add_argument(
        "--max-latency",
        type=float,
        default=5.0,
        help="acceptance budget in seconds for homeserver -> sygnal (default 5)",
    )
    parser.add_argument(
        "--timeout", type=int, default=180, help="max seconds to wait for the APIs (default 180)"
    )
    parser.add_argument(
        "--push-wait", type=int, default=60, help="max seconds to wait for the push record (default 60)"
    )
    args = parser.parse_args()

    state = {}

    run_step("homeserver versions", lambda: smoke.Smoke(args.base_url, None).wait_for_versions(args.timeout))
    run_step(
        "sygnal online + unknown app_id rejected",
        lambda: wait_for_sygnal(args.sygnal_base_url, args.timeout),
    )
    run_step(
        "register alice (UIA)",
        lambda: state.setdefault(
            "alice", register_user(args.base_url, args.registration_token, "alice")
        ).user_id,
    )
    run_step(
        "register bob (UIA)",
        lambda: state.setdefault(
            "bob", register_user(args.base_url, args.registration_token, "bob")
        ).user_id,
    )
    alice, bob = state["alice"], state["bob"]

    run_step(
        "alice opens private chat with bob",
        lambda: state.setdefault("room", _create_chat(alice, bob)),
    )
    run_step("bob joins", lambda: join_room(bob, state["room"]))
    run_step(
        "bob sets http pusher (org.botville.mock, event_id_only)",
        lambda: state.setdefault("pushkey", set_pusher(bob, args.sygnal_notify_url)),
    )

    def send_and_wait():
        sent_at_ms = int(time.time() * 1000)
        event_id = _send(alice, state["room"])
        record = wait_for_push(args, event_id, state["pushkey"])
        if record.get("room_id") != state["room"]:
            raise smoke.Failure(
                "push record room_id {} != {}".format(record.get("room_id"), state["room"])
            )
        has_content = record.get("has_content") or []
        if has_content:
            raise smoke.Failure(
                "push record carries content keys {} — the pusher is not "
                "event_id_only, message bodies must never reach the gateway "
                "(docs/deploy/push-sygnal.md §8)".format(has_content)
            )
        latency_ms = record["received_at_ms"] - sent_at_ms
        state["latency_ms"] = latency_ms
        return "{:.2f}s ({} -> {}, no content)".format(
            latency_ms / 1000.0, event_id, record["pushkey"]
        )

    run_step("alice sends; push reaches sygnal", send_and_wait)

    latency_s = state["latency_ms"] / 1000.0
    run_step(
        "latency < {:.1f}s budget".format(args.max_latency),
        lambda: _assert_latency(latency_s, args.max_latency),
    )
    print("PUSH E2E OK: HS -> sygnal latency {:.2f}s < {:.1f}s (mock pushkin; APNs delivery not covered)".format(latency_s, args.max_latency))
    return 0


if __name__ == "__main__":
    sys.exit(main())
