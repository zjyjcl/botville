#!/usr/bin/env python3
"""Unit checks for server/tests/gateway_hs_smoke.py against a fake HTTP server.

- hs_ping fails at once on 401/403, keeps retrying 429/5xx.
- fixtures (human user, device, room) are reused instead of piling up.
- nothing secret is printed: AS/HS tokens, the registration token, the
  fixture password/token, nor GATEWAY_MODEL_API_KEY from the env file.
"""

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gateway_hs_smoke as smoke  # noqa: E402

SECRETS = {
    "APPSERVICE_AS_TOKEN": "sentinel-as-token-0123456789abcdef0123456789",
    "APPSERVICE_HS_TOKEN": "sentinel-hs-token-0123456789abcdef0123456789",
    "BOTVILLE_REGISTRATION_TOKEN": "sentinel-registration-token-0123456789",
    "GATEWAY_MODEL_API_KEY": "sentinel-model-key-must-not-print-0123456789",
}
USER_TOKEN = "sentinel-user-access-token-0123456789"
PASSWORD = "sentinel-fixture-password-0123456789"


class FakeServer:
    """Scripted responses keyed by (method, path); records every request."""

    def __init__(self):
        self.routes = {}
        self.calls = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep test output quiet
                pass

            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                path = self.path.split("?", 1)[0]
                server.calls.append((self.command, path, self.headers.get("Authorization"), body))
                responder = server.routes.get((self.command, path))
                status, payload = responder(self) if callable(responder) else (responder or (404, {"errcode": "M_NOT_FOUND"}))
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = do_PUT = _handle

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:{}".format(self.httpd.server_address[1])
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def count(self, method, path):
        return sum(1 for c in self.calls if c[0] == method and c[1] == path)


PING = "/_matrix/client/v1/appservice/botville-gateway/ping"


