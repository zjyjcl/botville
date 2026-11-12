#!/usr/bin/env python3
"""No `producer | grep -q` / `| head` in CI steps and pipefail scripts.

GitHub Actions steps inherit SIGPIPE=ignored. Under `set -o pipefail`, a
consumer that exits early (grep -q / grep -m / head) makes the producer's
next write fail with EPIPE -- e.g. `tar: stdout: write error`, exit 2 --
depending on timing and entry order. That broke backup-restore (synapse).
Write the producer's output to a file or variable and grep that instead.
"""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKED = [REPO_ROOT / ".github" / "workflows" / "ci.yml"] + sorted(
    (REPO_ROOT / "server" / "scripts").glob("*.sh")
)
EARLY_EXIT = re.compile(r"\|\s*(grep\s+(-[A-Za-z]*[qm][A-Za-z]*\b|--quiet|--max-count)|head\b)")


class NoEarlyExitPipesTest(unittest.TestCase):
    def test_no_early_exit_consumers(self):
        offenders = []
        for path in CHECKED:
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
                if EARLY_EXIT.search(code):
                    offenders.append("{}:{}: {}".format(path.relative_to(REPO_ROOT), n, line.strip()))
        self.assertEqual(offenders, [], "early-exit consumer in a pipe:\n" + "\n".join(offenders))

    def test_pattern_catches_the_original_bug(self):
        self.assertTrue(EARLY_EXIT.search("tar -tzf x.tar.gz | grep -qx './ci-seed.txt'"))
        self.assertTrue(EARLY_EXIT.search("sed -n p f | head -n 1"))
        self.assertFalse(EARLY_EXIT.search("grep -qx './ci-seed.txt' \"${listing}\""))


if __name__ == "__main__":
    unittest.main()
