#!/usr/bin/env python3
"""Hermetic tests for the continuwuity first-boot bootstrap (#68) and init.sh.

No Docker and no real homeserver: bootstrap-continuwuity.sh runs against a
local http.server fake (versions / register/available / UIA register /
registration-token validity) plus a fake COMPOSE command that prints canned
container logs. Standard library only.

Run:  python3 -m unittest discover -s server/tests -p "test_*.py" -v
"""

import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1]
BOOTSTRAP = SERVER_DIR / "scripts" / "bootstrap-continuwuity.sh"
INIT = SERVER_DIR / "scripts" / "init.sh"

ADMIN_USER = "botville-admin"
ADMIN_PASSWORD = "correct-horse-battery"
CONFIG_TOKEN = "cfg-token-123"
ONE_TIME_TOKEN = "onet1me_T0ken"


def ansi_token_line(token):
    """A continuwuity bootstrap-mode log line (with CSI colors) as it appears
    in `docker compose logs --no-color` output."""
    return (
        "\x1b[32m2026-11-04T00:00:00Z  WARN This server has no registered users.\n"
        "To administer this server it is required to register an account on "
        "localhost using the registration token \x1b[0m"
        + token
        + "\x1b[32m . Pick your own username and password!\x1b[0m"
    )


QUIET_LOGS = "2026-11-04T00:00:00Z  INFO server started and listening on 0.0.0.0:8008\n"


class FakeHomeserver:
    """Fake continuwuity client API for bootstrap-continuwuity.sh.

    Modes:
      fresh        userless database: the one-time token from the logs works,
                   the configured token is invalid (pre-first-user).
      user_exists  BOTVILLE_ADMIN_USER is already registered; the configured
                   token is valid (post-first-user).
      other_admin  a different first user consumed the one-time token; the
                   stale token line is still in the logs; the configured
                   token is valid (the upgrade scenario from the review).
    """

    def __init__(self, mode):
        self.mode = mode
        self.registered = []  # (username, password) pairs accepted via UIA
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, status, body):
                payload = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _query(self):
                _, _, query = self.path.partition("?")
                params = {}
                for part in query.split("&"):
                    if "=" in part:
                        key, value = part.split("=", 1)
                        params[key] = value
                return params

            def do_GET(self):
                path = self.path.partition("?")[0]
                params = self._query()
                if path == "/_matrix/client/versions":
                    self._send(200, {"versions": ["v1.12"]})
                elif path == "/_matrix/client/v1/register/m.login.registration_token/validity":
                    valid = (
                        outer.mode != "fresh"
                        and params.get("token") == CONFIG_TOKEN
                    )
                    self._send(200, {"valid": valid})
                elif path == "/_matrix/client/v3/register/available":
                    if (
                        outer.mode == "user_exists"
                        and params.get("username") == ADMIN_USER
                    ):
                        self._send(
                            400,
                            {"errcode": "M_USER_IN_USE", "error": "User ID already taken."},
                        )
                    else:
                        self._send(200, {"available": True})
                else:
                    self._send(
                        404, {"errcode": "M_UNRECOGNIZED", "error": "Unrecognized request"}
                    )

            def do_POST(self):
                assert self.path.partition("?")[0] == "/_matrix/client/v3/register"
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                if outer.mode == "user_exists":
                    self._send(
                        400, {"errcode": "M_USER_IN_USE", "error": "User ID already taken."}
                    )
                    return
                auth = body.get("auth") or {}
                if not auth:
                    self._send(
                        401,
                        {
                            "session": "sess-1",
                            "flows": [{"stages": ["m.login.registration_token"]}],
                        },
                    )
                    return
                if outer.mode == "fresh" and auth.get("token") == ONE_TIME_TOKEN:
                    outer.registered.append((body.get("username"), body.get("password")))
                    self._send(
                        200,
                        {
                            "user_id": "@{}:localhost".format(body.get("username")),
                            "access_token": "acs",
                            "device_id": "DEV",
                        },
                    )
                else:
                    self._send(
                        401,
                        {"errcode": "M_FORBIDDEN", "error": "Invalid registration token"},
                    )

            def log_message(self, *args):  # keep test output quiet
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self):
        host, port = self.httpd.server_address
        return "http://{}:{}".format(host, port)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def run_bootstrap(tmp, base_url, log_text, *, password=ADMIN_PASSWORD, admin_user=ADMIN_USER,
                  reset_confirm=None, compose_status=0, env=None):
    """Run bootstrap-continuwuity.sh against the fake homeserver/log source.

    tmp must be a private temp directory; returns the CompletedProcess.
    """
    tmp = Path(tmp)
    env_file = tmp / ".env"
    env_file.write_text(
        "BOTVILLE_ADMIN_USER={}\n".format(admin_user)
        + "BOTVILLE_ADMIN_PASSWORD={}\n".format(password)
        + "BOTVILLE_REGISTRATION_TOKEN={}\n".format(CONFIG_TOKEN)
        + "BOTVILLE_HTTP_PORT=8008\n",
        encoding="utf-8",
    )
    logs_file = tmp / "logs.txt"
    logs_file.write_text(log_text, encoding="utf-8")
    fake_compose = tmp / "fake-compose.sh"
    fake_compose.write_text(
        "#!/usr/bin/env bash\n"
        'cat "${FAKE_LOGS_FILE:?}"\n'
        "exit ${FAKE_COMPOSE_STATUS:-0}\n",
        encoding="utf-8",
    )
    fake_compose.chmod(0o755)
    proc_env = dict(os.environ)
    proc_env.update(
        {
            "BOTVILLE_ENV_FILE": str(env_file),
            "COMPOSE": "bash {}".format(fake_compose),
            "BOTVILLE_BASE_URL": base_url,
            "BOTVILLE_BOOTSTRAP_TIMEOUT": "20",
            "FAKE_LOGS_FILE": str(logs_file),
            "FAKE_COMPOSE_STATUS": str(compose_status),
        }
    )
    if reset_confirm is not None:
        proc_env["BOTVILLE_BOOTSTRAP_RESET_CONFIRM"] = reset_confirm
    proc_env.update(env or {})
    return subprocess.run(
        ["bash", str(BOOTSTRAP)],
        env=proc_env,
        capture_output=True,
        text=True,
        timeout=90,
    )


