#!/usr/bin/env python3
"""Every `docker compose config` in CI, the Makefile and shell scripts is
`--quiet` (or a names-only listing), and tests never print its output.

Plain `docker compose config` prints the fully interpolated stack -- every
secret from the env file (gateway auth token / HMAC secret, AS/HS tokens,
GATEWAY_MODEL_API_KEY, GATEWAY_EMBEDDING_API_KEY, ...) -- straight into the
terminal or the CI log (#208 nit). Allowed forms:

  - `config --quiet` / `config -q`: validation only, prints nothing;
  - `config --volumes` / `--services` / `--profiles`: names only, no values.

Tests may render `config --format json` into memory (capture_output) to
inspect it, but must never put that stdout into a failure message or print.
"""

import re
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _tracked(*patterns):
    proc = subprocess.run(["git", "ls-files", *patterns], cwd=REPO_ROOT,
                          stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if proc.returncode != 0:  # not a git checkout: fall back to globbing
        found = set()
        for pat in patterns:
            found.update(p.relative_to(REPO_ROOT).as_posix() for p in REPO_ROOT.glob("**/" + pat))
        return sorted(found)
    return sorted(set(proc.stdout.split()))


# Shell-ish sources: every tracked *.sh, the Makefile and the workflows.
# gateway/ is not ours to change and runs no compose.
SHELL_FILES = [REPO_ROOT / p for p in _tracked("*.sh", "Makefile", ".github/workflows/*.yml")
               if not p.startswith("gateway/")]
TEST_FILES = sorted((REPO_ROOT / "server" / "tests").glob("*.py"))

COMPOSE_CALL = re.compile(
    r"(docker[ -]compose|\$\(COMPOSE\)|\$\{?COMPOSE\}?|\$\{?STACK\}?|(?<![\w/.-])compose(?![\w/.-]))")
CONFIG_WORD = re.compile(r"(?<![\w/.-])config(?![\w/.=-])")
SAFE = re.compile(r"(?<![\w-])(--quiet|-q|--volumes|--services|--profiles)(?![\w-])")
QUOTED = re.compile(r'"(?:\\.|[^"\\])*"|\'[^\']*\'')


def logical_lines(text):
    """Join backslash-continued lines; drop full-line comments."""
    buf, start = "", None
    for n, line in enumerate(text.splitlines(), 1):
        if not buf and line.lstrip().startswith("#"):
            continue
        if start is None:
            start = n
        if line.rstrip().endswith("\\"):
            buf += line.rstrip()[:-1] + " "
            continue
        yield start, buf + line
        buf, start = "", None
    if buf:
        yield start, buf


def unsafe_config_call(line):
    """True if `line` runs `compose ... config` without --quiet or a
    names-only flag. Quoted strings (echo messages, step names) and YAML
    `name:` keys are not commands."""
    stripped = line.strip()
    if re.match(r"-?\s*name:", stripped):
        return False
    code = QUOTED.sub(" ", stripped.split(" #", 1)[0])
    m = COMPOSE_CALL.search(code)
    if not m:
        return False
    tail = code[m.end():]
    c = CONFIG_WORD.search(tail)
    if not c:
        return False
    return not SAFE.search(tail[c.end():].split(";", 1)[0].split("&&", 1)[0].split("|", 1)[0])


PRINTS_STDOUT = re.compile(r"(\.fail\(|print\(|sys\.stdout\.write\()[^\n]*\bstdout\b")


class ComposeConfigQuietTest(unittest.TestCase):
    def test_shell_sources_only_run_quiet_or_names_only_config(self):
        self.assertTrue(SHELL_FILES)
        offenders = []
        for path in SHELL_FILES:
            for n, line in logical_lines(path.read_text(encoding="utf-8")):
                if unsafe_config_call(line):
                    offenders.append("{}:{}: {}".format(path.relative_to(REPO_ROOT), n, line.strip()))
        self.assertEqual(offenders, [], "compose config without --quiet:\n" + "\n".join(offenders))

    def test_tests_rendering_config_never_print_its_stdout(self):
        offenders = []
        for path in TEST_FILES:
            if path.name == Path(__file__).name:
                continue
            text = path.read_text(encoding="utf-8")
            if '"config"' not in text:
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if PRINTS_STDOUT.search(line):
                    offenders.append("{}:{}: {}".format(path.relative_to(REPO_ROOT), n, line.strip()))
        self.assertEqual(offenders, [], "test prints compose config stdout:\n" + "\n".join(offenders))

    def test_checker_flags_unsafe_calls(self):
        bad = (
            "docker compose -f server/compose/docker-compose.yml --env-file server/compose/.env config",
            "docker-compose config",
            "$(COMPOSE) --profile synapse config",
            "${STACK} config --format json",
            "$STACK config > out.yml",
            "docker compose -f a.yml config --services --format json | jq . && docker compose config",
            "compose config",
            "docker compose -f a.yml --env-file e --profile x config | tee log",
        )
        for line in bad:
            with self.subTest(line=line):
                # The second command in the && example is the unsafe one.
                if "&&" in line:
                    self.assertTrue(unsafe_config_call(line.split("&&", 1)[1]))
                else:
                    self.assertTrue(unsafe_config_call(line), line)
        joined = list(logical_lines("docker compose -f a.yml \\\n  --env-file e \\\n  config\n"))
        self.assertEqual(len(joined), 1)
        self.assertTrue(unsafe_config_call(joined[0][1]))

    def test_checker_allows_safe_calls(self):
        good = (
            "docker compose -f a.yml --env-file e --profile x config --quiet",
            "$(COMPOSE) --profile $(HS) --profile push --profile gateway config --quiet",
            "volumes=\"$(${STACK} config --volumes)\"",
            "${STACK} config --volumes",
            "docker compose config -q",
            "docker compose config --services",
            "echo \"== docker compose config --profile ${hs}\"",
            "- name: compose config --volumes lists gateway-data",
            "- name: Compose config validates for every profile",
            "docker compose -f server/compose/config.yml up -d",
            "make config",
            "bash server/scripts/gateway-preflight.sh .env",
        )
        for line in good:
            with self.subTest(line=line):
                self.assertFalse(unsafe_config_call(line), line)
        joined = list(logical_lines("docker compose -f a.yml \\\n  --env-file e \\\n  config --quiet\n"))
        self.assertFalse(unsafe_config_call(joined[0][1]))


if __name__ == "__main__":
    unittest.main()
