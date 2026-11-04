#!/usr/bin/env python3
"""Botville paired-federation acceptance test (A2.1).

Runs against the `make federation-e2e` compose project (botville-fed):
two Synapse instances hs-a/hs-b, each behind its own fedgate gate +
pairing service. Standard library + smoke.py helpers only; no admin API,
no /_synapse/admin. Phases (issue A2.1 acceptance):

  1. unpaired   alice@hs-a invites @bob:hs-b -> the invite must never
                arrive (fedgate denies the federation traffic);
  2. pairing    A creates an invite (QR payload), B accepts -> both
                sides report the peer active; invite/join/messages work
                in both directions;
  3. B severs   alice's new messages stop reaching bob AND bob's stop
                reaching alice (A learned via notify-severed);
  4. re-pair    (B invites this time) traffic flows again;
  5. A severs   the same isolation asserted the other way round.

Every polling step is bounded (<=60s positive, 20s negative windows).
First failure exits non-zero with the failing step name.
"""

import argparse
import os
import secrets
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import smoke  # noqa: E402  (same directory as this file)
from smoke import Failure, expect, request_json  # noqa: E402

POLL_INTERVAL = 2.0
POSITIVE_TIMEOUT = 60.0
NEGATIVE_TIMEOUT = 20.0
DEFAULT_A_CLIENT = "http://127.0.0.1:18101"
DEFAULT_B_CLIENT = "http://127.0.0.1:18102"
DEFAULT_A_ADMIN = "http://127.0.0.1:18111"
DEFAULT_B_ADMIN = "http://127.0.0.1:18112"


def run_step(name, fn):
    try:
        detail = fn()
    except Exception as exc:  # noqa: BLE001 - report anything and stop
        print("FAIL {}: {}".format(name, exc))
        sys.exit(1)
    print("PASS {}{}".format(name, " ({})".format(detail) if detail else ""))


def load_env(path):
    values = {}
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


class PairingAdmin:
    """Admin client for one side's pairing service (loopback host port)."""

    def __init__(self, base_url, token):
        self.base = base_url.rstrip("/")
        self.token = token

    def _call(self, method, path, body=None):
        status, resp = request_json(
            method, self.base + path, body=body, token=self.token)
        expect(status in (200, 201),
               "pairing {} {} returned HTTP {}: {!r}".format(
                   method, path, status, resp))
        return resp

    def health(self):
        status, _ = request_json("GET", self.base + "/health")
        return status == 200

    def create_invite(self):
        resp = self._call("POST", "/v1/pairing/invites", body={})
        payload = resp.get("qr_payload")
        expect(payload, "invite response without qr_payload: {!r}".format(resp))
        return payload

    def accept(self, qr_payload):
        return self._call("POST", "/v1/pairing/accept",
                          body={"qr_payload": qr_payload})

    def peers(self):
        return {p["server_name"]: p
                for p in self._call("GET", "/v1/pairing/peers")["peers"]}

    def peer_state(self, server_name):
        return self.peers().get(server_name)

    def sever(self, server_name):
        return self._call(
            "DELETE", "/v1/pairing/peers/{}".format(server_name))


def register_user(base_url, name):
    client = smoke.Smoke(base_url, None)
    client.username = name
    client.password = secrets.token_hex(16)
    client.register()
    return client


def quote_room(room_id):
    return urllib.parse.quote(room_id, safe="")


def create_room(client, invite=()):
    body = {"room_version": "10", "invite": list(invite)}
    status, resp = request_json(
        "POST", client.url("/_matrix/client/v3/createRoom"),
        body=body, token=client.access_token)
    expect(status in (200, 201),
           "createRoom returned HTTP {}: {!r}".format(status, resp))
    return resp["room_id"]


def send_message(client, room_id, label):
    body_text = "fed-e2e {} {}".format(label, secrets.token_hex(6))
    txn = "fede2e" + secrets.token_hex(6)
    status, resp = request_json(
        "PUT",
        client.url("/_matrix/client/v3/rooms/{}/send/m.room.message/{}".format(
            quote_room(room_id), txn)),
        body={"msgtype": "m.text", "body": body_text},
        token=client.access_token)
    expect(status == 200, "send returned HTTP {}: {!r}".format(status, resp))
    return body_text


