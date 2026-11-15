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

A-R3 relation transport (federation_relations.py): while paired, a
relation message sent through hs-a's internal API reaches hs-b's test
recorder signed and once; a byte-identical replay is refused (401); after
hs-b severs, hs-a's internal API refuses (409) and a correctly signed
message from hs-a is stopped at fedgate-b (403) -- nothing more recorded.

Alongside every phase the read-only pairing-facts API (A-R1,
/v1/pairing/internal/*, service token) is checked on both sides: the
peer state it reports and the peer_paired / peer_severed events it
emits (re-pair included), plus that the admin token and no token are
refused there.

Homeserver whitelist (A-R2, #118 item 8): each pairing service renders its
Synapse's federation_domain_whitelist include (active peers + own name)
and a restart marker when it changes. Nothing restarts Synapse
automatically, so this script plays the operator/supervisor: after each
pairing change it checks the rendered lists and markers, then runs the
documented procedure (move the marker aside, `docker compose restart
synapse-X`, wait, delete it). On top of the phases above it proves:
  - fedgate is the real-time gate: right after a sever (no restart yet)
    hs-a still sends, the peer's fedgate sees and drops it;
  - after sever + restart the local Synapse sends NOTHING to the peer
    (zero /_matrix/ requests in the peer fedgate's access log while both
    users talk; a new invite fails locally);
  - after re-pair + restart federation resumes;
  - an idempotent repeat sever renders nothing and leaves no marker.

Every polling step is bounded (<=60s positive, 20s negative windows).
Negative checks ("never arrives", "no outbound request") only prove
nothing happened inside their window, not that it never will (#247
review N4): a retry backoff longer than 20s would go unobserved here;
that guarantee rests on the code paths and unit tests.
First failure exits non-zero with the failing step name.
"""

import argparse
import os
import re
import secrets
import shlex
import subprocess
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import smoke  # noqa: E402  (same directory as this file)
import federation_relations  # noqa: E402  (A-R3 relation transport steps)
from smoke import Failure, expect, request_json  # noqa: E402

POLL_INTERVAL = 2.0
POSITIVE_TIMEOUT = 60.0
NEGATIVE_TIMEOUT = 20.0
# fedgate re-reads the pairing DB at most every 1s; give both gates that
# refresh (plus slack) before asserting a sever is enforced.
GATE_REFRESH_GRACE = 2.5
DEFAULT_A_CLIENT = "http://127.0.0.1:18101"
DEFAULT_B_CLIENT = "http://127.0.0.1:18102"
DEFAULT_A_ADMIN = "http://127.0.0.1:18111"
DEFAULT_B_ADMIN = "http://127.0.0.1:18112"
HS_WHITELIST = "/hs/federation-whitelist.yaml"
HS_MARKER = HS_WHITELIST + ".restart-required"
# Requests a homeserver sends to a peer arrive at the peer's fedgate as
# /_matrix/federation|key/... lines in its access log (BaseHTTPRequestHandler
# log_request). /_matrix/pairing (pairing service) and /health
# (healthchecks) are not homeserver traffic.
HS_TRAFFIC_RE = re.compile(r'"[A-Z]+ /_matrix/(?:federation|key)/')
SIDES = {"a": "hs-a", "b": "hs-b"}


class Compose:
    """`docker compose` for the botville-fed project (stdin isolated)."""

    def __init__(self, command):
        self.argv = shlex.split(command)

    def run(self, *args, check=True, input_text=None):
        """stdin is never inherited: DEVNULL, or the explicit `input_text`
        (A-R3: the relations signer script is fed to `python -`)."""
        stdin = {"stdin": subprocess.DEVNULL} if input_text is None \
            else {"input": input_text}
        proc = subprocess.run(self.argv + list(args), capture_output=True,
                              text=True, timeout=180, **stdin)
        if check and proc.returncode != 0:
            raise Failure("{} failed ({}): {}".format(
                " ".join(args[:3]), proc.returncode,
                (proc.stderr or proc.stdout).strip()[-400:]))
        return proc

    def whitelist(self, side):
        """Names in pairing-<side>'s rendered include (parsed YAML list)."""
        text = self.run("exec", "-T", "pairing-" + side, "cat",
                        HS_WHITELIST).stdout
        names = []
        for line in text.splitlines():
            if line.startswith("  - "):
                names.append(line[4:].strip().strip('"'))
        expect(names, "empty whitelist on {}: {!r}".format(side, text))
        return names

    def marker(self, side):
        return self.run("exec", "-T", "pairing-" + side, "test", "-e",
                        HS_MARKER, check=False).returncode == 0

    def hs_requests(self, side):
        """Homeserver requests fedgate-<side> has logged so far."""
        out = self.run("logs", "--no-color", "--no-log-prefix",
                       "fedgate-" + side).stdout
        return sum(1 for line in out.splitlines() if HS_TRAFFIC_RE.search(line))

    def restart_homeserver(self, side, client_url):
        """The documented supervisor procedure for one side: claim the
        marker, restart Synapse, wait until it serves, drop the claim."""
        claimed = HS_MARKER + ".processing"
        self.run("exec", "-T", "pairing-" + side, "mv", HS_MARKER, claimed)
        self.run("restart", "synapse-" + side)
        smoke.Smoke(client_url, None).wait_for_versions(POSITIVE_TIMEOUT)
        self.run("exec", "-T", "pairing-" + side, "rm", "-f", claimed)


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


class PairingFacts:
    """Service-token client for one side's internal facts API (A-R1)."""

    def __init__(self, base_url, token):
        self.base = base_url.rstrip("/")
        self.token = token
        self.cursor = 0
        self.seen = []

    def peer(self, server_name):
        status, resp = request_json(
            "GET", self.base + "/v1/pairing/internal/peers/" + server_name,
            token=self.token)
        expect(status == 200, "facts peer returned HTTP {}".format(status))
        return resp

    def poll(self):
        """Drain new facts; returns the list of (kind, server_name, extra)."""
        new = []
        while True:
            status, resp = request_json(
                "GET", self.base + "/v1/pairing/internal/events?since={}".format(
                    self.cursor), token=self.token)
            expect(status == 200, "facts events returned HTTP {}".format(status))
            for event in resp["events"]:
                expect(event["cursor"] > self.cursor, "cursor went backwards")
                new.append((event["kind"], event["server_name"],
                            event.get("repaired", event.get("severed_by"))))
            self.cursor = resp["next_cursor"]
            if not resp["has_more"]:
                break
        self.seen += new
        return new

    def status_with(self, token):
        status, _ = request_json(
            "GET", self.base + "/v1/pairing/internal/events", token=token)
        return status


def expect_facts(facts, server_name, state, events):
    """The facts API on one side shows `state` for server_name and emitted
    exactly `events` (kind, server_name, repaired|severed_by) since the
    last check."""
    got = facts.poll()
    expect(got == events, "facts events {!r} != {!r}".format(got, events))
    peer = facts.peer(server_name)
    expect(peer["state"] == state and peer["changed_at"],
           "facts peer {}: {!r}".format(server_name, peer))


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
    """Rooms the client has (ever) been invited to, via incremental sync.

    Synapse caches identical initial-sync responses, so repeated
    `/sync?timeout=0` without `since` keeps returning a stale snapshot;
    track next_batch per client and accumulate invites instead."""
    seen = client.__dict__.setdefault("_fed_invites", set())
    query = {"timeout": 0}
    since = client.__dict__.get("_fed_since")
    if since:
        query["since"] = since
    status, resp = request_json(
        "GET", client.url("/_matrix/client/v3/sync", query=query),
        token=client.access_token)
    expect(status == 200, "sync returned HTTP {}: {!r}".format(status, resp))
    resp = resp or {}
    client.__dict__["_fed_since"] = resp.get("next_batch", since)
    seen.update(((resp.get("rooms") or {}).get("invite") or {}).keys())
    return list(seen)


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
    parser.add_argument(
        "--compose", default=None,
        help="docker compose command for the botville-fed project (default:"
             " docker compose -p botville-fed -f <federation compose>"
             " --env-file <env-file>)")
    args = parser.parse_args()
    if args.compose is None:
        compose_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "compose",
            "federation", "docker-compose.federation.yml")
        args.compose = "docker compose -p botville-fed -f {} --env-file {}".format(
            shlex.quote(compose_file), shlex.quote(args.env_file))
    compose = Compose(args.compose)
    clients = {"a": args.a_client, "b": args.b_client}

    env = load_env(args.env_file)
    admin_a = PairingAdmin(args.a_admin, env["PAIRING_ADMIN_TOKEN_A"])
    admin_b = PairingAdmin(args.b_admin, env["PAIRING_ADMIN_TOKEN_B"])
    facts_a = PairingFacts(args.a_admin, env["PAIRING_SERVICE_TOKEN_A"])
    facts_b = PairingFacts(args.b_admin, env["PAIRING_SERVICE_TOKEN_B"])
    rel = federation_relations.Relations(
        compose, args.a_admin, args.b_admin,
        env["PAIRING_SERVICE_TOKEN_A"], env["PAIRING_SERVICE_TOKEN_B"])
    suffix = secrets.token_hex(3)

    # --- readiness + users ------------------------------------------------

    def step_ready():
        smoke.Smoke(args.a_client, None).wait_for_versions(POSITIVE_TIMEOUT)
        smoke.Smoke(args.b_client, None).wait_for_versions(POSITIVE_TIMEOUT)
        wait_until("both pairing services healthy",
                   lambda: admin_a.health() and admin_b.health(), 60)
        # A-R1 auth: the facts API answers its own token only.
        for facts, admin_token in ((facts_a, env["PAIRING_ADMIN_TOKEN_A"]),
                                   (facts_b, env["PAIRING_ADMIN_TOKEN_B"])):
            expect(facts.status_with(None) == 401, "facts without token not 401")
            expect(facts.status_with(admin_token) == 401,
                   "facts with the admin token not 401")
            expect(facts.status_with(facts.token) == 200,
                   "facts with the service token not 200")
            facts.poll()  # start both feeds from "now"
        # First boot: pairing rendered "self only" before Synapse started,
        # and created no marker (nothing was running to restart).
        for side, own in SIDES.items():
            names = compose.whitelist(side)
            expect(names == [own], "{} whitelist at boot: {}".format(side, names))
            expect(not compose.marker(side),
                   "first boot left a restart marker on {}".format(side))
        return ("versions + pairing /health ok; facts API token-gated;"
                " whitelists self-only, no marker")

    def step_users():
        globals()["alice"] = register_user(args.a_client, "alice" + suffix)
        globals()["bob"] = register_user(args.b_client, "bob" + suffix)
        return "{} @ hs-a, {} @ hs-b".format(alice.user_id, bob.user_id)

    # --- phase 1: unpaired -> federation is denied -------------------------

    def step_unpaired():
        # Synapse sends the invite synchronously over federation. hs-b is
        # not in hs-a's whitelist yet, so hs-a refuses it locally (the
        # remote fedgate would answer 403 as well). Create the room
        # first, then the invite must be refused AND never arrive.
        room = create_room(alice)
        status, resp = request_json(
            "POST", alice.url("/_matrix/client/v3/rooms/{}/invite".format(quote_room(room))),
            body={"user_id": "@{}:hs-b".format(bob.username)},
            token=alice.access_token)
        expect(status >= 400,
               "unpaired invite unexpectedly accepted: HTTP {} {!r}".format(status, resp))
        assert_never(
            "an invite into {} on hs-b".format(room),
            lambda: room in sync_invited_rooms(bob))
        globals()["stale_room"] = room
        return "invite refused (HTTP {}) and never reached hs-b".format(status)

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
        expect_facts(facts_a, "hs-b", "active", [("peer_paired", "hs-b", False)])
        expect_facts(facts_b, "hs-a", "active", [("peer_paired", "hs-a", False)])
        return "both peers active; peer_paired facts on both sides"

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

    # --- homeserver whitelist helpers (A-R2) ---------------------------------

    def expect_whitelists(expected):
        for side, names in expected.items():
            got = compose.whitelist(side)
            expect(got == names, "{} whitelist {} != {}".format(side, got, names))

    def restart_both(expected):
        """Check rendered lists + markers, then restart both homeservers."""
        expect_whitelists(expected)
        for side in SIDES:
            expect(compose.marker(side),
                   "no restart marker on {} after a pairing change".format(side))
        for side in SIDES:
            compose.restart_homeserver(side, clients[side])
        for side in SIDES:
            expect(not compose.marker(side),
                   "marker still present on {} after restart".format(side))

    def step_restart_after_pair():
        restart_both({"a": ["hs-a", "hs-b"], "b": ["hs-a", "hs-b"]})
        return "both whitelists list hs-a + hs-b; markers consumed, restarted"

    def assert_no_hs_traffic(sender_side, label):
        """hs-<sender> talks into fed_room / invites; the peer's gate must
        log zero homeserver requests from it, and the invite must fail
        locally."""
        peer_side = "b" if sender_side == "a" else "a"
        sender = alice if sender_side == "a" else bob
        peer_user = bob if sender_side == "a" else alice
        before = compose.hs_requests(peer_side)
        send_message(sender, fed_room, label)
        room = create_room(sender)
        status, resp = request_json(
            "POST", sender.url("/_matrix/client/v3/rooms/{}/invite".format(
                quote_room(room))),
            body={"user_id": peer_user.user_id}, token=sender.access_token)
        expect(status >= 400,
               "invite to {} accepted after sever+restart: HTTP {} {!r}".format(
                   peer_user.user_id, status, resp))
        # Synapse's own FederationDeniedError: refused before any request
        # left this homeserver (a peer-gate refusal reads differently).
        expect("Federation denied" in str((resp or {}).get("error")),
               "invite failed, but not by the local whitelist: {!r}".format(resp))
        time.sleep(NEGATIVE_TIMEOUT)
        after = compose.hs_requests(peer_side)
        expect(after == before,
               "hs-{} still sent {} request(s) to hs-{} after sever+restart".format(
                   sender_side, after - before, peer_side))
        return "invite refused locally (HTTP {}: {})".format(
            status, (resp or {}).get("error"))

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
        expect_facts(facts_b, "hs-a", "severed", [("peer_severed", "hs-a", "local")])
        expect_facts(facts_a, "hs-b", "severed", [("peer_severed", "hs-b", "remote")])
        time.sleep(GATE_REFRESH_GRACE)

        cut_b2a = send_message(bob, fed_room, "b-severed-a")
        assert_never("alice seeing {!r}".format(cut_b2a),
                     lambda: cut_b2a in message_bodies(alice, fed_room))
        before = compose.hs_requests("b")
        cut_a2b = send_message(alice, fed_room, "a-severed-b")
        assert_never("bob seeing {!r}".format(cut_a2b),
                     lambda: cut_a2b in message_bodies(bob, fed_room))
        # Positive control for the restart step below: before any restart
        # hs-a still has hs-b whitelisted and DOES send; fedgate-b, the
        # real-time gate, is what drops it.
        expect(compose.hs_requests("b") > before,
               "fedgate-b logged no hs-a traffic before the restart; the"
               " access-log counter cannot prove anything")
        return "no traffic either way after hs-b severed (fedgate-b dropped" \
               " hs-a's attempts)"

    def step_restart_after_b_sever():
        restart_both({"a": ["hs-a"], "b": ["hs-b"]})
        a = assert_no_hs_traffic("a", "a-after-restart")
        b = assert_no_hs_traffic("b", "b-after-restart")
        return "hs-a: {}; hs-b: {}".format(a, b)

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
        expect_facts(facts_a, "hs-b", "active", [("peer_paired", "hs-b", True)])
        expect_facts(facts_b, "hs-a", "active", [("peer_paired", "hs-a", True)])
        # Both homeservers still run the severed whitelist: restart them
        # (A-R2). The gates reloaded the pairing table meanwhile.
        restart_both({"a": ["hs-a", "hs-b"], "b": ["hs-a", "hs-b"]})
        time.sleep(GATE_REFRESH_GRACE)
        again = send_message(alice, fed_room, "a-repaired")
        wait_until("bob seeing {!r}".format(again),
                   lambda: again in message_bodies(bob, fed_room),
                   POSITIVE_TIMEOUT)
        back = send_message(bob, fed_room, "b-repaired")
        wait_until("alice seeing {!r}".format(back),
                   lambda: back in message_bodies(alice, fed_room),
                   POSITIVE_TIMEOUT)
        return "re-paired, restarted, delivering both ways again"

    def step_a_severs():
        resp = admin_a.sever("hs-b")
        expect(resp.get("state") == "severed" and resp.get("severed_by") == "local",
               "sever response: {!r}".format(resp))
        wait_until("hs-b's peers[hs-a] severed by remote",
                   lambda: peer_state_is(admin_b, "hs-a", "severed", "remote"), 15)
        expect_facts(facts_a, "hs-b", "severed", [("peer_severed", "hs-b", "local")])
        expect_facts(facts_b, "hs-a", "severed", [("peer_severed", "hs-a", "remote")])
        time.sleep(GATE_REFRESH_GRACE)

        cut_a2b = send_message(alice, fed_room, "a-final")
        assert_never("bob seeing {!r}".format(cut_a2b),
                     lambda: cut_a2b in message_bodies(bob, fed_room))
        cut_b2a = send_message(bob, fed_room, "b-final")
        assert_never("alice seeing {!r}".format(cut_b2a),
                     lambda: cut_b2a in message_bodies(alice, fed_room))
        return "no traffic either way after hs-a severed"

    def step_restart_after_a_sever():
        restart_both({"a": ["hs-a"], "b": ["hs-b"]})
        # Severing an already-severed peer changes no list: no marker.
        admin_a.sever("hs-b")
        time.sleep(1)
        for side in SIDES:
            expect(not compose.marker(side),
                   "repeat sever left a restart marker on {}".format(side))
        detail = assert_no_hs_traffic("b", "b-after-final-restart")
        return "repeat sever: no marker; hs-b: {}".format(detail)

    steps = [
        ("readiness", step_ready),
        ("users", step_users),
        ("unpaired servers cannot federate", step_unpaired),
        ("pair hs-a -> hs-b", step_pair),
        ("whitelists rendered, homeservers restarted", step_restart_after_pair),
        ("federated invite/join/messages", step_federated_chat),
        ("relations: signed delivery, replay refused",
         lambda: federation_relations.step_active(rel)),
        ("hs-b severs unilaterally", step_b_severs),
        ("relations: non-active peer refused",
         lambda: federation_relations.step_inactive(rel)),
        ("sever + restart: no homeserver traffic to the peer",
         step_restart_after_b_sever),
        ("re-pair hs-b -> hs-a (+ restart)", step_repair),
        ("hs-a severs unilaterally", step_a_severs),
        ("hs-a sever + restart, idempotent repeat leaves no marker",
         step_restart_after_a_sever),
    ]
    for name, fn in steps:
        run_step(name, fn)
    print("federation-e2e: all {} steps passed".format(len(steps)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
