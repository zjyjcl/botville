#!/usr/bin/env python3
"""Hermetic tests for backup-synapse.sh gateway-data handling (A2.2 / #7).

No Docker and no real homeserver: lib.sh / backup-synapse.sh run against a
sandboxed checkout with a fake `docker` and a fake COMPOSE prefix. Standard
library only.

Run:  python3 -m unittest discover -s server/tests -p "test_*.py" -v
"""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1]
LIB = SERVER_DIR / "scripts" / "lib.sh"
BACKUP = SERVER_DIR / "scripts" / "backup-synapse.sh"

FAKE_DOCKER = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${FAKE_DOCKER_LOG:?}"
if [ "$1" = volume ] && [ "$2" = inspect ]; then
    if [ "${FAKE_DOCKER_VOLUME_EXISTS:-0}" = 1 ]; then exit 0; else exit 1; fi
fi
exit 0
"""

# COMPOSE is invoked as: $COMPOSE up|exec|run|ps|stop|images ...
# The real backup bind-mounts BACKUP_DIR at /backup inside a container; the
# fake writes the archive into FAKE_BACKUP_DIR instead.
FAKE_COMPOSE = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${FAKE_COMPOSE_LOG:?}"
set -- "$@"
# pg_dump writes to stdout (the script redirects it into synapse-db.dump).
for arg in "$@"; do
    case "$arg" in
        *pg_dump*) echo "FAKE-PGDUMP"; exit 0 ;;
        *pg_isready*) exit 0 ;;
    esac
done
# compose run ... tar -czf /backup/<name>.tar.gz  -> write that artifact.
if [ "${1-}" = run ] || [[ "$*" == *'tar -czf /backup/'* ]]; then
    dest="${FAKE_BACKUP_DIR:?}"
    if [[ "$*" == *'gateway-data.tar.gz'* ]]; then
        printf 'fake-gateway-tar\\n' > "${dest}/gateway-data.tar.gz"
        chmod 600 "${dest}/gateway-data.tar.gz"
    elif [[ "$*" == *'synapse-data.tar.gz'* ]]; then
        printf 'fake-synapse-tar\\n' > "${dest}/synapse-data.tar.gz"
        chmod 600 "${dest}/synapse-data.tar.gz"
    fi
    exit 0
fi
# ps --quiet --status running synapse: empty output => treat as already down
# so the script will not try to start synapse again at the end.
exit 0
"""

ENSURE_DRIVER = """#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${script_dir}/lib.sh"
ensure_gateway_data_volume
"""