def room_messages(client, room_id):
    status, resp = request_json(
        "GET",
        client.url("/_matrix/client/v3/rooms/{}/messages".format(
            quote_room(room_id)), query={"dir": "b", "limit": 50}),
        token=client.access_token)
    expect(status == 200,
           "messages returned HTTP {}: {!r}".format(status, resp))
    return (resp or {}).get("chunk") or []


def message_bodies(client, room_id):
    return [(event.get("content") or {}).get("body")
            for event in room_messages(client, room_id)]


def wait_until(desc, fn, timeout, poll=POLL_INTERVAL):
    """Poll fn() until truthy; returns its value or raises after timeout."""
    deadline = time.monotonic() + timeout
    last = None
    while True:
        last = fn()
        if last:
            return last
        if time.monotonic() >= deadline:
            raise Failure("timed out after {}s waiting for {}".format(
                timeout, desc))
        time.sleep(poll)


def assert_never(desc, fn, quiet=NEGATIVE_TIMEOUT, poll=POLL_INTERVAL):
    """Poll fn(); it must stay falsy for the whole quiet window."""
    deadline = time.monotonic() + quiet
    while time.monotonic() < deadline:
        if fn():
            raise Failure("unexpectedly observed {} within {}s".format(
                desc, quiet))
        time.sleep(poll)


def sync_invited_rooms(client):
    status, resp = request_json(
        "GET", client.url("/_matrix/client/v3/sync", query={"timeout": 0}),
        token=client.access_token)
    expect(status == 200, "sync returned HTTP {}: {!r}".format(status, resp))
    return list((((resp or {}).get("rooms") or {}).get("invite") or {}).keys())