class BootstrapScriptTest(unittest.TestCase):
    maxDiff = None

    def bootstrap(self, mode, log_text, **kwargs):
        tmp = tempfile.mkdtemp(prefix="botville-bootstrap-test-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with FakeHomeserver(mode) as hs:
            proc = run_bootstrap(tmp, hs.base_url, log_text, **kwargs)
        return proc, hs

    def test_fresh_server_registers_admin(self):
        proc, hs = self.bootstrap("fresh", ansi_token_line(ONE_TIME_TOKEN) + "\n")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("registered @{}:localhost".format(ADMIN_USER), proc.stdout)
        self.assertIn((ADMIN_USER, ADMIN_PASSWORD), hs.registered)
        # The one-time token must never be echoed anywhere.
        self.assertNotIn(ONE_TIME_TOKEN, proc.stdout + proc.stderr)

    def test_fresh_server_base_url_trailing_slash(self):
        tmp = tempfile.mkdtemp(prefix="botville-bootstrap-test-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with FakeHomeserver("fresh") as hs:
            proc = run_bootstrap(
                tmp,
                hs.base_url,
                ansi_token_line(ONE_TIME_TOKEN) + "\n",
                env={"BOTVILLE_BASE_URL": hs.base_url + "/"},
            )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("registered @{}:localhost".format(ADMIN_USER), proc.stdout)

    def test_admin_username_already_registered(self):
        proc, hs = self.bootstrap("user_exists", ansi_token_line(ONE_TIME_TOKEN) + "\n")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("already registered; bootstrap was already complete", proc.stdout)
        self.assertEqual(hs.registered, [])

    def test_consumed_token_means_other_first_user(self):
        # The review's blocker 1: an upgraded deployment whose first user was
        # registered manually; the consumed token line is still in the logs.
        proc, hs = self.bootstrap("other_admin", ansi_token_line(ONE_TIME_TOKEN) + "\n")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("BOTVILLE_ADMIN_USER", proc.stdout)
        self.assertIn("different first user", proc.stdout)
        # Guidance only — never a suggestion to wipe/delete data.
        out = proc.stdout + proc.stderr
        self.assertNotIn("wipe", out.lower())
        self.assertNotIn("down -v", out)
        self.assertNotIn("volume rm", out)
        self.assertNotIn("continuwuity-data", out)

    def test_consumed_token_reset_confirm_only_prints_commands(self):
        proc, hs = self.bootstrap(
            "other_admin",
            ansi_token_line(ONE_TIME_TOKEN) + "\n",
            reset_confirm="yes",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("docker volume rm botville_continuwuity-data", proc.stdout)
        self.assertIn("never runs them", proc.stdout)

    def test_no_log_token_config_token_valid_means_bootstrapped(self):
        proc, hs = self.bootstrap("user_exists", QUIET_LOGS)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("bootstrap complete", proc.stdout)
        self.assertEqual(hs.registered, [])

    def test_no_log_token_config_token_invalid_fails_loud(self):
        proc, hs = self.bootstrap("fresh", QUIET_LOGS)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("bootstrap state unclear", proc.stderr)
        self.assertIn("BOTVILLE_REGISTRATION_TOKEN", proc.stderr)
        # The bash wrapper points at the full logs.
        self.assertIn("grep -a 'registration token'", proc.stderr)

    def test_stale_token_on_userless_server_fails_loud(self):
        # Token line found, but it is stale while the database still has no
        # users (configured token invalid): must not exit 0.
        proc, hs = self.bootstrap("fresh", ansi_token_line("stale-t0ken") + "\n")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("stale", proc.stderr)

    def test_password_placeholder_ok_when_already_bootstrapped(self):
        # No token in the logs + configured token valid: the password is
        # never needed.
        proc, hs = self.bootstrap("user_exists", QUIET_LOGS, password="<change-me>")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        # Stale token line in the logs + admin name taken: also fine.
        proc, hs = self.bootstrap(
            "user_exists", ansi_token_line(ONE_TIME_TOKEN) + "\n", password="<change-me>"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("already registered", proc.stdout)

    def test_password_placeholder_fails_only_when_registration_needed(self):
        proc, hs = self.bootstrap("fresh", ansi_token_line(ONE_TIME_TOKEN) + "\n",
                                  password="<change-me>")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("BOTVILLE_ADMIN_PASSWORD", proc.stderr)
        self.assertIn("init.sh", proc.stderr)
        self.assertEqual(hs.registered, [])

    def test_compose_logs_failure_prints_no_raw_logs(self):
        logs = "SECRETLOGMARKER some private log line\n"
        proc, hs = self.bootstrap("fresh", logs, compose_status=1)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("could not read the continuwuity logs", proc.stderr)
        # Raw log contents (which may contain live tokens) stay out of output.
        self.assertNotIn("SECRETLOGMARKER", proc.stdout + proc.stderr)


ENV_EXAMPLE = """\
# Botville server stack configuration.
BOTVILLE_HOMESERVER=synapse
BOTVILLE_SERVER_NAME=localhost
BOTVILLE_HTTP_PORT=8008
BOTVILLE_REGISTRATION_TOKEN=<change-me>
BOTVILLE_ADMIN_USER=botville-admin
BOTVILLE_ADMIN_PASSWORD=<change-me>
POSTGRES_USER=botville
POSTGRES_PASSWORD=<change-me>
SYNAPSE_MACAROON_SECRET=<change-me>
"""

TEMPLATE = (
    "server_name: ${BOTVILLE_SERVER_NAME}\n"
    "macaroon_secret: ${SYNAPSE_MACAROON_SECRET}\n"
    "port: ${BOTVILLE_HTTP_PORT}\n"
)

HEX64 = r"[0-9a-f]{64}"


class InitScriptTest(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="botville-init-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "scripts").mkdir()
        (self.tmp / "compose" / "synapse").mkdir(parents=True)
        shutil.copy(INIT, self.tmp / "scripts" / "init.sh")
        (self.tmp / "compose" / ".env.example").write_text(ENV_EXAMPLE, encoding="utf-8")
        (self.tmp / "compose" / "synapse" / "homeserver.yaml.tmpl").write_text(
            TEMPLATE, encoding="utf-8"
        )
        self.env_file = self.tmp / "compose" / ".env"
        self.generated = self.tmp / "compose" / "generated" / "synapse" / "homeserver.yaml"

    def run_init(self):
        return subprocess.run(
            ["bash", str(self.tmp / "scripts" / "init.sh")],
            capture_output=True,
            text=True,
            timeout=60,
        )

    def env_value(self, key):
        values = [
            line.split("=", 1)[1]
            for line in self.env_file.read_text(encoding="utf-8").splitlines()
            if line.startswith(key + "=")
        ]
        return values[-1] if values else None

    def test_fresh_run_creates_env_with_secrets(self):
        proc = self.run_init()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        content = self.env_file.read_text(encoding="utf-8")
        self.assertNotIn("<change-me>", content)
        # Non-secret values survive verbatim, secrets are generated.
        self.assertEqual(self.env_value("BOTVILLE_ADMIN_USER"), "botville-admin")
        self.assertRegex(self.env_value("BOTVILLE_ADMIN_PASSWORD"), HEX64)
        self.assertRegex(self.env_value("POSTGRES_PASSWORD"), HEX64)
        self.assertNotEqual(
            self.env_value("BOTVILLE_ADMIN_PASSWORD"), self.env_value("POSTGRES_PASSWORD")
        )
        # 0600, and the template rendered without leftovers.
        self.assertEqual(stat.S_IMODE(self.env_file.stat().st_mode), 0o600)
        rendered = self.generated.read_text(encoding="utf-8")
        self.assertIn("server_name: localhost", rendered)
        self.assertNotIn("${", rendered)

    def test_appends_missing_keys_preserving_values(self):
        # Old .env from before BOTVILLE_ADMIN_*/secrets existed, no trailing
        # newline, mode 0644.
        self.env_file.write_text(
            "BOTVILLE_HOMESERVER=continuwuity\n"
            "BOTVILLE_SERVER_NAME=old.example\n"
            "BOTVILLE_HTTP_PORT=8008",
            encoding="utf-8",
        )
        self.env_file.chmod(0o644)
        proc = self.run_init()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.env_value("BOTVILLE_HOMESERVER"), "continuwuity")
        self.assertEqual(self.env_value("BOTVILLE_SERVER_NAME"), "old.example")
        self.assertRegex(self.env_value("BOTVILLE_ADMIN_PASSWORD"), HEX64)
        self.assertRegex(self.env_value("SYNAPSE_MACAROON_SECRET"), HEX64)
        content = self.env_file.read_text(encoding="utf-8")
        self.assertNotIn("<change-me>", content)
        self.assertTrue(content.endswith("\n"))
        self.assertEqual(content.count("BOTVILLE_HOMESERVER="), 1)
        self.assertEqual(stat.S_IMODE(self.env_file.stat().st_mode), 0o600)

    def test_replaces_leftover_placeholder_values(self):
        # A hand-copied .env.example: keys exist but secret values are still
        # placeholders (re-running the old init.sh would keep them forever).
        self.env_file.write_text(ENV_EXAMPLE, encoding="utf-8")
        self.env_file.chmod(0o644)
        proc = self.run_init()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.env_value("BOTVILLE_ADMIN_USER"), "botville-admin")
        self.assertRegex(self.env_value("BOTVILLE_ADMIN_PASSWORD"), HEX64)
        self.assertRegex(self.env_value("POSTGRES_PASSWORD"), HEX64)
        self.assertEqual(self.env_value("BOTVILLE_HTTP_PORT"), "8008")
        self.assertNotIn("<change-me>", self.env_file.read_text(encoding="utf-8"))
        self.assertEqual(stat.S_IMODE(self.env_file.stat().st_mode), 0o600)

    def test_rerun_is_stable(self):
        self.run_init()
        first = self.env_file.read_text(encoding="utf-8")
        proc = self.run_init()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("keeping current values", proc.stdout)
        self.assertEqual(self.env_file.read_text(encoding="utf-8"), first)


if __name__ == "__main__":
    unittest.main()
