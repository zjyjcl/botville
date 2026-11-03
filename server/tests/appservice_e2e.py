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
  6. the rendered registration carries a valid hs_token (64 hex, differs from
     the as_token; PR #99 review);
  7. gateway HTTP e2e (PR #99 review blocker 1): alice creates a room with
     room_alias_name=botville_conv_<conversation_id> and invites
     @ai_<persona>:<server>; the real gateway HTTP server (build_server,
     appservice transport, port 0) receives POST
     /v1/conversations/<conversation_id>/messages with
     sender_persona_id=<persona>; alice reads the room and asserts the event
     sender/body (persona -> @ai_* mapping + alias -> room resolution);
  8. a normal UIA registration inside the @ai_* namespace must be rejected
     with errcode M_EXCLUSIVE (exclusive namespace);
  9. the transport refuses out-of-namespace senders client-side (no request);
  10. raw masquerade (?user_id=@bob:...) with the as_token must be rejected by
      the homeserver (M_EXCLUSIVE / M_FORBIDDEN).

Only standard endpoints; no /_synapse/admin, no admin-room commands. The
as_token is read from --env-file and never printed. First failure exits
non-zero (each step prints PASS/FAIL).
"""

import argparse
import os
import re
import secrets
import sys
import threading
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import smoke  # noqa: E402  (same directory as this file)
from smoke import expect, request_json  # noqa: E402

# Import the real gateway transport (src-layout, no install needed).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_GATEWAY_SRC = os.path.join(_REPO_ROOT, "gateway", "src")
if _GATEWAY_SRC not in sys.path:
    sys.path.insert(0, _GATEWAY_SRC)

from botville_gateway.config import Config  # noqa: E402
from botville_gateway.http_server import build_server  # noqa: E402
from botville_gateway.matrix import DeliveryError  # noqa: E402
from botville_gateway.matrix_appservice import AppServiceMatrixTransport  # noqa: E402

# Reuse the A1.1 e2e helpers (step runner, UIA registration, room creation).
from e2e_synapse import create_room, quote_room, register_user, run_step  # noqa: E402

# Conversation -> room alias convention (gateway GATEWAY_ROOM_ALIAS_PREFIX
# default; see docs/deploy/appservice.md).
ROOM_ALIAS_PREFIX = "botville_conv_"


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
    """A normal UIA registration of ai_<name> must be refused with M_EXCLUSIVE —
    anything else (e.g. registration closed for other reasons) would paint a
    green light over a broken exclusive namespace (PR #99 review)."""
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
                errcode == "M_EXCLUSIVE",
                "register ai_* returned HTTP {} errcode {!r}, expected M_EXCLUSIVE: {!r}".format(
                    status, errcode, resp
                ),
            )
            return "HTTP {} M_EXCLUSIVE".format(status)
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


def expect_registration_tokens(registration_path, env_as_token):
    """The rendered registration must carry both tokens: 64 hex, non-empty,
    and different from each other (PR #99 review: an empty as_token turns the
    @ai_* namespace into an unauthenticated takeover)."""
    with open(registration_path, encoding="utf-8") as f:
        text = f.read()

    def find(key):
        m = re.search(r'^{}: "?([0-9a-f]{{64}})"?$'.format(key), text, re.MULTILINE)
        expect(m, "{} missing/invalid in {}".format(key, registration_path))
        return m.group(1)

    file_as = find("as_token")
    file_hs = find("hs_token")
    expect(
        file_as != file_hs,
        "as_token and hs_token must differ in {}".format(registration_path),
    )
    expect(
        file_as == env_as_token,
        "rendered as_token differs from APPSERVICE_AS_TOKEN in the env file",
    )
    return "hs_token ok (64 hex, differs from as_token)"


class GatewayHttpFixture:
    """The real gateway HTTP server (build_server + appservice transport,
    port 0) started in-process for the HTTP-level e2e leg."""

    def __init__(self, base_url, server_name, as_token):
        self.config = Config(
            host="127.0.0.1",
            port=0,
            dev_mode=True,  # e2e exercises delivery, not gateway auth
            silent_hours_enabled=False,  # CI runs at any local time
            matrix_transport="appservice",
            homeserver_url=base_url,
            matrix_server_name=server_name,
            as_token=as_token,
        )
        self.server = build_server(self.config)

    def __enter__(self):
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @property
    def base(self):
        host, port = self.server.server_address[:2]
        return "http://{}:{}".format(host, port)


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
        "--registration-file",
        default=os.path.join(
            _REPO_ROOT, "server", "compose", "generated", "appservice",
            "botville-gateway.yaml",
        ),
        help="rendered appservice registration to verify hs_token "
        "(default: server/compose/generated/appservice/botville-gateway.yaml)",
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
    conv = {}

    def _create_aliased_room():
        # PR #99 review blocker 1: the gateway-level flow. Alice creates a room
        # whose alias follows the botville_conv_<conversation_id> convention
        # and invites @ai_<persona>:<server>; the gateway then only sees the
        # persona/conversation ids.
        conv["conversation_id"] = secrets.token_hex(6)
        conv["persona"] = "e2e" + secrets.token_hex(3)
        conv["sender"] = "@ai_{}:{}".format(conv["persona"], server_name)
        conv["text"] = "gateway http e2e {}".format(secrets.token_hex(4))
        transport.ensure_user(conv["sender"])
        room["alias_id"] = create_room(
            alice["client"],
            room_alias_name=ROOM_ALIAS_PREFIX + conv["conversation_id"],
            invite=[conv["sender"]],
            preset="private_chat",
        )
        return "{} invites {}".format(room["alias_id"], conv["sender"])

    def _gateway_http_deliver():
        with GatewayHttpFixture(base_url, server_name, as_token) as gateway:
            status, resp = request_json(
                "POST",
                gateway.base + "/v1/conversations/{}/messages".format(
                    conv["conversation_id"]
                ),
                body={
                    "sender_persona_id": conv["persona"],
                    "content": {"type": "text", "text": conv["text"]},
                },
            )
        expect(
            200 <= status < 300,
            "gateway sendMessage returned HTTP {}: {!r}".format(status, resp),
        )
        expect(
            (resp or {}).get("outcome") == "delivered",
            "gateway receipt outcome={!r}: {!r}".format(
                (resp or {}).get("outcome"), resp
            ),
        )
        event_id = ((resp or {}).get("delivery") or {}).get("matrix_event_id")
        expect(event_id, "receipt without matrix_event_id: {!r}".format(resp))
        conv["event_id"] = event_id
        return event_id

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
        "rendered registration carries a valid hs_token",
        lambda: expect_registration_tokens(args.registration_file, as_token),
    )
    run_step(
        "alice creates aliased room #{}<conv> and invites @ai_<persona>".format(
            ROOM_ALIAS_PREFIX
        ),
        _create_aliased_room,
    )
    run_step(
        "gateway HTTP POST /v1/conversations/<id>/messages delivers via appservice",
        _gateway_http_deliver,
    )
    run_step(
        "alice reads the persona message (sender + body)",
        lambda: read_ai_message(
            alice["client"], room["alias_id"], conv["event_id"], conv["sender"],
            conv["text"],
        ),
    )
    run_step(
        "normal ai_* UIA registration rejected (M_EXCLUSIVE)",
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
    print("APPSERVICE E2E OK: {} + gateway HTTP {} sent via {}".format(
        sent["event"].event_id, conv["event_id"], base_url))
    return 0


if __name__ == "__main__":
    sys.exit(main())
