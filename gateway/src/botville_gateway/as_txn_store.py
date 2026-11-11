# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""Matrix AS 事务持久化存储（协议层，B 线 as-transactions）。

设计约束：
- SQLite，WAL + synchronous=FULL（持久化完成后才返回 200）。
- 单连接 + 进程内锁，线程安全（ThreadingHTTPServer 多线程写）。
- **单进程假设**：判重与插入靠 `threading.Lock` 串行化，SQLite 自身只保证
  文件级一致性、不保证「先查后插」的原子性。因此同一事务库上只允许**一个**
  网关进程接收回调；多副本/多进程共用同一库属不支持的部署（并发下可能撞
  `UNIQUE`/`database is locked` → 500，或两侧都读到不存在后重复插入失败）。
- 幂等：同一 txn_id 第二次写直接返回 False（已存在），不重复落库。
- 表含 txn_id 主键、原始请求体（文本）、接收时间（ISO UTC）、
  可空 processed_at（供后续消费者标记已处理，不在 #192 消费）、
  可空 purged_at（保留期清理标记，见下）。

保留期与清理（contract.md §1.7.7）：
  超过保留期的事务**不删行**，只把原始请求体清空（`body = ''`）并写入
  `purged_at`，形成「墓碑」：txn_id / received_at / processed_at 全部保留。
  于是清理之后的重放仍按幂等返回 200，且**不会**被重新插入或重新记为未处理。
  墓碑只保留元数据、永不删除，库会继续增长（运维可在停机时归档/压缩）。
  清理对**未处理**的过期事务同样生效（body 清空后即不再是可消费事务，
  即「过期未消费 = 丢弃」），只按条数记日志，不记录 txn_id 或事件内容。

消费侧钩子：
  `get_unprocessed_transactions()` 返回 txn_id 有序列表，供后续 PR 读取
  并交给角色引擎 / 记忆 / 主动发言模块处理；处理后应调用
  `mark_processed(txn_id)` 更新 processed_at，防止重复消费。
  目前（#192 起）只定义接口，**不**向任何角色引擎分发事件；消费留给后续 PR。
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .timeutil import iso_utc, utcnow

_log = logging.getLogger(__name__)

# 清理任务的最小间隔（秒）：与 moments 保留期清理一致（0 = 关闭）。
AS_TXN_PURGE_INTERVAL_MIN_SECONDS = 60

# DDL：txn_id 主键保证幂等；body 保存原始 JSON 文本（清理后为空串）；
# processed_at 为 NULL 表示尚未被消费侧处理；purged_at 非空 = 保留期清理过的墓碑。
_DDL = """
CREATE TABLE IF NOT EXISTS as_transactions (
    txn_id       TEXT    PRIMARY KEY,
    body         TEXT    NOT NULL,
    received_at  TEXT    NOT NULL,
    processed_at TEXT,
    purged_at    TEXT
);
"""

# 内存库 / URI 形式不是真实文件，跳过 0600 预建。
_NON_FILE_DB_PREFIXES = (":memory:", "file:")


def _precreate_private_file(path: Path) -> None:
    """新建库文件时以 `0600` 创建（库内含原始事务体，按敏感用户数据管理）。

    只作用于**新文件**：已存在的库保持部署侧设定的权限，不悄悄改动机器上
    既有的文件模式。`:memory:` / `file:` URI 不是文件，直接跳过。
    """
    try:
        os.close(os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600))
    except FileExistsError:
        pass


