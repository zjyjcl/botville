# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""AS 事务库的运维回归测试（B 线 as-txn-nits，contract §1.7.7）。

覆盖：
- 新建库文件以 `0600` 创建；已存在的库文件权限**不被修改**（如 0644 保持 0644）。
- 主库是 0600 时，存在的 `-wal` / `-shm` 旁文件被收紧到 0600（尽力而为）。
- 旧库（#192/#194 时期，无 purged_at、无索引）被就地补上清理用的部分索引，
  `EXPLAIN QUERY PLAN` 对清理查询命中该索引。
- 后台清理调度**真的反复执行**（注入快速等待函数，不 sleep 真实间隔）；
  间隔 0 = 不跑启动清理、不起线程；`start_purge_scheduler(0)` 抛 ValueError。
- `GATEWAY_AS_TXN_RETENTION_DAYS` / `GATEWAY_AS_TXN_PURGE_INTERVAL_SECONDS`
  的校验在 **`GATEWAY_AS_TXN_DB_PATH` 未配置时同样生效**（启动即拒绝）。
- `get_unprocessed_transactions()` 按**插入顺序**（rowid）返回、支持批量上限、
  默认上限 100、`limit < 1` 抛 ValueError。

运行：python3 -m unittest gateway.tests.test_as_txn_store_ops -v
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading
import unittest

