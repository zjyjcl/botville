#!/usr/bin/env python3
"""Regression tests for the compose NO_PROXY pinning (issue #134, PR1).

Root cause: the docker client (~/.docker/config.json "proxies") and Docker
Desktop inject the host's HTTP(S)_PROXY into every new container; inside a
container 127.0.0.1 is the container itself, so a host proxy like
http://127.0.0.1:7890 is unreachable and any *_PROXY-honoring client (the
Synapse pusher, reqwest in tuwunel/continuwuity) sends its http://sygnal:5000
traffic into a dead proxy.

The fix pins NO_PROXY + no_proxy (both cases) on every service of
server/compose/docker-compose.yml via the top-level x-noproxy-env anchor:
a fixed default list (all in-stack service names + loopback + RFC1918;
Twisted only matches hostnames, not CIDR, hence the explicit names) that
cannot be overridden — BOTVILLE_NO_PROXY_EXTRA only APPENDS (no trailing
comma when empty), and HTTP_PROXY/HTTPS_PROXY are never set or cleared by
compose. init.sh additionally WARNs (never fails) when a host proxy env
var or ~/.docker/config.json proxies entry points at loopback.

These tests render `docker compose config` against a hermetic temp env file
(no dependency on server/compose/.env, so they run in the CI lint job
before init.sh creates it; skipped when docker compose is unavailable) and
run init.sh in a disposable copy of server/ with a scrubbed environment and
an overridden HOME. Standard library only.

Run:  python3 -m unittest discover -s server/tests -p "test_*.py" -v
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from itertools import count
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "server" / "compose" / "docker-compose.yml"
SERVER_DIR = REPO_ROOT / "server"

# All profiles at once so one `compose config` renders every service
# (config never starts anything, so the profile port overlap is irrelevant).
ALL_PROFILES = [
    "--profile", "synapse",
    "--profile", "push",
    "--profile", "tuwunel",
    "--profile", "continuwuity",
]

# Every service the main compose file must merge x-noproxy-env into.
TARGET_SERVICES = ("postgres", "synapse", "tuwunel", "continuwuity", "sygnal")

# The fixed default list pinned in docker-compose.yml (x-noproxy-env). Keep
# in sync with the anchor; a change here must be a conscious test update.
EXPECTED_DEFAULT_ENTRIES = frozenset((
    "localhost", "127.0.0.1", "::1",
    "sygnal", "postgres", "synapse", "tuwunel", "continuwuity",
    # reserved names for services not yet in compose (gateway/admin) and
    # the federation stack's services (PR2 keeps the same list).
    "gateway", "admin",
    "synapse-a", "synapse-b", "pairing-a", "pairing-b",
    "fedgate-a", "fedgate-b",
    ".local", ".internal",
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
))

PROXY_ENV_VARS = (
    "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
    "ALL_PROXY", "all_proxy",
)


def scrub_proxy_env(env):
    """os.environ minus every proxy variable (deterministic init.sh runs)."""
    return {k: v for k, v in env.items() if k not in PROXY_ENV_VARS}


class ComposeNoProxyConfigTest(unittest.TestCase):
    """docker compose config renders NO_PROXY/no_proxy exactly as specified."""

    @classmethod
    def setUpClass(cls):
        if not shutil.which("docker"):
            raise unittest.SkipTest("docker not available")
        probe = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            text=True,
        )
        if probe.returncode != 0:
            raise unittest.SkipTest("docker compose v2 not available")
        cls._tmp = tempfile.TemporaryDirectory(prefix="bv-noproxy-")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls._env_dir = Path(cls._tmp.name)
        cls._seq = count()

    def _config(self, extra_value=None):
        """`docker compose config --format json` with a hermetic env file.

        `extra_value` controls BOTVILLE_NO_PROXY_EXTRA: None = the variable
        is absent from the env file entirely, "" = set but empty (exactly
        what .env.example ships), anything else = that value.
        """
        env_file = self._env_dir / f"env-{next(type(self)._seq)}.env"
        lines = [
            # The only variables compose requires (:?) — everything else
            # has in-file defaults.
            "POSTGRES_PASSWORD=test-only",
            "BOTVILLE_REGISTRATION_TOKEN=test-only",
        ]
        if extra_value is not None:
            lines.append(f"BOTVILLE_NO_PROXY_EXTRA={extra_value}")
        env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        proc = subprocess.run(
            [
                "docker", "compose",
                "-f", str(COMPOSE_FILE),
                "--env-file", str(env_file),
                *ALL_PROFILES,
                "config", "--format", "json",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            # hermetic interpolation: a BOTVILLE_NO_PROXY_EXTRA from the
            # caller's shell must not leak into the assertions
            env={k: v for k, v in os.environ.items()
                 if k != "BOTVILLE_NO_PROXY_EXTRA"},
            check=True,
        )
        return json.loads(proc.stdout)

    def _service_env(self, config, service):
        env = config["services"][service].get("environment")
        self.assertIsInstance(
            env, dict, f"{service}: environment is not a mapping: {env!r}"
        )
        return env

    def test_defaults_on_every_service(self):
        """Without BOTVILLE_NO_PROXY_EXTRA every target service carries
        NO_PROXY and no_proxy with exactly the fixed default list — which
        must cover sygnal and every service name in the compose file (a new
        service added without extending x-noproxy-env fails here)."""
        config = self._config()
        self.assertEqual(
            set(config["services"]),
            set(TARGET_SERVICES),
            "service set changed — update TARGET_SERVICES and the anchor",
        )
        for service in sorted(config["services"]):
            env = self._service_env(config, service)
            for var in ("NO_PROXY", "no_proxy"):
                self.assertIn(var, env, f"{service}: missing {var}")
                value = env[var]
                self.assertIsInstance(value, str)
                entries = value.split(",")
                self.assertNotIn(
                    "", entries, f"{service}: {var} has empty entries "
                    "(leading/trailing/double comma)"
                )
                self.assertEqual(
                    frozenset(entries), EXPECTED_DEFAULT_ENTRIES,
                    f"{service}: {var} != fixed default list",
                )
                self.assertIn("sygnal", entries)
                # consistency guard: every sibling service name is listed
                for name in config["services"]:
                    self.assertIn(name, entries, f"{service}: {var} lacks {name}")

    def test_extra_appends_after_defaults(self):
        """BOTVILLE_NO_PROXY_EXTRA appends — the value lands after a comma
        and every default entry survives (defaults are not overridable)."""
        config = self._config(extra_value="foo.example")
        for service in TARGET_SERVICES:
            env = self._service_env(config, service)
            for var in ("NO_PROXY", "no_proxy"):
                value = env[var]
                self.assertTrue(
                    value.endswith(",foo.example"),
                    f"{service}: {var} should end with ',foo.example': {value!r}",
                )
                entries = frozenset(value.split(","))
                self.assertTrue(
                    EXPECTED_DEFAULT_ENTRIES <= entries,
                    f"{service}: {var} lost default entries: {value!r}",
                )

    def test_empty_extra_matches_defaults(self):
        """BOTVILLE_NO_PROXY_EXTRA set but empty (what .env.example ships)
        renders the bare default list — never a trailing comma."""
        config = self._config(extra_value="")
        for service in TARGET_SERVICES:
            env = self._service_env(config, service)
            for var in ("NO_PROXY", "no_proxy"):
                value = env[var]
                self.assertFalse(
                    value.endswith(","),
                    f"{service}: {var} has a trailing comma: {value!r}",
                )
                self.assertEqual(frozenset(value.split(",")), EXPECTED_DEFAULT_ENTRIES)

    def test_compose_never_sets_or_clears_http_proxy(self):
        """compose must not set (or clear) HTTP(S)_PROXY itself: outbound
        proxying is a deliberate deployment choice, not the stack's."""
        for config in (self._config(), self._config(extra_value="foo.example")):
            for service in TARGET_SERVICES:
                env = self._service_env(config, service)
                for var in PROXY_ENV_VARS:
                    self.assertNotIn(
                        var, env, f"{service}: compose must not set {var}"
                    )


