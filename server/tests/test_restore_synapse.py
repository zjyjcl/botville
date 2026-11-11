#!/usr/bin/env python3
"""Hermetic tests for restore-synapse.sh (issue #95, A1.1 follow-ups).

No Docker and no real homeserver: the script runs against sandboxed copies of
lib.sh/restore-synapse.sh with a fake `docker` first on PATH (it logs every
invocation, including the -p <project> prefix default_compose builds) and a
local http.server standing in for GET /health. Backup artifacts are dummies
with a real sha256 manifest. Standard library only.

Run:  python3 -m unittest discover -s server/tests -p "test_*.py" -v
"""

import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1]
LIB = SERVER_DIR / "scripts" / "lib.sh"
RESTORE = SERVER_DIR / "scripts" / "restore-synapse.sh"

ARTIFACTS = ("synapse-db.dump", "synapse-data.tar.gz", "homeserver.yaml", "env")


def env_file_content(project, port):
    """Minimal .env: the keys restore-synapse.sh reads after installing it."""
    return (
        "BOTVILLE_HOMESERVER=synapse\n"
        "BOTVILLE_SERVER_NAME=localhost\n"
        "BOTVILLE_HTTP_PORT={}\n".format(port)
        + "COMPOSE_PROJECT_NAME={}\n".format(project)
        + "POSTGRES_USER=botville\n"
        + "POSTGRES_PASSWORD=testpgpass123\n"
        + "POSTGRES_DB=synapse\n"
    )