class SmokeUnitTest(unittest.TestCase):
    def setUp(self):
        self.srv = FakeServer()
        self.addCleanup(self.srv.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_ping_fails_fast_on_auth_errors(self):
        for status in (401, 403):
            with self.subTest(status=status):
                self.srv.routes[("POST", PING)] = (status, {"errcode": "M_FORBIDDEN"})
                start = time.monotonic()
                with self.assertRaises(SystemExit) as ctx:
                    smoke.hs_ping(self.srv.url, SECRETS["APPSERVICE_AS_TOKEN"])
                self.assertLess(time.monotonic() - start, 5)
                msg = str(ctx.exception)
                self.assertIn("HTTP {}".format(status), msg)
                self.assertIn(self.srv.url + PING, msg)  # full URL
                self.assertNotIn(SECRETS["APPSERVICE_AS_TOKEN"], msg)

    def test_ping_retries_transient_errors(self):
        answers = [(429, {"errcode": "M_LIMIT_EXCEEDED"}), (502, {}), (200, {})]
        self.srv.routes[("POST", PING)] = lambda h: answers.pop(0)
        orig_sleep = smoke.time.sleep
        smoke.time.sleep = lambda s: None
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                smoke.hs_ping(self.srv.url, SECRETS["APPSERVICE_AS_TOKEN"])
        finally:
            smoke.time.sleep = orig_sleep
        self.assertIn("PASS", out.getvalue())
        self.assertEqual(self.srv.count("POST", PING), 3)

    def test_fixture_user_is_reused(self):
        self.srv.routes[("GET", "/_matrix/client/v3/account/whoami")] = (
            lambda h: (200, {"user_id": "@gwsmoke_x:localhost"})
            if h.headers.get("Authorization") == "Bearer " + USER_TOKEN
            else (401, {"errcode": "M_UNKNOWN_TOKEN"})
        )
        fixture = {"user_id": "@gwsmoke_x:localhost", "access_token": USER_TOKEN, "password": PASSWORD}
        user_id, token = smoke.ensure_human(self.srv.url, "unused", fixture)
        self.assertEqual((user_id, token), ("@gwsmoke_x:localhost", USER_TOKEN))
        self.assertEqual(self.srv.count("POST", "/_matrix/client/v3/register"), 0)
        self.assertEqual(self.srv.count("POST", "/_matrix/client/v3/login"), 0)

    def test_stale_token_logs_in_on_the_fixed_device(self):
        new_token = "sentinel-new-token-0123456789"
        self.srv.routes[("GET", "/_matrix/client/v3/account/whoami")] = (
            lambda h: (200, {"user_id": "@gwsmoke_x:localhost"})
            if h.headers.get("Authorization") == "Bearer " + new_token
            else (401, {"errcode": "M_UNKNOWN_TOKEN"})
        )
        self.srv.routes[("POST", "/_matrix/client/v3/login")] = (200, {"access_token": new_token})
        fixture = {"user_id": "@gwsmoke_x:localhost", "access_token": "old", "password": PASSWORD}
        user_id, token = smoke.ensure_human(self.srv.url, "unused", fixture)
        self.assertEqual(token, new_token)
        self.assertEqual(fixture["access_token"], new_token)
        self.assertEqual(self.srv.count("POST", "/_matrix/client/v3/register"), 0)
        login_body = json.loads([c for c in self.srv.calls if c[1].endswith("/login")][0][3])
        self.assertEqual(login_body["device_id"], smoke.DEVICE_ID)

    def test_room_is_reused_when_both_joined(self):
        room = "!fixture:localhost"
        self.srv.routes[("GET", "/_matrix/client/v3/rooms/%21fixture%3Alocalhost/joined_members")] = (
            200, {"joined": {"@gwsmoke_x:localhost": {}, "@ai_gwsmoke:localhost": {}}})
        fixture = {"room_id": room}
        got = smoke.ensure_room(self.srv.url, USER_TOKEN, SECRETS["APPSERVICE_AS_TOKEN"],
                                "@gwsmoke_x:localhost", "@ai_gwsmoke:localhost", fixture)
        self.assertEqual(got, room)
        self.assertEqual(self.srv.count("POST", "/_matrix/client/v3/createRoom"), 0)

    def test_unusable_room_is_left_forgotten_and_replaced(self):
        self.srv.routes[("POST", "/_matrix/client/v3/createRoom")] = (200, {"room_id": "!new:localhost"})
        self.srv.routes[("POST", "/_matrix/client/v3/join/%21new%3Alocalhost")] = (200, {})
        fixture = {"room_id": "!gone:localhost"}
        got = smoke.ensure_room(self.srv.url, USER_TOKEN, SECRETS["APPSERVICE_AS_TOKEN"],
                                "@gwsmoke_x:localhost", "@ai_gwsmoke:localhost", fixture)
        self.assertEqual(got, "!new:localhost")
        self.assertEqual(fixture["room_id"], "!new:localhost")
        self.assertEqual(self.srv.count("POST", "/_matrix/client/v3/rooms/%21gone%3Alocalhost/leave"), 1)
        self.assertEqual(self.srv.count("POST", "/_matrix/client/v3/rooms/%21gone%3Alocalhost/forget"), 1)

    def test_ai_user_in_use_is_reused(self):
        self.srv.routes[("POST", "/_matrix/client/v3/register")] = (400, {"errcode": "M_USER_IN_USE"})
        self.assertEqual(
            smoke.as_register_ai_user(self.srv.url, SECRETS["APPSERVICE_AS_TOKEN"], "localhost"),
            "@ai_gwsmoke:localhost",
        )

    def test_run_cmd_does_not_inherit_stdin(self):
        r, w = os.pipe()
        os.write(w, b"must-not-be-read\n")
        os.close(w)
        saved = os.dup(0)
        os.dup2(r, 0)
        try:
            out = smoke.run_cmd(sys.executable + " -c", ["import sys; print(repr(sys.stdin.read()))"], "probe")
        finally:
            os.dup2(saved, 0)
            os.close(saved)
            os.close(r)
        self.assertEqual(out.strip(), "''")

    def test_fixture_file_is_private(self):
        path = os.path.join(self.tmp.name, "generated", "gateway-hs-smoke.json")
        smoke.save_fixtures(path, {"x": {"password": PASSWORD}})
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(smoke.load_fixtures(path)["x"]["password"], PASSWORD)
        self.assertEqual(smoke.load_fixtures(path + ".missing"), {})

    def test_full_run_never_prints_secrets(self):
        """Drive main() up to a failing ping and check the whole output."""
        env_file = os.path.join(self.tmp.name, ".env")
        with open(env_file, "w", encoding="utf-8") as fh:
            for key, value in SECRETS.items():
                fh.write("{}={}\n".format(key, value))
        r = self.srv.routes
        r[("GET", "/health")] = (200, {"status": "ok", "checks": [{"name": "matrix", "status": "ok"}]})
        r[("POST", "/_matrix/client/v3/register")] = (
            200, {"user_id": "@gwsmoke_x:localhost", "access_token": USER_TOKEN})
        r[("POST", "/_matrix/client/v3/user/%40gwsmoke_x%3Alocalhost/openid/request_token")] = (
            200, {"access_token": "sentinel-openid-token-0123456789"})
        r[("GET", "/v1/moments")] = (200, {"items": []})
        r[("POST", PING)] = (403, {"errcode": "M_FORBIDDEN"})
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                smoke.main([
                    "--hs-url", self.srv.url, "--gateway-url", self.srv.url,
                    "--env-file", env_file, "--timeout", "5",
                    "--gateway-exec", "true", "--gateway-restart", "true",
                ])
        text = out.getvalue() + err.getvalue() + str(ctx.exception)
        self.assertIn("HTTP 403", text)
        for value in list(SECRETS.values()) + [USER_TOKEN, "sentinel-openid-token-0123456789"]:
            self.assertNotIn(value, text)
        # The fixture landed next to the env file (generated/, 0600) for reuse.
        fixture_path = os.path.join(self.tmp.name, "generated", "gateway-hs-smoke.json")
        self.assertEqual(stat.S_IMODE(os.stat(fixture_path).st_mode), 0o600)
        self.assertIn(self.srv.url, smoke.load_fixtures(fixture_path))


if __name__ == "__main__":
    unittest.main()
