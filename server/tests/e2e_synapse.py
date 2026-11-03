#!/usr/bin/env python3
"""Botville Synapse e2e acceptance test (A1.1).

Standard-library Matrix Client-Server API test in two phases:

  --phase seed    On a fresh database: register alice/bob/carol through
                  generic UIA (reuses smoke.py helpers), then
                  - alice opens a direct chat with bob (is_direct=true,
                    invite, preset trusted_private_chat); bob joins; alice
                    and bob exchange messages and each reads the other's;
                  - alice creates a named group, invites bob and carol;
                    both join; alice posts and carol (and bob) read it.
                  Saves users/passwords/rooms/events to --state.

  --phase verify  Log back in from --state and confirm the direct chat and
                  group messages are still readable -- run this after
                  backup/restore to prove accounts and history survived.

Only standard endpoints; no /_synapse/admin. Each step prints PASS/FAIL and
the first failure exits non-zero. seed expects a clean homeserver database
(re-running it against an existing one fails with M_USER_IN_USE).
"""

import argparse
import json
import os
import secrets
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import smoke  # noqa: E402  (same directory as this file)
from smoke import expect, request_json  # noqa: E402

USER_NAMES = ("alice", "bob", "carol")


def run_step(name, fn):
    """Run one step; print PASS/FAIL; exit non-zero on the first failure."""
    try:
        detail = fn()
    except Exception as exc:  # noqa: BLE001 - report anything and stop
        print("FAIL {}: {}".format(name, exc))
        sys.exit(1)
    print("PASS {}{}".format(name, " ({})".format(detail) if detail else ""))


# --- client helpers (reuse smoke.Smoke for register/login/wait) ----------


def register_user(base_url, registration_token, name):
    """Register a fixed-name user through generic UIA via smoke.Smoke."""
    client = smoke.Smoke(base_url, registration_token)
    client.username = name
    client.password = secrets.token_hex(16)
    client.register()  # sets .user_id and .access_token
    return client


def login_user(base_url, username, password, user_id):
    client = smoke.Smoke(base_url, None)
    client.username = username
    client.password = password
    client.user_id = user_id
    client.login()  # replaces .access_token
    return client


def wait_for_versions(base_url, timeout):
    prober = smoke.Smoke(base_url, None)
    return prober.wait_for_versions(timeout)


def quote_room(room_id):
    return urllib.parse.quote(room_id, safe="")


def create_room(client, **extra):
    body = {"room_version": "10"}
    body.update(extra)
    status, resp = request_json(
        "POST",
        client.url("/_matrix/client/v3/createRoom"),
        body=body,
        token=client.access_token,
    )
    expect(
        status in (200, 201),
        "createRoom returned HTTP {}: {!r}".format(status, resp),
    )
    room_id = (resp or {}).get("room_id")
    expect(room_id, "createRoom without room_id: {!r}".format(resp))
    return room_id


def join_room(client, room_id):
    status, resp = request_json(
        "POST",
        client.url("/_matrix/client/v3/rooms/{}/join".format(quote_room(room_id))),
        body={},
        token=client.access_token,
    )
    expect(
        status in (200, 201),
        "{} joining {} returned HTTP {}: {!r}".format(client.username, room_id, status, resp),
    )
    return room_id


def send_message(client, room_id, body_text):
    txn = "e2e" + secrets.token_hex(6)
    status, resp = request_json(
        "PUT",
        client.url(
            "/_matrix/client/v3/rooms/{}/send/m.room.message/{}".format(
                quote_room(room_id), txn
            )
        ),
        body={"msgtype": "m.text", "body": body_text},
        token=client.access_token,
    )
    expect(status == 200, "send returned HTTP {}: {!r}".format(status, resp))
    event_id = (resp or {}).get("event_id")
    expect(event_id, "send 200 without event_id: {!r}".format(resp))
    return event_id


def expect_events(client, room_id, event_ids):
    """All event_ids must be visible to client in room /messages (dir=b)."""
    status, resp = request_json(
        "GET",
        client.url(
            "/_matrix/client/v3/rooms/{}/messages".format(quote_room(room_id)),
            query={"dir": "b", "limit": 100},
        ),
        token=client.access_token,
    )
    expect(status == 200, "messages returned HTTP {}: {!r}".format(status, resp))
    chunk = (resp or {}).get("chunk") or []
    visible = {e.get("event_id") for e in chunk}
    missing = [e for e in event_ids if e not in visible]
    expect(
        not missing,
        "{} cannot see {} of {} in {} (chunk has {} events)".format(
            client.username, missing, event_ids, room_id, len(chunk)
        ),
    )
    return "events={}".format(len(event_ids))


