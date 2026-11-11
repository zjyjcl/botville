#!/usr/bin/env python3
"""ps / logs always carry the optional push and gateway profiles, like down.

`make up` still requires PUSH=1 / GATEWAY=1 to start those services; after
they are up, `make ps` / `make logs` should show them without repeating the
flag (same idea as `make down` already always passing both profiles).
"""

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


if __name__ == "__main__":
    unittest.main()
