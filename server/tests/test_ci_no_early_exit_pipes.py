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
wrappers: env assignments, `env [-i] [-u NAME]`, `env -S 'cmd args'` (the
string is split and checked as the command), `sudo [-u user] [-E] [--]`
including clustered short options (`sudo -Eu bob`, `-nu bob`, `-ubob`),
`doas -u user`, `timeout [-s SIG] [-k DUR] [--foreground] DURATION`,
`command`, `exec`, `nohup`, `time`, `nice`, and multi-call binaries
`busybox grep` / `toybox head` are checked as the command that follows):
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

Splitting is quote-aware: a `|` (or `;`, `&`, `)`) inside '...' or "..." is
not a pipe (`grep -E 'a|b'`, `grep 'x| head' f`), while one inside `$(...)` /
backticks still is, even within double quotes (`out="$(a | grep -q y)"`).
Words are split with shlex.

Known limitations (conservative: they only ever cut more segments, never fewer):
  - quotes are tracked per line. A line whose quotes do not balance (prose
    with an apostrophe in docs, a string continued on the next line) falls
    back to splitting on every `|`, so a `|` inside such a quote IS cut into
    a new segment; that segment is then split on whitespace with quotes
    removed (shlex cannot parse it);
  - heredoc bodies are scanned like any other line.

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
    "timeout": {"-s", "-k"},
    "gtimeout": {"-s", "-k"},
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
    "timeout": {"--signal", "--kill-after"},
    "gtimeout": {"--signal", "--kill-after"},
    "time": {"--format", "--output"},
    "nice": {"--adjustment"},
}
# env -S STRING: STRING is itself the command line.
PREFIX_SPLIT_STRING = {"env": {"-S", "--split-string"}}
# Words after the options before the command (timeout DURATION cmd ...).
PREFIX_POSITIONAL = {"timeout": 1, "gtimeout": 1}


def _unquote(tok):
    """Shell-level name of a word: quotes removed ("grep", g"rep", 'head')
    and a leading backslash (\\grep bypasses aliases) dropped."""
    return re.sub(r"[\"']", "", tok).lstrip("\\")


def _shell_cuts(text, stop_at_terminator):
    """Positions of unquoted pipes (`|` / `|&`, not `||`) in `text`, or -- with
    stop_at_terminator -- the index of the first unquoted `;`, `&`, `||`,
    unmatched `)` or backtick. Quotes nest inside $(...) / backticks.
    Returns None when the quotes on the line do not balance."""
    stack = []  # "'", '"', "(" or "`"
    cuts = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        top = stack[-1] if stack else ""
        if top == "'":
            if c == "'":
                stack.pop()
            i += 1
            continue
        if c == "\\":
            i += 2
            continue
        if top == '"':
            if c == '"':
                stack.pop()
            elif text.startswith("$(", i):
                stack.append("(")
                i += 1
            elif c == "`":
                stack.append("`")
            i += 1
            continue
        # Unquoted (top level, or inside $( ) / backticks).
        if c in "'\"":
            stack.append(c)
        elif text.startswith("$(", i) or c == "(":
            stack.append("(")
            i += 0 if c == "(" else 1
        elif c == ")":
            if top == "(":
                stack.pop()
            elif stop_at_terminator:
                return i
        elif c == "`":
            if top == "`":
                stack.pop()
            elif stop_at_terminator:
                return i
            else:
                stack.append("`")
        elif c == "|":
            if text.startswith("||", i):
                if stop_at_terminator and not stack:
                    return i
                i += 2
                continue
            if not stop_at_terminator:
                cuts.append((i, 2 if text.startswith("|&", i) else 1))
                i += cuts[-1][1]
                continue
        elif c in ";&" and stop_at_terminator and not stack:
            return i
        i += 1
    if any(q in stack for q in "'\""):
        return None
    return len(text) if stop_at_terminator else cuts


