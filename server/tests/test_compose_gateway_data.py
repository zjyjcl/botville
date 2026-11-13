#!/usr/bin/env python3
"""Checks for the gateway compose service + gateway-data volume (A2.2 / #7).

Static checks read the compose file / gateway Dockerfile; the render checks
run `docker compose config` against a hermetic env file (skipped when docker
compose is unavailable, like test_compose_noproxy). Run with:
  python3 -m unittest discover -s server/tests -p "test_*.py" -v
"""

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "server" / "compose" / "docker-compose.yml"
DOCKERFILE = REPO_ROOT / "gateway" / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / "gateway" / ".dockerignore"
REQUIRED_ENV = "POSTGRES_PASSWORD=test-only\nBOTVILLE_REGISTRATION_TOKEN=test-only\n"


class ComposeGatewayDataVolumeTest(unittest.TestCase):
    def test_top_level_volume_declared(self):
        text = COMPOSE.read_text(encoding="utf-8")
        # Split on the top-level "volumes:" key at column 0 (service-level
        # volumes: are indented).
        marker = "\nvolumes:\n"
        self.assertIn(marker, text)
        block = text[text.rfind(marker):]
        self.assertRegex(block, r"(?m)^  gateway-data:\s*$")
        # Not marked external — compose should be able to create it.
        gateway_entry = block.split("gateway-data:", 1)[1]
        next_key = gateway_entry.split("\n")[1] if "\n" in gateway_entry else ""
        self.assertNotIn("external:", gateway_entry.split("\n")[0] + next_key)
        self.assertIn("/var/lib/botville-gateway", block)
        # Docs in the comment must not claim make down deletes volumes.
        self.assertIn("`make down` 不传 -v", block)


class GatewayDockerfileTest(unittest.TestCase):
    def setUp(self):
        self.text = DOCKERFILE.read_text(encoding="utf-8")

    def test_base_image_pinned(self):
        froms = re.findall(r"(?m)^FROM\s+(\S+)", self.text)
        self.assertEqual(len(froms), 1, froms)
        self.assertRegex(froms[0], r":[^:@\s]+@sha256:[0-9a-f]{64}$")
        self.assertNotIn(":latest", froms[0])

    def test_migrations_and_source_copied_in_repo_layout(self):
        # DEFAULT_MIGRATIONS_DIR = parents[3] of moments/migrations.py, so
        # src/ and migrations/ must be siblings under the same root.
        self.assertRegex(self.text, r"(?m)^WORKDIR /app/gateway$")
        self.assertRegex(self.text, r"(?m)^COPY migrations \./migrations$")
        self.assertRegex(self.text, r"(?m)^COPY src/botville_gateway \./src/botville_gateway$")
        self.assertIn("PYTHONPATH=/app/gateway/src", self.text)
        self.assertTrue((REPO_ROOT / "gateway" / "migrations" / "moments" / "0001_moments.sql").is_file())

    def test_non_root_with_data_dir_and_healthcheck(self):
        self.assertRegex(self.text, r"(?m)^USER 10001:10001$")
        self.assertIn("/var/lib/botville-gateway", self.text)
        self.assertRegex(self.text, r"(?m)^HEALTHCHECK ")
        self.assertIn("/health", self.text)

    def test_no_secrets_baked_in(self):
        env_lines = re.findall(r"(?ms)^ENV .*?(?<!\\)\n", self.text)
        joined = "".join(env_lines)
        for word in ("TOKEN", "SECRET", "PASSWORD", "APNS", "BUNDLE"):
            self.assertNotIn(word, joined)
        self.assertNotRegex(self.text, r"(?m)^(COPY|ADD) .*(\.env|\.p8|secrets)")
        ignore = DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        self.assertEqual(ignore[next(i for i, l in enumerate(ignore) if not l.startswith("#"))], "*")
        self.assertIn("**/.env", ignore)
        self.assertIn("**/*.p8", ignore)


def compose_available():
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "compose", "version"],
                          capture_output=True).returncode == 0


