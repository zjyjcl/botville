#!/usr/bin/env python3
"""No early-exit consumer at the end of a pipe in CI, the Makefile, scripts,
test shell snippets or docs.

GitHub Actions steps inherit SIGPIPE=ignored. Under `set -o pipefail`, a
consumer that stops reading early makes the producer's next write fail with
EPIPE -- e.g. `tar: stdout: write error`, exit 2 -- depending on timing and
entry order. That broke backup-restore (synapse) (#204). Write the producer's
output to a file or variable and grep that instead (or read the whole stream).

Early-exit consumers caught (any path prefix, e.g. /usr/bin/head, quoted or
backslash-escaped names such as "grep", 'head', \\grep or g"rep", and behind
wrappers: env assignments, `env [-i] [-u NAME]`, `sudo [-u user] [-E] [--]`,
`doas -u user`, `command`, `exec`, `nohup`, `time`, and multi-call
binaries `busybox grep` / `toybox head` are checked as the command that
follows):
  - head / ghead (always);
  - any of these run by xargs (`xargs -0 -n1 grep -q`, `xargs -I {} ggrep
    -m1 ... {}`, `busybox xargs grep -q`): xargs' own options are skipped
    and the command it runs is checked. The deprecated -e / -i / -l and
    their long forms --eof / --replace / --max-lines only take an attached
    optional argument (-eEOF, -i{}, -l1, --replace=X), never the next word;
  - grep / egrep / fgrep / rg and the GNU-prefixed ggrep / gegrep / gfgrep
    with -q / -m N / -mN / -l in any short-flag cluster (`-E -q`, `-Eq`,
    `-qxF`, `-m1`, `-m 1`) or --quiet / --silent / --max-count /
    --files-with-matches.
A consumer is the command right after a single `|` or `|&` (not `||`),
anywhere in an `&&` / `||` / `;` chain, including the first command of a
line whose previous line ends in `|`.

Docs are scanned too (whole lines, comments included: readers copy them). If
a doc ever genuinely needs such a pipe, add it to ALLOWED with the reason.
"""

import re
import shlex
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

# `|` or `|&` (pipe stdout+stderr), never `||`.
PIPE_SPLIT = re.compile(r"(?<!\|)\|&?(?!\|)")
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
# Not listed on purpose: -e/-i/-l and --eof/--replace/--max-lines (optional
# argument, attached only).
XARGS_ARG_LONG = {"--arg-file", "--delimiter", "--max-args", "--max-procs",
                  "--max-chars", "--process-slot-var"}
# Prefix commands that run the next word as the command, with the options
# that take a separate argument (sudo -u bob, env -u VAR, doas -u bob,
# exec -a name, nice -n 5).
PREFIX_ARG_SHORT = {
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U", "-T", "-R"},
    "doas": {"-u", "-C"},
    "env": {"-u", "-C", "-S"},
    "exec": {"-a"},
    "command": set(),
    "nohup": set(),
    "time": {"-f", "-o"},
    "nice": {"-n"},
}
PREFIX_ARG_LONG = {
    "sudo": {"--user", "--group", "--host", "--prompt", "--close-from", "--chdir",
             "--role", "--type", "--other-user", "--command-timeout", "--chroot"},
    "env": {"--unset", "--chdir", "--split-string"},
    "time": {"--format", "--output"},
    "nice": {"--adjustment"},
}


def _unquote(tok):
    """Shell-level name of a word: quotes removed ("grep", g"rep", 'head')
    and a leading backslash (\\grep bypasses aliases) dropped."""
    return re.sub(r"[\"']", "", tok).lstrip("\\")


def _words(segment):
    """Split like the shell (quotes group, `\\grep` -> grep); a segment cut
    mid-quote by the pipe split falls back to whitespace words, unquoted."""
    try:
        return shlex.split(segment)
    except ValueError:
        return [_unquote(t) for t in segment.split()]


def _skip_options(rest, short_arg, long_arg):
    """Drop leading options (and their separate arguments) plus a `--`."""
    while rest and rest[0].startswith("-"):
        opt = rest.pop(0)
        if opt == "--":
            break
        if opt in short_arg or ("=" not in opt and opt in long_arg):
            if rest:
                rest.pop(0)
    return rest


def _command_name(tokens):
    """Strip env assignments / command / exec / sudo, multi-call wrappers
    (busybox grep) and xargs with its options (xargs -0 -n1 grep -q ...);
    return (name, remaining tokens) of the command that actually reads."""
    while True:
        while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=\S*", tokens[0]):
            tokens = tokens[1:]
        if not tokens:
            return "", []
        name = tokens[0].rsplit("/", 1)[-1]
        if name in WRAPPERS and len(tokens) > 1:
            tokens = tokens[1:]
            continue
        if name in PREFIX_ARG_SHORT or name in XARGS:
            if name in XARGS:
                short_arg, long_arg = XARGS_ARG_SHORT, XARGS_ARG_LONG
            else:
                short_arg, long_arg = PREFIX_ARG_SHORT[name], PREFIX_ARG_LONG.get(name, set())
            tokens = _skip_options(tokens[1:], short_arg, long_arg)
            if not tokens:
                return "", []
            continue
        return name, tokens


