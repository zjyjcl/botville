#!/usr/bin/env python3
"""No early-exit consumer at the end of a pipe in CI, the Makefile, scripts,
test shell snippets or docs.

GitHub Actions steps inherit SIGPIPE=ignored. Under `set -o pipefail`, a
consumer that stops reading early makes the producer's next write fail with
EPIPE -- e.g. `tar: stdout: write error`, exit 2 -- depending on timing and
entry order. That broke backup-restore (synapse) (#204). Write the producer's
output to a file or variable and grep that instead (or read the whole stream).

Early-exit consumers caught (any path prefix, e.g. /usr/bin/head, and
behind a multi-call wrapper: `busybox grep`, `toybox head` are checked as
the applet that follows):
  - head / ghead (always);
  - any of these run by xargs (`xargs -0 -n1 grep -q`, `xargs -I {} ggrep
    -m1 ... {}`, `busybox xargs grep -q`): xargs' own options are skipped
    and the command it runs is checked;
  - grep / egrep / fgrep / rg and the GNU-prefixed ggrep / gegrep / gfgrep
    with -q / -m N / -mN / -l in any short-flag cluster (`-E -q`, `-Eq`,
    `-qxF`, `-m1`, `-m 1`) or --quiet / --silent / --max-count /
    --files-with-matches.
A consumer is the command right after a single `|` (not `||`), including the
first command of a line whose previous line ends in `|`.

Docs are scanned too (whole lines, comments included: readers copy them). If
a doc ever genuinely needs such a pipe, add it to ALLOWED with the reason.
"""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CODE = (
    sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
    + [REPO_ROOT / "Makefile"]
    + sorted((REPO_ROOT / "server" / "scripts").glob("*.sh"))
    + sorted((REPO_ROOT / "scripts").glob("*.sh"))
    # Shell snippets embedded in tests (fake docker/compose scripts).
    + sorted(p for p in (REPO_ROOT / "server" / "tests").glob("*.py")
             if p.name != Path(__file__).name)
)
DOCS = sorted((REPO_ROOT / "docs").rglob("*.md")) + [REPO_ROOT / "server" / "README.md"]

# (repo-relative path, exact stripped line) -> reason. Keep empty unless a doc
# truly needs the pattern.
ALLOWED = {}

PIPE_SPLIT = re.compile(r"(?<!\|)\|(?!\|)")
GREP_LIKE = {"grep", "egrep", "fgrep", "rg", "ggrep", "gegrep", "gfgrep"}
HEAD_LIKE = {"head", "ghead"}
# Multi-call binaries: the applet name follows.
WRAPPERS = {"busybox", "toybox"}
LONG_EARLY = ("--quiet", "--silent", "--max-count", "--files-with-matches")
SEGMENT_END = re.compile(r"[;&)`]|\|\|")
# xargs runs the command that follows its own options; these options take a
# separate argument when not attached (-n 1, -I {}, --max-args 2, ...).
XARGS = {"xargs", "gxargs"}
XARGS_ARG_SHORT = {"-I", "-n", "-P", "-L", "-d", "-E", "-s", "-a"}
XARGS_ARG_LONG = {"--arg-file", "--delimiter", "--max-args", "--max-procs",
                  "--max-lines", "--max-chars", "--replace", "--eof", "--process-slot-var"}


def _command_name(tokens):
    """Strip env assignments / command / exec / sudo, multi-call wrappers
    (busybox grep) and xargs with its options (xargs -0 -n1 grep -q ...);
    return (name, remaining tokens) of the command that actually reads."""
    while True:
        while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=\S*|command|exec|sudo|env", tokens[0]):
            tokens = tokens[1:]
        if not tokens:
            return "", []
        name = tokens[0].strip("\"'").rsplit("/", 1)[-1]
        if name in WRAPPERS and len(tokens) > 1:
            tokens = tokens[1:]
            continue
        if name in XARGS:
            rest = tokens[1:]
            while rest and rest[0].startswith("-"):
                opt = rest.pop(0)
                if opt == "--":
                    break
                if opt in XARGS_ARG_SHORT or (opt.startswith("--") and "=" not in opt
                                              and opt in XARGS_ARG_LONG):
                    if rest:
                        rest.pop(0)
            if not rest:
                return "", []
            tokens = rest
            continue
        return name, tokens


def _consumer_is_early_exit(segment):
    segment = SEGMENT_END.split(segment, 1)[0]
    name, tokens = _command_name(segment.strip().lstrip("!{(").split())
    if not name:
        return False
    if name in HEAD_LIKE:
        return True
    if name not in GREP_LIKE:
        return False
    for tok in tokens[1:]:
        tok = tok.strip("\"'")
        if tok == "--":
            break
        if tok.startswith("--"):
            if tok.split("=", 1)[0] in LONG_EARLY:
                return True
        elif re.fullmatch(r"-[A-Za-z0-9]+", tok):
            letters = re.sub(r"[0-9]", "", tok[1:])
            if set(letters) & {"q", "m", "l"}:
                return True
    return False


def early_exit_in(line, continued=False):
    """True if `line` pipes into an early-exit consumer. `continued`: the
    previous line ended in a single `|`, so the first command is a consumer."""
    parts = PIPE_SPLIT.split(line)
    consumers = parts[1:] if not continued else parts
    return any(_consumer_is_early_exit(p) for p in consumers)