def _migrate(conn: sqlite3.Connection) -> None:
    """就地迁移 #192 建出的旧库：补 `purged_at` 列（幂等）。

    旧库的 `body` 是 `NOT NULL`，清理时写空串而不是 NULL，因此无需重建表；
    ALTER 只加列、不动既有行，旧库升级后行为与新建库一致。
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(as_transactions)")}
    if "purged_at" not in columns:
        conn.execute("ALTER TABLE as_transactions ADD COLUMN purged_at TEXT")


def connect(db_path: str) -> sqlite3.Connection:
    """打开（必要时创建）AS 事务 SQLite 库：WAL + synchronous=FULL。

    synchronous=FULL：每次 WAL 提交前 fsync，确保「200 返回前数据已落盘」
    的合规保证（Matrix AS 规范要求幂等存储后才应答）。
    """
    path = Path(db_path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    if not db_path.startswith(_NON_FILE_DB_PREFIXES):
        _precreate_private_file(path)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    with conn:
        conn.execute(_DDL)
        _migrate(conn)
    return conn


class AsTransactionStore:
    """线程安全的 AS 事务持久化存储（单进程假设，见模块注释）。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        purge_retention_days: Optional[int] = None,
    ) -> None:
        self._conn = conn
        self._lock = threading.Lock()
        # 保留期天数；None = 不启用清理（直接构造 / 测试重开库时）。
        self._purge_retention_days = purge_retention_days
        self._purge_stop: Optional[threading.Event] = None
        self._purge_thread: Optional[threading.Thread] = None

    def store_transaction(self, txn_id: str, body_text: str) -> bool:
        """持久化一条 AS 事务（锁内判重 + 插入，单事务提交）。

        返回 True 表示新写入，False 表示 txn_id 已存在（幂等重放）。
        **已清理（墓碑）的 txn_id 同样返回 False**：重放不会重新落库，也不会
        把墓碑复活成「未处理」。数据库写入失败会抛出 sqlite3.Error，调用方应
        映射为 500 M_UNKNOWN。本方法**不**进行任何事件分发。
        """
        now_str = iso_utc(utcnow())
        with self._lock:
            existing = self._conn.execute(
                "SELECT 1 FROM as_transactions WHERE txn_id = ?", (txn_id,)
            ).fetchone()
            if existing is not None:
                return False
            with self._conn:
                self._conn.execute(
                    "INSERT INTO as_transactions(txn_id, body, received_at)"
                    " VALUES (?, ?, ?)",
                    (txn_id, body_text, now_str),
                )
            return True

    def get_unprocessed_transactions(self) -> List[Tuple[str, str, str]]:
        """返回尚未处理（processed_at IS NULL）的事务列表。

        每条为 (txn_id, body, received_at)，按 received_at 升序排列。
        **不含墓碑**（purged_at IS NOT NULL）：过期未处理的事务在清理时被丢弃，
        不再交给消费侧。

        TODO（后续 PR）：实现消费者调用本接口并触发角色引擎 / 记忆 /
        主动发言逻辑，处理后调用 mark_processed(txn_id)。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT txn_id, body, received_at"
                " FROM as_transactions"
                " WHERE processed_at IS NULL AND purged_at IS NULL"
                " ORDER BY received_at ASC"
            ).fetchall()
            return [(r["txn_id"], r["body"], r["received_at"]) for r in rows]

    def mark_processed(self, txn_id: str, processed_at: Optional[str] = None) -> None:
        """标记事务已处理；processed_at 缺省时用当前 UTC 时间。"""
        ts = processed_at or iso_utc(utcnow())
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "UPDATE as_transactions SET processed_at = ?"
                    " WHERE txn_id = ? AND processed_at IS NULL",
                    (ts, txn_id),
                )

    # ------------------------------------------------------- retention
    def purge(
        self, retention_days: int, now: Optional[datetime] = None
    ) -> Dict[str, int]:
        """清空超过保留期事务的原始请求体，保留墓碑（**不删行**）。

        - 只处理 `purged_at IS NULL` 且 `received_at` 早于截止时间的行，因此
          重复调用是幂等的（第二次返回 0）。
        - `body` 置空串、写入 `purged_at`；`txn_id` / `received_at` /
          `processed_at` 原样保留，重放的幂等语义因此不变。
        - 未处理（`processed_at IS NULL`）的过期行同样清理，即「过期未消费 =
          丢弃」：`get_unprocessed_transactions()` 之后不再返回它们。
        - 返回 `{"bodies_purged": n, "unprocessed_dropped": m}`（只用于日志，
          不含 txn_id / 事件内容）。
        """
        if retention_days < 1:
            raise ValueError("retention_days must be >= 1")
        current = now if now is not None else utcnow()
        cutoff = iso_utc(current - timedelta(days=retention_days))
        purged_at = iso_utc(current)
        with self._lock:
            with self._conn:
                dropped = self._conn.execute(
                    "SELECT COUNT(*) FROM as_transactions"
                    " WHERE received_at < ? AND purged_at IS NULL"
                    " AND processed_at IS NULL",
                    (cutoff,),
                ).fetchone()[0]
                cur = self._conn.execute(
                    "UPDATE as_transactions SET body = '', purged_at = ?"
                    " WHERE received_at < ? AND purged_at IS NULL",
                    (purged_at, cutoff),
                )
                purged = cur.rowcount
        return {"bodies_purged": purged, "unprocessed_dropped": int(dropped)}

    def run_purge_safely(self) -> Optional[Dict[str, int]]:
        """跑一次清理；任何异常只记日志、不外抛（清理失败不能影响收事务）。

        日志只记条数与异常类型名：不写 txn_id、请求体或任何 token。
        """
        retention = self._purge_retention_days
        if retention is None:
            return None
        try:
            result = self.purge(retention)
        except Exception as exc:  # noqa: BLE001 - 清理是尽力而为
            _log.warning(
                "AS transaction purge failed: %s: %s", type(exc).__name__, exc
            )
            return None
        if result["bodies_purged"]:
            _log.info(
                "AS transaction purge cleared %d expired body/bodies "
                "(%d unprocessed dropped)",
                result["bodies_purged"],
                result["unprocessed_dropped"],
            )
        return result

    def start_purge_scheduler(self, interval_seconds: float) -> threading.Thread:
        """后台守护线程：每隔 interval_seconds 跑一次 `run_purge_safely`。

        首次在等待一个间隔后执行（启动时那次由 `build_as_txn_store` 同步执行）。
        `stop_purge_scheduler()` 可让线程尽快退出。
        """
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be > 0")
        self.stop_purge_scheduler()
        stop = threading.Event()
        self._purge_stop = stop

        def _loop() -> None:
            while not stop.wait(interval_seconds):
                self.run_purge_safely()

        thread = threading.Thread(target=_loop, name="as-txn-purge", daemon=True)
        self._purge_thread = thread
        thread.start()
        return thread

    def stop_purge_scheduler(self, timeout: Optional[float] = 5.0) -> None:
        stop = self._purge_stop
        thread = self._purge_thread
        if stop is not None:
            stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        self._purge_stop = None
        self._purge_thread = None


def build_as_txn_store(
    db_path: Optional[str],
    *,
    purge_retention_days: Optional[int] = None,
    purge_interval_seconds: int = 0,
) -> Optional[AsTransactionStore]:
    """按配置装配 AS 事务存储；db_path 为空/None → None（调用方需处理 500）。

    `purge_retention_days` 非 None 时启用保留期清理：启动阶段同步跑一次
    （失败只记日志），`purge_interval_seconds > 0` 时再启动后台守护线程按间隔
    执行。只读地重开同一库（测试 / 运维检查）不传该参数即可，不会启动线程。
    """
    if not db_path:
        return None
    conn = connect(db_path)
    store = AsTransactionStore(conn, purge_retention_days=purge_retention_days)
    if purge_retention_days is not None:
        store.run_purge_safely()
        if purge_interval_seconds > 0:
            store.start_purge_scheduler(purge_interval_seconds)
    return store