def peer_state_is(admin, server_name, state, severed_by=None):
    """True when admin's pairing DB shows server_name in that state."""
    peer = admin.peer_state(server_name)
    if not peer or peer["state"] != state:
        return False
    return severed_by is None or peer["severed_by"] == severed_by


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "compose",
        "federation", "generated", "federation.env"),
        help="federation.env written by server/scripts/federation-setup.py")
    parser.add_argument("--a-client", default=DEFAULT_A_CLIENT)
    parser.add_argument("--b-client", default=DEFAULT_B_CLIENT)
    parser.add_argument("--a-admin", default=DEFAULT_A_ADMIN)
    parser.add_argument("--b-admin", default=DEFAULT_B_ADMIN)
    args = parser.parse_args()

    env = load_env(args.env_file)
    admin_a = PairingAdmin(args.a_admin, env["PAIRING_ADMIN_TOKEN_A"])
    admin_b = PairingAdmin(args.b_admin, env["PAIRING_ADMIN_TOKEN_B"])
    suffix = secrets.token_hex(3)

    # --- readiness + users ------------------------------------------------

    def step_ready():
        smoke.Smoke(args.a_client, None).wait_for_versions(POSITIVE_TIMEOUT)
        smoke.Smoke(args.b_client, None).wait_for_versions(POSITIVE_TIMEOUT)
        wait_until("both pairing services healthy",
                   lambda: admin_a.health() and admin_b.health(), 60)
        return "versions + pairing /health ok"

    def step_users():
        globals()["alice"] = register_user(args.a_client, "alice" + suffix)
        globals()["bob"] = register_user(args.b_client, "bob" + suffix)
        return "{} @ hs-a, {} @ hs-b".format(alice.user_id, bob.user_id)

    # --- phase 1: unpaired -> federation is denied -------------------------

    def step_unpaired():
        room = create_room(alice, invite=["@{}:hs-b".format(bob.username)])
        assert_never(
            "an invite into {} on hs-b".format(room),
            lambda: room in sync_invited_rooms(bob))
        globals()["stale_room"] = room
        return "invite never reached hs-b (kept room {} aside)".format(room)

    # --- phase 2: pair A -> B, then talk over federation -------------------

    def step_pair():
        qr = admin_a.create_invite()
        resp = admin_b.accept(qr)
        expect(resp.get("server_name") == "hs-a",
               "accept paired with {!r}".format(resp))
        # A wrote peers[hs-b] during its own accept, B during redeem —
        # both are synchronous with the responses above.
        peer = admin_a.peer_state("hs-b")
        expect(peer and peer["state"] == "active",
               "hs-a's view of hs-b: {!r}".format(peer))
        peer = admin_b.peer_state("hs-a")
        expect(peer and peer["state"] == "active",
               "hs-b's view of hs-a: {!r}".format(peer))
        return "both peers active"

    def step_federated_chat():
        room = create_room(alice, invite=["@{}:hs-b".format(bob.username)])
        wait_until("bob's invite", lambda: room in sync_invited_rooms(bob),
                   POSITIVE_TIMEOUT)
        status, resp = request_json(
            "POST", bob.url("/_matrix/client/v3/join/{}".format(quote_room(room))),
            body={}, token=bob.access_token)
        expect(status in (200, 201), "bob join returned HTTP {}".format(status))
        globals()["fed_room"] = room

        hello = send_message(bob, room, "b2a-hello")
        wait_until("alice seeing {!r}".format(hello),
                   lambda: hello in message_bodies(alice, room),
                   POSITIVE_TIMEOUT)
        reply = send_message(alice, room, "a2b-reply")
        wait_until("bob seeing {!r}".format(reply),
                   lambda: reply in message_bodies(bob, room),
                   POSITIVE_TIMEOUT)
        return "invite, join and messages both ways in {}".format(room)

    # --- phase 3: B severs unilaterally -------------------------------------

    def step_b_severs():
        resp = admin_b.sever("hs-a")
        expect(resp.get("state") == "severed" and resp.get("severed_by") == "local",
               "sever response: {!r}".format(resp))
        wait_until("hs-b's peers[hs-a] severed locally",
                   lambda: peer_state_is(admin_b, "hs-a", "severed", "local"), 10)
        # The best-effort notify makes hs-a drop hs-b as well (both gates
        # then refuse; each side's own messages stop reaching the other).
        wait_until("hs-a's peers[hs-b] severed by remote",
                   lambda: peer_state_is(admin_a, "hs-b", "severed", "remote"), 15)

        cut_b2a = send_message(bob, fed_room, "b-severed-a")
        assert_never("alice seeing {!r}".format(cut_b2a),
                     lambda: cut_b2a in message_bodies(alice, fed_room))
        cut_a2b = send_message(alice, fed_room, "a-severed-b")
        assert_never("bob seeing {!r}".format(cut_a2b),
                     lambda: cut_a2b in message_bodies(bob, fed_room))
        return "no traffic either way after hs-b severed"

    # --- phase 4+5: re-pair (B invites), then A severs -----------------------

    def step_repair():
        qr = admin_b.create_invite()
        resp = admin_a.accept(qr)
        expect(resp.get("server_name") == "hs-b",
               "accept paired with {!r}".format(resp))
        for admin, name in ((admin_a, "hs-b"), (admin_b, "hs-a")):
            wait_until("{} active again".format(name),
                       lambda admin=admin, name=name:
                       peer_state_is(admin, name, "active"), 10)
        again = send_message(alice, fed_room, "a-repaired")
        wait_until("bob seeing {!r}".format(again),
                   lambda: again in message_bodies(bob, fed_room),
                   POSITIVE_TIMEOUT)
        return "re-paired and delivering again"

    def step_a_severs():
        resp = admin_a.sever("hs-b")
        expect(resp.get("state") == "severed" and resp.get("severed_by") == "local",
               "sever response: {!r}".format(resp))
        wait_until("hs-b's peers[hs-a] severed by remote",
                   lambda: peer_state_is(admin_b, "hs-a", "severed", "remote"), 15)

        cut_a2b = send_message(alice, fed_room, "a-final")
        assert_never("bob seeing {!r}".format(cut_a2b),
                     lambda: cut_a2b in message_bodies(bob, fed_room))
        cut_b2a = send_message(bob, fed_room, "b-final")
        assert_never("alice seeing {!r}".format(cut_b2a),
                     lambda: cut_b2a in message_bodies(alice, fed_room))
        return "no traffic either way after hs-a severed"

    steps = [
        ("readiness", step_ready),
        ("users", step_users),
        ("unpaired servers cannot federate", step_unpaired),
        ("pair hs-a -> hs-b", step_pair),
        ("federated invite/join/messages", step_federated_chat),
        ("hs-b severs unilaterally", step_b_severs),
        ("re-pair hs-b -> hs-a", step_repair),
        ("hs-a severs unilaterally", step_a_severs),
    ]
    for name, fn in steps:
        run_step(name, fn)
    print("federation-e2e: all {} steps passed".format(len(steps)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