def _consumer_is_early_exit(segment):
    segment = SEGMENT_END.split(segment, 1)[0]
    name, tokens = _command_name(_words(segment.strip().lstrip("!{(")))
    if not name:
        return False
    if name in HEAD_LIKE:
        return True
    if name not in GREP_LIKE:
        return False
    for tok in tokens[1:]:
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
            # sudo / doas / env / exec / nohup / time / nice wrappers.
            "a | sudo grep -q foo",
            "a | sudo -u bob grep -q foo",
            "a | sudo -E -u bob -- grep -q foo",
            "a | sudo -n -g staff head -1",
            "a | sudo --user=bob grep -q foo",
            "a | sudo --user bob grep --quiet foo",
            "a | sudo -u bob env LC_ALL=C grep -q foo",
            "a | doas -u bob grep -q foo",
            "a | env -i PATH=/bin grep -q foo",
            "a | env -u LANG grep -q foo",
            "a | exec -a g grep -q foo",
            "a | nohup head -1",
            "a | time grep -q foo",
            "a | nice -n 5 grep -q foo",
            "find . | sudo -u bob xargs -0 grep -q foo",
            "find . | xargs sudo -u bob grep -q foo",
            # && / || / ; chains: every single-pipe consumer is checked.
            "a && b | grep -q foo",
            "a || b | grep -q foo",
            "a | b && c | head -1",
            "a | sort || c | grep -m1 foo",
            "a; b | grep -q foo",
            "a | grep -q foo && echo ok",
            "a | grep -q foo || echo missing",
            "a | grep -q foo&&echo ok",
            "x || tar -tzf f | grep -q y && echo ok",
            "a |& grep -q foo",
            "a && b |& head -1",
            # Quoted / escaped command names.
            'a | "grep" -q foo',
            "a | 'head' -n1",
            "a | \\grep -q foo",
            'a | g"rep" -q foo',
            'a | "/usr/bin/grep" -q foo',
            'a | "busybox" "grep" -q foo',
            'a | "sudo" -u bob "grep" "-q" foo',
            "find . | 'xargs' -0 'grep' -q foo",
            # Deprecated xargs options: optional argument attached only.
            "find . | xargs -e grep -q foo",
            "find . | xargs -eEOF grep -q foo",
            "find . | xargs -i grep -q foo {}",
            "find . | xargs -iX grep -q foo X",
            "find . | xargs -l grep -q foo",
            "find . | xargs -l1 grep -q foo",
            "find . | xargs --eof grep -q foo",
            "find . | xargs --eof=EOF grep -q foo",
            "find . | xargs --replace grep -q foo {}",
            "find . | xargs --replace=X grep -q foo X",
            "find . | xargs --max-lines grep -q foo",
            "find . | xargs --max-lines=1 head -n1",
            "find . | xargs -0 -i -l1 -e grep -q foo {}",
        )
        for line in bad:
            with self.subTest(line=line):
                self.assertTrue(early_exit_in(line), line)
        self.assertTrue(early_exit_in("  grep -q foo", continued=True))
        self.assertTrue(early_exit_in("  sudo -u bob grep -q foo", continued=True))
        self.assertTrue(early_exit_in("  grep -q foo && echo ok", continued=True))

    def test_scan_follows_line_continuations_and_chains(self):
        import tempfile
        cases = (
            ("tar -tzf x |\n  grep -q y\n", 1),
            ("tar -tzf x \\\n  | grep -q y\n", 1),
            ("tar -tzf x | sort \\\n  && sudo -u bob true\n", 0),
            ("a ||\n  grep -q y f\n", 0),
            ("a &&\n  grep -q y f\n", 0),
            ("a | b &&\n  c | head -1\n", 1),
        )
        with tempfile.TemporaryDirectory(dir=str(REPO_ROOT / "server" / "tests")) as tmp:
            for text, expected in cases:
                path = Path(tmp) / "snippet.sh"
                path.write_text(text, encoding="utf-8")
                with self.subTest(text=text):
                    self.assertEqual(len(scan(path, strip_comments=True)), expected)

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
            # Wrappers whose command reads everything.
            "a | sudo -u bob grep foo",
            "a | sudo -u grep cat",
            "a | sudo -u bob tee /tmp/x",
            "a | env -u GREP wc -l",
            "a | doas -u head cat",
            "a | nice -n 5 sort",
            # && / || chains with no early-exit pipe consumer.
            "a && grep -q foo f",
            "a || grep -q foo f",
            "a | sort && grep -q foo f",
            "a | wc -l || head -n1 f",
            "a | tee x && grep -q y x",
            "test -f x && head -n1 x || echo none",
            "a |& tee log",
            # Quoted names / arguments of full readers.
            'a | "grep" -v foo',
            "a | 'tail' -n1",
            "a | \\grep -c foo",
            "a | grep \"foo -q\" -c",
            # Deprecated xargs options followed by a full reader.
            "find . | xargs -i chmod 600 {}",
            "find . | xargs -l1 grep foo",
            "find . | xargs -e grep -c foo",
            "find . | xargs --replace echo {} -q",
            "find . | xargs --max-lines wc -l",
        )
        for line in good:
            with self.subTest(line=line):
                self.assertFalse(early_exit_in(line), line)


if __name__ == "__main__":
    unittest.main()
