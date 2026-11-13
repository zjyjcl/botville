#!/usr/bin/env python3
"""Hermetic tests for the profile-agnostic gateway-data backup/restore
(A2.2 / #7): backup-gateway-data.sh / restore-gateway-data.sh and the shared
lib.sh helpers they use together with backup-synapse.sh/restore-synapse.sh.

tuwunel and continuwuity have no homeserver backup script, so these two
scripts are how their gateway-data gets backed up. Semantics match #188:
gateway-data.tar.gz; skip when the volume is absent; a backup without the
artifact leaves an existing volume untouched; WAL checkpoint for every
*.db in the volume (subdirectories too); a running gateway container is stopped around the archive.

No Docker: a fake `docker` (logs calls; `volume inspect` and `ps` are
scriptable via env) and a fake COMPOSE prefix. Standard library only.

Run:  python3 -m unittest discover -s server/tests -p "test_*.py" -v
"""

import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1]
SCRIPTS = SERVER_DIR / "scripts"
REPO_ROOT = SERVER_DIR.parent

FAKE_DOCKER = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${FAKE_DOCKER_LOG:?}"
if [ "$1" = volume ] && [ "$2" = inspect ]; then
    if [ "${FAKE_DOCKER_VOLUME_EXISTS:-0}" = 1 ]; then exit 0; else exit 1; fi
fi
if [ "$1" = ps ]; then
    [ -n "${FAKE_DOCKER_PS:-}" ] && printf '%s\\n' "${FAKE_DOCKER_PS}"
    exit 0
fi
# default COMPOSE prefix (docker compose ... run ...): same artifact stub.
if [ "$1" = compose ] && [[ "$*" == *'tar -czf /backup/gateway-data.tar.gz'* ]]; then
    printf 'fake-gateway-tar\\n' > "${FAKE_BACKUP_DIR:?}/gateway-data.tar.gz"
fi
exit 0
"""

# compose run ... -c <script>: when the script writes gateway-data.tar.gz,
# create it in FAKE_BACKUP_DIR (the real one bind-mounts it at /backup).
FAKE_COMPOSE = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${FAKE_COMPOSE_LOG:?}"
if [ "${1-}" = run ] && [[ "$*" == *'tar -czf /backup/gateway-data.tar.gz'* ]]; then
    [ "${FAKE_COMPOSE_FAIL:-0}" = 1 ] && exit 3
    printf 'fake-gateway-tar\\n' > "${FAKE_BACKUP_DIR:?}/gateway-data.tar.gz"
    chmod 600 "${FAKE_BACKUP_DIR}/gateway-data.tar.gz"
fi
# backup-synapse.sh's other artifacts (pg_dump to stdout, synapse-data tar).
case "$*" in
    *pg_dump*) echo FAKE-PGDUMP ;;
    *"tar -czf /backup/synapse-data.tar.gz"*)
        printf 'fake-synapse-tar\\n' > "${FAKE_BACKUP_DIR:?}/synapse-data.tar.gz" ;;
esac
exit 0
"""


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class GatewayDataScriptsCase(unittest.TestCase):
    PROJECT = "botville-gw"

    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="botville-gwdata-test-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.tmp = tmp
        scripts = tmp / "server" / "scripts"
        scripts.mkdir(parents=True)
        for name in ("lib.sh", "backup-gateway-data.sh", "restore-gateway-data.sh",
                     "backup-synapse.sh"):
            shutil.copy(SCRIPTS / name, scripts / name)
        self.scripts = scripts
        compose_dir = tmp / "server" / "compose"
        (compose_dir / "generated" / "synapse").mkdir(parents=True)
        (compose_dir / ".env").write_text(
            "COMPOSE_PROJECT_NAME={}\nPOSTGRES_USER=botville\n"
            "POSTGRES_PASSWORD=test-only\nPOSTGRES_DB=synapse\n".format(self.PROJECT),
            encoding="utf-8",
        )
        (compose_dir / "generated" / "synapse" / "homeserver.yaml").write_text(
            "server_name: localhost\n", encoding="utf-8")
        self.bin_dir = tmp / "bin"
        self.bin_dir.mkdir()
        (self.bin_dir / "docker").write_text(FAKE_DOCKER, encoding="utf-8")
        (self.bin_dir / "docker").chmod(0o755)
        self.fake_compose = tmp / "fake-compose.sh"
        self.fake_compose.write_text(FAKE_COMPOSE, encoding="utf-8")
        self.fake_compose.chmod(0o755)
        self.docker_log = tmp / "docker.log"
        self.compose_log = tmp / "compose.log"
        self.out = tmp / "out"

    def env(self, volume_exists="1", ps="", fake_compose=True, **extra):
        env = dict(os.environ)
        env["PATH"] = str(self.bin_dir) + os.pathsep + env["PATH"]
        env["FAKE_DOCKER_LOG"] = str(self.docker_log)
        env["FAKE_COMPOSE_LOG"] = str(self.compose_log)
        env["FAKE_BACKUP_DIR"] = str(self.out)
        env["FAKE_DOCKER_VOLUME_EXISTS"] = volume_exists
        env["FAKE_DOCKER_PS"] = ps
        env["BACKUP_DIR"] = str(self.out)
        if fake_compose:
            env["COMPOSE"] = "bash {}".format(self.fake_compose)
        else:
            env.pop("COMPOSE", None)
        env.pop("BOTVILLE_RESTORE_ALLOW_CROSS_INSTANCE", None)
        env.update(extra)
        return env

    def run_script(self, name, *args, **env_kw):
        return subprocess.run(
            ["bash", str(self.scripts / name), *args],
            capture_output=True, text=True, timeout=30,
            env=self.env(**env_kw), cwd=str(self.tmp),
        )

    def lines(self, path):
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def make_backup(self, with_artifact=True, manifest=True, project=None,
                    corrupt=False, list_artifact=True):
        backup = self.tmp / "backup"
        backup.mkdir()
        if with_artifact:
            (backup / "gateway-data.tar.gz").write_bytes(b"fake gateway volume tar\n")
        if manifest:
            rows = ["# Botville gateway-data backup manifest",
                    "compose_project={}".format(project or self.PROJECT)]
            if list_artifact:
                digest = sha256(backup / "gateway-data.tar.gz") if with_artifact else "0" * 64
                if corrupt:
                    digest = "f" * 64
                rows.append("{}  24  gateway-data.tar.gz".format(digest))
            (backup / "manifest.txt").write_text("\n".join(rows) + "\n", encoding="utf-8")
        return backup


