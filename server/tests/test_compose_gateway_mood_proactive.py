#!/usr/bin/env python3
"""Gateway mood (B line PR4b) / proactive speech (B line PR4c) pass-through.

The names and defaults must agree across the compose file, both deployment
.env.example files, gateway/.env.example, the CI allowlist, the docs and what
gateway/src config.py actually does (load_config is run in a subprocess with
a throwaway env; nothing secret is involved). Render checks need docker
compose and are skipped without it. Run with:
  python3 -m unittest discover -s server/tests -p "test_*.py" -v
"""

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "server" / "compose" / "docker-compose.yml"
GATEWAY = REPO_ROOT / "gateway"
CONFIG_PY = GATEWAY / "src" / "botville_gateway" / "config.py"
ENV_EXAMPLES = (
    REPO_ROOT / ".env.example",
    REPO_ROOT / "server" / "compose" / ".env.example",
    GATEWAY / ".env.example",
)
REQUIRED_ENV = "POSTGRES_PASSWORD=test-only\nBOTVILLE_REGISTRATION_TOKEN=test-only\n"

# name -> (compose/.env.example default, Config field, parsed default)
EXPECTED = {
    "GATEWAY_MOOD_ENABLED": ("false", "mood_enabled", False),
    "GATEWAY_MOOD_HALF_LIFE_MINUTES": ("240", "mood_half_life_minutes", 240),
    "GATEWAY_PROACTIVE_ENABLED": ("false", "proactive_enabled", False),
    "GATEWAY_PROACTIVE_RECENT_CONTACTS_ENABLED": (
        "false", "proactive_recent_contacts_enabled", False),
    "GATEWAY_PROACTIVE_TICK_SECONDS": ("300", "proactive_tick_seconds", 300),
    "GATEWAY_PROACTIVE_MIN_IDLE_MINUTES": ("180", "proactive_min_idle_minutes", 180),
    "GATEWAY_PROACTIVE_MIN_INTERVAL_MINUTES": (
        "240", "proactive_min_interval_minutes", 240),
    "GATEWAY_PROACTIVE_PER_TARGET_PER_DAY": ("1", "proactive_per_target_per_day", 1),
}
BOOLS = ("GATEWAY_MOOD_ENABLED", "GATEWAY_PROACTIVE_ENABLED",
         "GATEWAY_PROACTIVE_RECENT_CONTACTS_ENABLED")

# Runs gateway/src load_config on a JSON env from argv; prints the parsed
# fields, or the ConfigError message. Dev mode, no secrets.
PROBE = r"""
import json, sys
from botville_gateway.config import ConfigError, load_config
env = json.loads(sys.argv[1])
fields = json.loads(sys.argv[2])
try:
    cfg = load_config(env)
except ConfigError as exc:
    print(json.dumps({"error": str(exc)}))
else:
    print(json.dumps({f: getattr(cfg, f) for f in fields}))
"""


def env_example_values(path):
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values.setdefault(key, []).append(value)
    return values