GATEWAY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(GATEWAY_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from botville_gateway.as_txn_store import (  # noqa: E402
    AS_TXN_PURGE_INTERVAL_MIN_SECONDS,
    AsTransactionStore,
    build_as_txn_store,
    connect,
)
from botville_gateway.config import (  # noqa: E402
    AS_TXN_PURGE_INTERVAL_MIN_SECONDS as CONFIG_MIN_INTERVAL,
    ConfigError,
    load_config,
)

_POSIX = os.name == "posix"


def _test_config(**overrides):
    """构造测试用 Config（dev 模式、固定假 HMAC 密钥，无真实凭据）。"""
    from botville_gateway.config import Config

    base = dict(
        host="127.0.0.1",
        port=0,
        dev_mode=True,
        auth_token=None,
        silent_hours_enabled=False,
        hmac_secret=b"test-hmac-secret" * 2,
    )
    base.update(overrides)
    return Config(**base)

# #192 时期的表结构：没有 purged_at，也没有清理索引。
_LEGACY_DDL = (
    "CREATE TABLE as_transactions ("
    "txn_id TEXT PRIMARY KEY, body TEXT NOT NULL, "
    "received_at TEXT NOT NULL, processed_at TEXT)"
)


class _TmpDb(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "as_transactions.db")

    def tearDown(self):
        self._tmp.cleanup()


class TestJoinSafetyStore(_TmpDb):
    def test_legacy_migration_restart_and_hashed_invitation(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(_LEGACY_DDL)
        conn.close()
        store = AsTransactionStore(connect(self.db_path))
        store.invitation_update("@ai_test:local", "!private:local", "invite", True, "$private")
        store.cleanup_enqueue("@ai_test:local", "!private:local")
        store._conn.close()
        store = AsTransactionStore(connect(self.db_path))
        self.addCleanup(store._conn.close)
        self.assertIs(store.invitation("@ai_test:local", "!private:local"), True)
        self.assertEqual(len(store.cleanup_due(0)), 1)
        row = tuple(store._conn.execute("SELECT * FROM as_invitations").fetchone())
        self.assertEqual(len(row[0]), 64)
        self.assertNotIn("private", str(row))
        self.assertNotIn("ai_test", str(row))
        store.invitation_update("@ai_test:local", "!private:local", "leave", event_id="$leave")
        store.invitation_update("@ai_test:local", "!private:local", "invite", event_id="$private")
        self.assertIsNone(store.invitation("@ai_test:local", "!private:local"))

    def test_memory_invites_bounded_and_unknown_denied(self):
        store = AsTransactionStore(connect(":memory:"))
        self.addCleanup(store._conn.close)
        for index in range(1001):
            store.invitation_update("@ai_test:local", "!" + str(index), "invite")
        self.assertIsNone(store.invitation("@ai_test:local", "!0"))
        self.assertIsNone(store.invitation("@ai_unknown:local", "!1000"))
        self.assertIs(store.invitation("@ai_test:local", "!1000"), False)
        self.assertEqual(store._conn.execute("SELECT COUNT(*) FROM as_invitations").fetchone()[0], 1000)

    def test_memory_observer_saturation_is_fail_closed(self):
        store = AsTransactionStore(connect(":memory:"))
        self.addCleanup(store._conn.close)
        for index in range(1001):
            store.invitation_update("p", "!" + str(index), "invite", event_id="$" + str(index))
        self.assertIsNone(store.invitation("p", "!1000"))
        self.assertEqual(store._conn.execute("SELECT COUNT(*) FROM as_invitation_events").fetchone()[0], 1000)
        store.invitation_update("p", "!0", "leave", event_id="$leave")
        store.invitation_update("p", "!0", "invite", event_id="$0")
        self.assertIsNone(store.invitation("p", "!0"))

    def test_cleanup_backoff_is_capped_and_enqueue_idempotent(self):
        store = AsTransactionStore(connect(self.db_path))
        self.addCleanup(store._conn.close)
        store.cleanup_enqueue("p", "r")
        key = store.room_persona_key("p", "r")
        for _ in range(20):
            store.cleanup_failed(key, 100)
        store.cleanup_enqueue("p", "r")
        self.assertEqual(store.cleanup_due(3699), [])
        row = store.cleanup_due(3700)[0]
        self.assertEqual(row["attempts"], 20)
        self.assertEqual(row["next_at"], 3700)
        store.cleanup_done(key)
        self.assertFalse(store.cleanup_pending("p", "r"))


# ---------------------------------------------------------------------------
# 1. 文件权限
# ---------------------------------------------------------------------------
@unittest.skipUnless(_POSIX, "POSIX file modes only")
class TestAsTxnFileModes(_TmpDb):
    def test_new_db_created_0600(self):
        conn = connect(self.db_path)
        try:
            mode = os.stat(self.db_path).st_mode & 0o777
        finally:
            conn.close()
        self.assertEqual(mode, 0o600)

    def test_existing_db_mode_is_untouched(self):
        # 部署侧刻意设成 0644：打开后不得悄悄改成 0600。
        os.close(os.open(self.db_path, os.O_CREAT | os.O_RDWR, 0o644))
        os.chmod(self.db_path, 0o644)
        conn = connect(self.db_path)
        try:
            mode = os.stat(self.db_path).st_mode & 0o777
        finally:
            conn.close()
        self.assertEqual(mode, 0o644)

    def test_wal_sidecars_are_0600_when_main_is_0600(self):
        # WAL 模式下 -wal 里是未 checkpoint 的原始事务体，与主库同级敏感。
        conn = connect(self.db_path)
        try:
            # 插一行让 WAL 里确实有内容；旁文件通常在这一步已存在。
            conn.execute(
                "INSERT INTO as_transactions(txn_id, body, received_at)"
                " VALUES ('w1', '{}', '2026-01-01T00:00:00Z')"
            )
            conn.commit()
            seen = []
            for suffix in ("-wal", "-shm"):
                sidecar = self.db_path + suffix
                if os.path.exists(sidecar):
                    seen.append(suffix)
                    self.assertEqual(
                        os.stat(sidecar).st_mode & 0o777,
                        0o600,
                        f"{suffix} should be 0600 alongside a 0600 main db",
                    )
            # 至少 -wal 应该在 WAL 模式下存在（连接仍打开）。
            self.assertIn("-wal", seen)
        finally:
            conn.close()

    def test_existing_0644_db_sidecars_not_forced(self):
        # 主库不是 0600 → 不做反向收紧（尊重部署侧的选择）。
        os.close(os.open(self.db_path, os.O_CREAT | os.O_RDWR, 0o644))
        os.chmod(self.db_path, 0o644)
        conn = connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO as_transactions(txn_id, body, received_at)"
                " VALUES ('w2', '{}', '2026-01-01T00:00:00Z')"
            )
            conn.commit()
            sidecar = self.db_path + "-wal"
            if os.path.exists(sidecar):
                self.assertNotEqual(os.stat(sidecar).st_mode & 0o777, 0o600)
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 2. 迁移：清理索引
# ---------------------------------------------------------------------------
class TestAsTxnPurgeIndex(_TmpDb):
    _PURGE_QUERY = (
        "UPDATE as_transactions SET body = '', purged_at = ?"
        " WHERE received_at < ? AND purged_at IS NULL"
    )

    def _explain(self, conn, query):
        rows = conn.execute("EXPLAIN QUERY PLAN " + query, ("2026-01-01T00:00:00Z",) * 2)
        return " ".join(str(part) for row in rows for part in tuple(row))

    def test_index_created_on_fresh_db(self):
        conn = connect(self.db_path)
        try:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                    " AND tbl_name = 'as_transactions'"
                )
            }
        finally:
            conn.close()
        self.assertIn("idx_as_transactions_purge", names)

    def test_index_added_to_legacy_db_and_used_by_purge_query(self):
        legacy = sqlite3.connect(self.db_path)
        legacy.execute(_LEGACY_DDL)
        legacy.execute(
            "INSERT INTO as_transactions(txn_id, body, received_at)"
            " VALUES ('legacy1', '{\"events\":[]}', '2026-01-01T00:00:00Z')"
        )
        legacy.commit()
        legacy.close()

        conn = connect(self.db_path)
        try:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                    " AND tbl_name = 'as_transactions'"
                )
            }
            self.assertIn("idx_as_transactions_purge", names)
            plan = self._explain(conn, self._PURGE_QUERY)
            self.assertIn("idx_as_transactions_purge", plan)
            # 旧行仍在、且未被误清理。
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM as_transactions WHERE txn_id='legacy1'"
                ).fetchone()[0],
                1,
            )
        finally:
            conn.close()

    def test_migrate_is_idempotent_on_reopen(self):
        conn = connect(self.db_path)
        conn.close()
        conn = connect(self.db_path)  # 二次迁移不应报错
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='index'"
                " AND name='idx_as_transactions_purge'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 1)