class BackupGatewayDataTest(GatewayDataScriptsCase):
    def test_skips_when_volume_missing(self):
        proc = self.run_script("backup-gateway-data.sh", volume_exists="0")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("gateway-data volume botville-gw_gateway-data not found; skipping", proc.stdout)
        self.assertFalse(self.out.exists(), "nothing may be written when the volume is absent")
        self.assertEqual(self.lines(self.compose_log), [])

    def test_archives_existing_volume(self):
        proc = self.run_script("backup-gateway-data.sh")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.strip().splitlines()[-1], str(self.out))
        artifact = self.out / "gateway-data.tar.gz"
        manifest = (self.out / "manifest.txt").read_text(encoding="utf-8")
        self.assertIn("{}  ".format(sha256(artifact)), manifest)
        self.assertIn("compose_project=botville-gw", manifest)
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o700)
        for name in ("gateway-data.tar.gz", "manifest.txt"):
            self.assertEqual(stat.S_IMODE((self.out / name).stat().st_mode), 0o600)
        # No .env copy: secrets never go into this backup.
        self.assertFalse((self.out / "env").exists())
        run = [c for c in self.lines(self.compose_log) if c.startswith("run ")]
        self.assertEqual(len(run), 1, run)
        self.assertIn("--user 0:0", run[0])
        self.assertIn("botville-gw_gateway-data:/gateway-data", run[0])
        # The runner is the gateway service (tuwunel/continuwuity are OS-less).
        self.assertIn(" gateway -c ", run[0])
        self.assertEqual((self.tmp / "backups" / ".last-backup").read_text().strip(), str(self.out))

    def test_checkpoint_covers_subdirectories(self):
        """#206 nit: every *.db in the volume (any depth) is checkpointed."""
        proc = self.run_script("backup-gateway-data.sh")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        run = "\n".join(self.lines(self.compose_log))
        self.assertIn('find /gateway-data -type f -name "*.db" | while IFS= read -r db; do', run)
        self.assertIn("wal_checkpoint(TRUNCATE)", run)
        self.assertNotIn("for db in /gateway-data/*.db", run)

    def test_running_gateway_is_stopped_and_started(self):
        proc = self.run_script("backup-gateway-data.sh", ps="abc123")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        calls = self.lines(self.docker_log)
        ps = [c for c in calls if c.startswith("ps ")]
        self.assertTrue(ps)
        self.assertIn("label=com.docker.compose.project=botville-gw", ps[0])
        self.assertIn("label=com.docker.compose.service=gateway", ps[0])
        self.assertIn("label=com.docker.compose.oneoff=False", ps[0])
        self.assertIn("stop abc123", calls)
        self.assertIn("start abc123", calls)
        self.assertLess(calls.index("stop abc123"), calls.index("start abc123"))

    def test_gateway_restarted_even_if_archive_fails(self):
        proc = self.run_script("backup-gateway-data.sh", ps="abc123", FAKE_COMPOSE_FAIL="1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("start abc123", self.lines(self.docker_log))

    def test_default_compose_uses_gateway_profile(self):
        # Without COMPOSE the default prefix carries -p <project> and
        # --profile gateway; the fake docker records it.
        proc = self.run_script("backup-gateway-data.sh", fake_compose=False)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        run = [c for c in self.lines(self.docker_log) if " run " in c]
        self.assertTrue(run)
        self.assertTrue(run[0].startswith(
            "compose -p botville-gw -f server/compose/docker-compose.yml "
            "--env-file server/compose/.env --profile gateway run "), run[0])

    def test_synapse_backup_also_stops_running_gateway(self):
        """backup-synapse.sh goes through the same helper (runner: synapse)."""
        self.out.mkdir()
        proc = self.run_script("backup-synapse.sh", ps="gw1")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        calls = self.lines(self.docker_log)
        self.assertIn("stop gw1", calls)
        self.assertIn("start gw1", calls)
        # (the -c script spans lines; the first line carries the mounts)
        run = [c for c in self.lines(self.compose_log)
               if c.startswith("run ") and "_gateway-data:/gateway-data" in c]
        self.assertEqual(len(run), 1)
        self.assertIn(" synapse -c ", run[0])


class RestoreGatewayDataTest(GatewayDataScriptsCase):
    def test_requires_yes(self):
        backup = self.make_backup()
        proc = self.run_script("restore-gateway-data.sh", str(backup))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("refusing to run without --yes", proc.stderr)
        self.assertEqual(self.lines(self.compose_log), [])

    def test_backup_without_artifact_leaves_volume_untouched(self):
        backup = self.make_backup(with_artifact=False, list_artifact=False)
        proc = self.run_script("restore-gateway-data.sh", str(backup), "--yes")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("leaving gateway-data volume untouched", proc.stdout)
        self.assertEqual(self.lines(self.compose_log), [])
        self.assertFalse(any("gateway-data" in c or c.startswith(("stop", "volume"))
                             for c in self.lines(self.docker_log)))

    def test_restores_artifact(self):
        backup = self.make_backup()
        proc = self.run_script("restore-gateway-data.sh", str(backup), "--yes", ps="gw9",
                               volume_exists="0")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("checksum OK: gateway-data.tar.gz", proc.stdout)
        calls = self.lines(self.docker_log)
        create = [c for c in calls if c.startswith("volume create")]
        self.assertEqual(len(create), 1, calls)
        self.assertIn("com.docker.compose.volume=gateway-data", create[0])
        self.assertIn("stop gw9", calls)
        self.assertIn("start gw9", calls)
        run = [c for c in self.lines(self.compose_log) if c.startswith("run ")]
        self.assertEqual(len(run), 1)
        self.assertIn("tar -xzf /backup/gateway-data.tar.gz", run[0])
        self.assertIn("chown -R", run[0])
        self.assertIn("GATEWAY_UID=10001", run[0])
        self.assertIn(":/backup:ro", run[0])
        self.assertIn(" gateway -c ", run[0])

    def test_sha_mismatch_aborts_before_anything(self):
        backup = self.make_backup(corrupt=True)
        proc = self.run_script("restore-gateway-data.sh", str(backup), "--yes", ps="gw9")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("sha256 mismatch for gateway-data.tar.gz", proc.stderr)
        self.assertEqual(self.lines(self.compose_log), [])
        self.assertNotIn("stop gw9", self.lines(self.docker_log))

    def test_artifact_missing_from_manifest_aborts(self):
        backup = self.make_backup(list_artifact=False)
        proc = self.run_script("restore-gateway-data.sh", str(backup), "--yes")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("no sha256 entry for gateway-data.tar.gz", proc.stderr)
        self.assertEqual(self.lines(self.compose_log), [])

    def test_manifest_lists_missing_artifact_aborts(self):
        backup = self.make_backup(with_artifact=False)
        proc = self.run_script("restore-gateway-data.sh", str(backup), "--yes")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("lists gateway-data.tar.gz but the file is missing", proc.stderr)

    def test_cross_instance_refused_unless_allowed(self):
        backup = self.make_backup(project="other")
        proc = self.run_script("restore-gateway-data.sh", str(backup), "--yes")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("COMPOSE_PROJECT_NAME mismatch", proc.stderr)
        self.assertEqual(self.lines(self.compose_log), [])
        ok = self.run_script("restore-gateway-data.sh", str(backup), "--yes",
                             BOTVILLE_RESTORE_ALLOW_CROSS_INSTANCE="yes")
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)

    def test_synapse_backup_dir_is_accepted(self):
        """A backup-synapse.sh directory (env copy, no compose_project=) works;
        the project comes from its env copy."""
        backup = self.make_backup(manifest=False)
        (backup / "env").write_text("COMPOSE_PROJECT_NAME={}\n".format(self.PROJECT),
                                    encoding="utf-8")
        proc = self.run_script("restore-gateway-data.sh", str(backup), "--yes")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("no manifest.txt", proc.stderr)
        self.assertTrue(any("tar -xzf" in c for c in self.lines(self.compose_log)))


