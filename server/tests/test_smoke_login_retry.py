#!/usr/bin/env python3
"""Hermetic tests for smoke.py's login 429 backoff (issue #95, A1.1 follow-up).

No Docker, no real homeserver: the mock-based cases patch smoke.request_json
and time.sleep; one case drives the real request_json against a local
http.server that answers 429 (with a JSON body) before 200, proving urllib's
HTTPError body is parsed for retry_after_ms. Standard library only.

Run:  python3 -m unittest discover -s server/tests -p "test_*.py" -v
"""

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import smoke  # noqa: E402  (same directory as this file)


def limit_body(retry_after_ms=None):
    body = {"errcode": "M_LIMIT_EXCEEDED", "error": "Too many requests"}
    if retry_after_ms is not None:
        body["retry_after_ms"] = retry_after_ms
    return body


class LoginRetryMockTest(unittest.TestCase):
    def login(self):
        client = smoke.Smoke("http://127.0.0.1:1", None)
        with mock.patch("time.sleep") as sleep:
            client.login()
        return client, sleep

    def test_429_then_200_succeeds_with_retry_after_ms(self):
        responses = [
            (429, limit_body(retry_after_ms=250)),
            (200, {"user_id": "@u:localhost", "access_token": "tok", "device_id": "DEV"}),
        ]
        with mock.patch.object(smoke, "request_json", side_effect=responses):
            client, sleep = self.login()
        self.assertEqual(client.access_token, "tok")
        sleep.assert_called_once_with(0.25)

    def test_429_without_retry_after_ms_defaults_to_1000(self):
        responses = [
            (429, limit_body()),
            (429, None),  # unparseable/empty body must not crash the retry
            (200, {"user_id": "@u:localhost", "access_token": "tok"}),
        ]
        with mock.patch.object(smoke, "request_json", side_effect=responses):
            client, sleep = self.login()
        self.assertEqual(client.access_token, "tok")
        self.assertEqual(
            [call.args for call in sleep.call_args_list], [(1.0,), (1.0,)]
        )

    def test_persistent_429_fails_after_max_retries(self):
        with mock.patch.object(
            smoke, "request_json", return_value=(429, limit_body(retry_after_ms=10))
        ) as req, self.assertRaises(smoke.Failure) as ctx:
            self.login()
        # 1 initial attempt + LOGIN_MAX_RETRIES retries, then the usual fail.
        self.assertEqual(req.call_count, smoke.LOGIN_MAX_RETRIES + 1)
        self.assertIn("login returned HTTP 429", str(ctx.exception))

    def test_non_429_error_fails_without_retry(self):
        with mock.patch.object(
            smoke,
            "request_json",
            return_value=(403, {"errcode": "M_FORBIDDEN", "error": "Bad password"}),
        ) as req, self.assertRaises(smoke.Failure):
            self.login()
        req.assert_called_once()


class LoginRetryRealHttpTest(unittest.TestCase):
    """login() against a local server sending a genuine 429 + JSON body.

    request_json must read the body out of urllib's HTTPError for the retry
    delay to be honoured; retry_after_ms is tiny so no sleep patching needed.
    """

    maxDiff = None

    def test_real_429_body_is_parsed_and_retried(self):
        state = {"attempts": 0}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                state["attempts"] += 1
                if state["attempts"] == 1:
                    payload = limit_body(retry_after_ms=5)
                    self.send_response(429)
                else:
                    payload = {
                        "user_id": "@u:localhost",
                        "access_token": "tok",
                        "device_id": "DEV",
                    }
                    self.send_response(200)
                data = json.dumps(payload).encode("utf-8")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):  # keep test output quiet
                pass

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(httpd.server_close)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.shutdown)

        host, port = httpd.server_address
        client = smoke.Smoke("http://{}:{}".format(host, port), None)
        with mock.patch("time.sleep") as sleep:
            client.login()
        self.assertEqual(client.access_token, "tok")
        self.assertEqual(state["attempts"], 2)
        sleep.assert_called_once_with(0.005)


if __name__ == "__main__":
    unittest.main()
