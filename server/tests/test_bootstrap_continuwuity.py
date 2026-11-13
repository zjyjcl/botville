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
RENDERER = SERVER_DIR / "scripts" / "render_push_denylist.py"

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
            # Single scrape: hermetic logs are already complete.
            "BOTVILLE_BOOTSTRAP_TOKEN_WAIT": "0",
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
        self.assertIn("--profile continuwuity --profile gateway down -v", proc.stdout)
        self.assertIn("docker volume rm -f <project>_gateway-data", proc.stdout)
        self.assertIn("never runs them", proc.stdout)
        self.assertNotIn("docker volume rm botville_continuwuity-data", proc.stdout)

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

    def test_token_wait_must_be_an_integer(self):
        # #191 suggestion 4: a non-integer wait must fail with a clear message
        # before any HTTP call or arithmetic, never deep inside $(( )).
        for bad in ("abc", "1.5", "-3", " 5", "5s"):
            with self.subTest(value=bad):
                with tempfile.TemporaryDirectory() as tmp:
                    proc = run_bootstrap(
                        tmp,
                        "http://127.0.0.1:9",
                        QUIET_LOGS,
                        env={"BOTVILLE_BOOTSTRAP_TOKEN_WAIT": bad},
                    )
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("BOTVILLE_BOOTSTRAP_TOKEN_WAIT must be a non-negative integer", proc.stderr)
                self.assertNotIn("waiting for", proc.stdout)

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
BOTVILLE_DOCKER_SUBNET=172.31.250.0/24
BOTVILLE_DOCKER_IP_RANGE=172.31.250.64/26
BOTVILLE_SYGNAL_IP=
BOTVILLE_PUSH_MOCK=true
BOTVILLE_APNS_ENABLED=false
APPSERVICE_AS_TOKEN=<change-me>
APPSERVICE_HS_TOKEN=<change-me>
"""

TEMPLATE = (
    "server_name: ${BOTVILLE_SERVER_NAME}\n"
    "macaroon_secret: ${SYNAPSE_MACAROON_SECRET}\n"
    "port: ${BOTVILLE_HTTP_PORT}\n"
)

# Mirrors compose/appservice/botville-gateway.yaml.tmpl closely enough to
# exercise init.sh's appservice rendering (A1.3), including the derived
# regex-escaped server name (also in the exclusive aliases namespace, #107).
APPSERVICE_TEMPLATE = (
    "id: botville-gateway\n"
    "url: ${BOTVILLE_APPSERVICE_URL}\n"
    'as_token: "${APPSERVICE_AS_TOKEN}"\n'
    'hs_token: "${APPSERVICE_HS_TOKEN}"\n'
    "namespaces:\n"
    "  users:\n"
    "    - exclusive: true\n"
    "      regex: '^@ai_.*:${BOTVILLE_SERVER_NAME_REGEX}$'\n"
    "  aliases:\n"
    "    - exclusive: true\n"
    "      regex: '^#botville_conv_.*:${BOTVILLE_SERVER_NAME_REGEX}$'\n"
)

HEX64 = r"[0-9a-f]{64}"
# Valid fixed appservice tokens (64 lowercase hex, different from each other).
AS_TOKEN_FIXED = "a1" * 32
HS_TOKEN_FIXED = "b2" * 32


class InitScriptTest(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="botville-init-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "scripts").mkdir()
        (self.tmp / "compose" / "synapse").mkdir(parents=True)
        (self.tmp / "compose" / "appservice").mkdir(parents=True)
        shutil.copy(INIT, self.tmp / "scripts" / "init.sh")
        # init.sh calls the renderer for generated/push-denylist.env (A1.2).
        shutil.copy(RENDERER, self.tmp / "scripts" / "render_push_denylist.py")
        (self.tmp / "compose" / ".env.example").write_text(ENV_EXAMPLE, encoding="utf-8")
        (self.tmp / "compose" / "synapse" / "homeserver.yaml.tmpl").write_text(
            TEMPLATE, encoding="utf-8"
        )
        (self.tmp / "compose" / "appservice" / "botville-gateway.yaml.tmpl").write_text(
            APPSERVICE_TEMPLATE, encoding="utf-8"
        )
        # init.sh also renders the sygnal templates (A1.2); placeholder-free
        # one-liners keep these tests focused on the .env logic. The sygnal
        # rendering itself has its own tests in test_push_sygnal.py.
        sygnal_dir = self.tmp / "compose" / "sygnal"
        sygnal_dir.mkdir()
        (sygnal_dir / "sygnal.yaml.tmpl").write_text(
            "sygnal:\n  logging: {}\napps:\n",
            encoding="utf-8",
        )
        (sygnal_dir / "mock-app.yaml.part").write_text(
            '  "org.botville.mock":\n    type: botville_mock_pushkin.MockPushkin\n',
            encoding="utf-8",
        )
        (sygnal_dir / "apns-app.yaml.part").write_text(
            '  "${APNS_APP_ID}":\n    type: apns\n', encoding="utf-8"
        )
        (self.tmp / "compose" / "synapse" / "ip-range-whitelist.part").write_text(
            'ip_range_whitelist:\n  - "${BOTVILLE_SYGNAL_IP}/32"\n', encoding="utf-8"
        )
        self.env_file = self.tmp / "compose" / ".env"
        self.generated = self.tmp / "compose" / "generated" / "synapse" / "homeserver.yaml"
        self.appservice = (
            self.tmp / "compose" / "generated" / "appservice" / "botville-gateway.yaml"
        )

    def run_init(self, appservice_url=None):
        env = dict(os.environ)
        env.pop("BOTVILLE_APPSERVICE_URL", None)
        if appservice_url is not None:
            env["BOTVILLE_APPSERVICE_URL"] = appservice_url
        return subprocess.run(
            ["bash", str(self.tmp / "scripts" / "init.sh")],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )

    def env_value(self, key):
        values = [
            line.split("=", 1)[1]
            for line in self.env_file.read_text(encoding="utf-8").splitlines()
            if line.startswith(key + "=")
        ]
        return values[-1] if values else None

    MODEL_BLOCK = (
        "GATEWAY_CONSUMER_ENABLED=false\n"
        "GATEWAY_GROUP_MAX_RESPONDERS=3\n"
        "GATEWAY_MODEL_BASE_URL=\n"
        "GATEWAY_MODEL_NAME=\n"
        "GATEWAY_MODEL_API_KEY=\n"
        "GATEWAY_MODEL_TIMEOUT_SECONDS=30\n"
        "GATEWAY_PERSONAS_FILE=\n"
        "GATEWAY_MEMORY_DB_PATH=\n"
        "GATEWAY_MEMORY_RETENTION_DAYS=90\n"
        "GATEWAY_MEMORY_PURGE_INTERVAL_SECONDS=86400\n"
        "GATEWAY_MEMORY_SUMMARY_MODEL=\n"
        "GATEWAY_EMBEDDING_BASE_URL=\n"
        "GATEWAY_EMBEDDING_MODEL=\n"
        "GATEWAY_EMBEDDING_API_KEY=\n"
        "GATEWAY_EMBEDDING_DIM=0\n"
        "GATEWAY_MEMORY_VECTOR_BACKEND=python\n"
        "GATEWAY_MEMORY_OWNER_CAN_READ_DM=false\n"
        "GATEWAY_MOOD_ENABLED=false\n"
        "GATEWAY_MOOD_HALF_LIFE_MINUTES=240\n"
        "GATEWAY_PROACTIVE_ENABLED=false\n"
        "GATEWAY_PROACTIVE_RECENT_CONTACTS_ENABLED=false\n"
        "GATEWAY_PROACTIVE_TICK_SECONDS=300\n"
        "GATEWAY_PROACTIVE_MIN_IDLE_MINUTES=180\n"
        "GATEWAY_PROACTIVE_MIN_INTERVAL_MINUTES=240\n"
        "GATEWAY_PROACTIVE_PER_TARGET_PER_DAY=1\n"
    )

    def test_model_env_is_passed_through_never_generated_or_printed(self):
        """B line PR2 vars: init.sh appends them with their (empty/default)
        example values, never generates the API key, keeps a deployer value
        verbatim and never prints it."""
        (self.tmp / "compose" / ".env.example").write_text(
            ENV_EXAMPLE + self.MODEL_BLOCK, encoding="utf-8"
        )
        proc = self.run_init()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.env_value("GATEWAY_MODEL_API_KEY"), "")
        self.assertEqual(self.env_value("GATEWAY_MODEL_BASE_URL"), "")
        self.assertEqual(self.env_value("GATEWAY_MODEL_TIMEOUT_SECONDS"), "30")
        self.assertEqual(self.env_value("GATEWAY_CONSUMER_ENABLED"), "false")
        self.assertEqual(self.env_value("GATEWAY_PERSONAS_FILE"), "")
        self.assertEqual(self.env_value("GATEWAY_MEMORY_DB_PATH"), "")
        self.assertEqual(self.env_value("GATEWAY_MEMORY_RETENTION_DAYS"), "90")
        self.assertEqual(self.env_value("GATEWAY_MEMORY_PURGE_INTERVAL_SECONDS"), "86400")
        for key in ("GATEWAY_MEMORY_SUMMARY_MODEL", "GATEWAY_EMBEDDING_BASE_URL",
                    "GATEWAY_EMBEDDING_MODEL", "GATEWAY_EMBEDDING_API_KEY"):
            self.assertEqual(self.env_value(key), "", key)
        self.assertEqual(self.env_value("GATEWAY_EMBEDDING_DIM"), "0")
        self.assertEqual(self.env_value("GATEWAY_MEMORY_VECTOR_BACKEND"), "python")
        self.assertEqual(self.env_value("GATEWAY_MEMORY_OWNER_CAN_READ_DM"), "false")
        for key, value in (("GATEWAY_MOOD_ENABLED", "false"),
                           ("GATEWAY_MOOD_HALF_LIFE_MINUTES", "240"),
                           ("GATEWAY_PROACTIVE_ENABLED", "false"),
                           ("GATEWAY_PROACTIVE_RECENT_CONTACTS_ENABLED", "false"),
                           ("GATEWAY_PROACTIVE_TICK_SECONDS", "300"),
                           ("GATEWAY_PROACTIVE_MIN_IDLE_MINUTES", "180"),
                           ("GATEWAY_PROACTIVE_MIN_INTERVAL_MINUTES", "240"),
                           ("GATEWAY_PROACTIVE_PER_TARGET_PER_DAY", "1"),
                           ("GATEWAY_GROUP_MAX_RESPONDERS", "3")):
            self.assertEqual(self.env_value(key), value, key)

        sentinel = "sentinel-model-key-must-not-print-0123456789"
        emb_sentinel = "sentinel-embedding-key-must-not-print-0123456789"
        content = self.env_file.read_text(encoding="utf-8")
        content = content.replace("GATEWAY_MODEL_API_KEY=\n",
                                  "GATEWAY_MODEL_API_KEY={}\n".format(sentinel))
        content = content.replace("GATEWAY_EMBEDDING_API_KEY=\n",
                                  "GATEWAY_EMBEDDING_API_KEY={}\n".format(emb_sentinel))
        self.env_file.write_text(content, encoding="utf-8")
        proc = self.run_init()
        self.assertEqual(proc.returncode, 0, "init.sh failed")
        self.assertNotIn(sentinel, proc.stdout + proc.stderr)
        self.assertNotIn(emb_sentinel, proc.stdout + proc.stderr)
        self.assertEqual(self.env_value("GATEWAY_MODEL_API_KEY"), sentinel)
        self.assertEqual(self.env_value("GATEWAY_EMBEDDING_API_KEY"), emb_sentinel)

        # An existing .env without the block gets the keys appended (names
        # only in the message) and the key stays empty.
        self.env_file.write_text(
            "\n".join(l for l in self.env_file.read_text(encoding="utf-8").splitlines()
                      if not l.startswith("GATEWAY_")) + "\n",
            encoding="utf-8",
        )
        proc = self.run_init()
        self.assertEqual(proc.returncode, 0, "init.sh failed")
        self.assertIn("GATEWAY_MODEL_API_KEY", proc.stdout)
        self.assertEqual(self.env_value("GATEWAY_MODEL_API_KEY"), "")
        self.assertIn("GATEWAY_EMBEDDING_API_KEY", proc.stdout)
        self.assertEqual(self.env_value("GATEWAY_EMBEDDING_API_KEY"), "")

    def test_generated_dir_is_private(self):
        proc = self.run_init()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        mode = os.stat(self.tmp / "compose" / "generated").st_mode & 0o777
        self.assertEqual(mode, 0o700)
        # Re-tightened on every run (a hand-made 0755 dir is fixed too).
        os.chmod(self.tmp / "compose" / "generated", 0o755)
        self.assertEqual(self.run_init().returncode, 0)
        self.assertEqual(os.stat(self.tmp / "compose" / "generated").st_mode & 0o777, 0o700)

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
        # Appservice tokens: 64 hex, and as_token != hs_token (PR #99 review).
        self.assertRegex(self.env_value("APPSERVICE_AS_TOKEN"), HEX64)
        self.assertRegex(self.env_value("APPSERVICE_HS_TOKEN"), HEX64)
        self.assertNotEqual(
            self.env_value("APPSERVICE_AS_TOKEN"), self.env_value("APPSERVICE_HS_TOKEN")
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

    def test_renders_appservice_registration(self):
        # A1.3: the appservice registration is rendered from .env, carries the
        # configured (valid 64-hex) tokens, is 0600, and regex-escapes the
        # server name.
        self.env_file.write_text(
            "BOTVILLE_HOMESERVER=synapse\n"
            "BOTVILLE_SERVER_NAME=old.example\n"
            "BOTVILLE_HTTP_PORT=8008\n"
            "APPSERVICE_AS_TOKEN=" + AS_TOKEN_FIXED + "\n"
            "APPSERVICE_HS_TOKEN=" + HS_TOKEN_FIXED + "\n",
            encoding="utf-8",
        )
        proc = self.run_init()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        rendered = self.appservice.read_text(encoding="utf-8")
        self.assertIn('as_token: "{}"'.format(AS_TOKEN_FIXED), rendered)
        self.assertIn('hs_token: "{}"'.format(HS_TOKEN_FIXED), rendered)
        # Dots in the server name are escaped so the anchored regex cannot
        # match lookalike domains.
        self.assertIn(r"regex: '^@ai_.*:old\.example$'", rendered)
        # #107: the exclusive alias namespace renders the same way.
        self.assertIn(r"regex: '^#botville_conv_.*:old\.example$'", rendered)
        self.assertNotIn("${", rendered)
        # as_token = full control over the @ai_* namespace -> 0600 like .env.
        self.assertEqual(stat.S_IMODE(self.appservice.stat().st_mode), 0o600)

    # ------------------ registration url switch (A2.2 + #192) ---------------
    def _url_line(self):
        return [
            l for l in self.appservice.read_text(encoding="utf-8").splitlines()
            if l.startswith("url:")
        ]

    def _marker(self):
        return self.appservice.parent / ".url-changed"

    def test_registration_url_defaults_to_null(self):
        proc = self.run_init()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self._url_line(), ["url: null"])
        self.assertFalse(self._marker().exists())

    def test_registration_url_gateway_and_back_marks_change(self):
        proc = self.run_init("http://gateway:8080")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self._url_line(), ["url: http://gateway:8080"])
        self.assertTrue(self._marker().exists())
        self.assertIn("restart the homeserver", proc.stdout)
        self._marker().unlink()
        # Unset (make smoke / e2e also run init): the rendered url is kept.
        proc = self.run_init()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self._url_line(), ["url: http://gateway:8080"])
        self.assertFalse(self._marker().exists())
        # Same explicit value: no change, no marker.
        proc = self.run_init("http://gateway:8080")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(self._marker().exists())
        # Back to null (make up without GATEWAY=1, or continuwuity).
        proc = self.run_init("null")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self._url_line(), ["url: null"])
        self.assertTrue(self._marker().exists())

    def test_registration_url_rejects_anything_else(self):
        for bad in ("", "http://gateway:8080/_botville/gateway", "http://127.0.0.1:8080",
                    "https://gateway:8080", "NULL"):
            with self.subTest(value=bad):
                proc = self.run_init(bad)
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertIn("BOTVILLE_APPSERVICE_URL must be exactly", proc.stderr)

    def test_generated_tokens_satisfy_gateway_rules(self):
        # #192: the gateway refuses GATEWAY_HS_TOKEN == GATEWAY_AS_TOKEN, or
        # a value that is short (<32), a placeholder or has whitespace.
        # init.sh's fresh values (injected as those two variables) must pass.
        proc = self.run_init()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        as_token = self.env_value("APPSERVICE_AS_TOKEN")
        hs_token = self.env_value("APPSERVICE_HS_TOKEN")
        for value in (as_token, hs_token):
            self.assertRegex(value, "^" + HEX64 + "$")
            self.assertGreaterEqual(len(value), 32)
        self.assertNotEqual(as_token, hs_token)
        # Neither value may leak to stdout/stderr.
        self.assertNotIn(as_token, proc.stdout + proc.stderr)
        self.assertNotIn(hs_token, proc.stdout + proc.stderr)

    # ------------------ appservice token gate (PR #99 review, blocker 2) ----
    def _write_env_replacing(self, replacements):
        content = ENV_EXAMPLE
        for old, new in replacements.items():
            content = content.replace(old, new)
        self.env_file.write_text(content, encoding="utf-8")

    def test_appservice_token_empty_is_regenerated(self):
        # Empty token: regenerated randomly (with a notice), never rendered empty.
        self._write_env_replacing({"APPSERVICE_AS_TOKEN=<change-me>": "APPSERVICE_AS_TOKEN="})
        proc = self.run_init()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(self.env_value("APPSERVICE_AS_TOKEN"), HEX64)
        self.assertIn("APPSERVICE_AS_TOKEN", proc.stderr)
        self.assertIn("generated", proc.stderr)
        # The rendered registration carries the regenerated value.
        rendered = self.appservice.read_text(encoding="utf-8")
        self.assertIn('as_token: "{}"'.format(self.env_value("APPSERVICE_AS_TOKEN")), rendered)

    def test_appservice_token_placeholder_is_regenerated(self):
        self._write_env_replacing(
            {"APPSERVICE_AS_TOKEN=<change-me>": "APPSERVICE_AS_TOKEN=<change-me>",
             "APPSERVICE_HS_TOKEN=<change-me>": "APPSERVICE_HS_TOKEN=<change-me>"}
        )
        proc = self.run_init()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(self.env_value("APPSERVICE_AS_TOKEN"), HEX64)
        self.assertRegex(self.env_value("APPSERVICE_HS_TOKEN"), HEX64)
        self.assertNotIn("<change-me>", self.appservice.read_text(encoding="utf-8"))

    def test_appservice_token_invalid_aborts_without_rendering(self):
        # A hand-filled but invalid value is an error: never overwritten, never
        # rendered (an empty/short as_token would let Synapse accept
        # unauthenticated @ai_* registrations).
        self._write_env_replacing({"APPSERVICE_AS_TOKEN=<change-me>": "APPSERVICE_AS_TOKEN=short"})
        self.env_file.chmod(0o600)
        proc = self.run_init()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("APPSERVICE_AS_TOKEN", proc.stderr)
        self.assertIn("refusing to render", proc.stderr)
        # The hand-filled value is untouched and nothing was rendered.
        self.assertEqual(self.env_value("APPSERVICE_AS_TOKEN"), "short")
        self.assertFalse(self.appservice.exists())

    def test_appservice_tokens_must_differ(self):
        same = "c3" * 32
        self._write_env_replacing(
            {
                "APPSERVICE_AS_TOKEN=<change-me>": "APPSERVICE_AS_TOKEN=" + same,
                "APPSERVICE_HS_TOKEN=<change-me>": "APPSERVICE_HS_TOKEN=" + same,
            }
        )
        proc = self.run_init()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("must be different", proc.stderr)
        self.assertFalse(self.appservice.exists())

    def test_env_file_chmod_600_even_when_unchanged(self):
        # Unconditional chmod: a hand-created 0644 .env with nothing to fix up
        # still ends up 0600.
        valid = []
        for line in ENV_EXAMPLE.splitlines():
            key, sep, value = line.partition("=")
            if sep and value == "<change-me>":
                if key == "APPSERVICE_AS_TOKEN":
                    value = AS_TOKEN_FIXED
                elif key == "APPSERVICE_HS_TOKEN":
                    value = HS_TOKEN_FIXED
                else:
                    value = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
            valid.append(key + sep + value if sep else line)
        self.env_file.write_text("\n".join(valid) + "\n", encoding="utf-8")
        self.env_file.chmod(0o644)
        proc = self.run_init()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("keeping current values", proc.stdout)
        self.assertEqual(stat.S_IMODE(self.env_file.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