class GatewayUidConsistencyTest(unittest.TestCase):
    def test_lib_uid_matches_dockerfile_user(self):
        lib = (SCRIPTS / "lib.sh").read_text(encoding="utf-8")
        dockerfile = (REPO_ROOT / "gateway" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("GATEWAY_DATA_UID=10001", lib)
        self.assertRegex(dockerfile, r"(?m)^USER 10001:10001$")
        self.assertIn("--uid 10001", dockerfile)



class GatewayArchiveRestoreScriptsRunTest(unittest.TestCase):
    """Run lib.sh's real in-container scripts against a sandbox directory
    (paths /gateway-data and /backup rewritten; owner = the test user):
    the WAL checkpoint reaches *.db in subdirectories, and restore forces
    every SQLite file to 0600 (#206 nits)."""

    def script(self, name):
        proc = subprocess.run(
            ["bash", "-c", 'source "$1" && printf "%s" "${!2}"', "x",
             str(SCRIPTS / "lib.sh"), name],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True)
        return proc.stdout

    def sandboxed(self, text, data, backup):
        uid_gid = "{}:{}".format(os.getuid(), os.getgid())
        text = text.replace('chown -R "$GATEWAY_UID:$GATEWAY_UID"', "chown -R " + uid_gid)
        archive = "/backup/gateway-data.tar.gz"
        self.assertIn(archive, text)
        text = text.replace(archive, "@ARCHIVE@").replace("/gateway-data", str(data))
        self.assertNotIn("/backup", text)
        return text.replace("@ARCHIVE@", str(backup / "gateway-data.tar.gz"))

    def run_sh(self, text):
        env = dict(os.environ, HOST_UID=str(os.getuid()), HOST_GID=str(os.getgid()))
        return subprocess.run(["sh", "-c", text], env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=60)

    def test_archive_checkpoints_nested_db_and_restore_chmods_600(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            data, backup, live = tmp / "data", tmp / "backup", tmp / "live"
            for d in (data / "sub", backup, live):
                d.mkdir(parents=True)
            # A WAL-mode db with rows still only in the -wal file: copy the
            # files while a connection is open (as if the process had stopped).
            conn = sqlite3.connect(str(live / "nested.db"))
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA wal_autocheckpoint=0")
            conn.execute("CREATE TABLE t (v TEXT)")
            conn.executemany("INSERT INTO t VALUES (?)", [("row",)] * 50)
            conn.commit()
            for suffix in ("", "-wal"):
                shutil.copy(str(live / ("nested.db" + suffix)), str(data / "sub" / ("nested.db" + suffix)))
            conn.close()
            (data / "top.db").write_bytes((data / "sub" / "nested.db").read_bytes())
            self.assertGreater((data / "sub" / "nested.db-wal").stat().st_size, 0)

            proc = self.run_sh(self.sandboxed(self.script("GATEWAY_ARCHIVE_SH"), data, backup))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            wal = data / "sub" / "nested.db-wal"
            self.assertTrue(not wal.exists() or wal.stat().st_size == 0,
                            "nested db was not checkpointed")
            archive = backup / "gateway-data.tar.gz"
            self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)

            # Loosen modes, then restore: SQLite files come back 0600, other
            # files keep their mode.
            for f in data.rglob("*"):
                if f.is_file():
                    f.chmod(0o644)
            (data / "notes.txt").write_text("x", encoding="utf-8")
            (data / "notes.txt").chmod(0o644)
            subprocess.run(["tar", "-czf", str(archive), "-C", str(data), "."],
                           stdin=subprocess.DEVNULL, check=True)
            shutil.rmtree(str(data))
            data.mkdir()
            (data / "stale.db").write_text("old", encoding="utf-8")
            proc = self.run_sh(self.sandboxed(self.script("GATEWAY_RESTORE_SH"), data, backup))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse((data / "stale.db").exists())
            for rel in ("top.db", "sub/nested.db"):
                self.assertEqual(stat.S_IMODE((data / rel).stat().st_mode), 0o600, rel)
            for side in data.rglob("*.db-*"):
                self.assertEqual(stat.S_IMODE(side.stat().st_mode), 0o600, side.name)
            self.assertEqual(stat.S_IMODE((data / "notes.txt").stat().st_mode), 0o644)
            # The checkpointed rows are in the main file alone.
            check = sqlite3.connect(str(data / "sub" / "nested.db"))
            self.assertEqual(check.execute("SELECT count(*) FROM t").fetchone()[0], 50)
            check.close()

if __name__ == "__main__":
    unittest.main()