class BackupSynapseGatewayDataTest(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="botville-backup-test-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.tmp = tmp
        scripts = tmp / "server" / "scripts"
        scripts.mkdir(parents=True)
        shutil.copy(LIB, scripts / "lib.sh")
        shutil.copy(BACKUP, scripts / "backup-synapse.sh")
        self.backup = scripts / "backup-synapse.sh"
        (scripts / "ensure.sh").write_text(ENSURE_DRIVER, encoding="utf-8")
        (scripts / "ensure.sh").chmod(0o755)

        compose_dir = tmp / "server" / "compose"
        (compose_dir / "generated" / "synapse").mkdir(parents=True)
        (compose_dir / ".env").write_text(
            "COMPOSE_PROJECT_NAME=botville-bak\n"
            "POSTGRES_USER=botville\n"
            "POSTGRES_PASSWORD=testpgpass123\n"
            "POSTGRES_DB=synapse\n",
            encoding="utf-8",
        )
        (compose_dir / "generated" / "synapse" / "homeserver.yaml").write_text(
            "server_name: localhost\n", encoding="utf-8"
        )

        self.bin_dir = tmp / "bin"
        self.bin_dir.mkdir()
        self.docker_log = tmp / "docker.log"
        self.compose_log = tmp / "compose.log"
        (self.bin_dir / "docker").write_text(FAKE_DOCKER, encoding="utf-8")
        (self.bin_dir / "docker").chmod(0o755)
        self.fake_compose = tmp / "fake-compose.sh"
        self.fake_compose.write_text(FAKE_COMPOSE, encoding="utf-8")
        self.fake_compose.chmod(0o755)

    def _env(self, backup_dir, volume_exists="0"):
        env = dict(os.environ)
        env["PATH"] = str(self.bin_dir) + os.pathsep + env["PATH"]
        env["FAKE_DOCKER_LOG"] = str(self.docker_log)
        env["FAKE_COMPOSE_LOG"] = str(self.compose_log)
        env["FAKE_BACKUP_DIR"] = str(backup_dir)
        env["FAKE_DOCKER_VOLUME_EXISTS"] = volume_exists
        env["COMPOSE"] = "bash {}".format(self.fake_compose)
        env["BACKUP_DIR"] = str(backup_dir)
        env.pop("BOTVILLE_RESTORE_ALLOW_CROSS_INSTANCE", None)
        return env

    def _run_backup(self, volume_exists="0"):
        backup_dir = self.tmp / "out"
        backup_dir.mkdir()
        # Script chmod 700's BACKUP_DIR; start writable.
        proc = subprocess.run(
            ["bash", str(self.backup)],
            capture_output=True,
            text=True,
            timeout=30,
            env=self._env(backup_dir, volume_exists),
            cwd=str(self.tmp),
        )
        return proc, backup_dir

    def docker_calls(self):
        if not self.docker_log.exists():
            return []
        return self.docker_log.read_text(encoding="utf-8").splitlines()

    def test_backup_skips_gateway_data_when_volume_missing(self):
        proc, backup_dir = self._run_backup(volume_exists="0")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("gateway-data volume botville-bak_gateway-data not found; skipping", proc.stdout)
        self.assertFalse((backup_dir / "gateway-data.tar.gz").exists())
        manifest = (backup_dir / "manifest.txt").read_text(encoding="utf-8")
        self.assertNotIn("gateway-data.tar.gz", manifest)
        for name in ("synapse-db.dump", "synapse-data.tar.gz", "homeserver.yaml", "env", "manifest.txt"):
            self.assertTrue((backup_dir / name).is_file(), name)
            self.assertEqual(stat.S_IMODE((backup_dir / name).stat().st_mode), 0o600)
        self.assertTrue(any("volume inspect" in c and "botville-bak_gateway-data" in c for c in self.docker_calls()))
        self.assertFalse(any("gateway-data.tar.gz" in c for c in self.compose_log.read_text(encoding="utf-8").splitlines()) if self.compose_log.exists() else True)

    def test_backup_archives_gateway_data_when_volume_exists(self):
        proc, backup_dir = self._run_backup(volume_exists="1")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("archiving gateway-data volume botville-bak_gateway-data", proc.stdout)
        self.assertTrue((backup_dir / "gateway-data.tar.gz").is_file())
        self.assertIn("gateway-data.tar.gz", (backup_dir / "manifest.txt").read_text(encoding="utf-8"))
        self.assertEqual(stat.S_IMODE((backup_dir / "gateway-data.tar.gz").stat().st_mode), 0o600)

    def test_ensure_gateway_data_volume_creates_when_missing(self):
        env = self._env(self.tmp / "unused")
        env["FAKE_DOCKER_VOLUME_EXISTS"] = "0"
        (self.tmp / "unused").mkdir(exist_ok=True)
        proc = subprocess.run(
            ["bash", str(self.tmp / "server" / "scripts" / "ensure.sh")],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
            cwd=str(self.tmp),
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        calls = self.docker_calls()
        self.assertTrue(any("volume inspect" in c and "botville-bak_gateway-data" in c for c in calls))
        create = [c for c in calls if "volume create" in c]
        self.assertEqual(len(create), 1, calls)
        self.assertIn("com.docker.compose.project=botville-bak", create[0])
        self.assertIn("com.docker.compose.volume=gateway-data", create[0])
        self.assertTrue(create[0].rstrip().endswith("botville-bak_gateway-data"), create[0])

    def test_ensure_gateway_data_volume_is_noop_when_present(self):
        env = self._env(self.tmp / "unused")
        env["FAKE_DOCKER_VOLUME_EXISTS"] = "1"
        (self.tmp / "unused").mkdir(exist_ok=True)
        proc = subprocess.run(
            ["bash", str(self.tmp / "server" / "scripts" / "ensure.sh")],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
            cwd=str(self.tmp),
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        calls = self.docker_calls()
        self.assertTrue(any("volume inspect" in c for c in calls))
        self.assertFalse(any("volume create" in c for c in calls), calls)


if __name__ == "__main__":
    unittest.main()
