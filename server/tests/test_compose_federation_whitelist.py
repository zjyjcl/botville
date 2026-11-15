"""Static wiring of the A-R2 homeserver whitelist in the federation stack.

Text-level checks (no Docker, no PyYAML): each synapse-X loads pairing's
include as a second --config-path from a read-only volume and waits for
pairing-X; pairing-X writes it; nothing gets a docker socket.
"""

import os
import re
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
COMPOSE = os.path.join(REPO_ROOT, "server", "compose", "federation",
                       "docker-compose.federation.yml")
INCLUDE = "/hs/federation-whitelist.yaml"


def service_block(text, name):
    match = re.search(r"^  {}:\n(.*?)(?=^  \S|^\S)".format(re.escape(name)),
                      text, re.S | re.M)
    assert match, name
    return match.group(1)


class FederationWhitelistComposeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(COMPOSE, encoding="utf-8") as handle:
            cls.text = handle.read()

    def test_synapse_loads_include_read_only_after_pairing(self):
        for side in "ab":
            block = service_block(self.text, "synapse-" + side)
            with self.subTest(side=side):
                self.assertEqual(block.count("--config-path " + INCLUDE), 1)
                # Both the --generate-keys run and the real one see
                # homeserver.yaml; the long-running exec must load the include.
                exec_line = block[block.index("exec python"):]
                self.assertIn("--config-path " + INCLUDE, exec_line)
                self.assertIn("- fed-hs-{}-whitelist:/hs:ro".format(side), block)
                self.assertRegex(block, r"depends_on:\n\s+pairing-{}:\n\s+condition: service_healthy".format(side))

    def test_pairing_renders_into_the_same_volume(self):
        for side in "ab":
            block = service_block(self.text, "pairing-" + side)
            with self.subTest(side=side):
                self.assertIn("PAIRING_HS_WHITELIST_PATH: " + INCLUDE, block)
                self.assertRegex(block, r"- fed-hs-{}-whitelist:/hs\n".format(side))
        for side in "ab":
            self.assertRegex(self.text, r"\n  fed-hs-{}-whitelist:\n".format(side))

    def test_no_docker_socket_anywhere(self):
        # Restarts are an operator/supervisor job; no container may hold
        # the docker API (A-R2 decision).
        self.assertNotIn("docker.sock", self.text)

    def test_generated_homeserver_yaml_sets_no_whitelist(self):
        # The include is loaded last and would win anyway; a second copy in
        # homeserver.yaml would only confuse operators.
        path = os.path.join(REPO_ROOT, "server", "scripts", "federation-setup.py")
        with open(path, encoding="utf-8") as handle:
            setup = handle.read()
        template = setup[setup.index('HOMESERVER_TEMPLATE = """'):]
        template = template[:template.index('"""\n', 30)]
        self.assertNotRegex(template, r"(?m)^federation_domain_whitelist:")


if __name__ == "__main__":
    unittest.main()
