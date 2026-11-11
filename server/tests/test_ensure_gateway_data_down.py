#!/usr/bin/env python3
"""ensure_gateway_data_volume + compose down -v (A2.2 / #189 follow-up).

The helper creates <project>_gateway-data with compose labels. Once a
service in the active project mounts that volume, `docker compose
--profile gateway down -v` must delete it — the same deletion path the
docs and CI describe. This test does not change the helper; it only
proves that relationship.

No gateway process is started: a throwaway compose file with the same
project name, volume key and mount is enough (compose down -v consults
the file + labels, not a running container). Skipped when docker compose
is unavailable. Standard library only.

Run:  python3 -m unittest discover -s server/tests -p "test_*.py" -v
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1]
LIB = SERVER_DIR / "scripts" / "lib.sh"

MINIMAL_COMPOSE = """
services:
  gateway:
    image: docker.io/library/python:3.12-slim
    profiles: [gateway]
    volumes:
      - gateway-data:/var/lib/botville-gateway
volumes:
  gateway-data:
"""

ENSURE = """#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${script_dir}/lib.sh"
ensure_gateway_data_volume
printf '%s\\n' "$(gateway_data_volume)"
"""


def compose_available():
    if not shutil.which("docker"):
        return False
    return subprocess.run(
        ["docker", "compose", "version"], capture_output=True
    ).returncode == 0


@unittest.skipUnless(compose_available(), "docker compose v2 not available")
class EnsureGatewayDataDownVTest(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="botville-ensure-down-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        # project_name() allows [a-z0-9][a-z0-9_-]*; keep it unique and
        # short so we never collide with a real checkout's project.
        self.project = "botville-edv{}".format(os.getpid())
        self.volume = self.project + "_gateway-data"
        scripts = tmp / "server" / "scripts"
        scripts.mkdir(parents=True)
        shutil.copy(LIB, scripts / "lib.sh")
        (scripts / "ensure.sh").write_text(ENSURE, encoding="utf-8")
        (scripts / "ensure.sh").chmod(0o755)
        compose_dir = tmp / "server" / "compose"
        compose_dir.mkdir(parents=True)
        (compose_dir / ".env").write_text(
            "COMPOSE_PROJECT_NAME={}\n".format(self.project), encoding="utf-8"
        )
        self.compose_file = tmp / "docker-compose.yml"
        self.compose_file.write_text(MINIMAL_COMPOSE, encoding="utf-8")
        self.ensure = scripts / "ensure.sh"
        self.addCleanup(self._cleanup_project)

    def _cleanup_project(self):
        # Only this test's project. down -v first; volume rm -f if anything
        # is somehow left. Never touches other compose projects.
        subprocess.run(
            [
                "docker", "compose", "-p", self.project,
                "-f", str(self.compose_file),
                "--profile", "gateway",
                "down", "-v", "--remove-orphans",
            ],
            capture_output=True, text=True, timeout=60,
        )
        subprocess.run(
            ["docker", "volume", "rm", "-f", self.volume],
            capture_output=True, text=True, timeout=30,
        )

    def _compose(self, *args):
        return subprocess.run(
            ["docker", "compose", "-p", self.project, "-f", str(self.compose_file), *args],
            capture_output=True, text=True, timeout=60,
        )

    def _volume_exists(self):
        return subprocess.run(
            ["docker", "volume", "inspect", self.volume],
            capture_output=True, timeout=30,
        ).returncode == 0

    def test_helper_created_volume_is_removed_by_down_v_when_mounted(self):
        proc = subprocess.run(
            ["bash", str(self.ensure)],
            capture_output=True, text=True, timeout=30,
            cwd=str(self.ensure.parent),
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.strip(), self.volume)
        self.assertTrue(self._volume_exists(), "helper did not create the volume")

        inspect = subprocess.run(
            ["docker", "volume", "inspect", self.volume, "--format",
             "{{index .Labels \"com.docker.compose.project\"}} "
             "{{index .Labels \"com.docker.compose.volume\"}}"],
            capture_output=True, text=True, check=True, timeout=30,
        ).stdout.strip()
        self.assertEqual(inspect, "{} gateway-data".format(self.project))

        # Without the gateway profile no service mounts the volume, so
        # down -v must leave it (the reason the helper exists at all).
        left = self._compose("down", "-v", "--remove-orphans")
        self.assertEqual(left.returncode, 0, left.stdout + left.stderr)
        self.assertTrue(
            self._volume_exists(),
            "down -v without --profile gateway deleted the volume unexpectedly",
        )

        # With the profile (service mounts gateway-data): down -v deletes it.
        # The service is never started; the compose file + labels are enough.
        gone = self._compose("--profile", "gateway", "down", "-v", "--remove-orphans")
        self.assertEqual(gone.returncode, 0, gone.stdout + gone.stderr)
        self.assertFalse(
            self._volume_exists(),
            "down -v with --profile gateway left {}".format(self.volume),
        )


if __name__ == "__main__":
    unittest.main()