@unittest.skipUnless(compose_available(), "docker compose v2 not available")
class ComposeGatewayServiceRenderTest(unittest.TestCase):
    def compose(self, *args, extra_env=""):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "env"
            env_file.write_text(REQUIRED_ENV + extra_env, encoding="utf-8")
            proc = subprocess.run(
                ["docker", "compose", "-p", "botville-test", "-f", str(COMPOSE),
                 "--env-file", str(env_file), *args],
                cwd=REPO_ROOT, capture_output=True, text=True,
            )
            if proc.returncode != 0:
                # Never proc.stdout: rendered config holds interpolated values.
                self.fail(proc.stderr or "compose failed (exit {})".format(proc.returncode))
            return proc.stdout

    def test_gateway_service_shape(self):
        config = json.loads(self.compose("--profile", "gateway", "config", "--format", "json"))
        svc = config["services"]["gateway"]
        self.assertEqual(svc.get("profiles"), ["gateway"])
        self.assertEqual(Path(svc["build"]["context"]).resolve(), (REPO_ROOT / "gateway").resolve())
        self.assertEqual(svc["build"].get("dockerfile"), "Dockerfile")
        self.assertNotIn("image", svc)  # local build only; nothing is pulled
        env = svc["environment"]
        self.assertEqual(env["GATEWAY_MOMENTS_DB_PATH"], "/var/lib/botville-gateway/moments.db")
        self.assertEqual(env["GATEWAY_HOST"], "0.0.0.0")
        self.assertEqual(env["GATEWAY_MATRIX_TRANSPORT"], "memory")
        # Default URL uses the compose service name, never 127.0.0.1.
        self.assertEqual(env["GATEWAY_HOMESERVER_URL"], "http://synapse:8008")
        self.assertEqual(env.get("GATEWAY_HOMESERVER_FEDERATION_URL") or "", "")
        self.assertEqual(env["GATEWAY_MATRIX_SERVER_NAME"], "localhost")
        self.assertEqual(env.get("GATEWAY_AS_TOKEN") or "", "")
        # #192 wiring: hs_token injected from APPSERVICE_HS_TOKEN (empty
        # fallback = AS callbacks 403); raw transactions on the volume.
        self.assertEqual(env.get("GATEWAY_HS_TOKEN") or "", "")
        self.assertNotIn("APPSERVICE_HS_TOKEN", env)
        self.assertEqual(
            env["GATEWAY_AS_TXN_DB_PATH"], "/var/lib/botville-gateway/as_transactions.db"
        )
        # Top level of the gateway-data mount, so backups checkpoint it.
        self.assertEqual(
            str(Path(env["GATEWAY_AS_TXN_DB_PATH"]).parent), "/var/lib/botville-gateway"
        )
        # Secrets are interpolated from .env only; empty here (no defaults).
        self.assertEqual(env["GATEWAY_AUTH_TOKEN"], "")
        self.assertEqual(env["GATEWAY_HMAC_SECRET"], "")
        depends = svc.get("depends_on") or {}
        for name in ("synapse", "tuwunel", "continuwuity"):
            self.assertIn(name, depends, name)
            self.assertIs(depends[name].get("required"), False)
        self.assertEqual(depends["synapse"].get("condition"), "service_healthy")
        mounts = [v for v in svc["volumes"] if v["type"] == "volume"]
        self.assertEqual(len(mounts), 1)
        self.assertEqual(mounts[0]["source"], "gateway-data")
        self.assertEqual(mounts[0]["target"], "/var/lib/botville-gateway")
        self.assertEqual(len(svc["ports"]), 1)
        port = svc["ports"][0]
        self.assertEqual(port["host_ip"], "127.0.0.1")
        self.assertEqual(str(port["published"]), "8020")
        self.assertEqual(port["target"], 8080)
        self.assertIn("healthcheck", svc)

    def test_config_volumes_lists_gateway_data_only_when_enabled(self):
        enabled = self.compose("--profile", "synapse", "--profile", "gateway",
                               "config", "--volumes").split()
        self.assertIn("gateway-data", enabled)
        for hs in ("tuwunel", "continuwuity"):
            self.assertIn("gateway-data", self.compose(
                "--profile", hs, "--profile", "gateway", "config", "--volumes").split())
        # Without the gateway profile no service uses it -> not listed (and
        # `down -v` without --profile gateway leaves it; documented).
        disabled = self.compose("--profile", "synapse", "config", "--volumes").split()
        self.assertNotIn("gateway-data", disabled)

    def test_gateway_not_in_default_homeserver_profiles(self):
        for hs in ("synapse", "tuwunel", "continuwuity"):
            services = self.compose("--profile", hs, "config", "--services").split()
            self.assertNotIn("gateway", services, hs)

    def test_transport_and_url_follow_homeserver_profile(self):
        """synapse/tuwunel get appservice + http://<service>:8008; continuwuity
        stays memory. AS/HS tokens are APPSERVICE_AS_TOKEN/APPSERVICE_HS_TOKEN
        (same values, different from each other); federation URL left empty
        so OpenID userinfo falls back to the CS URL."""
        as_token = "test-only-as-token"
        hs_token = "test-only-hs-token"
        for hs, transport in (
            ("synapse", "appservice"),
            ("tuwunel", "appservice"),
            ("continuwuity", "memory"),
        ):
            config = json.loads(self.compose(
                "--profile", hs, "--profile", "gateway",
                "config", "--format", "json",
                extra_env=(
                    "BOTVILLE_HOMESERVER={hs}\n"
                    "BOTVILLE_SERVER_NAME=localhost\n"
                    "GATEWAY_MATRIX_TRANSPORT={transport}\n"
                    "APPSERVICE_AS_TOKEN={token}\n"
                    "APPSERVICE_HS_TOKEN={hs_token}\n"
                ).format(hs=hs, transport=transport, token=as_token, hs_token=hs_token),
            ))
            env = config["services"]["gateway"]["environment"]
            self.assertEqual(env["GATEWAY_MATRIX_TRANSPORT"], transport, hs)
            self.assertEqual(
                env["GATEWAY_HOMESERVER_URL"],
                "http://{}:8008".format(hs),
                hs,
            )
            self.assertEqual(env.get("GATEWAY_HOMESERVER_FEDERATION_URL") or "", "")
            self.assertEqual(env["GATEWAY_MATRIX_SERVER_NAME"], "localhost")
            self.assertEqual(env["GATEWAY_AS_TOKEN"], as_token)
            self.assertEqual(env["GATEWAY_HS_TOKEN"], hs_token)
            self.assertNotEqual(env["GATEWAY_HS_TOKEN"], env["GATEWAY_AS_TOKEN"])
            self.assertNotIn("APPSERVICE_HS_TOKEN", env)


