#!/usr/bin/env python3
"""Botville appservice e2e acceptance test (A1.3): the gateway sends as an AI.

Standard-library Matrix test against a running stack whose homeserver has the
botville-gateway appservice registered (rendered by server/scripts/init.sh
from server/compose/.env into generated/appservice/). It imports the real
gateway transport from gateway/src — no install needed:

  1. wait for /versions, then register a human user alice through generic UIA
     (reuses smoke.py helpers; works on synapse dummy and token flows);
  2. AppServiceMatrixTransport pre-registers @ai_e2e_<rand>:<server>
     namespace-style (m.login.application_service, inhibit_login);
  3. alice creates a room (room_version 10) and invites that AI user;
  4. the transport delivers a message under the AI identity, auto-joining the
     invited room on M_FORBIDDEN;
  5. alice reads /messages and asserts sender == the AI user and body matches;
  6. a normal UIA registration inside the @ai_* namespace must be rejected
     (exclusive: M_EXCLUSIVE / 400 / 403);
  7. the transport refuses out-of-namespace senders client-side (no request);
  8. raw masquerade (?user_id=@bob:...) with the as_token must be rejected by
     the homeserver (M_EXCLUSIVE / M_FORBIDDEN).

Only standard endpoints; no /_synapse/admin, no admin-room commands. The
as_token is read from --env-file and never printed. First failure exits
non-zero (each step prints PASS/FAIL).
"""

import argparse
import os
import secrets
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import smoke  # noqa: E402  (same directory as this file)
from smoke import expect, request_json  # noqa: E402

# Import the real gateway transport (src-layout, no install needed).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_GATEWAY_SRC = os.path.join(_REPO_ROOT, "gateway", "src")
if _GATEWAY_SRC not in sys.path:
    sys.path.insert(0, _GATEWAY_SRC)

from botville_gateway.matrix import DeliveryError  # noqa: E402
from botville_gateway.matrix_appservice import AppServiceMatrixTransport  # noqa: E402

# Reuse the A1.1 e2e helpers (step runner, UIA registration, room creation).
from e2e_synapse import create_room, quote_room, register_user, run_step  # noqa: E402


def read_env(path):
    """Parse KEY=VALUE lines; the last assignment of a key wins (like init.sh
    and docker compose --env-file)."""
    values = {}
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key] = value
    return values


def read_ai_message(alice, room_id, event_id, want_sender, want_body):
    """alice pulls /messages and asserts the event's sender and body."""
    status, resp = request_json(
        "GET",
        alice.url(
            "/_matrix/client/v3/rooms/{}/messages".format(quote_room(room_id)),
            query={"dir": "b", "limit": 50},
        ),
        token=alice.access_token,
    )
    expect(status == 200, "messages returned HTTP {}: {!r}".format(status, resp))
    chunk = (resp or {}).get("chunk") or []
    match = next((e for e in chunk if e.get("event_id") == event_id), None)
    expect(match, "event {} not visible to alice (chunk: {})".format(
        event_id, [e.get("event_id") for e in chunk]))
    expect(
        match.get("sender") == want_sender,
        "sender {} != {}".format(match.get("sender"), want_sender),
    )
    body = ((match.get("content") or {}).get("body"))
    expect(body == want_body, "body {!r} != {!r}".format(body, want_body))
    return want_sender


def expect_normal_ai_registration_rejected(base_url, registration_token):
    """A normal UIA registration of ai_<name> must be refused — the namespace
    is exclusive to the appservice (M_EXCLUSIVE / 400 / 403)."""
    body = {
        "username": "ai_" + secrets.token_hex(4),
        "password": secrets.token_hex(16),
    }
    for _ in range(4):
        status, resp = request_json(
            "POST", base_url + "/_matrix/client/v3/register", body=body
        )
        if status == 200:
            raise smoke.Failure(
                "registered {} although the namespace is exclusive: {!r}".format(
                    body["username"], resp
                )
            )
        if status != 401:
            errcode = (resp or {}).get("errcode")
            expect(
                status in (400, 403),
                "register ai_* returned HTTP {}: {!r}".format(status, resp),
            )
            return "HTTP {} {}".format(status, errcode)
        # Spend the UIA flow (dummy / registration token); the rejection may
        # only come after the stages complete.
        session = (resp or {}).get("session")
        expect(session, "register 401 without session: {!r}".format(resp))
        completed = set((resp or {}).get("completed") or [])
        flows = (resp or {}).get("flows") or []
        stages = next(
            (
                f["stages"]
                for f in flows
                if f.get("stages")
                and all(s in smoke.SUPPORTED_STAGES for s in f["stages"])
            ),
            None,
        )
        expect(stages, "no registrable flow among: {!r}".format(flows))
        remaining = [s for s in stages if s not in completed]
        expect(remaining, "UIA flow {} completed but registration still 401".format(stages))
        auth = {"session": session, "type": remaining[0]}
        if remaining[0] == "m.login.registration_token":
            auth["token"] = registration_token
        body["auth"] = auth
    raise smoke.Failure(
        "registration of {} neither succeeded nor was rejected".format(body["username"])
    )


