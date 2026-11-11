# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""Matrix AS 事务持久化存储（协议层，B 线 as-transactions）。

设计约束：
- SQLite，WAL + synchronous=FULL（持久化完成后才返回 200）。
- 单连接 + 进程内锁，线程安全（ThreadingHTTPServer 多线程写）。
- 幂等：同一 txn_id 第二次写直接返回 False（已存在），不重复落库。
- 表含 txn_id 主键、原始请求体（文本）、接收时间（ISO UTC）、
  可空 processed_at（供后续消费者标记已处理，本 PR 不消费）。

消费侧钩子：
  `get_unprocessed_transactions()` 返回 txn_id 有序列表，供后续 PR 读取
  并交给角色引擎 / 记忆 / 主动发言模块处理；处理后应调用
  `mark_processed(txn_id)` 更新 processed_at，防止重复消费。
  本 PR 只定义接口，**不**向任何角色引擎分发事件。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import List, Optional, Tuple

from .timeutil import iso_utc, utcnow

# DDL：txn_id 主键保证幂等；body 保存原始 JSON 文本；
# processed_at 为 NULL 表示尚未被消费侧处理。
_DDL = """
CREATE TABLE IF NOT EXISTS as_transactions (
    txn_id       TEXT    PRIMARY KEY,
    body         TEXT    NOT NULL,
    received_at  TEXT    NOT NULL,
    processed_at TEXT
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    """打开（必要时创建）AS 事务 SQLite 库：WAL + synchronous=FULL。

    synchronous=FULL：每次 WAL 提交前 fsync，确保「200 返回前数据已落盘」
    的合规保证（Matrix AS 规范要求幂等存储后才应答）。
    """
    path = Path(db_path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    with conn:
        conn.execute(_DDL)
    return conn


class AsTransactionStore:
    """线程安全的 AS 事务持久化存储。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.Lock()

    def store_transaction(self, txn_id: str, body_text: str) -> bool:
        """持久化一条 AS 事务（锁内判重 + 插入，单事务提交）。

        返回 True 表示新写入，False 表示 txn_id 已存在（幂等重放）。
        数据库写入失败会抛出 sqlite3.Error，调用方应映射为 500 M_UNKNOWN。
        本方法**不**进行任何事件分发；消费留给后续 PR（见模块注释）。
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

        TODO（后续 PR）：实现消费者调用本接口并触发角色引擎 / 记忆 /
        主动发言逻辑，处理后调用 mark_processed(txn_id)。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT txn_id, body, received_at"
                " FROM as_transactions"
                " WHERE processed_at IS NULL"
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


def build_as_txn_store(db_path: Optional[str]) -> Optional[AsTransactionStore]:
    """按配置装配 AS 事务存储；db_path 为空/None → None（调用方需处理 500）。"""
    if not db_path:
        return None
    conn = connect(db_path)
    return AsTransactionStore(conn)
