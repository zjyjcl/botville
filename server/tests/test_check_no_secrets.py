#!/usr/bin/env python3
"""Tests for server/scripts/check_no_secrets.sh (issue #4 guard).

The scanner fails when a *.p8 file is tracked, when any tracked file carries
a PEM private-key header, or when .gitignore stops ignoring *.p8/secrets/.
These tests build throwaway git repos and run the real script against them.

PEM headers in the fixtures are ASSEMBLED from fragments at run time for the
same reason as in the script itself: a literal header in a tracked file
would make the guard (and the repo-wide CI step) trip over its own test.
Standard library only.

Run:  python3 -m unittest discover -s server/tests -p "test_*.py" -v
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1]
CHECK = SERVER_DIR / "scripts" / "check_no_secrets.sh"
REPO_ROOT = SERVER_DIR.parent
GITIGNORE = REPO_ROOT / ".gitignore"

# Fixture key material: assembled, never a literal (and never a real key).
PEM_HEADER = "-----BEGIN " + "EC " + "PRIVATE KEY-----"


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


class CheckNoSecretsTest(unittest.TestCase):
    maxDiff = None

    def make_repo(self, gitignore_text=None):
        """A throwaway git repo; files must be `self.track(...)`-ed to be
        picked up by git ls-files / git grep (tracked = staged + committed)."""
        repo = Path(tempfile.mkdtemp(prefix="botville-nosecrets-"))
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        self.assertEqual(git(repo, "init", "-q").returncode, 0)
        (repo / ".gitignore").write_text(
            gitignore_text
            if gitignore_text is not None
            else GITIGNORE.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        return repo

    def track(self, repo):
        """Stage and commit everything so the files are genuinely tracked."""
        self.assertEqual(git(repo, "add", "-A").returncode, 0)
        self.assertEqual(
            git(
                repo,
                "-c",
                "user.name=fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-q",
                "-m",
                "fixture",
            ).returncode,
            0,
        )

    def run_check(self, repo):
        return subprocess.run(
            ["bash", str(CHECK), str(repo)],
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_clean_repo_passes(self):
        repo = self.make_repo()
        (repo / "README.md").write_text("nothing to see\n", encoding="utf-8")
        self.track(repo)
        proc = self.run_check(repo)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("OK", proc.stdout)

    def test_ignored_local_p8_file_passes(self):
        # An untracked local key in secrets/ is exactly the intended state
        # (that is where real keys live); only *tracked* .p8 files fail.
        repo = self.make_repo()
        (repo / "server" / "compose" / "secrets").mkdir(parents=True)
        (repo / "server" / "compose" / "secrets" / "apns.p8").write_text(
            "local-only\n", encoding="utf-8"
        )
        (repo / "README.md").write_text("clean\n", encoding="utf-8")
        self.track(repo)
        proc = self.run_check(repo)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_tracked_p8_file_fails(self):
        repo = self.make_repo()
        (repo / "keys").mkdir()
        (repo / "keys" / "AuthKey_ABC123DEFG.p8").write_text("key\n", encoding="utf-8")
        # Force it into the index despite the .gitignore rules (simulates a
        # key added before the rules existed, or with git add -f).
        self.assertEqual(
            git(repo, "add", "-f", "keys/AuthKey_ABC123DEFG.p8").returncode, 0
        )
        proc = self.run_check(repo)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("AuthKey_ABC123DEFG.p8", proc.stderr)

    def test_pem_header_in_tracked_file_fails_without_printing_the_line(self):
        repo = self.make_repo()
        (repo / "docs").mkdir()
        # The header line itself carries the sentinel; the guard must report
        # the file but never echo key-looking content.
        (repo / "docs" / "oops.md").write_text(
            PEM_HEADER + "SENTINEL-KEY-BODY\n", encoding="utf-8"
        )
        self.track(repo)
        proc = self.run_check(repo)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("oops.md", proc.stderr)
        self.assertIn("private-key header", proc.stderr)
        self.assertNotIn("SENTINEL-KEY-BODY", proc.stdout + proc.stderr)
        # ... and the assembled header is not printed either.
        self.assertNotIn(PEM_HEADER, proc.stdout + proc.stderr)

    def test_pem_header_in_untracked_file_passes(self):
        repo = self.make_repo()
        (repo / "README.md").write_text("clean\n", encoding="utf-8")
        self.track(repo)  # commits README + .gitignore only
        # The key-carrying file appears only AFTER the commit, so it is
        # genuinely untracked (track() stages with add -A).
        (repo / "local-notes.md").write_text(PEM_HEADER + "\n", encoding="utf-8")
        self.assertEqual(git(repo, "ls-files", "--", "local-notes.md").stdout.strip(), "")
        proc = self.run_check(repo)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_missing_gitignore_rules_fail(self):
        repo = self.make_repo(gitignore_text="# no rules here\n")
        (repo / "README.md").write_text("clean\n", encoding="utf-8")
        self.track(repo)
        proc = self.run_check(repo)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("NOT ignored", proc.stderr)
        self.assertIn("*.p8", proc.stderr)

    def test_this_repo_passes_its_own_guard(self):
        # The repository (including this test file and the scanner script)
        # must stay clean: no tracked .p8, no PEM header literals, ignore
        # rules intact. Doubles as the self-hit guard for the script.
        proc = self.run_check(REPO_ROOT)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
