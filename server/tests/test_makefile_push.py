#!/usr/bin/env python3
"""Regression tests for the `up` target's PUSH=1 sygnal reload (PR #127 B1).

APNs-related .env keys (APNS_PLATFORM, BOTVILLE_APNS_ENABLED, ...) only change
init.sh's rendering of the bind-mounted generated/sygnal/sygnal.yaml — compose
interpolation never sees them, so `up -d` alone never reloads the running
sygnal (it parses its config only at startup). `make up PUSH=1` therefore has
to force-recreate ONLY the sygnal container after `up -d`, leaving the
homeserver/postgres untouched; without PUSH, `up` must not touch sygnal at
all. These tests pin that shape against the Makefile text (plus a `make -n`
dry run when make is available). Standard library only.

Run:  python3 -m unittest discover -s server/tests -p "test_*.py" -v
"""

import shutil
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"

# make-level lines that may be interleaved with a recipe's tab lines
_CONDITIONALS = ("ifeq", "ifneq", "ifdef", "ifndef", "else", "endif")


def up_target_lines():
    """The `up:` target's recipe lines (tab lines + conditionals/blank lines).

    The recipe ends at the first line that is neither tab-prefixed, a make
    conditional, nor blank.
    """
    lines = MAKEFILE.read_text(encoding="utf-8").splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith("up:"):
            start = i + 1
            break
    if start is None:
        raise AssertionError("no `up:` target found in Makefile")
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


class MakefilePushUpTest(unittest.TestCase):
    def setUp(self):
        self.lines = up_target_lines()
        self.joined = "\n".join(self.lines)

    def test_sygnal_force_recreate_is_guarded_by_push_profile(self):
        """The sygnal recreate command exists and sits inside the
        `ifneq ($(PUSH_PROFILE),)` guard (i.e. only runs with PUSH=1)."""
        guard = "ifneq ($(PUSH_PROFILE),)"
        self.assertIn(guard, self.joined)
        guard_idx = self.lines.index(guard)
        sygnal_idx = self._sygnal_recreate_index()
        self.assertGreater(sygnal_idx, guard_idx)
        endif_indices = [i for i, l in enumerate(self.lines) if l == "endif"]
        closing = [i for i in endif_indices if i > sygnal_idx]
        self.assertTrue(closing, "sygnal recreate line not closed by endif")
        self.assertLess(sygnal_idx, closing[0])

    def test_sygnal_recreate_targets_only_sygnal_with_no_deps(self):
        """`up -d --force-recreate --no-deps sygnal` and nothing else after
        `up -d` — a stray service name there would restart the homeserver."""
        line = self.lines[self._sygnal_recreate_index()]
        self.assertIn("--profile push", line)
        after_up_d = line.split("up -d", 1)[1].strip()
        self.assertEqual(after_up_d, "--force-recreate --no-deps sygnal")

    def test_sygnal_recreate_runs_after_plain_up(self):
        """The recreate happens after the regular `up -d`, so the stack
        (network/dependencies) exists first."""
        plain = [
            i
            for i, l in enumerate(self.lines)
            if l.startswith("\t") and l.rstrip().endswith("up -d")
        ]
        self.assertTrue(plain, "plain `up -d` line missing from up target")
        self.assertLess(plain[0], self._sygnal_recreate_index())

    def test_plain_up_still_carries_push_profile_variable(self):
        """The first `up -d` keeps $(PUSH_PROFILE) so PUSH=1 starts sygnal at
        all (the recreate below then reloads it)."""
        plain = [
            l
            for l in self.lines
            if l.startswith("\t") and l.rstrip().endswith("up -d")
        ]
        self.assertTrue(plain)
        self.assertIn("$(PUSH_PROFILE)", plain[0])

    def _sygnal_recreate_index(self):
        hits = [
            i
            for i, l in enumerate(self.lines)
            if l.startswith("\t") and "--force-recreate --no-deps sygnal" in l
        ]
        self.assertEqual(
            len(hits), 1, f"expected exactly one sygnal recreate line, got {hits}"
        )
        return hits[0]


class MakefilePushDryRunTest(unittest.TestCase):
    """`make -n up` dry runs: PUSH=1 prints the sygnal-only recreate, plain
    `up` prints nothing of the sort. Skipped when make is unavailable."""

    @classmethod
    def setUpClass(cls):
        if not shutil.which("make"):
            raise unittest.SkipTest("make not available")

    def _dry_run(self, *args):
        return subprocess.run(
            ["make", "-n", "up", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    def test_push_dry_run_recreates_only_sygnal(self):
        out = self._dry_run("PUSH=1")
        self.assertIn("up -d --force-recreate --no-deps sygnal", out)
        # exactly one recreate line, and it must not name other services
        # after its `up -d` (postgres/synapse/... would mean restarting them)
        recreates = [l for l in out.splitlines() if "--force-recreate" in l]
        self.assertEqual(len(recreates), 1)
        self.assertTrue(recreates[0].rstrip().endswith("--no-deps sygnal"))

    def test_plain_up_dry_run_never_touches_sygnal(self):
        out = self._dry_run()
        self.assertNotIn("--force-recreate", out)
        self.assertNotIn("sygnal", out)


if __name__ == "__main__":
    unittest.main()