def run_gateway_config(env):
    fields = [field for _, field, _ in EXPECTED.values()]
    proc = subprocess.run(
        [sys.executable, "-c", PROBE, json.dumps(env), json.dumps(fields)],
        cwd=str(GATEWAY), env={"PYTHONPATH": str(GATEWAY / "src"), "PATH": "/usr/bin:/bin"},
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise AssertionError("gateway config probe failed (exit {}): {}".format(
            proc.returncode, proc.stderr[-2000:]))
    return json.loads(proc.stdout.strip().splitlines()[-1])


class MoodProactiveEnvStaticTest(unittest.TestCase):
    def test_compose_passes_through_with_defaults(self):
        text = COMPOSE.read_text(encoding="utf-8")
        for key, (default, _, _) in EXPECTED.items():
            line = "      {0}: ${{{0}:-{1}}}\n".format(key, default)
            self.assertEqual(text.count(line), 1, key)
            # Nowhere else (e.g. a hard-coded value in another service).
            self.assertEqual(len(re.findall(r"(?m)^\s+{}:".format(key), text)), 1, key)

    def test_env_examples_register_the_same_defaults(self):
        for path in ENV_EXAMPLES:
            values = env_example_values(path)
            for key, (default, _, _) in EXPECTED.items():
                with self.subTest(path=str(path.relative_to(REPO_ROOT)), key=key):
                    self.assertEqual(values.get(key), [default])

    def test_names_are_read_by_gateway_src(self):
        src = CONFIG_PY.read_text(encoding="utf-8")
        for key, (_, field, _) in EXPECTED.items():
            self.assertIn('"{}"'.format(key), src, key + " is not read by config.py")
            self.assertRegex(src, r"(?m)^\s+{}\s*[:=]".format(field), field)
        for key in BOOLS:
            self.assertRegex(src, r'_env_bool\(\s*source,\s*"{}",\s*False\s*\)'.format(key), key)

    def test_gateway_src_defaults_match(self):
        """Run load_config with nothing set: the parsed defaults are the ones
        compose / .env.example ship."""
        got = run_gateway_config({"GATEWAY_DEV_MODE": "true"})
        for key, (default, field, parsed) in EXPECTED.items():
            self.assertEqual(got.get(field), parsed, key)
            self.assertEqual(str(parsed).lower(), default, key)
        # Explicitly setting the shipped defaults parses to the same thing.
        env = {"GATEWAY_DEV_MODE": "true"}
        env.update({k: v[0] for k, v in EXPECTED.items()
                    if k != "GATEWAY_PROACTIVE_ENABLED"})
        got = run_gateway_config(env)
        for key, (_, field, parsed) in EXPECTED.items():
            self.assertEqual(got.get(field), parsed, key)

    def test_gateway_src_rejects_invalid_values_without_echoing_them(self):
        bad = [(key, "maybe-7319") for key in BOOLS] + [
            ("GATEWAY_MOOD_HALF_LIFE_MINUTES", "0"),
            ("GATEWAY_MOOD_HALF_LIFE_MINUTES", "10081"),
            ("GATEWAY_MOOD_HALF_LIFE_MINUTES", "abc7319"),
            ("GATEWAY_PROACTIVE_TICK_SECONDS", "29"),
            ("GATEWAY_PROACTIVE_TICK_SECONDS", "86401"),
            ("GATEWAY_PROACTIVE_MIN_IDLE_MINUTES", "-1"),
            ("GATEWAY_PROACTIVE_MIN_IDLE_MINUTES", "43201"),
            ("GATEWAY_PROACTIVE_MIN_INTERVAL_MINUTES", "43201"),
            ("GATEWAY_PROACTIVE_MIN_INTERVAL_MINUTES", "x7319"),
            ("GATEWAY_PROACTIVE_PER_TARGET_PER_DAY", "25"),
            ("GATEWAY_PROACTIVE_PER_TARGET_PER_DAY", "-3"),
        ]
        for key, value in bad:
            with self.subTest(key=key, value=value):
                got = run_gateway_config({"GATEWAY_DEV_MODE": "true", key: value})
                self.assertIn("error", got)
                if "7319" in value:
                    self.assertNotIn(value, got["error"])
        # Range edges are accepted.
        edges = {"GATEWAY_MOOD_HALF_LIFE_MINUTES": "10080",
                 "GATEWAY_PROACTIVE_TICK_SECONDS": "30",
                 "GATEWAY_PROACTIVE_MIN_IDLE_MINUTES": "0",
                 "GATEWAY_PROACTIVE_MIN_INTERVAL_MINUTES": "43200",
                 "GATEWAY_PROACTIVE_PER_TARGET_PER_DAY": "0"}
        got = run_gateway_config(dict(edges, GATEWAY_DEV_MODE="true"))
        self.assertNotIn("error", got)

    def test_proactive_requires_a_persistent_hmac_secret(self):
        """Even in dev mode the gateway refuses GATEWAY_PROACTIVE_ENABLED=true
        without an explicit GATEWAY_HMAC_SECRET (documented prerequisite)."""
        got = run_gateway_config({"GATEWAY_DEV_MODE": "true",
                                  "GATEWAY_PROACTIVE_ENABLED": "true"})
        self.assertIn("error", got)
        self.assertIn("GATEWAY_HMAC_SECRET", got["error"])

    def test_init_sh_generates_none_of_them(self):
        init = (REPO_ROOT / "server" / "scripts" / "init.sh").read_text(encoding="utf-8")
        for key in EXPECTED:
            self.assertNotIn(key, init, key)
        self.assertNotIn("GATEWAY_MOOD_", init)
        self.assertNotIn("GATEWAY_PROACTIVE_", init)

    def test_ci_allowlist(self):
        ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        for key, (default, _, _) in EXPECTED.items():
            self.assertEqual(ci.count('"{}": "{}",'.format(key, default)), 1, key)

    def test_docs_cover_names_defaults_and_prerequisites(self):
        doc = (REPO_ROOT / "docs" / "deploy" / "appservice.md").read_text(encoding="utf-8")
        readme = (REPO_ROOT / "server" / "README.md").read_text(encoding="utf-8")
        for key, (default, _, _) in EXPECTED.items():
            self.assertIn("| `{0}` | `${{{0}:-{1}}}` | `{1}` |".format(key, default), doc, key)
            self.assertIn("`{}`".format(key), readme, key)
        self.assertIn("**开启 `GATEWAY_PROACTIVE_ENABLED` 的前提**", doc)
        self.assertIn("**先启用记忆库**：`GATEWAY_MEMORY_DB_PATH=", doc)
        self.assertIn("**持久的 `GATEWAY_HMAC_SECRET`**", doc)
        self.assertIn("开启前必须先启用记忆库（`GATEWAY_MEMORY_DB_PATH`），并有持久的 `GATEWAY_HMAC_SECRET`", doc)
        self.assertIn("前必须先启用记忆库（`GATEWAY_MEMORY_DB_PATH`），并且要有持久的\n`GATEWAY_HMAC_SECRET`", readme)
        for path in ENV_EXAMPLES[:2]:
            text = path.read_text(encoding="utf-8")
            self.assertIn("GATEWAY_MEMORY_DB_PATH", text.split("GATEWAY_PROACTIVE_ENABLED", 1)[1][:400])

    def test_no_hmac_secret_value_written_anywhere(self):
        """The HMAC secret is only ever a placeholder / empty in tracked text."""
        hexval = re.compile(r"HMAC_SECRET\s*[=:]\s*['\"]?[0-9a-fA-F]{16,}")
        paths = [REPO_ROOT / "docs" / "deploy" / "appservice.md",
                 REPO_ROOT / "docs" / "deploy" / "z423-synapse.md",
                 REPO_ROOT / "server" / "README.md", COMPOSE] + list(ENV_EXAMPLES[:2])
        for path in paths:
            self.assertIsNone(hexval.search(path.read_text(encoding="utf-8")), str(path))


def compose_available():
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "compose", "version"], stdin=subprocess.DEVNULL,
                          capture_output=True).returncode == 0


