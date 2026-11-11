#!/usr/bin/env python3
"""Hermetic checks that compose declares the gateway-data volume (A2.2 / #7).

No Docker: just reads the compose file. Run with:
  python3 -m unittest discover -s server/tests -p "test_*.py" -v
"""

import unittest
from pathlib import Path

COMPOSE = Path(__file__).resolve().parents[1] / "compose" / "docker-compose.yml"


class ComposeGatewayDataVolumeTest(unittest.TestCase):
    def test_top_level_volume_declared(self):
        text = COMPOSE.read_text(encoding="utf-8")
        # Split on the top-level "volumes:" key at column 0 (service-level
        # volumes: are indented). Compose must declare gateway-data there so
        # backup/restore and a future gateway service share the same name.
        marker = "\nvolumes:\n"
        self.assertIn(marker, text)
        block = text[text.rfind(marker):]
        self.assertRegex(block, r"(?m)^  gateway-data:\s*$")
        # Not marked external — compose should be able to create it.
        gateway_entry = block.split("gateway-data:", 1)[1]
        next_key = gateway_entry.split("\n")[1] if "\n" in gateway_entry else ""
        self.assertNotIn("external:", gateway_entry.split("\n")[0] + next_key)
        self.assertIn("/var/lib/botville-gateway", block)


if __name__ == "__main__":
    unittest.main()
