#!/usr/bin/env python3
"""Hermetic tests for the A1.2 push gateway pieces: the mock pushkin and
init.sh's sygnal rendering.

No Docker and no sygnal install required: botville_mock_pushkin.py is loaded
from server/compose/sygnal/ with a minimal `sygnal.notifications` stub (the
real package is used instead when it happens to be importable, e.g. inside
the sygnal image), and init.sh runs against sandboxed copies of the real
.env.example/sygnal templates. Standard library only.

Run:  python3 -m unittest discover -s server/tests -p "test_*.py" -v
"""

import asyncio
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1]
INIT = SERVER_DIR / "scripts" / "init.sh"
RENDERER = SERVER_DIR / "scripts" / "render_push_denylist.py"
COMPOSE_DIR = SERVER_DIR / "compose"
MOCK_PUSHKIN = COMPOSE_DIR / "sygnal" / "botville_mock_pushkin.py"


def _install_sygnal_stub_if_needed():
    """Ensure `sygnal.notifications.Pushkin` is importable.

    Returns the base class used (real when sygnal is installed, stub here
    otherwise) — the stub mirrors the 0.17.0 interface the pushkin relies on:
    Pushkin(name, sygnal, config) storing .cfg.
    """
    try:
        from sygnal.notifications import Pushkin

        return Pushkin
    except ImportError:
        pass

    stub_pushkin = type(
        "Pushkin",
        (object,),
        {
            "__init__": lambda self, name, sygnal, config: setattr(
                self, "__dict__", {"name": name, "sygnal": sygnal, "cfg": config}
            )
        },
    )
    sygnal_pkg = types.ModuleType("sygnal")
    notifications = types.ModuleType("sygnal.notifications")
    notifications.Pushkin = stub_pushkin
    sygnal_pkg.notifications = notifications
    sys.modules.setdefault("sygnal", sygnal_pkg)
    sys.modules.setdefault("sygnal.notifications", notifications)
    return stub_pushkin