class InitLoopbackProxyWarningTest(unittest.TestCase):
    """init.sh warns about loopback host proxies but always exits 0.

    Each case runs init.sh in a disposable copy of server/ (no `generated/`,
    no `secrets/`, no .env — fresh bootstrap) with HOME overridden so the
    real ~/.docker/config.json cannot leak in, and proxy vars scrubbed from
    the environment unless the case sets its own. Nothing touches Docker.
    """

    @classmethod
    def setUpClass(cls):
        if not shutil.which("bash"):
            raise unittest.SkipTest("bash not available")

    def _run_init(self, tmp, extra_env=None, config_json=None):
        """Copy server/ into tmp, optional ~/.docker/config.json, run
        init.sh; returns the CompletedProcess."""
        server_dst = shutil.copytree(
            SERVER_DIR,
            Path(tmp) / "server",
            ignore=shutil.ignore_patterns(
                "generated", "secrets", ".env", "__pycache__", "*.pyc",
            ),
        )
        home = Path(tmp) / "home"
        home.mkdir()
        if config_json is not None:
            docker_dir = home / ".docker"
            docker_dir.mkdir()
            (docker_dir / "config.json").write_text(
                json.dumps(config_json), encoding="utf-8"
            )
        env = scrub_proxy_env(os.environ)
        env["HOME"] = str(home)
        env.update(extra_env or {})
        return subprocess.run(
            ["bash", str(server_dst / "scripts" / "init.sh")],
            capture_output=True,
            text=True,
            env=env,
        )

    def test_loopback_env_proxies_warn_but_exit_zero(self):
        with tempfile.TemporaryDirectory(prefix="bv-init-proxy-") as tmp:
            proc = self._run_init(
                tmp,
                extra_env={
                    "HTTP_PROXY": "http://127.0.0.1:7890",
                    "https_proxy": "http://localhost:7890",
                },
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("HTTP_PROXY points at the loopback proxy 127.0.0.1:7890", proc.stderr)
        self.assertIn("https_proxy points at the loopback proxy localhost:7890", proc.stderr)
        self.assertIn("host.docker.internal", proc.stderr)

    def test_loopback_proxy_credentials_are_stripped(self):
        """The warning prints host:port only — userinfo never reaches the
        log (proxy URLs can carry user:password)."""
        secret = "hunter2-password"
        with tempfile.TemporaryDirectory(prefix="bv-init-creds-") as tmp:
            proc = self._run_init(
                tmp,
                extra_env={
                    "HTTP_PROXY": f"http://user:{secret}@127.0.0.1:7890",
                },
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("loopback proxy 127.0.0.1:7890", proc.stderr)
        self.assertNotIn(secret, proc.stdout + proc.stderr)
        self.assertNotIn("user@", proc.stdout + proc.stderr)

    def test_docker_config_json_loopback_proxy_warns(self):
        config_json = {
            "proxies": {
                "default": {"httpsProxy": "http://127.0.0.1:7890"},
            }
        }
        with tempfile.TemporaryDirectory(prefix="bv-init-cfg-") as tmp:
            proc = self._run_init(tmp, config_json=config_json)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(
            "~/.docker/config.json proxies.default.httpsProxy points at the "
            "loopback proxy 127.0.0.1:7890",
            proc.stderr,
        )

    def test_non_loopback_and_clean_hosts_stay_silent(self):
        """A reachable proxy address (or no proxy at all) is none of
        init.sh's business — no loopback warning."""
        for extra_env, config_json in (
            ({"HTTP_PROXY": "http://proxy.corp.example:3128"}, None),
            (None, {"proxies": {"default": {"httpProxy": "http://10.0.0.9:7890"}}}),
            (None, None),
        ):
            with tempfile.TemporaryDirectory(prefix="bv-init-clean-") as tmp:
                proc = self._run_init(tmp, extra_env=extra_env, config_json=config_json)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn("loopback proxy", proc.stderr)


if __name__ == "__main__":
    unittest.main()