class HealthEndpoint:
    """Local server answering 200 on GET /health for wait_for_health."""

    def __init__(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"OK")

            def log_message(self, *args):  # keep test output quiet
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def port(self):
        return self.httpd.server_address[1]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class RestoreSynapseTest(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="botville-restore-test-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.tmp = tmp
        scripts = tmp / "server" / "scripts"
        scripts.mkdir(parents=True)
        shutil.copy(LIB, scripts / "lib.sh")
        shutil.copy(RESTORE, scripts / "restore-synapse.sh")
        self.restore = scripts / "restore-synapse.sh"

        # Fake `docker`, first on PATH: log the full argument string (so the
        # -p <project> prefix of default_compose is visible) and succeed.
        self.bin_dir = tmp / "bin"
        self.bin_dir.mkdir()
        self.docker_log = tmp / "docker.log"
        (self.bin_dir / "docker").write_text(
            '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> "${FAKE_DOCKER_LOG:?}"\nexit 0\n',
            encoding="utf-8",
        )
        (self.bin_dir / "docker").chmod(0o755)
        # Same recorder for the caller-COMPOSE test (no -p in its arguments).
        self.compose_log = tmp / "compose.log"
        fake_compose = tmp / "fake-compose.sh"
        fake_compose.write_text(
            '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> "${FAKE_COMPOSE_LOG:?}"\nexit 0\n',
            encoding="utf-8",
        )
        fake_compose.chmod(0o755)
        self.fake_compose = fake_compose

        self.health = HealthEndpoint()
        self.addCleanup(self.health.close)

        # The current instance's .env (mode 600 like init.sh leaves it).
        self.env_file = tmp / "server" / "compose" / ".env"
        self.env_file.parent.mkdir(parents=True)
        self.current_env = env_file_content("botville-cur", self.health.port)
        self.env_file.write_text(self.current_env, encoding="utf-8")
        self.env_file.chmod(0o600)

    # --- helpers -----------------------------------------------------------

    def make_backup(self, project="botville-cur", port=None, drop_manifest=False,
                    manifest_names=ARTIFACTS, corrupt_sha_for=None,
                    with_gateway_data=False):
        """Backup dir with dummy artifacts and a (possibly partial) manifest."""
        backup = self.tmp / "backup"
        backup.mkdir()
        contents = {
            "synapse-db.dump": b"PGDMP fake dump\n",
            "synapse-data.tar.gz": b"fake tar.gz bytes\n",
            "homeserver.yaml": b"server_name: localhost\n",
            "env": env_file_content(project, self.health.port if port is None else port).encode(),
        }
        if with_gateway_data:
            contents["gateway-data.tar.gz"] = b"fake gateway volume tar\n"
        for name, data in contents.items():
            (backup / name).write_bytes(data)
        if not drop_manifest:
            lines = ["# test manifest", "# files (sha256  bytes  name):"]
            for name in manifest_names:
                digest = hashlib.sha256(contents[name]).hexdigest()
                if name == corrupt_sha_for:
                    digest = hashlib.sha256(b"other bytes").hexdigest()
                lines.append("{}  {}  {}".format(digest, len(contents[name]), name))
            (backup / "manifest.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return backup

    def run_restore(self, backup, *, env=None, compose=None):
        proc_env = dict(os.environ)
        proc_env["PATH"] = str(self.bin_dir) + os.pathsep + proc_env["PATH"]
        proc_env["FAKE_DOCKER_LOG"] = str(self.docker_log)
        proc_env["FAKE_COMPOSE_LOG"] = str(self.compose_log)
        # The developer shell must not leak overrides into the test.
        proc_env.pop("COMPOSE", None)
        proc_env.pop("BOTVILLE_RESTORE_ALLOW_CROSS_INSTANCE", None)
        if compose is not None:
            proc_env["COMPOSE"] = compose
        proc_env.update(env or {})
        return subprocess.run(
            ["bash", str(self.restore), str(backup), "--yes"],
            capture_output=True,
            text=True,
            timeout=90,
            env=proc_env,
        )

    def docker_calls(self):
        if not self.docker_log.exists():
            return []
        return self.docker_log.read_text(encoding="utf-8").splitlines()

    # --- instance consistency check ---------------------------------------

    def test_same_instance_restore_completes(self):
        backup = self.make_backup()
        proc = self.run_restore(backup)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for name in ARTIFACTS:
            self.assertIn("==> checksum OK: {}".format(name), proc.stdout)
        # The .env was replaced by the backup copy and re-chmodded 600.
        self.assertEqual(
            self.env_file.read_text(encoding="utf-8"),
            (backup / "env").read_text(encoding="utf-8"),
        )
        self.assertEqual(stat.S_IMODE(self.env_file.stat().st_mode), 0o600)
        calls = self.docker_calls()
        self.assertTrue(calls)
        self.assertIn("stop synapse", calls[0])
        self.assertTrue(all("-p botville-cur" in c for c in calls))

    def test_project_name_mismatch_refused(self):
        backup = self.make_backup(project="botville-backup")
        proc = self.run_restore(backup)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("COMPOSE_PROJECT_NAME mismatch", proc.stderr)
        self.assertIn("'botville-cur'", proc.stderr)
        self.assertIn("'botville-backup'", proc.stderr)
        self.assertIn("BOTVILLE_RESTORE_ALLOW_CROSS_INSTANCE", proc.stderr)
        # Refused before anything was touched: no compose call, .env intact.
        self.assertEqual(self.docker_calls(), [])
        self.assertEqual(self.env_file.read_text(encoding="utf-8"), self.current_env)

    def test_port_mismatch_refused(self):
        backup = self.make_backup(port=self.health.port + 1)
        proc = self.run_restore(backup)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("BOTVILLE_HTTP_PORT mismatch", proc.stderr)
        self.assertEqual(self.docker_calls(), [])
        self.assertEqual(self.env_file.read_text(encoding="utf-8"), self.current_env)

    def test_allow_cross_instance_restores_into_backup_project(self):
        backup = self.make_backup(project="botville-backup")
        proc = self.run_restore(
            backup, env={"BOTVILLE_RESTORE_ALLOW_CROSS_INSTANCE": "yes"}
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("WARNING", proc.stderr)
        self.assertIn("cross-instance", proc.stderr)
        calls = self.docker_calls()
        # `stop synapse` still addresses the CURRENT project...
        self.assertIn("stop synapse", calls[0])
        self.assertIn("-p botville-cur", calls[0])
        # ...everything after the .env install targets the BACKUP's project
        # (COMPOSE recomputed from the restored .env).
        for call in calls[1:]:
            self.assertIn("-p botville-backup", call)
            self.assertNotIn("-p botville-cur", call)
        self.assertEqual(
            self.env_file.read_text(encoding="utf-8"),
            (backup / "env").read_text(encoding="utf-8"),
        )

    def test_explicit_compose_wins_over_recompute(self):
        backup = self.make_backup()
        proc = self.run_restore(backup, compose="bash {}".format(self.fake_compose))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        # #110: explicit COMPOSE must warn that instance check is .env-scoped only.
        self.assertIn("COMPOSE is explicitly set", proc.stderr)
        calls = self.compose_log.read_text(encoding="utf-8").splitlines()
        self.assertIn("stop synapse", calls)
        self.assertIn("up -d postgres", calls)
        # The caller's COMPOSE stayed in effect across the .env replacement
        # (default_compose would have gone through the `docker` on PATH).
        self.assertTrue(all("-p " not in c for c in calls))
        self.assertFalse(self.docker_log.exists())

    # --- manifest verification --------------------------------------------

    def test_manifest_missing_entry_fails(self):
        backup = self.make_backup(manifest_names=("synapse-db.dump", "synapse-data.tar.gz",
                                                  "homeserver.yaml"))
        proc = self.run_restore(backup)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("manifest.txt has no sha256 entry for env", proc.stderr)
        self.assertEqual(self.docker_calls(), [])
        self.assertEqual(self.env_file.read_text(encoding="utf-8"), self.current_env)

    def test_manifest_sha_mismatch_fails(self):
        backup = self.make_backup(corrupt_sha_for="synapse-db.dump")
        proc = self.run_restore(backup)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("sha256 mismatch for synapse-db.dump", proc.stderr)
        self.assertEqual(self.docker_calls(), [])

    def test_missing_manifest_warns_and_proceeds(self):
        backup = self.make_backup(drop_manifest=True)
        proc = self.run_restore(backup)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("no manifest.txt", proc.stderr)
        self.assertIn("skipping checksum verification", proc.stderr)
        self.assertNotIn("checksum OK", proc.stdout)
        self.assertTrue(self.docker_calls())
        self.assertIn("leaving gateway-data volume untouched", proc.stdout)
        self.assertFalse(any("gateway-data" in c for c in self.docker_calls()))

    def test_old_backup_leaves_gateway_volume_untouched(self):
        backup = self.make_backup()
        proc = self.run_restore(backup)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("leaving gateway-data volume untouched", proc.stdout)
        self.assertNotIn("checksum OK: gateway-data.tar.gz", proc.stdout)
        self.assertFalse(any("gateway-data" in c for c in self.docker_calls()))

    def test_gateway_data_artifact_is_restored(self):
        backup = self.make_backup(
            with_gateway_data=True,
            manifest_names=ARTIFACTS + ("gateway-data.tar.gz",),
        )
        proc = self.run_restore(backup)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("checksum OK: gateway-data.tar.gz", proc.stdout)
        self.assertIn("restoring gateway-data volume botville-cur_gateway-data", proc.stdout)
        self.assertNotIn("leaving gateway-data volume untouched", proc.stdout)
        calls = self.docker_calls()
        self.assertTrue(any("volume inspect" in c and "botville-cur_gateway-data" in c for c in calls))
        # volume already "exists" (fake docker inspect exits 0), so no create.
        self.assertTrue(any(
            "gateway-data" in c and "tar -xzf /backup/gateway-data.tar.gz" in c
            for c in calls
        ))

    def test_gateway_data_sha_mismatch_fails(self):
        backup = self.make_backup(
            with_gateway_data=True,
            manifest_names=ARTIFACTS + ("gateway-data.tar.gz",),
            corrupt_sha_for="gateway-data.tar.gz",
        )
        proc = self.run_restore(backup)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("sha256 mismatch for gateway-data.tar.gz", proc.stderr)
        self.assertEqual(self.docker_calls(), [])
        self.assertEqual(self.env_file.read_text(encoding="utf-8"), self.current_env)

    def test_gateway_data_file_without_manifest_entry_fails(self):
        backup = self.make_backup(with_gateway_data=True)  # manifest lists only ARTIFACTS
        proc = self.run_restore(backup)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("manifest.txt has no sha256 entry for gateway-data.tar.gz", proc.stderr)
        self.assertEqual(self.docker_calls(), [])

    def test_manifest_lists_gateway_data_but_file_missing_fails(self):
        backup = self.make_backup()
        manifest = (backup / "manifest.txt").read_text(encoding="utf-8")
        fake = hashlib.sha256(b"missing").hexdigest()
        (backup / "manifest.txt").write_text(
            manifest + "{}  0  gateway-data.tar.gz\n".format(fake),
            encoding="utf-8",
        )
        proc = self.run_restore(backup)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("lists gateway-data.tar.gz but the file is missing", proc.stderr)
        self.assertEqual(self.docker_calls(), [])



class EnvValueFromLastWinsTest(unittest.TestCase):
    """#110: env_value_from must match compose/init last-wins."""

    def test_last_assignment_wins(self):
        import subprocess
        import tempfile
        from pathlib import Path as P

        lib = P(__file__).resolve().parents[1] / "scripts" / "lib.sh"
        with tempfile.TemporaryDirectory() as tmp:
            env = P(tmp) / "e"
            env.write_text(
                "COMPOSE_PROJECT_NAME=first\n"
                "COMPOSE_PROJECT_NAME=second\n"
                "BOTVILLE_HTTP_PORT=8008\n"
                "BOTVILLE_HTTP_PORT=8018\n",
                encoding="utf-8",
            )
            script = (
                "set -euo pipefail; source {lib}; "
                'printf "%s\\n%s" '
                '"$(env_value_from {env} COMPOSE_PROJECT_NAME)" '
                '"$(env_value_from {env} BOTVILLE_HTTP_PORT)"'
            ).format(lib=repr(str(lib)), env=repr(str(env)))
            proc = subprocess.run(
                ["bash", "-c", script], capture_output=True, text=True, timeout=30
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "second\n8018")


if __name__ == "__main__":
    unittest.main()
