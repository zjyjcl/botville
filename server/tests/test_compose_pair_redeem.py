#!/usr/bin/env python3
"""Official compose wiring for optional pair-redeem (A-R7). Config only."""

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def compose_skip_reason():
    if not shutil.which("docker"):
        return "docker not available"
    probe = subprocess.run(
        ["docker", "compose", "version"],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
    if probe.returncode:
        return "docker compose v2 not available"
    return None


class ComposePairRedeemTests(unittest.TestCase):
    def rendered(self, profiles, **overrides):
        reason = compose_skip_reason()
        if reason:
            self.skipTest(reason)
        env = dict(os.environ)
        for key in (
            "BOTVILLE_PAIR_REDEEM_SYNAPSE_ADMIN_TOKEN",
            "BOTVILLE_PAIR_REDEEM_SYNAPSE_URL",
            "PAIRING_ADMIN_TOKEN",
        ):
            env.pop(key, None)
        env.update(overrides)
        cmd = [
            "docker", "compose", "-f", "server/compose/docker-compose.yml",
            "--env-file", "server/compose/.env.example",
        ]
        for profile in profiles:
            cmd.extend(["--profile", profile])
        cmd.extend(["config", "--format", "json"])
        result = subprocess.run(
            cmd, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
            capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0,
                         "compose config failed (details suppressed)")
        return json.loads(result.stdout)["services"]

    def test_default_off_does_not_render_pair_redeem(self):
        services = self.rendered(["synapse"])
        self.assertIn("synapse", services)
        self.assertNotIn("pair-redeem", services)

    def test_enabled_is_loopback_only_and_uses_internal_synapse_url(self):
        services = self.rendered(["synapse", "pair-redeem"])
        svc = services["pair-redeem"]
        ports = svc.get("ports") or []
        self.assertTrue(ports)
        self.assertTrue(all(p.get("host_ip") == "127.0.0.1" for p in ports))
        env = svc["environment"]
        self.assertEqual(env["BOTVILLE_PAIR_REDEEM_SYNAPSE_URL"],
                         "http://synapse:8008")
        self.assertEqual(env.get("BOTVILLE_PAIR_REDEEM_SYNAPSE_ADMIN_TOKEN") or "", "")
        self.assertEqual(env["BOTVILLE_PAIR_REDEEM_BIND"], "0.0.0.0:8092")

    def test_examples_document_empty_admin_token(self):
        for rel in (".env.example", "server/compose/.env.example"):
            values = {}
            for line in (ROOT / rel).read_text().splitlines():
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    values[key] = value
            self.assertEqual(values.get("BOTVILLE_PAIR_REDEEM_SYNAPSE_ADMIN_TOKEN"), "")
            self.assertEqual(values.get("BOTVILLE_PAIR_REDEEM_SYNAPSE_URL"),
                             "http://synapse:8008")
            self.assertEqual(values.get("BOTVILLE_PAIR_REDEEM_PORT"), "8092")
            self.assertNotIn("<change-me>",
                             values.get("BOTVILLE_PAIR_REDEEM_SYNAPSE_ADMIN_TOKEN", ""))

    def test_default_off_does_not_render_edge(self):
        services = self.rendered(["synapse"])
        self.assertNotIn("pair-redeem-edge", services)
        self.assertNotIn("pair-redeem", services)
        services = self.rendered(["synapse", "pair-redeem"])
        self.assertIn("pair-redeem", services)
        self.assertNotIn("pair-redeem-edge", services)

    def test_enabled_edge_is_loopback_only_and_allowlisted(self):
        services = self.rendered(["synapse", "pair-redeem", "pair-redeem-edge"])
        edge = services["pair-redeem-edge"]
        ports = edge.get("ports") or []
        self.assertTrue(ports)
        self.assertTrue(all(p.get("host_ip") == "127.0.0.1" for p in ports))
        self.assertTrue(all(int(p.get("target") or 0) == 8443 for p in ports))
        published = {int(p.get("published")) for p in ports}
        self.assertEqual(published, {8443})
        # Must not publish 80 or any other container port.
        self.assertTrue(all(int(p.get("published")) == 8443 for p in ports))
        image = edge.get("image") or ""
        self.assertIn("caddy:2.11.9-alpine@sha256:", image)
        self.assertIn(
            "003a97808e781aa9f7e75cc2f43d34d461099031faf5e20faefb9f61126836f0",
            image)

    def test_examples_document_edge_placeholders(self):
        for rel in (".env.example", "server/compose/.env.example"):
            values = {}
            for line in (ROOT / rel).read_text().splitlines():
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    values[key] = value
            self.assertEqual(values.get("BOTVILLE_PAIR_REDEEM_EDGE_PORT"), "8443")
            self.assertEqual(values.get("BOTVILLE_PAIR_REDEEM_EDGE_HOST"), "localhost")
            self.assertEqual(
                values.get("BOTVILLE_PAIR_REDEEM_EDGE_CERT_DIR"),
                "./generated/pair-redeem-edge")
            self.assertNotIn("example.com", values.get("BOTVILLE_PAIR_REDEEM_EDGE_HOST", ""))


class PairRedeemEdgeCaddyfileTests(unittest.TestCase):
    def setUp(self):
        self.text = (
            ROOT / "server" / "compose" / "pair-redeem-edge" / "Caddyfile"
        ).read_text(encoding="utf-8")

    def test_hardens_tls_and_admin(self):
        self.assertIn("auto_https off", self.text)
        self.assertIn("admin off", self.text)
        # No ACME / on-demand issuer configuration (comments may mention that
        # this cut will not request ACME certificates).
        self.assertNotRegex(self.text, r"(?i)^\s*(tls\s+acme|acme_ca|on_demand)\b")

    def test_exact_public_allowlist_only(self):
        self.assertIn(r"@public path_regexp ^/(?:_botville/pair/redeem|health)$", self.text)
        proxies = [ln.strip() for ln in self.text.splitlines() if "reverse_proxy" in ln]
        self.assertEqual(proxies, ["reverse_proxy pair-redeem:8092"])
        self.assertNotIn("synapse:", self.text)
        self.assertNotIn("gateway:", self.text)
        self.assertNotIn("postgres:", self.text)
        self.assertNotIn("/_synapse/", self.text)
        self.assertNotIn("/v1/pairing/redeem", self.text)
        self.assertNotIn("/_botville/gateway", self.text)

    def test_examples_stay_placeholder_domains(self):
        # Comments may mention example.org or warn that Cloudflare config
        # does not belong in the repository. Site addresses must not use a
        # real-looking public hostname.
        site_lines = [
            ln.strip() for ln in self.text.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        joined = "\n".join(site_lines)
        self.assertNotRegex(joined, r"[a-z0-9-]+\.(com|net|cn|io)\b")
        self.assertNotIn("example.com", joined)

    def test_logs_drop_auth_and_query(self):
        self.assertIn("request>headers>Authorization delete", self.text)
        self.assertIn("request>headers>Cookie delete", self.text)
        self.assertIn("request>uri query delete", self.text)




if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    unittest.main()
