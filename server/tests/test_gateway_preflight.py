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

    def test_env_file_parsed_like_compose(self):
        """Quotes, inline comments, `export`, blanks around `=` and CRLF are
        read the way compose's dotenv parser reads them (checked against
        `docker compose config`)."""
        with tempfile.TemporaryDirectory() as d:
            for text in (
                'BOTVILLE_GATEWAY_PERSONAS_DIR="{}"\n',
                "BOTVILLE_GATEWAY_PERSONAS_DIR='{}'\n",
                "BOTVILLE_GATEWAY_PERSONAS_DIR={} # my personas\n",
                'BOTVILLE_GATEWAY_PERSONAS_DIR="{}"   # quoted + comment\n',
                "export BOTVILLE_GATEWAY_PERSONAS_DIR={}\n",
                "  export   BOTVILLE_GATEWAY_PERSONAS_DIR = {}  \n",
                "BOTVILLE_GATEWAY_PERSONAS_DIR={}\r\n",
                "BOTVILLE_GATEWAY_PERSONAS_DIR=/nonexistent/a\nexport BOTVILLE_GATEWAY_PERSONAS_DIR='{}'\n",
            ):
                with self.subTest(text=text):
                    proc = run(text.format(d))
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertEqual(proc.stdout.strip(),
                                     "gateway-preflight: OK (personas dir {})".format(d))
            # A quoted missing path fails with the unquoted path in the message.
            proc = run('export BOTVILLE_GATEWAY_PERSONAS_DIR="/nonexistent/q" # c\n')
            self.assertEqual(proc.returncode, 2)
            self.assertIn("BOTVILLE_GATEWAY_PERSONAS_DIR=/nonexistent/q is not", proc.stderr)
            # Not the key: a longer key with the same prefix, or a commented line.
            proc = run("BOTVILLE_GATEWAY_PERSONAS_DIR_X=/nonexistent/z\n"
                       "# BOTVILLE_GATEWAY_PERSONAS_DIR=/nonexistent/y\n")
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_personas_file_parsed_like_compose(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "personas.json").write_text("{}", encoding="utf-8")
            proc = run(("BOTVILLE_GATEWAY_PERSONAS_DIR={}\n"
                        'export GATEWAY_PERSONAS_FILE="/etc/botville-gateway/personas.json" # real\n').format(d))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn("WARNING", proc.stderr)

    def test_never_prints_other_env_values(self):
        sentinel = "sentinel-model-key-must-not-print-0123456789"
        emb = "sentinel-embedding-key-must-not-print-0123456789"
        for tail in ("BOTVILLE_GATEWAY_PERSONAS_DIR=/nonexistent/x\n", ""):
            proc = run("GATEWAY_MODEL_API_KEY={}\nGATEWAY_EMBEDDING_API_KEY={}\n{}".format(
                sentinel, emb, tail))
            self.assertNotIn(sentinel, proc.stdout + proc.stderr)
            self.assertNotIn(emb, proc.stdout + proc.stderr)


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

    def test_config_writes_nothing(self):
        """`make config` only validates: no init.sh (which may append to .env
        or render generated/)."""
        lines = self.dry("config").splitlines()
        self.assertFalse([l for l in lines if "init.sh" in l and not l.startswith("test -f ")],
                         "make config must not run init.sh")
        self.assertNotIn("init.sh", self.dry("config").split("exit 1; }", 1)[1])

    def test_config_without_env_file_fails_without_creating_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "no.env"
            proc = subprocess.run(["make", "-s", "-C", str(REPO_ROOT), "config",
                                   "ENV_FILE={}".format(missing), "PROJECT=botville-dryrun"],
                                  stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("not found", proc.stderr)
            self.assertFalse(missing.exists())
            self.assertEqual(list(Path(tmp).iterdir()), [])



def compose_available():
    try:
        proc = subprocess.run(["docker", "compose", "version"], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", "node_modules", ".build", "DerivedData"}


def snapshot(*roots):
    """{path: (size, mtime_ns, mode)} of every file/dir under roots (VCS and
    bytecode caches excluded), plus each root itself."""
    state = {}
    for root in roots:
        root = Path(root)
        st = root.stat()
        state[str(root)] = (None, st.st_mtime_ns, st.st_mode)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in dirnames + filenames:
                full = os.path.join(dirpath, name)
                try:
                    st = os.lstat(full)
                except FileNotFoundError:
                    continue
                state[full] = (st.st_size if not os.path.isdir(full) else None,
                               st.st_mtime_ns, st.st_mode)
    return state


@unittest.skipUnless(compose_available(), "docker compose v2 not available")
class ConfigAndPreflightWriteNothingE2ETest(unittest.TestCase):
    """#206 nit: with the real docker compose, `make config` and
    gateway-preflight.sh leave the working tree (and the env file's directory)
    byte-for-byte unchanged: no init.sh, no .env append, no generated/."""

    def diff(self, before, after):
        added = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))
        changed = sorted(k for k in set(before) & set(after) if before[k] != after[k])
        return added, removed, changed

    def test_make_config_and_preflight_write_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "env"
            example = REPO_ROOT / "server" / "compose" / ".env.example"
            env_file.write_text(example.read_text(encoding="utf-8")
                                + "COMPOSE_PROJECT_NAME=botville-nowrite\n", encoding="utf-8")
            os.chmod(env_file, 0o600)
            before = snapshot(REPO_ROOT, tmp)
            for cmd in (
                ["make", "-s", "-C", str(REPO_ROOT), "config", "ENV_FILE={}".format(env_file),
                 "PROJECT=botville-nowrite"],
                ["make", "-s", "-C", str(REPO_ROOT), "gateway-preflight",
                 "ENV_FILE={}".format(env_file)],
                ["bash", str(SCRIPT), str(env_file)],
            ):
                with self.subTest(cmd=cmd[3] if cmd[0] == "make" else "preflight"):
                    proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                                          text=True, timeout=180)
                    self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            after = snapshot(REPO_ROOT, tmp)
            added, removed, changed = self.diff(before, after)
            self.assertEqual((added, removed, changed), ([], [], []),
                             "make config / preflight touched the disk")
            # Nothing was created in docker either.
            vols = subprocess.run(
                ["docker", "volume", "ls", "-q", "--filter",
                 "label=com.docker.compose.project=botville-nowrite"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
            self.assertEqual(vols.stdout.strip(), "")

if __name__ == "__main__":
    unittest.main()
