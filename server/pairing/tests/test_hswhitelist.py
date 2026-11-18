"""Homeserver whitelist include + restart marker (A-R2, #118 item 8)."""

import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "..", ".."))
sys.path.insert(0, TESTS_DIR)

from pairing import hswhitelist, service  # noqa: E402
import util as testutil  # noqa: E402
from test_service import (  # noqa: E402
    ADMIN, NOTIFY_SECRET, ServiceTestBase, signed_notify_headers, stub_peer)


def listed(text):
    """Server names in a rendered include (parsed, not substring-matched)."""
    names = []
    in_list = False
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        if line == hswhitelist.WHITELIST_KEY + ":":
            in_list = True
            continue
        assert in_list and line.startswith("  - "), line
        names.append(json.loads(line[4:]))
    return names


class RenderTest(unittest.TestCase):
    def test_own_server_plus_active_sorted_deduplicated(self):
        text = hswhitelist.render("hs-a", ["hs-c", "HS-B", "hs-a", "hs-c"])
        self.assertEqual(listed(text), ["hs-a", "hs-b", "hs-c"])

    def test_no_peers_still_lists_self(self):
        # An empty Synapse whitelist would block nothing useful and a
        # missing own name would break local key lookups; self is always in.
        self.assertEqual(listed(hswhitelist.render("hs-a", [])), ["hs-a"])

    def test_deterministic(self):
        self.assertEqual(hswhitelist.render("hs-a", ["b", "c"]),
                         hswhitelist.render("hs-a", ["c", "b"]))


class WriterTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.path = os.path.join(self.dir, "federation-whitelist.yaml")
        self.writer = hswhitelist.HsWhitelistWriter(self.path, "hs-a")
        self.marker = self.path + ".restart-required"

    def read(self, path):
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_first_boot_creates_file_without_marker(self):
        # No include yet => the homeserver (which loads it via
        # --config-path) cannot be running: nothing to restart.
        self.assertFalse(self.writer.sync(["hs-b"]))
        self.assertEqual(listed(self.read(self.path)), ["hs-a", "hs-b"])
        self.assertFalse(os.path.exists(self.marker))
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o644)

    def test_existing_file_changed_at_startup_marks(self):
        # Pairing changed while this service was down (or an upgrade
        # changed the list): the running homeserver has a stale list.
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(hswhitelist.render("hs-a", ["hs-old"]))
        self.assertTrue(self.writer.sync(["hs-b"], now=1000))
        marker = json.loads(self.read(self.marker))
        self.assertEqual(marker["at"], 1000)
        self.assertEqual(marker["whitelist"], "federation-whitelist.yaml")

    def test_file_deleted_later_is_recreated_with_marker(self):
        self.writer.sync(["hs-b"])
        os.unlink(self.path)
        self.assertTrue(self.writer.sync([]))
        self.assertEqual(listed(self.read(self.path)), ["hs-a"])
        self.assertTrue(os.path.exists(self.marker))

    def test_unchanged_content_writes_nothing_and_no_marker(self):
        self.writer.sync(["hs-b"])
        before = os.stat(self.path)
        self.assertFalse(self.writer.sync(["hs-b"]))
        self.assertFalse(self.writer.sync(["HS-B", "hs-b"]))
        self.assertFalse(os.path.exists(self.marker))
        after = os.stat(self.path)
        self.assertEqual((before.st_ino, before.st_mtime_ns),
                         (after.st_ino, after.st_mtime_ns))

    def test_change_rewrites_and_marks_again(self):
        self.writer.sync(["hs-b"])
        self.assertTrue(self.writer.sync(["hs-b", "hs-c"]))
        os.unlink(self.marker)  # operator restarted and cleared it
        self.assertTrue(self.writer.sync([]))  # severed
        self.assertEqual(listed(self.read(self.path)), ["hs-a"])
        self.assertTrue(os.path.exists(self.marker))

    def test_hand_edited_file_is_repaired_with_marker(self):
        self.writer.sync(["hs-b"])
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write('  - "evil.example"\n')
        self.assertTrue(self.writer.sync(["hs-b"]))
        self.assertEqual(listed(self.read(self.path)), ["hs-a", "hs-b"])
        self.assertTrue(os.path.exists(self.marker))

    def test_atomic_replace_leaves_no_temp_files(self):
        for peers in (["hs-b"], [], ["hs-c"]):
            self.writer.sync(peers)
        self.assertEqual(sorted(os.listdir(self.dir)),
                         ["federation-whitelist.yaml",
                          "federation-whitelist.yaml.restart-required"])

    def test_failed_write_keeps_old_file_intact(self):
        self.writer.sync(["hs-b"])
        old = self.read(self.path)
        with mock.patch.object(hswhitelist.os, "replace",
                               side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.writer.sync([])
        self.assertEqual(self.read(self.path), old)
        self.assertEqual([n for n in os.listdir(self.dir) if n.startswith(".tmp-")], [])
        # os.replace is mocked for every write, so the pre-marker never
        # landed either; nothing to clean up.
        self.assertFalse(os.path.exists(self.marker))

    def test_marker_failure_is_retried_even_if_content_current(self):
        self.writer.sync([])  # first boot
        real_write = hswhitelist._atomic_write

        def fail_marker(path, text):
            if path == self.marker:
                raise OSError("read-only marker dir")
            real_write(path, text)

        with mock.patch.object(hswhitelist, "_atomic_write", fail_marker):
            with self.assertRaises(OSError):
                self.writer.sync(["hs-b"])
        self.assertFalse(os.path.exists(self.marker))
        # Content is already current, but the restart must not be lost.
        self.assertTrue(self.writer.sync(["hs-b"]))
        self.assertTrue(os.path.exists(self.marker))
        os.unlink(self.marker)
        self.assertFalse(self.writer.sync(["hs-b"]))

    # --- #247 review follow-ups (A-R4) ---------------------------------------

    def test_peers_are_read_under_the_writer_lock(self):
        # S1: read -> render -> write is one critical section, so a slow
        # sync can never write an older snapshot over a newer one.
        seen = []

        def loader():
            seen.append(self.writer._lock.locked())
            return ["hs-b"]

        self.writer.sync(loader)
        self.assertEqual(seen, [True])
        self.assertEqual(listed(self.read(self.path)), ["hs-a", "hs-b"])

    def test_crash_between_include_and_marker_keeps_the_restart(self):
        # S2: the marker is written BEFORE the include, so a crash after
        # the include write cannot lose it -- the next process finds the
        # include current and writes nothing, but the marker is there.
        self.writer.sync([])  # first boot
        real_write = hswhitelist._atomic_write
        calls = []

        class Crash(BaseException):
            pass

        def crash_after_include(path, text):
            calls.append(path)
            if path == self.marker and self.path in calls:
                raise Crash()  # process dies before the second marker
            real_write(path, text)

        with mock.patch.object(hswhitelist, "_atomic_write",
                               crash_after_include):
            with self.assertRaises(Crash):
                self.writer.sync(["hs-b"])
        self.assertEqual(calls, [self.marker, self.path, self.marker])
        self.assertEqual(listed(self.read(self.path)), ["hs-a", "hs-b"])
        self.assertTrue(os.path.exists(self.marker))
        restarted = hswhitelist.HsWhitelistWriter(self.path, "hs-a")
        self.assertFalse(restarted.sync(["hs-b"]))
        self.assertTrue(os.path.exists(self.marker))

    def test_marker_consumed_before_include_lands_is_rewritten(self):
        # S2: a supervisor that claims the first marker and restarts before
        # the include is replaced gets a second marker afterwards.
        self.writer.sync([])
        real_write = hswhitelist._atomic_write

        def supervisor_races(path, text):
            if path == self.path and os.path.exists(self.marker):
                os.unlink(self.marker)  # claimed + restarted on old content
            real_write(path, text)

        with mock.patch.object(hswhitelist, "_atomic_write", supervisor_races):
            self.assertTrue(self.writer.sync(["hs-b"]))
        self.assertTrue(os.path.exists(self.marker))

    def test_include_write_failure_removes_a_new_pre_marker(self):
        # #253 review 2: include failed, no prior marker -> drop the
        # pre-marker so a 60s retry does not bounce Synapse.
        self.writer.sync([])
        real_write = hswhitelist._atomic_write

        def fail_include(path, text):
            if path == self.path:
                raise OSError("include volume full")
            real_write(path, text)

        with mock.patch.object(hswhitelist, "_atomic_write", fail_include):
            with self.assertRaises(OSError):
                self.writer.sync(["hs-b"])
        self.assertEqual(listed(self.read(self.path)), ["hs-a"])
        self.assertFalse(os.path.exists(self.marker))

    def test_include_write_failure_keeps_an_existing_marker(self):
        # #253 review 2: a marker that already belonged to a landed
        # include must survive a later include-write failure.
        self.writer.sync([])
        self.assertTrue(self.writer.sync(["hs-b"]))
        self.assertTrue(os.path.exists(self.marker))
        real_write = hswhitelist._atomic_write

        def fail_include(path, text):
            if path == self.path:
                raise OSError("include volume full")
            real_write(path, text)

        with mock.patch.object(hswhitelist, "_atomic_write", fail_include):
            with self.assertRaises(OSError):
                self.writer.sync(["hs-c"])
        self.assertTrue(os.path.exists(self.marker))
        self.assertEqual(listed(self.read(self.path)), ["hs-a", "hs-b"])

    def test_persistent_marker_failure_raises_every_time(self):
        # N3: include written, marker failing -> MarkerWriteError on every
        # sync (the service logs ERROR each time) until it works.
        self.writer.sync([])
        real_write = hswhitelist._atomic_write

        def fail_marker(path, text):
            if path == self.marker:
                raise OSError("read-only marker dir")
            real_write(path, text)

        with mock.patch.object(hswhitelist, "_atomic_write", fail_marker):
            for _ in range(3):
                with self.assertRaises(hswhitelist.MarkerWriteError):
                    self.writer.sync(["hs-b"])
        self.assertEqual(listed(self.read(self.path)), ["hs-a", "hs-b"])
        self.assertTrue(self.writer.sync(["hs-b"]))
        self.assertTrue(os.path.exists(self.marker))

    def test_restart_of_process_after_delete_recreates_without_marker(self):
        # S4 (documented limit): "recreated with a marker" is per process.
        self.writer.sync(["hs-b"])
        os.unlink(self.path)
        restarted = hswhitelist.HsWhitelistWriter(self.path, "hs-a")
        self.assertFalse(restarted.sync(["hs-b"]))
        self.assertFalse(os.path.exists(self.marker))

    def test_custom_marker_path(self):
        marker = os.path.join(self.dir, "restart-me")
        writer = hswhitelist.HsWhitelistWriter(self.path, "hs-a", marker)
        writer.sync([])
        self.assertTrue(writer.sync(["hs-b"]))
        self.assertTrue(os.path.exists(marker))


class ServiceWhitelistTest(ServiceTestBase):
    """Every pairing state change re-renders; unchanged state never marks."""

    def setUp(self):
        super().setUp()
        self.wl_dir = os.path.join(self._tmp.name, "hs")
        os.mkdir(self.wl_dir)
        self.path = os.path.join(self.wl_dir, "federation-whitelist.yaml")
        self.marker = self.path + ".restart-required"
        self.base, self.httpd = self.start_service(hs_whitelist_path=self.path)
        self.app = self.httpd.app

    def names(self):
        with open(self.path, encoding="utf-8") as handle:
            return listed(handle.read())

    def take_marker(self):
        """True if a marker was pending; clears it like an operator would."""
        if os.path.exists(self.marker):
            os.unlink(self.marker)
            return True
        return False

    def test_startup_sync_renders_self_only(self):
        self.assertFalse(self.app.sync_hs_whitelist())  # first boot
        self.assertEqual(self.names(), ["hs-a"])
        self.assertFalse(self.take_marker())
        self.assertFalse(self.app.sync_hs_whitelist())
        self.assertFalse(self.take_marker())

    def test_redeem_accept_sever_notify_cycle(self):
        self.app.sync_hs_whitelist()
        self.take_marker()
        # Redeem side (peer redeems our invite).
        status, invite = testutil.http_json(
            "POST", self.base + "/v1/pairing/invites", body={}, token=ADMIN)
        _, _, invite_id, secret = service.parse_qr_payload(invite["qr_payload"])
        status, resp = testutil.http_json(
            "POST", self.base + "/v1/pairing/redeem",
            body={"invite_id": invite_id, "secret": secret,
                  "server_name": "hs-b",
                  "api_url": "http://127.0.0.1:1/pairing"})
        self.assertEqual(status, 200, resp)
        self.assertEqual(self.names(), ["hs-a", "hs-b"])
        self.assertTrue(self.take_marker())

        # Local sever (peer unreachable: notify fails, render still runs).
        status, _ = testutil.http_json(
            "DELETE", self.base + "/v1/pairing/peers/hs-b", token=ADMIN)
        self.assertEqual(status, 200)
        self.assertEqual(self.names(), ["hs-a"])
        self.assertTrue(self.take_marker())
        # Severing again changes nothing: no new marker.
        testutil.http_json(
            "DELETE", self.base + "/v1/pairing/peers/hs-b", token=ADMIN)
        self.assertFalse(self.take_marker())

        # Accept side (we scan hs-c's QR).
        stub_url, _ = stub_peer(redeem_shared_secret=NOTIFY_SECRET)
        qr = service.build_qr_payload("hs-c", stub_url, "inv", "sec")
        status, resp = testutil.http_json(
            "POST", self.base + "/v1/pairing/accept", body={"qr_payload": qr},
            token=ADMIN)
        self.assertEqual(status, 200, resp)
        self.assertEqual(self.names(), ["hs-a", "hs-c"])
        self.assertTrue(self.take_marker())

        # Remote sever (hs-c notifies us).
        body = json.dumps({"server_name": "hs-c"}).encode("utf-8")
        status, resp = testutil.http_json(
            "POST", self.base + "/v1/pairing/notify-severed", data=body,
            headers=signed_notify_headers(NOTIFY_SECRET, "hs-c", body))
        self.assertEqual(status, 200, resp)
        self.assertEqual(self.names(), ["hs-a"])
        self.assertTrue(self.take_marker())

    def test_rejected_pairing_does_not_mark(self):
        self.app.sync_hs_whitelist()
        self.take_marker()
        stub_url, _ = stub_peer(redeem_status=403)
        qr = service.build_qr_payload("hs-b", stub_url, "i", "s")
        status, _ = testutil.http_json(
            "POST", self.base + "/v1/pairing/accept", body={"qr_payload": qr},
            token=ADMIN)
        self.assertEqual(status, 403)
        self.assertEqual(self.names(), ["hs-a"])
        self.assertFalse(self.take_marker())

    def test_write_failure_never_fails_the_request(self):
        self.app.store.upsert_peer_active(
            "hs-b", "http://127.0.0.1:1/pairing", shared_secret=NOTIFY_SECRET)
        self.app.sync_hs_whitelist()
        self.take_marker()
        self.assertEqual(self.names(), ["hs-a", "hs-b"])
        os.chmod(self.wl_dir, 0o500)
        self.addCleanup(os.chmod, self.wl_dir, 0o700)
        if os.access(self.wl_dir, os.W_OK):
            self.skipTest("running as a user that ignores directory modes")
        with self.assertLogs("pairing.service", "ERROR"):
            status, _ = testutil.http_json(
                "DELETE", self.base + "/v1/pairing/peers/hs-b", token=ADMIN)
        self.assertEqual(status, 200)  # pairing change committed regardless
        self.assertEqual(self.app.hs_whitelist_failures, 1)
        os.chmod(self.wl_dir, 0o700)
        self.assertTrue(self.app.sync_hs_whitelist())  # the retry loop's call
        self.assertEqual(self.names(), ["hs-a"])

    # --- #247 review follow-ups (A-R4) ---------------------------------------

    def test_any_exception_is_contained_and_counted(self):
        # S3: "never raises" holds for any Exception, not only OSError.
        with mock.patch.object(self.app.store, "active_server_names",
                               side_effect=RuntimeError("surprise")):
            with self.assertLogs("pairing.service", "ERROR") as logs:
                self.assertFalse(self.app.sync_hs_whitelist())
        self.assertIn("RuntimeError", logs.output[0])
        self.assertEqual(self.app.hs_whitelist_failures, 1)

    def test_marker_failure_logs_error_on_every_retry(self):
        # N3: each retry (the 60s loop) rewrites and reports ERROR.
        self.app.sync_hs_whitelist()
        self.app.store.upsert_peer_active("hs-b", "https://hs-b/x")
        real_write = hswhitelist._atomic_write

        def fail_marker(path, text):
            if path == self.marker:
                raise OSError("read-only marker dir")
            real_write(path, text)

        with mock.patch.object(hswhitelist, "_atomic_write", fail_marker):
            for attempt in (1, 2):
                with self.assertLogs("pairing.service", "ERROR") as logs:
                    self.assertFalse(self.app.sync_hs_whitelist())
                self.assertIn("restart marker could not be written",
                              logs.output[0])
                self.assertEqual(self.app.hs_whitelist_failures, attempt)
        self.assertTrue(self.app.sync_hs_whitelist())
        self.assertTrue(self.take_marker())

    def test_failure_counter_is_thread_safe(self):
        # N1 / #253 nit 5: stretch the read-modify-write window so a
        # missing lock would lose an increment (both threads read the
        # same value). This is not a smoke-only final-count check.
        in_gap = threading.Event()
        release = threading.Event()

        def gap():
            in_gap.set()
            self.assertTrue(release.wait(5))

        self.app._hs_whitelist_failure_gap = gap
        first = threading.Thread(target=self.app._note_hs_whitelist_failure)
        first.start()
        self.assertTrue(in_gap.wait(5))
        second = threading.Thread(target=self.app._note_hs_whitelist_failure)
        second.start()
        # Without the lock, second would enter the gap too. Give it a
        # moment: it must stay blocked on the lock, counter still 0.
        second.join(0.2)
        self.assertTrue(second.is_alive())
        self.assertEqual(self.app.hs_whitelist_failures, 0)
        release.set()
        first.join(5)
        second.join(5)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(self.app.hs_whitelist_failures, 2)

    def test_loop_survives_an_exception(self):
        # S3: the background loop keeps going after a surprise.
        # #253 nit 11: stop Event ends the daemon thread.
        calls = []
        done = threading.Event()
        stop = threading.Event()

        class FlakyApp:
            def sync_hs_whitelist(self):
                calls.append(1)
                if len(calls) == 1:
                    raise RuntimeError("bug")
                done.set()

        with self.assertLogs("pairing.service", "ERROR"):
            thread = service.start_hs_whitelist_loop(
                FlakyApp(), interval=0.01, stop=stop)
            self.assertTrue(done.wait(5))
            stop.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertGreaterEqual(len(calls), 2)

    def test_feature_off_without_path(self):
        app = service.PairingApp(service.PairingConfig(
            db_path=os.path.join(self._tmp.name, "other.db"),
            admin_token=ADMIN, server_name="hs-a",
            api_url="https://hs-a.example/_matrix/pairing"))
        self.assertIsNone(app.hs_whitelist)
        self.assertFalse(app.sync_hs_whitelist())


class WhitelistConfigTest(unittest.TestCase):
    BASE = {"PAIRING_ADMIN_TOKEN": ADMIN, "PAIRING_SERVER_NAME": "hs-a",
            "PAIRING_API_URL": "https://hs-a.example/_matrix/pairing"}

    def config(self, **extra):
        env = dict(self.BASE, **extra)
        return service.PairingConfig.from_env(env)

    def test_unset_is_off(self):
        config = self.config()
        self.assertIsNone(config.hs_whitelist_path)
        self.assertIsNone(config.hs_restart_marker)

    def test_paths(self):
        config = self.config(PAIRING_HS_WHITELIST_PATH="/hs/wl.yaml",
                             PAIRING_HS_RESTART_MARKER="/hs/restart")
        self.assertEqual(config.hs_whitelist_path, "/hs/wl.yaml")
        self.assertEqual(config.hs_restart_marker, "/hs/restart")

    def test_rejections(self):
        for extra in ({"PAIRING_HS_WHITELIST_PATH": "relative.yaml"},
                      {"PAIRING_HS_RESTART_MARKER": "/hs/restart"},
                      {"PAIRING_HS_WHITELIST_PATH": "/hs/wl.yaml",
                       "PAIRING_HS_RESTART_MARKER": "restart"},
                      {"PAIRING_HS_WHITELIST_PATH": "/hs/wl.yaml",
                       "PAIRING_HS_RESTART_MARKER": "/hs/wl.yaml"}):
            with self.subTest(extra=extra):
                with self.assertRaises(service.ConfigError):
                    self.config(**extra)


if __name__ == "__main__":
    unittest.main()