PERSONAS_DIR = REPO_ROOT / "server" / "compose" / "gateway-personas"
MODEL_KEYS = (
    "GATEWAY_MODEL_BASE_URL",
    "GATEWAY_MODEL_NAME",
    "GATEWAY_MODEL_API_KEY",
    "GATEWAY_MODEL_TIMEOUT_SECONDS",
    "GATEWAY_PERSONAS_FILE",
    "GATEWAY_CONSUMER_ENABLED",
    "GATEWAY_MEMORY_DB_PATH",
    "GATEWAY_MEMORY_RETENTION_DAYS",
    "GATEWAY_MEMORY_PURGE_INTERVAL_SECONDS",
    "GATEWAY_MEMORY_SUMMARY_MODEL",
    "GATEWAY_EMBEDDING_BASE_URL",
    "GATEWAY_EMBEDDING_MODEL",
    "GATEWAY_EMBEDDING_API_KEY",
    "GATEWAY_EMBEDDING_DIM",
    "GATEWAY_MEMORY_VECTOR_BACKEND",
    "GATEWAY_MEMORY_OWNER_CAN_READ_DM",
)


class GatewayMemoryEnvStaticTest(unittest.TestCase):
    """B line PR3a memory vars: compose defaults, .env.example and the names
    gateway/src actually reads stay in sync (no docker needed)."""

    EXPECTED = {
        "GATEWAY_MEMORY_DB_PATH": "",
        "GATEWAY_MEMORY_RETENTION_DAYS": "90",
        "GATEWAY_MEMORY_PURGE_INTERVAL_SECONDS": "86400",
        # B line PR3b (defaults as in config.py load_config).
        "GATEWAY_MEMORY_SUMMARY_MODEL": "",
        "GATEWAY_EMBEDDING_BASE_URL": "",
        "GATEWAY_EMBEDDING_MODEL": "",
        "GATEWAY_EMBEDDING_API_KEY": "",
        "GATEWAY_EMBEDDING_DIM": "0",
        "GATEWAY_MEMORY_VECTOR_BACKEND": "python",
        # B line PR3d (#211).
        "GATEWAY_MEMORY_OWNER_CAN_READ_DM": "false",
    }

    def test_compose_passes_through_with_defaults(self):
        text = COMPOSE.read_text(encoding="utf-8")
        for key, default in self.EXPECTED.items():
            self.assertIn("      {0}: ${{{0}:-{1}}}\n".format(key, default), text, key)

    def test_env_example_mirrors_defaults(self):
        lines = (REPO_ROOT / "server" / "compose" / ".env.example").read_text(
            encoding="utf-8").splitlines()
        for key, default in self.EXPECTED.items():
            self.assertIn("{}={}".format(key, default), lines, key)

    def test_names_and_defaults_match_gateway_src(self):
        src = (REPO_ROOT / "gateway" / "src" / "botville_gateway" / "config.py").read_text(
            encoding="utf-8")
        for key in self.EXPECTED:
            self.assertIn('"{}"'.format(key), src, key + " is not read by gateway/src")
        self.assertRegex(src, r"DEFAULT_MEMORY_RETENTION_DAYS\s*=\s*90\b")
        self.assertRegex(src, r"DEFAULT_MEMORY_PURGE_INTERVAL_SECONDS\s*=\s*86_?400\b")
        # PR3b defaults, read straight from load_config's _env_* calls.
        for key in ("GATEWAY_MEMORY_SUMMARY_MODEL", "GATEWAY_EMBEDDING_BASE_URL",
                    "GATEWAY_EMBEDDING_MODEL", "GATEWAY_EMBEDDING_API_KEY"):
            self.assertRegex(src, r'_env_str\(\s*source,\s*"{}",\s*""\s*\)'.format(key), key)
        self.assertRegex(src, r'_env_int\(\s*source,\s*"GATEWAY_EMBEDDING_DIM",\s*0\b')
        self.assertRegex(src, r'"GATEWAY_MEMORY_VECTOR_BACKEND",\s*"python"')
        self.assertIn('MEMORY_VECTOR_BACKENDS = frozenset({"none", "python", "sqlite-vec"})', src)
        # PR3d: strict boolean via _env_bool, default False.
        self.assertRegex(src, r'_env_bool\(\s*source,\s*"GATEWAY_MEMORY_OWNER_CAN_READ_DM",\s*False\s*\)')
        self.assertRegex(src, r"memory_owner_can_read_dm:\s*bool\s*=\s*False")

    def test_owner_can_read_dm_is_a_strict_boolean_in_gateway_src(self):
        """Load _env_bool from config.py's source (no import of the package)
        and check the strict parsing the docs promise: empty = default,
        true/false (and 1/0, yes/no, on/off, any case) accepted, anything
        else refused."""
        import ast
        src = (REPO_ROOT / "gateway" / "src" / "botville_gateway" / "config.py").read_text(
            encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_env_bool")

        class ConfigError(Exception):
            pass

        ns = {"ConfigError": ConfigError, "Mapping": dict}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "config.py:_env_bool", "exec"), ns)
        env_bool = ns["_env_bool"]
        key = "GATEWAY_MEMORY_OWNER_CAN_READ_DM"
        self.assertIs(env_bool({}, key, False), False)
        self.assertIs(env_bool({key: ""}, key, False), False)
        self.assertIs(env_bool({key: "  "}, key, False), False)
        for raw in ("true", "TRUE", " True ", "1", "yes", "on"):
            self.assertIs(env_bool({key: raw}, key, False), True, raw)
        for raw in ("false", "False", "0", "no", "off"):
            self.assertIs(env_bool({key: raw}, key, False), False, raw)
        for raw in ("2", "enabled", "y", "t", "truee", "none"):
            with self.subTest(raw=raw):
                with self.assertRaises(ConfigError):
                    env_bool({key: raw}, key, False)
        # Empty summary model / embedding URL / key fall back to the chat model's.
        self.assertRegex(src, r'"GATEWAY_MEMORY_SUMMARY_MODEL", ""\)\s*or _env_str\(source, "GATEWAY_MODEL_NAME"')
        self.assertRegex(src, r'"GATEWAY_EMBEDDING_BASE_URL", ""\)\s*or _env_str\(source, "GATEWAY_MODEL_BASE_URL"')
        self.assertRegex(src, r'"GATEWAY_EMBEDDING_API_KEY", ""\)\s*or _env_str\(source, "GATEWAY_MODEL_API_KEY"')

    def test_ci_allowlist_and_docs_cover_all_names(self):
        ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        doc = (REPO_ROOT / "docs" / "deploy" / "appservice.md").read_text(encoding="utf-8")
        for key, default in self.EXPECTED.items():
            self.assertIn('"{}": "{}",'.format(key, default), ci, key)
            self.assertIn("`${{{0}:-{1}}}`".format(key, default), doc, key)
        # The privacy warning is in the fallback table and the memory section.
        self.assertGreaterEqual(doc.count("私聊正文会发给"), 1)
        self.assertIn("隐私：配置了 `GATEWAY_EMBEDDING_MODEL` 之后", doc)
        # PR3d disclosure note in the table row and the memory section.
        self.assertIn("App 会向聊天对方显示披露", doc)
        self.assertIn("主人可读私聊记忆：`GATEWAY_MEMORY_OWNER_CAN_READ_DM` 默认 `false`", doc)
        example = (REPO_ROOT / "server" / "compose" / ".env.example").read_text(encoding="utf-8")
        self.assertIn("App shows a disclosure", example)

    def test_suggested_path_is_top_level_of_gateway_data(self):
        """The suggested path sits in the gateway-data volume, whose *.db files
        (any depth) are checkpointed before the whole-volume tar."""
        text = COMPOSE.read_text(encoding="utf-8")
        self.assertIn("gateway-data:/var/lib/botville-gateway", text)
        lib = (REPO_ROOT / "server" / "scripts" / "lib.sh").read_text(encoding="utf-8")
        self.assertIn('find /gateway-data -type f -name "*.db"', lib)
        self.assertIn("tar -czf /backup/gateway-data.tar.gz -C /gateway-data .", lib)