def split_pipes(line):
    """Pipe segments of `line`, quote-aware; per-`|` fallback (see Known
    limitations) when the line's quotes do not balance."""
    cuts = _shell_cuts(line, stop_at_terminator=False)
    if cuts is None:
        return PIPE_SPLIT.split(line)
    parts, start = [], 0
    for pos, width in cuts:
        parts.append(line[start:pos])
        start = pos + width
    parts.append(line[start:])
    return parts


def _cut_segment(segment):
    end = _shell_cuts(segment, stop_at_terminator=True)
    if end is None:
        return SEGMENT_END.split(segment, 1)[0]
    return segment[:end]


def _words(segment):
    """Split like the shell (quotes group, `\\grep` -> grep); a segment cut
    mid-quote by the pipe split falls back to whitespace words, unquoted."""
    try:
        return shlex.split(segment)
    except ValueError:
        return [_unquote(t) for t in segment.split()]


def _skip_options(rest, short_arg, long_arg, split_string=(), positional=0):
    """Drop leading options (and their separate arguments) plus a `--`.

    Short options may be clustered (`-Eu bob`, `-nu bob`, `-ubob`, `-0n1`):
    the first letter that takes an argument ends the cluster, consuming the
    rest of the word or, when it is last, the next word. Options in
    `split_string` (env -S / --split-string) carry a command line that is
    split and put back in front. `positional` words (timeout's DURATION)
    follow the options."""
    while rest and rest[0].startswith("-") and rest[0] != "-":
        opt = rest.pop(0)
        if opt == "--":
            break
        if opt.startswith("--"):
            key, eq, value = opt.partition("=")
            if key in long_arg:
                if not eq:
                    value = rest.pop(0) if rest else ""
                if key in split_string:
                    return _words(value) + rest
            continue
        for k, letter in enumerate(opt[1:], 1):
            flag = "-" + letter
            if flag in short_arg:
                value = opt[k + 1:]
                if not value:
                    value = rest.pop(0) if rest else ""
                if flag in split_string:
                    return _words(value) + rest
                break
    return rest[positional:]


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
            tokens = _skip_options(tokens[1:], short_arg, long_arg,
                                   PREFIX_SPLIT_STRING.get(name, ()),
                                   PREFIX_POSITIONAL.get(name, 0))
            if not tokens:
                return "", []
            continue
        return name, tokens


