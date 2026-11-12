#!/usr/bin/env python3
"""server/scripts/gateway-preflight.sh: a missing custom personas dir fails
before compose runs (make up GATEWAY=1 / make config), not at `up`."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "server" / "scripts" / "gateway-preflight.sh"


def run(env_text, extra_env=None):
    with tempfile.TemporaryDirectory() as tmp:
        env_file = Path(tmp) / "env"
        env_file.write_text(env_text, encoding="utf-8")
        env = {k: v for k, v in os.environ.items()
               if k not in ("BOTVILLE_GATEWAY_PERSONAS_DIR", "GATEWAY_PERSONAS_FILE", "BOTVILLE_ENV_FILE")}
        env.update(extra_env or {})
        return subprocess.run(["bash", str(SCRIPT), str(env_file)], env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)


class GatewayPreflightTest(unittest.TestCase):
    def test_default_repo_dir_passes(self):
        proc = run("COMPOSE_PROJECT_NAME=x\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("gateway-personas", proc.stdout)

    def test_missing_custom_dir_fails_early(self):
        proc = run("BOTVILLE_GATEWAY_PERSONAS_DIR=/nonexistent/botville-personas\n")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("BOTVILLE_GATEWAY_PERSONAS_DIR=/nonexistent/botville-personas", proc.stderr)

    def test_relative_dir_resolves_against_server_compose(self):
        self.assertEqual(run("BOTVILLE_GATEWAY_PERSONAS_DIR=./gateway-personas\n").returncode, 0)
        self.assertEqual(run("BOTVILLE_GATEWAY_PERSONAS_DIR=./no-such-dir\n").returncode, 2)

    def test_last_assignment_and_shell_env_win(self):
        with tempfile.TemporaryDirectory() as good:
            text = "BOTVILLE_GATEWAY_PERSONAS_DIR=/nonexistent/a\nBOTVILLE_GATEWAY_PERSONAS_DIR={}\n".format(good)
            self.assertEqual(run(text).returncode, 0)
            # The shell environment overrides the env file (compose precedence).
            proc = run("BOTVILLE_GATEWAY_PERSONAS_DIR={}\n".format(good),
                       {"BOTVILLE_GATEWAY_PERSONAS_DIR": "/nonexistent/b"})
            self.assertEqual(proc.returncode, 2)

    def test_missing_personas_file_only_warns(self):
        with tempfile.TemporaryDirectory() as d:
            text = ("BOTVILLE_GATEWAY_PERSONAS_DIR={}\n"
                    "GATEWAY_PERSONAS_FILE=/etc/botville-gateway/personas.json\n").format(d)
            proc = run(text)
            self.assertEqual(proc.returncode, 0)
            self.assertIn("WARNING", proc.stderr)
            Path(d, "personas.json").write_text("{}", encoding="utf-8")
            proc = run(text)
            self.assertNotIn("WARNING", proc.stderr)

    def test_never_prints_other_env_values(self):
        sentinel = "sentinel-model-key-must-not-print-0123456789"
        proc = run("GATEWAY_MODEL_API_KEY={}\nBOTVILLE_GATEWAY_PERSONAS_DIR=/nonexistent/x\n".format(sentinel))
        self.assertNotIn(sentinel, proc.stdout + proc.stderr)


class MakefileWiresPreflightTest(unittest.TestCase):
    def dry(self, *args):
        proc = subprocess.run(["make", "-n", "-C", str(REPO_ROOT), *args,
                               "ENV_FILE=/dev/null", "PROJECT=botville-dryrun"],
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_up_with_gateway_runs_preflight_before_compose(self):
        out = self.dry("up", "GATEWAY=1")
        pre = out.index("gateway-preflight.sh")
        self.assertLess(pre, out.index(" build gateway"))
        self.assertLess(pre, out.index(" up -d"))
        self.assertNotIn("gateway-preflight.sh", self.dry("up"))

    def test_config_runs_preflight_then_quiet_config(self):
        out = self.dry("config")
        self.assertLess(out.index("gateway-preflight.sh"), out.index("config --quiet"))
        self.assertIn("--profile gateway config --quiet", out)


if __name__ == "__main__":
    unittest.main()