def scan(path, strip_comments):
    offenders = []
    continued = False
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        code = line
        if strip_comments:
            code = "" if line.lstrip().startswith("#") else line.split(" #", 1)[0]
        rel = str(path.relative_to(REPO_ROOT))
        if early_exit_in(code, continued) and (rel, line.strip()) not in ALLOWED:
            offenders.append("{}:{}: {}".format(rel, n, line.strip()))
        stripped = code.rstrip().rstrip("\\").rstrip()
        continued = stripped.endswith("|") and not stripped.endswith("||")
    return offenders


class NoEarlyExitPipesTest(unittest.TestCase):
    def test_no_early_exit_consumers_in_code(self):
        offenders = [o for p in CODE for o in scan(p, strip_comments=True)]
        self.assertEqual(offenders, [], "early-exit consumer in a pipe:\n" + "\n".join(offenders))

    def test_no_early_exit_consumers_in_docs(self):
        offenders = [o for p in DOCS for o in scan(p, strip_comments=False)]
        self.assertEqual(offenders, [], "early-exit consumer in a doc pipe:\n" + "\n".join(offenders))

    def test_allowlist_entries_still_exist(self):
        for rel, line in ALLOWED:
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            self.assertIn(line, [l.strip() for l in text.splitlines()], rel)

    def test_pattern_catches_variants(self):
        bad = (
            "tar -tzf x.tar.gz | grep -qx './ci-seed.txt'",
            "tar -tzf x | grep -E -q 'a|b'",
            "tar -tzf x | grep -Eq foo",
            "tar -tzf x | grep -qxF foo",
            "tar -tzf x | grep -m1 foo",
            "tar -tzf x | grep -m 1 foo",
            "tar -tzf x | grep --max-count=1 foo",
            "tar -tzf x | grep --quiet foo",
            "tar -tzf x | grep --silent foo",
            "tar -tzf x | grep -l foo",
            "tar -tzf x | egrep -q foo",
            "tar -tzf x | fgrep -q foo",
            "tar -tzf x | rg -q foo",
            "tar -tzf x | rg -m1 foo",
            "tar -tzf x | /usr/bin/grep -q foo",
            "sed -n p f | head -n 1",
            "sed -n p f | head",
            "sed -n p f | /usr/bin/head -1",
            "a | LC_ALL=C grep -q foo",
            "a | sort | head -n 3",
            "x=$(a | grep -q y)",
            "tar -tzf x | busybox grep -q foo",
            "tar -tzf x | /bin/busybox grep -qx foo",
            "tar -tzf x | busybox grep -E -q foo",
            "tar -tzf x | busybox grep -m1 foo",
            "tar -tzf x | busybox head -n 1",
            "tar -tzf x | toybox head -1",
            "tar -tzf x | ggrep -q foo",
            "tar -tzf x | ggrep -Eq foo",
            "tar -tzf x | /opt/homebrew/bin/ggrep --quiet foo",
            "tar -tzf x | gegrep -q foo",
            "tar -tzf x | gfgrep -m 1 foo",
            "tar -tzf x | ghead -n1",
            "find . -name '*.db' | xargs grep -q foo",
            "find . -print0 | xargs -0 grep -q foo",
            "find . -print0 | xargs -0 -n1 grep -qx foo",
            "find . -print0 | xargs -0 -n 1 -P 4 egrep -q foo",
            "find . | xargs -I {} fgrep -q foo {}",
            "find . | xargs -I{} ggrep -m1 foo {}",
            "find . | xargs --max-args 2 grep --quiet foo",
            "find . | xargs --max-args=2 -r grep -l foo",
            "find . | xargs busybox grep -q foo",
            "find . | busybox xargs -0 grep -q foo",
            "find . | /usr/bin/xargs -0 /usr/bin/grep -q foo",
            "find . | xargs -- grep -q foo",
            "find . | xargs head -n1",
            "find . | env LC_ALL=C xargs grep -q foo",
        )
        for line in bad:
            with self.subTest(line=line):
                self.assertTrue(early_exit_in(line), line)
        self.assertTrue(early_exit_in("  grep -q foo", continued=True))

    def test_pattern_allows_full_readers(self):
        good = (
            "grep -qx './ci-seed.txt' \"${listing}\"",
            "grep -qx gateway-data <<<\"${volumes}\"",
            "docker info | grep -i proxy",
            "a | grep -iE 'proxy.*(error|refused)|7890' || echo clean",
            "a || grep -q x f",
            "a | tail -n 1",
            "a | wc -l",
            "a | sort -u",
            "a | grep -v foo | grep -c bar",
            "printf x | sha256sum",
            "a | busybox grep -i proxy",
            "a | ggrep -v foo",
            "a | busybox tail -n 1",
            "a | busybox wc -l",
            "find . -print0 | xargs -0 grep -c foo",
            "find . -print0 | xargs -0 -n1 grep foo",
            "find . | xargs -I {} chmod 600 {}",
            "find . -print0 | xargs -0 rm -f",
            "find . | xargs -n 1 echo -q",
            "find . | xargs",
        )
        for line in good:
            with self.subTest(line=line):
                self.assertFalse(early_exit_in(line), line)


if __name__ == "__main__":
    unittest.main()
