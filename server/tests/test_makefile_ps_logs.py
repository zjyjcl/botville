#!/usr/bin/env python3
"""ps / logs always carry the optional push and gateway profiles, like down.

`make up` still requires PUSH=1 / GATEWAY=1 to start those services; after
they are up, `make ps` / `make logs` should show them without repeating the
flag (same idea as `make down` already always passing both profiles).
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"

_CONDITIONALS = ("ifeq", "ifneq", "ifdef", "ifndef", "else", "endif")


def target_recipe(name):
    lines = MAKEFILE.read_text(encoding="utf-8").splitlines()
    start = None
    needle = name + ":"
    for i, line in enumerate(lines):
        if line == needle or line.startswith(needle + " "):
            start = i + 1
            break
    if start is None:
        raise AssertionError("no `{}:` target found in Makefile".format(name))
    out = []
    for line in lines[start:]:
        if (
            line.startswith("\t")
            or line == ""
            or line.split(" ", 1)[0] in _CONDITIONALS
        ):
            out.append(line)
        else:
            break
    return out


class MakefilePsLogsProfilesTest(unittest.TestCase):
    def test_ps_and_logs_always_include_push_and_gateway(self):
        for target in ("ps", "logs"):
            recipe = "\n".join(target_recipe(target))
            self.assertIn("--profile push", recipe, target)
            self.assertIn("--profile gateway", recipe, target)
            # Not gated on the make-level PUSH=/GATEWAY= variables: those
            # still control only `up`.
            self.assertNotIn("$(PUSH_PROFILE)", recipe, target)
            self.assertNotIn("$(GATEWAY_PROFILE)", recipe, target)

    def test_down_always_includes_both_and_does_not_pass_v(self):
        recipe = "\n".join(target_recipe("down"))
        self.assertIn("--profile push", recipe)
        self.assertIn("--profile gateway", recipe)
        self.assertNotIn("$(PUSH_PROFILE)", recipe)
        self.assertNotIn("$(GATEWAY_PROFILE)", recipe)
        self.assertNotIn(" -v", recipe)
        self.assertNotIn("\t-v", recipe)

    def test_up_still_requires_the_flags(self):
        recipe = "\n".join(target_recipe("up"))
        self.assertIn("$(PUSH_PROFILE)", recipe)
        self.assertIn("$(GATEWAY_PROFILE)", recipe)

    def test_up_exports_homeserver_url_and_transport(self):
        """`make up HS=...` must export BOTVILLE_HOMESERVER and
        GATEWAY_MATRIX_TRANSPORT so compose interpolation matches the
        profile actually started."""
        recipe = "\n".join(target_recipe("up"))
        self.assertIn("$(GATEWAY_HS_ENV)", recipe)
        makefile = MAKEFILE.read_text(encoding="utf-8")
        self.assertIn("GATEWAY_HS_ENV := BOTVILLE_HOMESERVER=$(HS) GATEWAY_MATRIX_TRANSPORT=$(GATEWAY_TRANSPORT)", makefile)
        self.assertIn("GATEWAY_TRANSPORT := memory", makefile)
        self.assertIn("GATEWAY_TRANSPORT := appservice", makefile)


@unittest.skipUnless(shutil.which("make"), "make not available")
class MakefileAppserviceUrlTest(unittest.TestCase):
    """Registration url (A2.2 + #192): http://gateway:8080 only for GATEWAY=1
    on synapse/tuwunel; null otherwise. `make up` hands it to init.sh and
    restarts the homeserver when init.sh reports a change."""

    def dry_run_up(self, hs, gateway):
        args = ["make", "-n", "-C", str(REPO_ROOT), "up", "HS=" + hs,
                "ENV_FILE=/dev/null", "PROJECT=botville-dryrun"]
        if gateway:
            args.append("GATEWAY=" + gateway)
        proc = subprocess.run(args, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_url_switch(self):
        cases = [
            ("synapse", "1", "http://gateway:8080"),
            ("tuwunel", "1", "http://gateway:8080"),
            ("tuwunel", "yes", "http://gateway:8080"),
            ("continuwuity", "1", "null"),
            ("synapse", "", "null"),
            ("tuwunel", "0", "null"),
            ("continuwuity", "", "null"),
        ]
        for hs, gateway, url in cases:
            with self.subTest(hs=hs, gateway=gateway):
                out = self.dry_run_up(hs, gateway)
                self.assertIn("BOTVILLE_APPSERVICE_URL={} bash server/scripts/init.sh".format(url), out)

    def test_restart_homeserver_when_url_changed_before_up(self):
        out = self.dry_run_up("synapse", "1")
        marker = "server/compose/generated/appservice/.url-changed"
        self.assertIn("if [ -f {} ]".format(marker), out)
        restart = out.index("--profile synapse restart synapse")
        up = out.index(" up -d")
        self.assertLess(restart, up)
        self.assertIn("rm -f {}".format(marker), out)

    def marker_block(self, hs):
        """The marker/restart shell block exactly as `make -n up` prints it."""
        lines = self.dry_run_up(hs, "1").splitlines()
        start = next(i for i, l in enumerate(lines) if l.startswith("if [ -f "))
        end = next(i for i in range(start, len(lines)) if lines[i].strip() == "fi")
        return "\n".join(lines[start:end + 1])

    def run_marker_block(self, restart_exit):
        """Run that block with a fake `docker` whose restart exits restart_exit."""
        marker = "server/compose/generated/appservice/.url-changed"
        with tempfile.TemporaryDirectory() as tmp:
            bindir = os.path.join(tmp, "bin")
            os.makedirs(bindir)
            fake = os.path.join(bindir, "docker")
            with open(fake, "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\nexit {}\n".format(restart_exit))
            os.chmod(fake, 0o755)
            os.makedirs(os.path.join(tmp, os.path.dirname(marker)))
            with open(os.path.join(tmp, marker), "w", encoding="utf-8"):
                pass
            env = dict(os.environ, PATH=bindir + os.pathsep + os.environ.get("PATH", ""))
            proc = subprocess.run(
                ["bash", "-c", self.marker_block("synapse")],
                cwd=tmp, env=env, capture_output=True, text=True, timeout=30,
            )
            return proc, os.path.exists(os.path.join(tmp, marker))

    def test_restart_failure_keeps_marker_and_fails(self):
        proc, marker_left = self.run_marker_block(restart_exit=1)
        self.assertNotEqual(proc.returncode, 0)
        self.assertTrue(marker_left, "marker must survive a failed HS restart")
        self.assertIn("keeping", proc.stderr)

    def test_restart_success_removes_marker(self):
        proc, marker_left = self.run_marker_block(restart_exit=0)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(marker_left)


class MakefileModelKeyNotPrintedTest(unittest.TestCase):
    """GATEWAY_MODEL_API_KEY reaches the gateway only through compose
    interpolation of the env file; make never puts it on a command line, so
    neither the echoed recipes nor `make -n` can print it."""

    SENTINEL = "sentinel-model-key-must-not-print-0123456789"

    def test_dry_runs_never_contain_the_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = os.path.join(tmp, "env")
            with open(env_file, "w", encoding="utf-8") as fh:
                fh.write("COMPOSE_PROJECT_NAME=botville-dryrun\n")
                fh.write("GATEWAY_MODEL_API_KEY={}\n".format(self.SENTINEL))
            for target in ("up", "down", "ps", "logs", "gateway-hs-smoke"):
                with self.subTest(target=target):
                    proc = subprocess.run(
                        ["make", "-n", "-C", str(REPO_ROOT), target, "GATEWAY=1",
                         "ENV_FILE=" + env_file],
                        capture_output=True, text=True, timeout=30,
                    )
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertNotIn(self.SENTINEL, proc.stdout + proc.stderr)
        self.assertNotIn("GATEWAY_MODEL_API_KEY", MAKEFILE.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