def expect_transport_rejects_out_of_namespace(transport, room_id, server_name):
    """Client-side guard: the transport must refuse to send as anyone outside
    @ai_*:<server_name> without talking to the homeserver."""
    for bad in ("@bob:" + server_name, "@ai_x:elsewhere.example"):
        try:
            transport.send_text(room_id=room_id, sender_id=bad, text="must not send")
        except DeliveryError as exc:
            expect(
                exc.code == "appservice.namespace",
                "unexpected DeliveryError code {!r} for {!r}".format(exc.code, bad),
            )
        else:
            raise smoke.Failure("transport delivered as {!r} outside the namespace".format(bad))
    return "appservice.namespace x2"


def expect_raw_masquerade_rejected(base_url, as_token, room_id, server_name):
    """Presenting the as_token with ?user_id=@bob (outside the namespace) must
    be rejected by the homeserver itself (M_EXCLUSIVE / M_FORBIDDEN)."""
    path = "/_matrix/client/v3/rooms/{}/send/m.room.message/{}".format(
        quote_room(room_id), "e2e" + secrets.token_hex(6)
    )
    url = base_url + path + "?" + urllib.parse.urlencode(
        {"user_id": "@bob:" + server_name}
    )
    status, resp = request_json(
        "PUT",
        url,
        body={"msgtype": "m.text", "body": "must be rejected"},
        token=as_token,
    )
    errcode = (resp or {}).get("errcode")
    expect(
        status in (400, 403) and errcode in ("M_EXCLUSIVE", "M_FORBIDDEN"),
        "masquerading @bob returned HTTP {} errcode {}: {!r}".format(
            status, errcode, resp
        ),
    )
    return "HTTP {} {}".format(status, errcode)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", required=True, help="e.g. http://127.0.0.1:8008")
    parser.add_argument(
        "--env-file",
        default=os.path.join(_REPO_ROOT, "server", "compose", ".env"),
        help="server/compose/.env rendered by init.sh (AS token + server name; "
        "default: server/compose/.env)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="max seconds to wait for /_matrix/client/versions (default 180)",
    )
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")

    values = read_env(args.env_file)
    as_token = values.get("APPSERVICE_AS_TOKEN")
    server_name = values.get("BOTVILLE_SERVER_NAME") or "localhost"
    registration_token = values.get("BOTVILLE_REGISTRATION_TOKEN")
    expect(
        as_token and as_token != "<change-me>",
        "APPSERVICE_AS_TOKEN missing or placeholder in {} (run: bash server/scripts/init.sh)".format(
            args.env_file
        ),
    )

    ai_user = "@ai_e2e_{}:{}".format(secrets.token_hex(4), server_name)
    transport = AppServiceMatrixTransport(
        homeserver_url=base_url,
        as_token=as_token,
        server_name=server_name,
    )
    text = "appservice e2e {}".format(secrets.token_hex(4))

    alice = {}

    def _register_alice():
        # Random suffix so re-running against a warm stack does not hit
        # M_USER_IN_USE on the human account (the AI user is random too).
        client = register_user(
            base_url, registration_token, "alice_appsvc_" + secrets.token_hex(3)
        )
        alice["client"] = client
        return client.user_id

    room = {}
    sent = {}

    run_step("versions", lambda: smoke.Smoke(base_url, None).wait_for_versions(args.timeout))
    run_step("register alice (UIA)", _register_alice)
    run_step(
        "transport registers the AI user (m.login.application_service)",
        lambda: transport.ensure_user(ai_user),
    )
    run_step(
        "alice creates room and invites {}".format(ai_user),
        lambda: room.setdefault(
            "id",
            create_room(
                alice["client"],
                invite=[ai_user],
                preset="private_chat",
            ),
        ),
    )
    run_step(
        "transport delivers as the AI user (join on M_FORBIDDEN + send)",
        lambda: sent.setdefault("event", transport.send_text(
            room_id=room["id"], sender_id=ai_user, text=text
        )),
    )
    run_step(
        "alice reads the AI message (sender + body)",
        lambda: read_ai_message(
            alice["client"], room["id"], sent["event"].event_id, ai_user, text
        ),
    )
    run_step(
        "normal ai_* UIA registration rejected (exclusive)",
        lambda: expect_normal_ai_registration_rejected(base_url, registration_token),
    )
    run_step(
        "transport refuses out-of-namespace senders (client-side)",
        lambda: expect_transport_rejects_out_of_namespace(
            transport, room["id"], server_name
        ),
    )
    run_step(
        "raw as_token masquerade of @bob rejected by the homeserver",
        lambda: expect_raw_masquerade_rejected(
            base_url, as_token, room["id"], server_name
        ),
    )
    print("APPSERVICE E2E OK: {} sent as {} via {}".format(
        sent["event"].event_id, ai_user, base_url))
    return 0


if __name__ == "__main__":
    sys.exit(main())