@unittest.skipUnless(compose_available(), "docker compose v2 not available")
class MoodProactiveEnvRenderTest(unittest.TestCase):
    def gateway_env(self, extra_env=""):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "env"
            env_file.write_text(REQUIRED_ENV + extra_env, encoding="utf-8")
            proc = subprocess.run(
                ["docker", "compose", "-p", "botville-test", "-f", str(COMPOSE),
                 "--env-file", str(env_file), "--profile", "gateway",
                 "config", "--format", "json"],
                cwd=REPO_ROOT, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            )
            if proc.returncode != 0:
                # Never proc.stdout: rendered config holds interpolated values.
                self.fail(proc.stderr or "compose failed (exit {})".format(proc.returncode))
            return json.loads(proc.stdout)["services"]["gateway"]["environment"]

    def test_defaults(self):
        env = self.gateway_env()
        for key, (default, _, _) in EXPECTED.items():
            self.assertEqual(env.get(key), default, key)

    def test_values_pass_through(self):
        values = {
            "GATEWAY_MOOD_ENABLED": "true",
            "GATEWAY_MOOD_HALF_LIFE_MINUTES": "60",
            "GATEWAY_PROACTIVE_ENABLED": "true",
            "GATEWAY_PROACTIVE_RECENT_CONTACTS_ENABLED": "true",
            "GATEWAY_PROACTIVE_TICK_SECONDS": "45",
            "GATEWAY_PROACTIVE_MIN_IDLE_MINUTES": "0",
            "GATEWAY_PROACTIVE_MIN_INTERVAL_MINUTES": "30",
            "GATEWAY_PROACTIVE_PER_TARGET_PER_DAY": "3",
        }
        self.assertEqual(set(values), set(EXPECTED))
        env = self.gateway_env("".join("{}={}\n".format(k, v) for k, v in values.items()))
        for key, value in values.items():
            self.assertEqual(env.get(key), value, key)


if __name__ == "__main__":
    unittest.main()