def load_mock_pushkin_module():
    """Import botville_mock_pushkin.py by path with the (stubbed) sygnal."""
    _install_sygnal_stub_if_needed()
    spec = importlib.util.spec_from_file_location("botville_mock_pushkin", MOCK_PUSHKIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_notification(**overrides):
    """A stand-in for sygnal.notifications.Notification (attributes only)."""
    fields = {
        "event_id": "$push-e2e-test",
        "room_id": "!room:localhost",
        "type": "m.room.message",
        "prio": "high",
    }
    fields.update(overrides)
    return types.SimpleNamespace(**fields)


def make_device(**overrides):
    """A stand-in for sygnal.notifications.Device (attributes only)."""
    fields = {"app_id": "org.botville.mock", "pushkey": "mock-deadbeef", "data": {}}
    fields.update(overrides)
    return types.SimpleNamespace(**fields)


class MockPushkinTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = _install_sygnal_stub_if_needed()
        cls.module = load_mock_pushkin_module()

    def make_pushkin(self, tmp, config=None):
        path = str(Path(tmp) / "mock-pushes.jsonl")
        cfg = {"pushes_path": path}
        if config is not None:
            cfg.update(config)
        return self.module.MockPushkin("org.botville.mock", None, cfg), path

    def test_subclasses_pushkin(self):
        self.assertTrue(issubclass(self.module.MockPushkin, self.base))

    def test_dispatch_writes_one_jsonl_line_and_rejects_nothing(self):
        tmp = tempfile.mkdtemp(prefix="botville-mockpush-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        pushkin, path = self.make_pushkin(tmp)
        before = int(time.time() * 1000)
        record_notification = make_notification()

        result = asyncio.run(
            pushkin.dispatch_notification(
                record_notification, make_device(), types.SimpleNamespace()
            )
        )

        self.assertEqual(result, [])  # the mock never rejects a pushkey
        with open(path, encoding="utf-8") as f:
            lines = [line for line in f.read().splitlines() if line]
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["event_id"], "$push-e2e-test")
        self.assertEqual(record["room_id"], "!room:localhost")
        self.assertEqual(record["pushkey"], "mock-deadbeef")
        self.assertEqual(record["app_id"], "org.botville.mock")
        self.assertEqual(record["type"], "m.room.message")
        self.assertGreaterEqual(record["received_at_ms"], before)

    def test_full_content_notification_records_content_key_names(self):
        # A full-text (non event_id_only) pusher must be observable: the
        # record lists the content KEY NAMES (never values) so push_e2e.py
        # can assert has_content == [] on the event_id_only path.
        tmp = tempfile.mkdtemp(prefix="botville-mockpush-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        pushkin, path = self.make_pushkin(tmp)
        notification = make_notification(content={"body": "secret text", "msgtype": "m.text"})
        asyncio.run(
            pushkin.dispatch_notification(notification, make_device(), types.SimpleNamespace())
        )
        with open(path, encoding="utf-8") as f:
            record = json.loads(f.read().splitlines()[0])
        self.assertEqual(record["has_content"], ["body", "msgtype"])
        self.assertNotIn("secret text", json.dumps(record))  # values never land

    def test_second_dispatch_appends_not_truncates(self):
        tmp = tempfile.mkdtemp(prefix="botville-mockpush-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        pushkin, path = self.make_pushkin(tmp)
        context = types.SimpleNamespace()
        for event_id in ("$one", "$two"):
            asyncio.run(
                pushkin.dispatch_notification(
                    make_notification(event_id=event_id), make_device(), context
                )
            )
        with open(path, encoding="utf-8") as f:
            events = [json.loads(line)["event_id"] for line in f if line.strip()]
        self.assertEqual(events, ["$one", "$two"])

    def test_event_id_only_notification_renders_nulls(self):
        # With format=event_id_only the homeserver omits most fields; sygnal
        # leaves them None, and the record must still be written.
        tmp = tempfile.mkdtemp(prefix="botville-mockpush-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        pushkin, path = self.make_pushkin(tmp)
        sparse = make_notification(
            event_id=None, type=None, prio=None, room_id="!room:localhost"
        )
        result = asyncio.run(
            pushkin.dispatch_notification(sparse, make_device(), types.SimpleNamespace())
        )
        self.assertEqual(result, [])
        with open(path, encoding="utf-8") as f:
            record = json.loads(f.read().splitlines()[0])
        self.assertIsNone(record["event_id"])
        self.assertIsNone(record["type"])
        self.assertEqual(record["room_id"], "!room:localhost")
        self.assertEqual(record["has_content"], [])  # event_id_only: no content keys

    def test_default_pushes_path_constant(self):
        # The compose stack relies on the /data default documented in
        # docs/deploy/push-sygnal.md; sygnal.yaml.tmpl sets it explicitly, so
        # a change here must be synced with both.
        self.assertEqual(self.module.DEFAULT_PUSHES_PATH, "/data/mock-pushes.jsonl")


# --- init.sh: sygnal rendering + conditional APNs app ----------------------

SYGNAL_ENV = """\
# fixture .env for the sygnal rendering tests (all values are fakes)
BOTVILLE_HOMESERVER=synapse
BOTVILLE_SERVER_NAME=localhost
BOTVILLE_HTTP_PORT=8008
BOTVILLE_REGISTRATION_TOKEN=fixture-token
BOTVILLE_PUSH_PORT=8019
POSTGRES_USER=botville
POSTGRES_PASSWORD=fixture
POSTGRES_DB=synapse
SYNAPSE_MACAROON_SECRET=fixture
SYNAPSE_FORM_SECRET=fixture
SYNAPSE_REGISTRATION_SHARED_SECRET=fixture
APPSERVICE_AS_TOKEN=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
APPSERVICE_HS_TOKEN=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
BOTVILLE_DOCKER_SUBNET=172.31.250.0/24
BOTVILLE_DOCKER_IP_RANGE=172.31.250.64/26
BOTVILLE_SYGNAL_IP=
BOTVILLE_PUSH_MOCK=true
"""

APNS_DISABLED_ENV = SYGNAL_ENV + "BOTVILLE_APNS_ENABLED=false\n"

APNS_PLACEHOLDER_ENV = SYGNAL_ENV + (
    "BOTVILLE_APNS_ENABLED=true\n"
    "BOTVILLE_APNS_BUNDLE_ID=<bundle-id>\n"
    "APNS_APP_ID=<bundle-id>.apns\n"
    "APNS_TOPIC=<bundle-id>\n"
    "APNS_KEY_ID=<key-id>\n"
    "APNS_TEAM_ID=<team-id>\n"
    "APNS_PLATFORM=sandbox\n"
)

# Real bundle id with APNS_TOPIC/APNS_APP_ID left as placeholders: init.sh
# must derive topic = bundle id and app id = "<bundle id>.apns" (issue #4
# single-app convention) and write them back to .env.
APNS_REAL_ENV = SYGNAL_ENV + (
    "BOTVILLE_APNS_ENABLED=true\n"
    "BOTVILLE_APNS_BUNDLE_ID=org.botville.app.ios\n"
    "APNS_APP_ID=<bundle-id>.apns\n"
    "APNS_TOPIC=<bundle-id>\n"
    "APNS_KEY_ID=AB12CD34EF\n"
    "APNS_TEAM_ID=WXYZ1234AB\n"
    "APNS_PLATFORM=sandbox\n"
)

# Same but with a capitalized "True": env_flag must accept it (an earlier
# strict == "true" comparison silently skipped it with no WARNING).
APNS_REAL_CAPITAL_ENV = APNS_REAL_ENV.replace("BOTVILLE_APNS_ENABLED=true", "BOTVILLE_APNS_ENABLED=True")

SYNAPSE_TEMPLATE = "server_name: ${BOTVILLE_SERVER_NAME}\nmacaroon: ${SYNAPSE_MACAROON_SECRET}\n"


class SygnalInitScriptTest(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="botville-sygnal-init-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "scripts").mkdir()
        (self.tmp / "compose" / "synapse").mkdir(parents=True)
        shutil.copy(INIT, self.tmp / "scripts" / "init.sh")
        # init.sh calls the renderer for generated/push-denylist.env.
        shutil.copy(RENDERER, self.tmp / "scripts" / "render_push_denylist.py")
        # The real templates: these tests double as a guard that the shipped
        # sygnal.yaml.tmpl / apns-app.yaml.part keep rendering cleanly.
        (self.tmp / "compose" / "sygnal").mkdir(parents=True)
        shutil.copytree(COMPOSE_DIR / "sygnal", self.tmp / "compose" / "sygnal", dirs_exist_ok=True)
        (self.tmp / "compose" / "synapse" / "homeserver.yaml.tmpl").write_text(
            SYNAPSE_TEMPLATE, encoding="utf-8"
        )

        # A1.3 appservice registration (init.sh always renders it).
        appservice_dir = self.tmp / "compose" / "appservice"
        appservice_dir.mkdir(parents=True)
        (appservice_dir / "botville-gateway.yaml.tmpl").write_text(
            "id: botville-gateway\n"
            'as_token: "${APPSERVICE_AS_TOKEN}"\n'
            'hs_token: "${APPSERVICE_HS_TOKEN}"\n'
            "namespaces:\n"
            "  users:\n"
            "    - exclusive: true\n"
            "      regex: '^@ai_.*:${BOTVILLE_SERVER_NAME_REGEX}$'\n"
            "  aliases:\n"
            "    - exclusive: true\n"
            "      regex: '^#botville_conv_.*:${BOTVILLE_SERVER_NAME_REGEX}$'\n",
            encoding="utf-8",
        )

        (self.tmp / "compose" / "synapse" / "ip-range-whitelist.part").write_text(
            'ip_range_whitelist:\n  - "${BOTVILLE_SYGNAL_IP}/32"\n', encoding="utf-8"
        )
        self.env_file = self.tmp / "compose" / ".env"
        self.generated = self.tmp / "compose" / "generated" / "sygnal" / "sygnal.yaml"

    def run_init(self):
        return subprocess.run(
            ["bash", str(self.tmp / "scripts" / "init.sh")],
            capture_output=True,
            text=True,
            timeout=60,
        )

    def write_env(self, env_text):
        """Write .env and a matching .env.example: the existing-.env path in
        init.sh reads .env.example (missing-key merge, image-override
        warnings), so it must exist and list the same keys to keep these
        tests focused on the sygnal rendering."""
        self.env_file.write_text(env_text, encoding="utf-8")
        (self.tmp / "compose" / ".env.example").write_text(env_text, encoding="utf-8")

    def sygnal_yaml(self):
        return self.generated.read_text(encoding="utf-8")

    def place_apns_key(self, mode=0o600, content="not-a-real-key-fixture\n"):
        """Fake APNs key file (never real key material) with a given mode.

        init.sh validates existence/regular-file/mode/size via stat only and
        never reads the contents, so a dummy string is all that is needed."""
        secrets_dir = self.tmp / "compose" / "secrets"
        secrets_dir.mkdir(parents=True, exist_ok=True)
        key = secrets_dir / "apns.p8"
        key.write_text(content, encoding="utf-8")
        os.chmod(key, mode)
        return key

    def apns_key_path(self):
        return self.tmp / "compose" / "secrets" / "apns.p8"

    def test_mock_only_by_default_and_data_dir_mode(self):
        self.write_env(APNS_DISABLED_ENV)
        proc = self.run_init()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        yaml_text = self.sygnal_yaml()
        self.assertIn('"org.botville.mock":', yaml_text)
        self.assertIn("botville_mock_pushkin.MockPushkin", yaml_text)
        self.assertNotIn("type: apns", yaml_text)
        self.assertNotIn("${", yaml_text)
        # The scratch dir bind-mounted at /data: 755 — the sygnal image runs
        # as root (verified against the pinned v0.17.0), so world-writable
        # would only widen the blast radius.
        data_dir = self.tmp / "compose" / "generated" / "sygnal-data"
        self.assertTrue(data_dir.is_dir())
        self.assertEqual(stat.S_IMODE(data_dir.stat().st_mode), 0o755)
        # init.sh also creates the secrets dir for the future apns.p8.
        secrets_dir = self.tmp / "compose" / "secrets"
        self.assertTrue(secrets_dir.is_dir())
        self.assertEqual(stat.S_IMODE(secrets_dir.stat().st_mode), 0o700)
        # With APNs disabled no key file is required — but compose mounts the
        # key as a single file (create_host_path: false), so init.sh keeps an
        # EMPTY 0600 placeholder that is never treated as a real key.
        placeholder = secrets_dir / "apns.p8"
        self.assertTrue(placeholder.is_file())
        self.assertEqual(stat.S_IMODE(placeholder.stat().st_mode), 0o600)
        self.assertEqual(placeholder.stat().st_size, 0)

    def test_push_mock_false_skips_mock_app_and_warns_no_apps(self):
        self.write_env(APNS_DISABLED_ENV + "BOTVILLE_PUSH_MOCK=false\n")
        proc = self.run_init()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn('"org.botville.mock":', self.sygnal_yaml())
        self.assertIn("no apps at all", proc.stderr)

    def test_apns_enabled_with_placeholder_values_is_skipped_with_warning(self):
        self.write_env(APNS_PLACEHOLDER_ENV)
        proc = self.run_init()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        yaml_text = self.sygnal_yaml()
        self.assertIn('"org.botville.mock":', yaml_text)
        self.assertNotIn("type: apns", yaml_text)
        # Names the missing pieces so the operator knows what to fill in.
        # APNS_TOPIC/APNS_APP_ID are NOT listed: they are derived from the
        # bundle id once that is real (issue #4).
        warnings = proc.stderr
        for key in ("BOTVILLE_APNS_BUNDLE_ID", "APNS_KEY_ID", "APNS_TEAM_ID", "secrets/apns.p8"):
            self.assertIn(key, warnings)
        self.assertIn("NOT rendered", warnings)

    def test_apns_rendered_when_enabled_and_key_present(self):
        self.write_env(APNS_REAL_ENV)
        self.place_apns_key()
        proc = self.run_init()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        yaml_text = self.sygnal_yaml()
        self.assertIn('"org.botville.mock":', yaml_text)  # mock stays available
        # app id derived as "<bundle id>.apns", topic as the bundle id.
        self.assertIn('"org.botville.app.ios.apns":', yaml_text)
        self.assertIn("type: apns", yaml_text)
        self.assertIn("keyfile: /secrets/apns.p8", yaml_text)
        self.assertIn('key_id: "AB12CD34EF"', yaml_text)
        self.assertIn('team_id: "WXYZ1234AB"', yaml_text)
        self.assertIn('topic: "org.botville.app.ios"', yaml_text)
        self.assertIn('platform: "sandbox"', yaml_text)
        self.assertNotIn("${", yaml_text)
        self.assertNotIn("<", yaml_text)  # no placeholder ever gets rendered
        # The success log names no APNs values either (issue #4: no value
        # echo — not even the app id).
        self.assertIn("rendered APNs app into", proc.stdout)
        self.assertNotIn("rendered APNs app (", proc.stdout)
        # Derived values are written back to .env (and survive re-runs).
        env_text = self.env_file.read_text(encoding="utf-8")
        self.assertIn("APNS_TOPIC=org.botville.app.ios\n", env_text)
        self.assertIn("APNS_APP_ID=org.botville.app.ios.apns\n", env_text)
        self.assertIn("derived from BOTVILLE_APNS_BUNDLE_ID", proc.stderr)
        self.assertIn("derived as BOTVILLE_APNS_BUNDLE_ID", proc.stderr)

    def test_apns_explicit_topic_and_app_id_accepted(self):
        # Explicit values are fine as long as topic is reverse-domain and
        # app id follows the "<bundle id>.apns" rule.
        env = APNS_REAL_ENV.replace(
            "APNS_APP_ID=<bundle-id>.apns", "APNS_APP_ID=org.botville.app.ios.apns"
        ).replace("APNS_TOPIC=<bundle-id>", "APNS_TOPIC=org.botville.app.ios")
        self.write_env(env)
        self.place_apns_key()
        proc = self.run_init()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        yaml_text = self.sygnal_yaml()
        self.assertIn('"org.botville.app.ios.apns":', yaml_text)
        self.assertIn('topic: "org.botville.app.ios"', yaml_text)
        self.assertNotIn("derived", proc.stderr)

    def test_apns_key_mode_644_rejected(self):
        # A world/group-readable key is a hard failure (issue #4): exit 1,
        # never a WARNING-skip that would leave the APNs app unrendered for
        # reasons the operator cannot see in the warning path.
        self.write_env(APNS_REAL_ENV)
        self.place_apns_key(mode=0o644)
        proc = self.run_init()
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("600 or 400", proc.stderr)
        self.assertIn("apns.p8", proc.stderr)
        self.assertNotIn("type: apns", self.sygnal_yaml())

    def test_apns_key_mode_400_accepted(self):
        self.write_env(APNS_REAL_ENV)
        self.place_apns_key(mode=0o400)
        proc = self.run_init()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("type: apns", self.sygnal_yaml())

    def test_apns_key_is_directory_rejected(self):
        self.write_env(APNS_REAL_ENV)
        secrets_dir = self.tmp / "compose" / "secrets"
        secrets_dir.mkdir(parents=True, exist_ok=True)
        (secrets_dir / "apns.p8").mkdir()
        proc = self.run_init()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("not a regular file", proc.stderr)

    def test_apns_key_id_format_rejected_without_echo(self):
        # 10 lowercase chars: right length, wrong case — must hard-fail with
        # the field name and rule only, never the value. This holds even
        # though the key file is missing too: present-but-invalid beats the
        # missing WARNING path.
        env = APNS_REAL_ENV.replace("APNS_KEY_ID=AB12CD34EF", "APNS_KEY_ID=ab12cd34ef")
        self.write_env(env)
        proc = self.run_init()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("APNS_KEY_ID", proc.stderr)
        self.assertIn("10 uppercase alphanumeric", proc.stderr)
        self.assertNotIn("ab12cd34ef", proc.stdout + proc.stderr)

    def test_apns_team_id_format_rejected_without_echo(self):
        env = APNS_REAL_ENV.replace("APNS_TEAM_ID=WXYZ1234AB", "APNS_TEAM_ID=TEAM123!")
        self.write_env(env)
        proc = self.run_init()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("APNS_TEAM_ID", proc.stderr)
        self.assertIn("10 uppercase alphanumeric", proc.stderr)
        self.assertNotIn("TEAM123!", proc.stdout + proc.stderr)

    def test_apns_bundle_id_bad_format_rejected_without_echo(self):
        # No dot: not a reverse-domain id.
        env = APNS_REAL_ENV.replace(
            "BOTVILLE_APNS_BUNDLE_ID=org.botville.app.ios", "BOTVILLE_APNS_BUNDLE_ID=botville"
        )
        self.write_env(env)
        proc = self.run_init()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("BOTVILLE_APNS_BUNDLE_ID", proc.stderr)
        self.assertIn("reverse-domain", proc.stderr)
        self.assertNotIn("botville\n", proc.stderr)
        self.assertNotIn("BOTVILLE_APNS_BUNDLE_ID=botville", proc.stdout + proc.stderr)

    def test_apns_app_id_mismatch_rejected_without_echo(self):
        # The single-app convention (issue #4): app id must be
        # "<bundle id>.apns" exactly.
        env = APNS_REAL_ENV.replace(
            "APNS_APP_ID=<bundle-id>.apns", "APNS_APP_ID=org.other.app.apns"
        )
        self.write_env(env)
        self.place_apns_key()
        proc = self.run_init()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("APNS_APP_ID", proc.stderr)
        self.assertIn(".apns", proc.stderr)
        self.assertNotIn("org.other.app.apns", proc.stdout + proc.stderr)

    def test_apns_platform_invalid_rejected_without_echo(self):
        env = APNS_REAL_ENV.replace("APNS_PLATFORM=sandbox", "APNS_PLATFORM=prod")
        self.write_env(env)
        proc = self.run_init()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("APNS_PLATFORM", proc.stderr)
        self.assertIn("sandbox or production", proc.stderr)
        self.assertNotIn("APNS_PLATFORM=prod", proc.stdout + proc.stderr)

    def test_apns_topic_bad_format_rejected_without_echo(self):
        env = APNS_REAL_ENV.replace("APNS_TOPIC=<bundle-id>", "APNS_TOPIC=not a domain")
        self.write_env(env)
        self.place_apns_key()
        proc = self.run_init()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("APNS_TOPIC", proc.stderr)
        self.assertIn("reverse-domain", proc.stderr)
        self.assertNotIn("not a domain", proc.stdout + proc.stderr)

    def test_apns_enabled_capitalized_true_is_accepted(self):
        # true/1/yes in any case must enable; only genuinely unrecognized
        # values may warn.
        self.write_env(APNS_REAL_CAPITAL_ENV)
        self.place_apns_key()
        proc = self.run_init()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("type: apns", self.sygnal_yaml())
        self.assertNotIn("is not a boolean", proc.stderr)

    def test_apns_enabled_garbage_value_warns_and_stays_enabled(self):
        env = APNS_REAL_ENV.replace("BOTVILLE_APNS_ENABLED=true", "BOTVILLE_APNS_ENABLED=maybe")
        self.write_env(env)
        self.place_apns_key()
        proc = self.run_init()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("BOTVILLE_APNS_ENABLED=maybe is not a boolean", proc.stderr)
        self.assertIn("type: apns", self.sygnal_yaml())  # truthy reading

    def test_apns_skipped_when_keyfile_missing(self):
        # Missing key file stays the soft path: WARNING + skip, mock keeps
        # working, and init.sh leaves an empty placeholder file so the
        # single-file compose bind mount stays valid.
        self.write_env(APNS_REAL_ENV)
        proc = self.run_init()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("type: apns", self.sygnal_yaml())
        self.assertIn("secrets/apns.p8", proc.stderr)
        placeholder = self.apns_key_path()
        self.assertTrue(placeholder.is_file())
        self.assertEqual(placeholder.stat().st_size, 0)

    def test_re_run_does_not_duplicate_apns_app(self):
        # init.sh rewrites sygnal.yaml from the template on every run, so a
        # second run must not append the APNs app twice.
        self.write_env(APNS_REAL_ENV)
        self.place_apns_key()
        self.run_init()
        self.run_init()
        self.assertEqual(self.sygnal_yaml().count("type: apns"), 1)


if __name__ == "__main__":
    unittest.main()
