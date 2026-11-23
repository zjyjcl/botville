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


if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    unittest.main()