def _consumer_is_early_exit(segment):
    segment = _cut_segment(segment)
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
    parts = split_pipes(line)
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
            # Clustered short options with an argument (#223 nit).
            "a | sudo -Eu bob grep -q foo",
            "a | sudo -nu bob head -1",
            "a | sudo -ubob grep -q foo",
            "a | sudo -Eu bob -- grep --quiet foo",
            "a | sudo -En -g staff -u bob grep -q foo",
            "find . | xargs -0n1 grep -q foo",
            "find . | xargs -0I{} grep -q foo {}",
            "find . | xargs -0I {} grep -q foo {}",
            # timeout DURATION [options] (#223 nit).
            "a | timeout 5 grep -q foo",
            "a | timeout 5s head -n1",
            "a | timeout -s KILL 5 grep -q foo",
            "a | timeout -k 2 10 grep -q foo",
            "a | timeout -sKILL -k2 10 grep -q foo",
            "a | timeout --signal=TERM --kill-after 3 1m grep -q foo",
            "a | timeout --foreground --preserve-status 5 head -1",
            "a | timeout -v 5 sudo -Eu bob grep -q foo",
            "a | gtimeout 5 grep -q foo",
            "a | sudo -u bob timeout 5 grep -q foo",
            # env -S 'command line' (#223 nit).
            "a | env -S 'grep -q foo'",
            'a | env -S"grep -q foo"',
            "a | env -S 'LC_ALL=C grep -q foo'",
            "a | env --split-string='head -n1'",
            "a | env --split-string 'grep -m1 foo'",
            "a | env -i -S 'grep -q foo'",
            "a | env -S grep -q foo",
            # Quote-aware splitting keeps real pipes.
            'out="$(a | grep -q y)"',
            "out=`a | head -1`",
            "echo 'x|y' | grep -q y",
            'a | grep -q "x|y"',
            "a | grep -q 'x;y'",
            "printf '%s|' a | head -c1",
        )
        for line in bad:
            with self.subTest(line=line):
                self.assertTrue(early_exit_in(line), line)
        self.assertTrue(early_exit_in("  grep -q foo", continued=True))
        self.assertTrue(early_exit_in("  sudo -u bob grep -q foo", continued=True))
        self.assertTrue(early_exit_in("  grep -q foo && echo ok", continued=True))

    def test_unbalanced_quotes_fall_back_to_plain_splitting(self):
        """shlex cannot parse a segment with an unclosed quote: the line is
        split on every `|` and words on whitespace with quotes removed. Known
        limitation: a `|` inside such a quote is cut into a new segment (more
        segments, never fewer)."""
        self.assertIsNone(_shell_cuts("echo it's | grep -q x", False))
        self.assertEqual(split_pipes("echo it's | grep -q x"), ["echo it's ", " grep -q x"])
        self.assertEqual(_words(" 'grep -q x"), ["grep", "-q", "x"])
        self.assertEqual(_words(' "head -n1'), ["head", "-n1"])
        self.assertEqual(_words(" g\"rep -q x"), ["grep", "-q", "x"])
        fallback_bad = (
            "echo it's | grep -q x",            # apostrophe in prose
            "printf 'a | grep -q x",            # unclosed quote, cut anyway
            'a | "grep -q x',                   # unclosed quote around the name
            "a | 'head -n1",
            "a | env -S 'grep -q x",
            "don't do this: tar -tzf x | grep -qx f",
        )
        for line in fallback_bad:
            with self.subTest(line=line):
                self.assertTrue(early_exit_in(line), line)
        fallback_good = (
            "echo it's | grep -c x",
            "printf 'a | wc -l",
            'a | "sort -u',
        )
        for line in fallback_good:
            with self.subTest(line=line):
                self.assertFalse(early_exit_in(line), line)

    def test_split_pipes_is_quote_aware(self):
        self.assertEqual(split_pipes("grep -E 'a|b' f"), ["grep -E 'a|b' f"])
        self.assertEqual(split_pipes('a | grep "x|y" | wc -l'), ["a ", ' grep "x|y" ', " wc -l"])
        self.assertEqual(split_pipes("a |& b || c"), ["a ", " b || c"])
        self.assertEqual(split_pipes('o="$(a | b)"'), ['o="$(a ', ' b)"'])
        self.assertEqual(_cut_segment(" grep -q 'a;b' && c"), " grep -q 'a;b' ")
        self.assertEqual(_cut_segment(' grep -q y)"'), " grep -q y")

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
            # Clustered sudo options, timeout and env -S with full readers.
            "a | sudo -Eu grep cat",
            "a | sudo -Eu bob grep -c foo",
            "a | sudo -nu head tee x",
            "find . | xargs -0n1 grep foo",
            "a | timeout 5 sort",
            "a | timeout 5 grep foo",
            "a | timeout -s KILL 5 cat",
            "a | timeout -k 2 10 wc -l",
            "a | timeout --signal head 5 cat",
            "a | timeout -s head 5 tee x",
            "a | env -S 'sort -u'",
            'a | env -S"wc -l"',
            "a | env --split-string='grep -c foo'",
            "a | env -u head cat",
            # Quoted pipes are not pipes.
            "grep -E 'a|b' f",
            "grep 'x| head' f",
            'grep "x| grep -q y" f',
            "awk -F'|' '{print $1}' f",
            "grep -q 'x;y | head' f",
            'out="$(a | grep -c y)"',
        )
        for line in good:
            with self.subTest(line=line):
                self.assertFalse(early_exit_in(line), line)


if __name__ == "__main__":
    unittest.main()