# ---------------------------------------------------------------------------
# 3. 后台清理调度
# ---------------------------------------------------------------------------
class _CountingWait:
    """注入用的等待函数：前 `ticks` 次返回 False（该跑清理），之后返回 True。

    返回 True 即让调度循环退出——测试因此不依赖真实 sleep，也不依赖 stop 事件。
    """

    def __init__(self, ticks: int):
        self._remaining = ticks
        self._lock = threading.Lock()
        self.released = threading.Event()
        self.calls = 0

    def __call__(self, stop, timeout):  # noqa: ARG002 - 语义见 start_purge_scheduler
        with self._lock:
            self.calls += 1
            if self._remaining <= 0:
                self.released.set()
                return True
            self._remaining -= 1
        return stop.wait(0.001)  # 极短真实等待，让出 GIL


class TestAsTxnPurgeScheduler(_TmpDb):
    def test_scheduler_purges_repeatedly(self):
        wait = _CountingWait(ticks=3)
        store = AsTransactionStore(connect(self.db_path), purge_retention_days=1, _wait_fn=wait)
        purges = []
        store.run_purge_safely = lambda: purges.append(1)  # type: ignore[method-assign]
        try:
            thread = store.start_purge_scheduler(3600)
            self.assertTrue(wait.released.wait(2.0), "scheduler did not tick in time")
            thread.join(timeout=2)
        finally:
            store.stop_purge_scheduler(timeout=1.0)
        # 3 次超时 → 3 次清理（第一次不在启动时，而在首个等待之后）。
        self.assertEqual(len(purges), 3)
        self.assertFalse(thread.is_alive())

    def test_scheduler_stop_event_prevents_further_purges(self):
        store = AsTransactionStore(connect(self.db_path), purge_retention_days=1)
        purges = []
        store.run_purge_safely = lambda: purges.append(1)  # type: ignore[method-assign]
        thread = store.start_purge_scheduler(3600)
        store.stop_purge_scheduler(timeout=2.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(purges, [])

    def test_zero_interval_rejected_by_scheduler(self):
        store = AsTransactionStore(connect(self.db_path), purge_retention_days=1)
        with self.assertRaises(ValueError):
            store.start_purge_scheduler(0)

    def test_server_interval_zero_disables_purge(self):
        # 间隔 0 的判定在 `build_server`（http_server.py）：它不给
        # build_as_txn_store 传 retention_days，于是既不做启动清理也不起线程。
        from botville_gateway.http_server import build_server

        conn = connect(self.db_path)
        conn.execute(
            "INSERT INTO as_transactions(txn_id, body, received_at)"
            " VALUES ('old0', '{\"events\":[]}', '2020-01-01T00:00:00Z')"
        )
        conn.commit()
        server = build_server(
            _test_config(
                hs_token="test-hs-token-0123456789abcdefghijklmnop",
                as_txn_db_path=self.db_path,
                as_txn_purge_interval_seconds=0,
            )
        )
        try:
            row = conn.execute(
                "SELECT body, purged_at FROM as_transactions WHERE txn_id='old0'"
            ).fetchone()
            self.assertNotEqual(row[0], "")
            self.assertIsNone(row[1])
            self.assertIsNone(server.as_txn_store._purge_thread)
        finally:
            server.server_close()
            conn.close()

    def test_build_without_retention_does_nothing(self):
        # 存储层契约：retention_days=None → 不清理、不起线程（与间隔无关）。
        conn = connect(self.db_path)
        conn.execute(
            "INSERT INTO as_transactions(txn_id, body, received_at)"
            " VALUES ('old_none', '{\"events\":[]}', '2020-01-01T00:00:00Z')"
        )
        conn.commit()
        store = build_as_txn_store(
            self.db_path, purge_retention_days=None, purge_interval_seconds=60
        )
        try:
            row = conn.execute(
                "SELECT body, purged_at FROM as_transactions WHERE txn_id='old_none'"
            ).fetchone()
            self.assertNotEqual(row[0], "")
            self.assertIsNone(row[1])
            self.assertIsNone(store._purge_thread)
        finally:
            store.stop_purge_scheduler(timeout=1.0)
            conn.close()

    def test_build_purges_synchronously_at_startup(self):
        conn = connect(self.db_path)
        conn.execute(
            "INSERT INTO as_transactions(txn_id, body, received_at)"
            " VALUES ('old1', '{\"events\":[]}', '2020-01-01T00:00:00Z')"
        )
        conn.commit()
        store = build_as_txn_store(
            self.db_path, purge_retention_days=1, purge_interval_seconds=0
        )
        try:
            row = conn.execute(
                "SELECT body, purged_at FROM as_transactions WHERE txn_id='old1'"
            ).fetchone()
            self.assertEqual(row[0], "")
            self.assertIsNotNone(row[1])
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 4. 配置校验与库路径无关；常量单一定义
# ---------------------------------------------------------------------------
class TestAsTxnConfigRegardlessOfDbPath(unittest.TestCase):
    _BASE = {"GATEWAY_DEV_MODE": "true"}

    def test_invalid_retention_rejected_without_db_path(self):
        env = dict(self._BASE, GATEWAY_AS_TXN_RETENTION_DAYS="0")
        self.assertNotIn("GATEWAY_AS_TXN_DB_PATH", env)
        with self.assertRaises(ConfigError):
            load_config(env)

    def test_invalid_interval_rejected_without_db_path(self):
        env = dict(self._BASE, GATEWAY_AS_TXN_PURGE_INTERVAL_SECONDS="59")
        self.assertNotIn("GATEWAY_AS_TXN_DB_PATH", env)
        with self.assertRaises(ConfigError):
            load_config(env)

    def test_non_integer_rejected_without_db_path(self):
        with self.assertRaises(ConfigError):
            load_config(
                dict(self._BASE, GATEWAY_AS_TXN_PURGE_INTERVAL_SECONDS="soon")
            )

    def test_direct_config_also_validated(self):
        # 直接构造 Config（不经 load_config）同样 fail closed。
        from botville_gateway.config import Config

        with self.assertRaises(ConfigError):
            Config(
                host="127.0.0.1",
                port=0,
                dev_mode=True,
                hmac_secret=b"test-hmac-secret" * 2,
                as_txn_purge_interval_seconds=59,
            )

    def test_min_interval_constant_single_definition(self):
        self.assertIs(AS_TXN_PURGE_INTERVAL_MIN_SECONDS, CONFIG_MIN_INTERVAL)
        self.assertEqual(CONFIG_MIN_INTERVAL, 60)


# ---------------------------------------------------------------------------
# 5. 未处理事务的读取顺序与批量上限
# ---------------------------------------------------------------------------
class TestAsTxnUnprocessedOrdering(_TmpDb):
    def setUp(self):
        super().setUp()
        self.conn = connect(self.db_path)
        self.store = AsTransactionStore(self.conn)

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def test_insertion_order_within_same_second(self):
        # received_at 只有秒精度：同一秒内必须按插入顺序（rowid）返回。
        same_second = "2026-01-01T00:00:00Z"
        for txn_id in ("zzz", "aaa", "mmm", "bbb"):
            self.store.store_transaction(txn_id, "{}")
        self.conn.execute("UPDATE as_transactions SET received_at = ?", (same_second,))
        self.conn.commit()
        self.assertEqual(
            [row[0] for row in self.store.get_unprocessed_transactions()],
            ["zzz", "aaa", "mmm", "bbb"],
        )

    def test_limit_respected(self):
        for txn_id in ("t1", "t2", "t3", "t4", "t5"):
            self.store.store_transaction(txn_id, "{}")
        self.assertEqual(
            [row[0] for row in self.store.get_unprocessed_transactions(limit=2)],
            ["t1", "t2"],
        )

    def test_default_limit_is_100(self):
        with self.conn:
            self.conn.executemany(
                "INSERT INTO as_transactions(txn_id, body, received_at)"
                " VALUES (?, '{}', '2026-01-01T00:00:00Z')",
                [(f"bulk{i:03d}",) for i in range(150)],
            )
        self.assertEqual(len(self.store.get_unprocessed_transactions()), 100)

    def test_invalid_limit_rejected(self):
        with self.assertRaises(ValueError):
            self.store.get_unprocessed_transactions(limit=0)
        with self.assertRaises(ValueError):
            self.store.get_unprocessed_transactions(limit=-1)
        with self.assertRaises(ValueError):
            self.store.get_unprocessed_transactions(limit=True)  # type: ignore[arg-type]

    def test_processed_rows_excluded(self):
        self.store.store_transaction("p1", "{}")
        self.store.store_transaction("p2", "{}")
        self.store.mark_processed("p1")
        self.assertEqual(
            [row[0] for row in self.store.get_unprocessed_transactions()], ["p2"]
        )


if __name__ == "__main__":
    unittest.main()