def write_state(path, state):
    """Write the state JSON with restrictive permissions (it has passwords)."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")
    return path


def load_state(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def validate_state(state):
    """Fail with a readable message (not a KeyError traceback) if the state
    file is truncated/wrong-phase/mismatched before verify relies on it."""
    problems = []
    if not isinstance(state, dict):
        return "not a JSON object: {!r}".format(state)
    users = state.get("users") or {}
    for name in USER_NAMES:
        info = users.get(name) or {}
        for field in ("username", "password", "user_id"):
            if not info.get(field):
                problems.append("users.{}.{}".format(name, field))
    for section, fields in (
        ("dm", ("room_id", "alice_event_id", "bob_event_id")),
        ("group", ("room_id", "alice_event_id")),
    ):
        for field in fields:
            if not (state.get(section) or {}).get(field):
                problems.append("{}.{}".format(section, field))
    if problems:
        raise ValueError(
            "missing/empty fields: {} (wrong file or truncated state?)".format(
                ", ".join(problems)
            )
        )
    return "users={} + dm/group rooms/events".format(len(USER_NAMES))


# --- phases --------------------------------------------------------------


def do_seed(args):
    tag = secrets.token_hex(4)
    clients = {}

    run_step("versions", lambda: wait_for_versions(args.base_url, args.timeout))
    for name in USER_NAMES:
        run_step(
            "register {} (UIA)".format(name),
            lambda name=name: clients.setdefault(
                name, register_user(args.base_url, args.registration_token, name)
            ).user_id,
        )
    alice, bob, carol = (clients[n] for n in USER_NAMES)

    room = {}
    events = {}
    run_step(
        "alice opens DM with bob (is_direct, trusted_private_chat)",
        lambda: room.setdefault(
            "dm",
            create_room(
                alice, is_direct=True, invite=[bob.user_id], preset="trusted_private_chat"
            ),
        ),
    )
    run_step("bob joins DM", lambda: join_room(bob, room["dm"]))
    run_step(
        "alice sends DM message",
        lambda: events.setdefault(
            "dm_alice", send_message(alice, room["dm"], "dm alice->bob {}".format(tag))
        ),
    )
    run_step(
        "bob sends DM message",
        lambda: events.setdefault(
            "dm_bob", send_message(bob, room["dm"], "dm bob->alice {}".format(tag))
        ),
    )
    run_step(
        "alice reads both DM messages",
        lambda: expect_events(alice, room["dm"], [events["dm_alice"], events["dm_bob"]]),
    )
    run_step(
        "bob reads both DM messages",
        lambda: expect_events(bob, room["dm"], [events["dm_alice"], events["dm_bob"]]),
    )

    run_step(
        "alice creates group and invites bob+carol",
        lambda: room.setdefault(
            "group",
            create_room(
                alice,
                name="botville-e2e-{}".format(tag),
                invite=[bob.user_id, carol.user_id],
                preset="private_chat",
            ),
        ),
    )
    run_step("bob joins group", lambda: join_room(bob, room["group"]))
    run_step("carol joins group", lambda: join_room(carol, room["group"]))
    run_step(
        "alice sends group message",
        lambda: events.setdefault(
            "group_alice", send_message(alice, room["group"], "group alice->all {}".format(tag))
        ),
    )
    run_step(
        "carol reads group message",
        lambda: expect_events(carol, room["group"], [events["group_alice"]]),
    )
    run_step(
        "bob reads group message",
        lambda: expect_events(bob, room["group"], [events["group_alice"]]),
    )

    state = {
        "base_url": args.base_url,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "users": {
            name: {
                "username": client.username,
                "password": client.password,
                "user_id": client.user_id,
            }
            for name, client in clients.items()
        },
        "dm": {
            "room_id": room["dm"],
            "alice_event_id": events["dm_alice"],
            "bob_event_id": events["dm_bob"],
        },
        "group": {
            "room_id": room["group"],
            "alice_event_id": events["group_alice"],
        },
    }
    run_step("write state", lambda: write_state(args.state, state))
    print("E2E SEED OK: users={}, state={}".format(",".join(USER_NAMES), args.state))


def do_verify(args):
    state = {}
    run_step(
        "read state",
        lambda: state.update(load_state(args.state))
        or "users={}".format(len(state.get("users") or {})),
    )
    run_step("state structure", lambda: validate_state(state))

    run_step("versions", lambda: wait_for_versions(args.base_url, args.timeout))
    clients = {}
    for name in USER_NAMES:
        info = state["users"][name]
        run_step(
            "login {}".format(name),
            lambda info=info, name=name: clients.setdefault(
                name,
                login_user(args.base_url, info["username"], info["password"], info["user_id"]),
            ).username,
        )
        run_step("whoami {}".format(name), lambda name=name: clients[name].whoami())

    dm, group = state["dm"], state["group"]
    dm_events = [dm["alice_event_id"], dm["bob_event_id"]]
    run_step(
        "alice still reads the DM history",
        lambda: expect_events(clients["alice"], dm["room_id"], dm_events),
    )
    run_step(
        "bob still reads the DM history",
        lambda: expect_events(clients["bob"], dm["room_id"], dm_events),
    )
    run_step(
        "carol still reads the group history",
        lambda: expect_events(clients["carol"], group["room_id"], [group["alice_event_id"]]),
    )
    print("E2E VERIFY OK: state={}".format(args.state))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", required=True, help="e.g. http://127.0.0.1:8008")
    parser.add_argument(
        "--phase", required=True, choices=("seed", "verify"), help="what to run"
    )
    parser.add_argument(
        "--state",
        default="backups/e2e-state.json",
        help="state file written by seed and read by verify "
        "(default: backups/e2e-state.json; contains passwords)",
    )
    parser.add_argument(
        "--registration-token",
        default=None,
        help="UIA token for seed when the homeserver is token-gated "
        "(unused when registration is open)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="max seconds to wait for /_matrix/client/versions (default 180)",
    )
    args = parser.parse_args()

    if args.phase == "seed":
        do_seed(args)
    else:
        do_verify(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
