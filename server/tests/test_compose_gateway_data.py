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
                self.fail(proc.stderr or proc.stdout or "compose failed")
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
        self.assertNotIn("GATEWAY_HS_TOKEN", env)
        self.assertNotIn("APPSERVICE_HS_TOKEN", env)
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
        stays memory. AS token is APPSERVICE_AS_TOKEN; no hs_token; federation
        URL left empty so OpenID userinfo falls back to the CS URL."""
        as_token = "test-only-as-token"
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
                ).format(hs=hs, transport=transport, token=as_token),
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
            self.assertNotIn("GATEWAY_HS_TOKEN", env)
            self.assertNotIn("APPSERVICE_HS_TOKEN", env)


class AppserviceRegistrationTemplateTest(unittest.TestCase):
    def test_registration_url_stays_null(self):
        tmpl = (
            REPO_ROOT / "server" / "compose" / "appservice"
            / "botville-gateway.yaml.tmpl"
        ).read_text(encoding="utf-8")
        self.assertRegex(tmpl, r"(?m)^url: null$")
        self.assertIn('as_token: "${APPSERVICE_AS_TOKEN}"', tmpl)
        # Comments may mention a future flip to http://gateway:<port>; the
        # active url field must stay null in this PR.
        active = [
            line.strip()
            for line in tmpl.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertFalse(
            any(line.startswith("url:") and "http://gateway" in line for line in active),
            active,
        )


if __name__ == "__main__":
    unittest.main()