class GatewayPersonasDirTest(unittest.TestCase):
    def test_repo_ships_only_example_and_readme(self):
        """The bind-mount source always exists (so an unconfigured deployment
        mounts it without error) and only holds the example + README."""
        self.assertTrue(PERSONAS_DIR.is_dir())
        tracked = subprocess.run(
            ["git", "ls-files", str(PERSONAS_DIR.relative_to(REPO_ROOT))],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        if tracked.returncode == 0 and tracked.stdout.strip():
            names = sorted(Path(line).name for line in tracked.stdout.split())
            self.assertEqual(names, ["README.md", "personas.example.json"])
        json.loads((PERSONAS_DIR / "personas.example.json").read_text(encoding="utf-8"))

    def test_real_personas_are_gitignored(self):
        if not shutil.which("git"):
            self.skipTest("git not available")
        for name in ("personas.json", "my-town.json"):
            proc = subprocess.run(
                ["git", "check-ignore", "-q",
                 "server/compose/gateway-personas/" + name],
                cwd=REPO_ROOT,
            )
            self.assertEqual(proc.returncode, 0, name + " must be gitignored")
        for name in ("personas.example.json", "README.md"):
            proc = subprocess.run(
                ["git", "check-ignore", "-q",
                 "server/compose/gateway-personas/" + name],
                cwd=REPO_ROOT,
            )
            self.assertEqual(proc.returncode, 1, name + " must stay tracked")


@unittest.skipUnless(compose_available(), "docker compose v2 not available")
class ComposeGatewayModelEnvTest(unittest.TestCase):
    """B line PR2 pass-through: names/defaults as agreed, the API key is
    interpolated from the env file only and never printed by `config --quiet`."""

    SENTINEL = "sentinel-model-key-must-not-print-0123456789"
    EMB_SENTINEL = "sentinel-embedding-key-must-not-print-0123456789"

    def run_compose(self, *args, extra_env=""):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "env"
            env_file.write_text(REQUIRED_ENV + extra_env, encoding="utf-8")
            return subprocess.run(
                ["docker", "compose", "-p", "botville-test", "-f", str(COMPOSE),
                 "--env-file", str(env_file), *args],
                cwd=REPO_ROOT, capture_output=True, text=True,
            )

    def gateway(self, extra_env=""):
        proc = self.run_compose("--profile", "gateway", "config", "--format", "json",
                                extra_env=extra_env)
        if proc.returncode != 0:
            self.fail("compose config failed (exit {})".format(proc.returncode))
        return json.loads(proc.stdout)["services"]["gateway"]

    def test_defaults_without_any_model_or_personas(self):
        svc = self.gateway()
        env = svc["environment"]
        for key in MODEL_KEYS:
            self.assertIn(key, env, key)
        self.assertEqual(env["GATEWAY_MODEL_BASE_URL"] or "", "")
        self.assertEqual(env["GATEWAY_MODEL_NAME"] or "", "")
        self.assertEqual(env["GATEWAY_MODEL_API_KEY"] or "", "")
        self.assertEqual(env["GATEWAY_MODEL_TIMEOUT_SECONDS"], "30")
        self.assertEqual(env["GATEWAY_PERSONAS_FILE"] or "", "")
        self.assertEqual(env["GATEWAY_CONSUMER_ENABLED"], "false")
        # Memory (B line PR3a): empty path = off; retention/purge defaults
        # mirror gateway/src config.py.
        self.assertEqual(env["GATEWAY_MEMORY_DB_PATH"] or "", "")
        self.assertEqual(env["GATEWAY_MEMORY_RETENTION_DAYS"], "90")
        self.assertEqual(env["GATEWAY_MEMORY_PURGE_INTERVAL_SECONDS"], "86400")
        # PR3b: empty = inherit / off; dim 0 = auto; backend python.
        for key in ("GATEWAY_MEMORY_SUMMARY_MODEL", "GATEWAY_EMBEDDING_BASE_URL",
                    "GATEWAY_EMBEDDING_MODEL", "GATEWAY_EMBEDDING_API_KEY"):
            self.assertEqual(env[key] or "", "", key)
        self.assertEqual(env["GATEWAY_EMBEDDING_DIM"], "0")
        self.assertEqual(env["GATEWAY_MEMORY_VECTOR_BACKEND"], "python")
        self.assertEqual(env["GATEWAY_MEMORY_OWNER_CAN_READ_DM"], "false")
        binds = [v for v in svc["volumes"] if v["type"] == "bind"]
        self.assertEqual(len(binds), 1)
        self.assertEqual(binds[0]["target"], "/etc/botville-gateway")
        self.assertTrue(binds[0].get("read_only"))
        self.assertEqual(Path(binds[0]["source"]).resolve(), PERSONAS_DIR.resolve())

    def test_values_pass_through(self):
        env = self.gateway(extra_env=(
            "GATEWAY_MODEL_BASE_URL=http://model.invalid/v1\n"
            "GATEWAY_MODEL_NAME=test-model\n"
            "GATEWAY_MODEL_API_KEY={0}\n"
            "GATEWAY_MODEL_TIMEOUT_SECONDS=12\n"
            "GATEWAY_PERSONAS_FILE=/etc/botville-gateway/personas.json\n"
            "GATEWAY_CONSUMER_ENABLED=true\n"
            "GATEWAY_MEMORY_DB_PATH=/var/lib/botville-gateway/memory.db\n"
            "GATEWAY_MEMORY_RETENTION_DAYS=30\n"
            "GATEWAY_MEMORY_PURGE_INTERVAL_SECONDS=3600\n"
            "GATEWAY_MEMORY_SUMMARY_MODEL=summary-model\n"
            "GATEWAY_EMBEDDING_BASE_URL=http://embed.invalid/v1\n"
            "GATEWAY_EMBEDDING_MODEL=embed-model\n"
            "GATEWAY_EMBEDDING_API_KEY={1}\n"
            "GATEWAY_EMBEDDING_DIM=384\n"
            "GATEWAY_MEMORY_VECTOR_BACKEND=sqlite-vec\n"
            "GATEWAY_MEMORY_OWNER_CAN_READ_DM=true\n"
        ).format(self.SENTINEL, self.EMB_SENTINEL))["environment"]
        self.assertEqual(env["GATEWAY_MEMORY_SUMMARY_MODEL"], "summary-model")
        self.assertEqual(env["GATEWAY_EMBEDDING_BASE_URL"], "http://embed.invalid/v1")
        self.assertEqual(env["GATEWAY_EMBEDDING_MODEL"], "embed-model")
        self.assertEqual(env["GATEWAY_EMBEDDING_API_KEY"], self.EMB_SENTINEL)
        self.assertEqual(env["GATEWAY_EMBEDDING_DIM"], "384")
        self.assertEqual(env["GATEWAY_MEMORY_VECTOR_BACKEND"], "sqlite-vec")
        self.assertEqual(env["GATEWAY_MEMORY_OWNER_CAN_READ_DM"], "true")
        self.assertEqual(env["GATEWAY_MODEL_BASE_URL"], "http://model.invalid/v1")
        self.assertEqual(env["GATEWAY_MODEL_NAME"], "test-model")
        self.assertEqual(env["GATEWAY_MODEL_API_KEY"], self.SENTINEL)
        self.assertEqual(env["GATEWAY_MODEL_TIMEOUT_SECONDS"], "12")
        self.assertEqual(env["GATEWAY_PERSONAS_FILE"], "/etc/botville-gateway/personas.json")
        self.assertEqual(env["GATEWAY_CONSUMER_ENABLED"], "true")
        self.assertEqual(env["GATEWAY_MEMORY_DB_PATH"], "/var/lib/botville-gateway/memory.db")
        self.assertEqual(env["GATEWAY_MEMORY_RETENTION_DAYS"], "30")
        self.assertEqual(env["GATEWAY_MEMORY_PURGE_INTERVAL_SECONDS"], "3600")

    def test_personas_dir_override(self):
        with tempfile.TemporaryDirectory() as other:
            svc = self.gateway(extra_env="BOTVILLE_GATEWAY_PERSONAS_DIR={}\n".format(other))
            binds = [v for v in svc["volumes"] if v["type"] == "bind"]
            self.assertEqual(Path(binds[0]["source"]).resolve(), Path(other).resolve())

    def test_quiet_config_never_prints_the_key(self):
        """CI and the docs only ever run `config --quiet` (non-quiet config
        interpolates values; documented as never-paste)."""
        extra = "GATEWAY_MODEL_API_KEY={}\nGATEWAY_EMBEDDING_API_KEY={}\n".format(
            self.SENTINEL, self.EMB_SENTINEL)
        for hs in ("synapse", "tuwunel", "continuwuity"):
            for profiles in ((), ("--profile", "push"), ("--profile", "gateway"),
                             ("--profile", "push", "--profile", "gateway")):
                with self.subTest(hs=hs, profiles=profiles):
                    proc = self.run_compose("--profile", hs, *profiles, "config", "--quiet",
                                            extra_env=extra)
                    self.assertEqual(proc.returncode, 0, "compose config --quiet failed")
                    self.assertNotIn(self.SENTINEL, proc.stdout + proc.stderr)
                    self.assertNotIn(self.EMB_SENTINEL, proc.stdout + proc.stderr)

    def test_key_is_not_in_other_services(self):
        proc = self.run_compose("--profile", "synapse", "--profile", "push",
                                "--profile", "gateway", "config", "--format", "json",
                                extra_env="GATEWAY_MODEL_API_KEY={}\nGATEWAY_EMBEDDING_API_KEY={}\n".format(
                                    self.SENTINEL, self.EMB_SENTINEL))
        self.assertEqual(proc.returncode, 0)
        services = json.loads(proc.stdout)["services"]
        for name, svc in services.items():
            if name == "gateway":
                continue
            self.assertNotIn(self.SENTINEL, json.dumps(svc), name)
            self.assertNotIn(self.EMB_SENTINEL, json.dumps(svc), name)


class AppserviceRegistrationTemplateTest(unittest.TestCase):
    def test_registration_url_is_rendered_by_init(self):
        """The url line is a placeholder init.sh fills with exactly `null` or
        `http://gateway:8080` (see InitScriptTest for the switch)."""
        tmpl = (
            REPO_ROOT / "server" / "compose" / "appservice"
            / "botville-gateway.yaml.tmpl"
        ).read_text(encoding="utf-8")
        self.assertRegex(tmpl, r"(?m)^url: \$\{BOTVILLE_APPSERVICE_URL\}$")
        self.assertIn('as_token: "${APPSERVICE_AS_TOKEN}"', tmpl)
        self.assertIn('hs_token: "${APPSERVICE_HS_TOKEN}"', tmpl)
        # No path prefix anywhere: callbacks live at the gateway root.
        self.assertNotIn("/_botville/gateway", tmpl)


if __name__ == "__main__":
    unittest.main()
