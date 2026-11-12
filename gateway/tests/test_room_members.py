# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

GATEWAY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(GATEWAY_ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from botville_gateway.room_members import RoomMemberSource
from botville_gateway.config import Config
from botville_gateway.http_server import build_server
from botville_gateway.matrix import InMemoryMatrixTransport
from botville_gateway.matrix import DeliveryError
from botville_gateway.matrix_appservice import AppServiceMatrixTransport
from botville_gateway.engine import ModelClient, ModelConfig


SERVER = "example.test"
AI_A = "@ai_a:" + SERVER
AI_B = "@ai_b:" + SERVER


def _local_ai(user_id):
    return isinstance(user_id, str) and user_id.startswith("@ai_") and user_id.endswith(":" + SERVER)


class TestRoomMemberSource(unittest.TestCase):
    def test_member_event_populates_cache_without_fetch(self):
        calls = []
        source = RoomMemberSource(
            fetcher=lambda room, personas: calls.append((room, personas)) or (AI_B,),
            personas=(AI_A, AI_B), is_local_ai=_local_ai,
        )
        source.observe_member_event({
            "type": "m.room.member", "room_id": "!room:" + SERVER,
            "state_key": AI_A, "content": {"membership": "join"},
        })
        self.assertEqual(source.members("!room:" + SERVER), [AI_A])
        self.assertEqual(calls, [])

    def test_leave_and_ban_remove_but_invite_does_not_add(self):
        source = RoomMemberSource(fetcher=None, personas=(AI_A,), is_local_ai=_local_ai)
        room = "!room:" + SERVER
        for membership in ("invite", "leave", "ban"):
            source.observe_member_event({
                "type": "m.room.member", "room_id": room, "state_key": AI_A,
                "content": {"membership": membership},
            })
        self.assertEqual(source.members(room), [])

    def test_fetch_failure_is_propagated_for_consumer_fail_closed_handling(self):
        source = RoomMemberSource(
            fetcher=lambda room, personas: (_ for _ in ()).throw(RuntimeError("down")),
            personas=(AI_A,), is_local_ai=_local_ai,
        )
        with self.assertRaises(RuntimeError):
            source.members("!room:" + SERVER)


class _FakeHomeserverTransport(InMemoryMatrixTransport):
    """Small AS transport double exposing the joined-members CS route."""

    def __init__(self, joined=(), failure=None):
        super().__init__()
        self.joined, self.failure, self.member_requests = tuple(joined), failure, []

    def joined_members(self, room_id, personas):
        self.member_requests.append((room_id, tuple(personas)))
        if self.failure:
            raise self.failure
        return self.joined


def _model_client():
    client = ModelClient(ModelConfig(base_url="http://fake.invalid", model="test"))
    client._post = lambda payload, persona: {"choices": [{"message": {"content": "morning"}}]}
    return client


class TestProductionWiring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _server(self, transport, personas):
        persona_path = os.path.join(self.tmp, "personas.json")
        with open(persona_path, "w", encoding="utf-8") as stream:
            json.dump({"personas": [
                {"user_id": uid, "system_prompt": "Be helpful."} for uid in personas
            ]}, stream)
        server = build_server(Config(
            host="127.0.0.1", port=0, dev_mode=True,
            hmac_secret=b"test-hmac-secret" * 2, matrix_server_name=SERVER,
            consumer_enabled=True, as_txn_db_path=os.path.join(self.tmp, "as.db"),
            personas_file=persona_path, model_base_url="http://fake.invalid", model_name="test",
            silent_hours_enabled=False,
        ), transport=transport)
        self.addCleanup(server.server_close)
        server.as_consumer.stop()
        server.as_consumer._stop.clear()  # deterministic direct run after stopping worker
        server.engine_hooks._client = _model_client()
        return server

    def _consume(self, server):
        body = {"events": [{
            "event_id": "$human", "type": "m.room.message", "room_id": "!room:" + SERVER,
            "sender": "@human:" + SERVER,
            "content": {"msgtype": "m.text", "body": "hello"},
        }]}
        server.as_txn_store.store_transaction("txn", json.dumps(body))
        server.as_consumer.run_once()

    def test_dm_without_mention_sends_once_via_production_wiring(self):
        transport = _FakeHomeserverTransport(joined=(AI_A, "@human:" + SERVER))
        server = self._server(transport, (AI_A,))
        self._consume(server)
        self.assertEqual([item.sender_id for item in transport.sent], [AI_A])
        self.assertEqual(len(transport.member_requests), 1)

    def test_group_with_two_ais_without_mention_does_not_reply(self):
        transport = _FakeHomeserverTransport(joined=(AI_A, AI_B, "@human:" + SERVER))
        server = self._server(transport, (AI_A, AI_B))
        self._consume(server)
        self.assertEqual(transport.sent, [])

    def test_membership_fetch_failure_fails_closed_and_counts_error(self):
        transport = _FakeHomeserverTransport(failure=RuntimeError("unavailable"))
        server = self._server(transport, (AI_A,))
        self._consume(server)
        self.assertEqual(transport.sent, [])
        self.assertEqual(server.as_consumer.stats.get("room_member_errors"), 1)

    def test_consumed_member_event_warms_production_cache_without_fetch(self):
        transport = _FakeHomeserverTransport(joined=(AI_A,))
        server = self._server(transport, (AI_A,))
        body = {"events": [
            {"event_id": "$member", "type": "m.room.member", "room_id": "!room:" + SERVER,
             "sender": "@admin:" + SERVER, "state_key": AI_A,
             "content": {"membership": "join"}},
            {"event_id": "$human", "type": "m.room.message", "room_id": "!room:" + SERVER,
             "sender": "@human:" + SERVER, "content": {"msgtype": "m.text", "body": "hello"}},
        ]}
        server.as_txn_store.store_transaction("member-cache", json.dumps(body))
        server.as_consumer.run_once()
        self.assertEqual([item.sender_id for item in transport.sent], [AI_A])
        self.assertEqual(transport.member_requests, [])


class _JoinedMembersHS:
    def __init__(self):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def _reply(self, payload):
                data = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                outer.requests.append(("GET", self.path))
                self._reply({"joined": {AI_A: {}, "@human:" + SERVER: {}}})

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                body = json.loads(raw or b"{}")
                outer.requests.append(("POST", self.path))
                self._reply({"user_id": "@" + body["username"] + ":" + SERVER})

            def do_PUT(self):
                outer.requests.append(("PUT", self.path))
                self._reply({"event_id": "$reply"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        host, port = self.httpd.server_address
        return "http://{}:{}".format(host, port)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)


class TestAppserviceProductionMembership(unittest.TestCase):
    def test_http_homeserver_joined_members_drives_dm_reply(self):
        hs = _JoinedMembersHS()
        self.addCleanup(hs.close)
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        persona_path = os.path.join(tmp, "personas.json")
        with open(persona_path, "w", encoding="utf-8") as stream:
            json.dump({"personas": [{"user_id": AI_A, "system_prompt": "Be helpful."}]}, stream)
        transport = AppServiceMatrixTransport(
            homeserver_url=hs.url, as_token="fake-test-token", server_name=SERVER,
        )
        server = build_server(Config(
            host="127.0.0.1", port=0, dev_mode=True, hmac_secret=b"test-hmac-secret" * 2,
            matrix_server_name=SERVER, consumer_enabled=True,
            as_txn_db_path=os.path.join(tmp, "as.db"), personas_file=persona_path,
            model_base_url="http://fake.invalid", model_name="test", silent_hours_enabled=False,
        ), transport=transport)
        self.addCleanup(server.server_close)
        server.as_consumer.stop()
        server.as_consumer._stop.clear()
        server.engine_hooks._client = _model_client()
        body = {"events": [{"event_id": "$human", "type": "m.room.message",
                 "room_id": "!room:" + SERVER, "sender": "@human:" + SERVER,
                 "content": {"msgtype": "m.text", "body": "hello"}}]}
        server.as_txn_store.store_transaction("http-members", json.dumps(body))
        server.as_consumer.run_once()
        requests = [path for method, path in hs.requests if method == "GET"]
        self.assertEqual(len(requests), 1)
        parsed = urlparse(requests[0])
        self.assertEqual(parsed.path, "/_matrix/client/v3/rooms/%21room%3Aexample.test/joined_members")
        self.assertEqual(parse_qs(parsed.query), {"user_id": [AI_A]})
        self.assertTrue(any(method == "PUT" for method, _path in hs.requests))


class TestAppserviceMembershipLookup(unittest.TestCase):
    def test_retries_personas_until_one_can_read_joined_members(self):
        transport = AppServiceMatrixTransport(
            homeserver_url="http://127.0.0.1:9", as_token="fake-test-token",
            server_name=SERVER,
        )
        calls = []

        def request(method, path, **kwargs):
            calls.append(kwargs["query"]["user_id"])
            if len(calls) == 1:
                raise DeliveryError("M_FORBIDDEN", "denied")
            return {"joined": {AI_A: {}, AI_B: {}}}

        transport._request = request
        self.assertEqual(
            transport.joined_members("!room:" + SERVER, (AI_A, AI_B)), (AI_A, AI_B)
        )
        self.assertEqual(calls, [AI_A, AI_B])


if __name__ == "__main__":
    unittest.main()
