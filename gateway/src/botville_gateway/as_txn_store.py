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
  `get_unprocessed_transactions(limit=100)` 按 `rowid`（插入顺序）升序返回一批
  尚未处理的事务 `(txn_id, body, received_at)`，供后续 PR 读取并交给角色引擎 /
  记忆 / 主动发言模块处理；处理后应调用 `mark_processed(txn_id)` 更新
  processed_at，防止重复消费。
  目前（#192 起）只定义接口，**不**向任何角色引擎分发事件；消费留给后续 PR。

消费骨架（B 线 PR1，contract §1.7.8）：
  在 #192/#194 的表结构上**增量**补四列（`attempts` / `last_error_type` /
  `failed_at` / `next_attempt_at`），迁移对旧库幂等。新增：
  - `get_consumable_batch(now, limit)`：按 rowid 取「未处理、未清理、未 failed、
    已到 next_attempt_at」的一批事务（含 attempts），供 `as_consumer` 逐条处理；
  - `record_failure()` / `record_transient()`：写失败次数、异常**类型名**与下次
    重试时间（不写异常内容、不写正文）；
  - `oldest_unprocessed_received_at()`：积压告警用的最老未处理接收时间；
  - `set_on_new_transaction(cb)`：新事务落库成功后在**锁外**通知消费者唤醒
    （回调异常吞掉；实现不得记录 txn_id）。
  `store_transaction` 的返回值与幂等语义保持不变。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .timeutil import iso_utc, utcnow

_log = logging.getLogger(__name__)

# 清理任务的最小间隔（秒）：与 moments 保留期清理一致（0 = 关闭）。
# **唯一定义处**：`config.py` 从这里 import 后再做环境变量校验，避免两处各写
# 一份常量而漂移（本模块不 import config，无循环依赖）。
AS_TXN_PURGE_INTERVAL_MIN_SECONDS = 60

# DDL：txn_id 主键保证幂等；body 保存原始 JSON 文本（清理后为空串）；
# processed_at 为 NULL 表示尚未被消费侧处理；purged_at 非空 = 保留期清理过的墓碑。
_DDL = """
CREATE TABLE IF NOT EXISTS as_transactions (
    txn_id          TEXT    PRIMARY KEY,
    body            TEXT    NOT NULL,
    received_at     TEXT    NOT NULL,
    processed_at    TEXT,
    purged_at       TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error_type TEXT,
    failed_at       TEXT,
    next_attempt_at TEXT
);
"""

# 清理查询（`WHERE received_at < ? AND purged_at IS NULL`）的**部分索引**：
# 只索引尚未清理的行，规模随保留窗口而不是随库总行数增长（墓碑不参与）。
# `CREATE INDEX IF NOT EXISTS` 是幂等的加法迁移：新库与 #192/#194 时期的旧库
# 走同一条 `_migrate` 路径都能补上。SQLite 3.8+ 支持 partial index。
_PURGE_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_as_transactions_purge
    ON as_transactions(received_at) WHERE purged_at IS NULL;
"""

# 消费取批（contract §1.7.8）的部分索引：只索引「待消费」的行
# （`processed_at` / `purged_at` / `failed_at` 均为 NULL）。与清理索引同样用
# `CREATE INDEX IF NOT EXISTS`，新库与 #192/#194 旧库走同一条迁移路径都幂等。
# 取批查询仍按 `rowid` 升序（插入顺序），索引只负责快速排除已处理/墓碑/failed 行。
_CONSUMABLE_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_as_transactions_consumable
    ON as_transactions(failed_at) WHERE processed_at IS NULL AND purged_at IS NULL;
"""

# 发言去重账本（B 线 PR2，contract §1.7.8 ④）：键是
# `speech_idempotency_key(persona, event_id)`（`spk_<sha256hex>`），只存时间戳。
# **不含**正文、persona 或 event_id：键本身已不可逆，存其他标识只会扩大泄露面。
# 幂等：`CREATE TABLE IF NOT EXISTS`，旧库升级直接补表。
_SPEECH_DEDUPE_DDL = """
CREATE TABLE IF NOT EXISTS as_speech_dedupe (
    key        TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    sent_at    TEXT
);
"""

# 邀请台账 TTL（秒）：超过即视为过期，不再作为 join 快路径，转 `/sync` 权威确认。
# **唯一定义处**：config.py 从这里 import 默认值与范围。
DEFAULT_INVITE_LEDGER_TTL_SECONDS = 86400
INVITE_LEDGER_TTL_MIN_SECONDS = 60
INVITE_LEDGER_TTL_MAX_SECONDS = 30 * 86400

# 内存库 / URI 形式不是真实文件，跳过 0600 预建。
_NON_FILE_DB_PREFIXES = (":memory:", "file:")


def _precreate_private_file(path: Path) -> None:
    """新建库文件时以 `0600` 创建（库内含原始事务体，按敏感用户数据管理）。

    只作用于**新文件**：已存在的库保持部署侧设定的权限，不悄悄改动机器上
    既有的文件模式。`:memory:` / `file:` URI 不是文件，直接跳过。

    内部调用 `_create_private_file`：文件不存在时以 `O_CREAT|O_EXCL|0600`
    独占创建，文件已存在时静默返回（不改权限、不报错）。
    """
    _create_private_file(path)


def _create_private_file(path: Path) -> bool:
    """尝试以 `0600` 独占创建；成功（我们新建了它）返回 True，已存在返回 False。"""
    try:
        os.close(os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600))
    except FileExistsError:
        return False
    return True


def _lock_down_wal_sidecars(path: Path) -> None:
    """把 SQLite WAL 的 `-wal` / `-shm` 旁文件收紧到 `0600`（尽力而为）。

    WAL 模式下 `-wal` 里是**尚未 checkpoint 的原始事务体**，与主库同级敏感；
    SQLite 按主库的模式创建这两个旁文件，但主库若是**我们新建**的 0600、
    旁文件仍可能带上 umask 的宽松位（部分版本 / 先建旁文件的路径）。因此
    主库是 0600 时，对已存在的旁文件补一次 `chmod 0600`。

    只处理 `0600` 的主库：部署侧刻意放宽过的主库（例如共享只读副本）不被
    这里反向收紧；失败（不存在 / 权限不足 / 平台不支持）一律忽略——收不紧
    旁文件不应该阻止网关启动。
    """
    try:
        if (os.stat(str(path)).st_mode & 0o777) != 0o600:
            return
    except OSError:
        return
    for suffix in ("-wal", "-shm"):
        sidecar = str(path) + suffix
        try:
            if os.path.exists(sidecar):
                os.chmod(sidecar, 0o600)
        except OSError:  # noqa: PERF203 - 尽力而为，逐个旁文件独立失败
            continue


def _migrate(conn: sqlite3.Connection) -> None:
    """就地迁移 #192 / #194 建出的旧库：补列与清理用的部分索引（幂等）。

    旧库的 `body` 是 `NOT NULL`，清理时写空串而不是 NULL，因此无需重建表；
    ALTER 只加列、不动既有行，旧库升级后行为与新建库一致。索引用
    `CREATE INDEX IF NOT EXISTS`，对新库与旧库都是加法、可重复执行。

    消费骨架（contract §1.7.8）增量补四列：
      - `attempts`（普通失败次数，transient 不计数）；
      - `last_error_type`（只存异常类型名，不存内容）；
      - `failed_at`（达到重试上限后写入；非空 = 永不再取出）；
      - `next_attempt_at`（指数退避的下次可处理时间；空 = 立即可取）。
    四列都带默认值 / 可空，旧行升级后 `attempts=0`、其余为 NULL，即「立即可消费」。

    运维注意：首次在大库（#192/#194 时期积累的旧库）上执行本迁移时，两个
    **部分索引**（`idx_as_transactions_purge` 与 `idx_as_transactions_consumable`）
    要各扫一遍表来建立。墓碑越多、建得越久，那**一次**启动会明显变慢；
    之后每次启动都是 `IF NOT EXISTS` 的空操作，开销可忽略。建议在低峰期升级，
    或先停机 `VACUUM` / 归档旧墓碑再升级（见 contract §1.7.7 运维第 1 点）。

    B 线 PR2（contract §1.7.8 ④）再补一张**发言去重账本** `as_speech_dedupe`：
    `CREATE TABLE IF NOT EXISTS` 是纯加法，旧库升级即补表、不影响既有行。
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(as_transactions)")}
    if "purged_at" not in columns:
        conn.execute("ALTER TABLE as_transactions ADD COLUMN purged_at TEXT")
    if "attempts" not in columns:
        conn.execute(
            "ALTER TABLE as_transactions ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"
        )
    if "last_error_type" not in columns:
        conn.execute("ALTER TABLE as_transactions ADD COLUMN last_error_type TEXT")
    if "failed_at" not in columns:
        conn.execute("ALTER TABLE as_transactions ADD COLUMN failed_at TEXT")
    if "next_attempt_at" not in columns:
        conn.execute("ALTER TABLE as_transactions ADD COLUMN next_attempt_at TEXT")
    # 索引必须在补列之后建（旧库上引用 purged_at）。
    conn.execute(_PURGE_INDEX_DDL)
    # 消费取批查询（`processed_at IS NULL AND purged_at IS NULL AND failed_at IS NULL`）
    # 的部分索引：只覆盖「待消费」的行，规模随积压而不是随墓碑总数增长。
    conn.execute(_CONSUMABLE_INDEX_DDL)
    # B 线 PR2 发言去重账本（幂等，不影响既有行）。
    conn.execute(_SPEECH_DEDUPE_DDL)
    conn.execute("CREATE TABLE IF NOT EXISTS as_invitation_events (key TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE IF NOT EXISTS as_invitations ("
                 "key TEXT PRIMARY KEY, encrypted INTEGER NOT NULL,"
                 " created_at REAL NOT NULL DEFAULT 0)")
    # Additive migration for #226 ledgers: pre-TTL rows get created_at=0 and
    # are therefore already expired, falling back to authoritative /sync.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(as_invitations)")}
    if "created_at" not in columns:
        conn.execute("ALTER TABLE as_invitations ADD COLUMN created_at REAL NOT NULL DEFAULT 0")
    conn.execute("CREATE TABLE IF NOT EXISTS as_pending_cleanup ("
                 "key TEXT PRIMARY KEY, persona TEXT NOT NULL, room TEXT NOT NULL,"
                 " attempts INTEGER NOT NULL, next_at REAL NOT NULL)")


def connect(db_path: str) -> sqlite3.Connection:
    """打开（必要时创建）AS 事务 SQLite 库：WAL + synchronous=FULL。

    synchronous=FULL：每次 WAL 提交前 fsync，确保「200 返回前数据已落盘」
    的合规保证（Matrix AS 规范要求幂等存储后才应答）。
    """
    path = Path(db_path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    is_file = not db_path.startswith(_NON_FILE_DB_PREFIXES)
    if is_file:
        _precreate_private_file(path)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    with conn:
        conn.execute(_DDL)
        _migrate(conn)
    if is_file:
        # WAL 旁文件在 PRAGMA journal_mode=WAL 之后才可能出现，因此放在这里。
        _lock_down_wal_sidecars(path)
    return conn


class AsTransactionStore:
    """线程安全的 AS 事务持久化存储（单进程假设，见模块注释）。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        purge_retention_days: Optional[int] = None,
        *,
        invite_ttl_seconds: int = DEFAULT_INVITE_LEDGER_TTL_SECONDS,
        _wait_fn=None,
        _clock: Callable[[], float] = time.time,
    ) -> None:
        self._conn = conn
        self._invite_ttl = invite_ttl_seconds
        self._clock = _clock
        self._lock = threading.Lock()
        # 保留期天数；None = 不启用清理（直接构造 / 测试重开库时）。
        self._purge_retention_days = purge_retention_days
        self._purge_stop: Optional[threading.Event] = None
        self._purge_thread: Optional[threading.Thread] = None
        # 调度等待函数：`event.wait(timeout)` 的替代注入点，供测试跑快速时钟。
        # 生产路径保持 None → 使用 Event.wait（真实 sleep）。
        self._wait_fn = _wait_fn
        # 新事务唤醒回调（消费骨架）：store_transaction 写入成功后在**锁外**调用，
        # 异常吞掉（消费者没起来不能影响接收）。回调不得记录 txn_id。
        self._on_new_transaction: Optional[Callable[[], None]] = None

    def set_on_new_transaction(self, callback: Optional[Callable[[], None]]) -> None:
        """注册「新事务落库成功」回调（消费骨架用，见 contract §1.7.8）。

        只在 `store_transaction` 返回 True（**新写入**）时、且**锁已释放**后调用；
        幂等重放不唤醒。回调抛出的任何异常都被吞掉——唤醒失败只是让消费者等到
        下一次 5 秒兜底轮询，绝不能让 HS 的 200 变成 500。
        """
        self._on_new_transaction = callback

    def store_transaction(self, txn_id: str, body_text: str) -> bool:
        """持久化一条 AS 事务（锁内判重 + 插入，单事务提交）。

        返回 True 表示新写入，False 表示 txn_id 已存在（幂等重放）。
        **已清理（墓碑）的 txn_id 同样返回 False**：重放不会重新落库，也不会
        把墓碑复活成「未处理」。数据库写入失败会抛出 sqlite3.Error，调用方应
        映射为 500 M_UNKNOWN。本方法**不**进行任何事件分发。

        新写入成功后在**锁外**触发 `on_new_transaction` 回调（若已注册），
        用于立即唤醒进程内消费者；回调异常吞掉，不影响返回值。
        """
        # Actual persistence time for audit/retention, independent of consumer scheduling.
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
        # 锁外通知：消费者线程可能立刻开始处理，不能在持锁时唤醒（否则它要等到
        # 本函数返回才能拿锁，反而放大接收路径的锁等待）。
        callback = self._on_new_transaction
        if callback is not None:
            try:
                callback()
            except Exception:  # noqa: BLE001 - 唤醒失败只退化为兜底轮询
                pass
        return True

    # ------------------------------------------------------- consumer helpers
    # 消费侧单次拉取的默认条数上限：消费侧按批处理，避免把整个积压一次读进内存。
    DEFAULT_UNPROCESSED_LIMIT = 100

    def has_processable_transactions(self, now: datetime) -> bool:
        """Whether work is due now, excluding future retries and terminal rows."""
        with self._lock:
            return self._conn.execute(
                "SELECT 1 FROM as_transactions WHERE processed_at IS NULL"
                " AND purged_at IS NULL AND failed_at IS NULL"
                " AND (next_attempt_at IS NULL OR next_attempt_at <= ?) LIMIT 1",
                (iso_utc(now),),
            ).fetchone() is not None

    def get_consumable_batch(
        self,
        now: datetime,
        limit: int = DEFAULT_UNPROCESSED_LIMIT,
    ) -> List[Tuple[str, str, str, int]]:
        """按插入顺序取一批「当前可处理」的事务（消费骨架，contract §1.7.8）。

        「可处理」的条件：
          - `processed_at IS NULL`（尚未成功消费）；
          - `purged_at IS NULL`（未清理/墓碑）；
          - `failed_at IS NULL`（未达 5 次失败上限）；
          - `next_attempt_at` 为 NULL **或** ≤ now（退避到期）。

        返回 (txn_id, body, received_at, attempts) 四元组列表，按 `rowid` 升序
        （插入顺序），单批最多 `limit` 条。`attempts` 供消费者计算下次退避。
        """
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValueError(f"limit must be an integer, got {limit!r}")
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")
        now_str = iso_utc(now)
        with self._lock:
            rows = self._conn.execute(
                "SELECT txn_id, body, received_at, attempts"
                " FROM as_transactions"
                " WHERE processed_at IS NULL AND purged_at IS NULL AND failed_at IS NULL"
                " AND (next_attempt_at IS NULL OR next_attempt_at <= ?)"
                " ORDER BY rowid ASC"
                " LIMIT ?",
                (now_str, limit),
            ).fetchall()
            return [(r["txn_id"], r["body"], r["received_at"], r["attempts"]) for r in rows]

    def record_failure(
        self,
        txn_id: str,
        error_type: str,
        now: datetime,
        *,
        next_attempt_at: Optional[datetime] = None,
        mark_failed: bool = False,
    ) -> None:
        """记录普通失败（attempts+1，存异常**类型名**，设退避时间）。

        `next_attempt_at` 缺省时用 `now`（无退避）。`mark_failed=True` 时额外
        写入 `failed_at = now`（达到重试上限），该行此后不再出现在
        `get_consumable_batch()` 结果中。

        安全约束：`error_type` 只存异常类型名（如 `'ValueError'`），不写异常
        消息正文；`txn_id` 不写日志（调用方同理）。
        """
        next_str = iso_utc(next_attempt_at if next_attempt_at is not None else now)
        failed_str = iso_utc(now) if mark_failed else None
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "UPDATE as_transactions"
                    " SET attempts = attempts + 1,"
                    "     last_error_type = ?,"
                    "     next_attempt_at = ?,"
                    "     failed_at = ?"
                    " WHERE txn_id = ? AND processed_at IS NULL AND failed_at IS NULL",
                    (error_type, next_str, failed_str, txn_id),
                )

    def record_transient(
        self,
        txn_id: str,
        next_attempt_at: datetime,
    ) -> None:
        """记录瞬态不可用（TransientUnavailable）：只设退避时间，**不计 attempts**。

        瞬态失败（模型/HS 临时不可用）不消耗重试次数；消费者下次到期后仍会
        取出此事务继续处理。
        """
        next_str = iso_utc(next_attempt_at)
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "UPDATE as_transactions"
                    " SET next_attempt_at = ?"
                    " WHERE txn_id = ? AND processed_at IS NULL AND failed_at IS NULL",
                    (next_str, txn_id),
                )

    def oldest_unprocessed_received_at(self) -> Optional[str]:
        """返回最老的「未处理且未清理且未 failed」事务的 `received_at`，供积压告警。

        无未处理事务时返回 None。日志调用方只记时间差（不记 txn_id）。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT received_at FROM as_transactions"
                " WHERE processed_at IS NULL AND purged_at IS NULL AND failed_at IS NULL"
                " ORDER BY rowid ASC LIMIT 1"
            ).fetchone()
            return row[0] if row else None

    def get_unprocessed_transactions(
        self, limit: int = DEFAULT_UNPROCESSED_LIMIT
    ) -> List[Tuple[str, str, str]]:
        """按**插入顺序**返回一批尚未处理（`processed_at IS NULL`）的事务。

        每条为 (txn_id, body, received_at)。`received_at` 只有秒精度，
        同一秒内收到的事务用时间排序是并列的、顺序不确定；因此按 `rowid`
        （SQLite 的插入顺序，单调递增）升序，保证同一秒内的事务也按到达
        顺序返回，消费侧能稳定地「先到先处理」。

        `limit` 限制单批条数（默认 100，必须 >= 1）：消费侧循环调用直到返回
        空列表，避免一次把整个积压读进内存。**不含墓碑**
        （purged_at IS NOT NULL）：过期未处理的事务在清理时被丢弃，不再交给
        消费侧。

        本方法保留为兼容接口（现有测试与运维脚本使用）；进程内消费者改用
        `get_consumable_batch()`，后者额外排除 failed 行并遵守退避时间。
        """
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValueError(f"limit must be an integer, got {limit!r}")
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")
        with self._lock:
            rows = self._conn.execute(
                "SELECT txn_id, body, received_at"
                " FROM as_transactions"
                " WHERE processed_at IS NULL AND purged_at IS NULL"
                " ORDER BY rowid ASC"
                " LIMIT ?",
                (limit,),
            ).fetchall()
            return [(r["txn_id"], r["body"], r["received_at"]) for r in rows]

    def mark_processed(self, txn_id: str, processed_at: Optional[str] = None,
                       *, now: Optional[datetime] = None) -> None:
        """标记事务已处理；优先 processed_at，其次注入 now，缺省用当前 UTC 时间。"""
        ts = processed_at or iso_utc(now if now is not None else utcnow())
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "UPDATE as_transactions SET processed_at = ?"
                    " WHERE txn_id = ? AND processed_at IS NULL",
                    (ts, txn_id),
                )

    @staticmethod
    def room_persona_key(persona: str, room: str) -> str:
        return hashlib.sha256(json.dumps([persona, room], separators=(",", ":")).encode()).hexdigest()

    def invitation_update(self, persona: str, room: str, membership: str,
                          encrypted: bool = False, event_id: Optional[str] = None) -> None:
        key = self.room_persona_key(persona, room)
        with self._lock, self._conn:
            if event_id is not None:
                memory_full = (
                    not self._conn.execute("PRAGMA database_list").fetchone()[2]
                    and self._conn.execute("SELECT COUNT(*) FROM as_invitation_events").fetchone()[0] >= 1000
                )
                # Do not evict replay tombstones and accidentally revive invites.
                # A saturated in-memory observer rejects new invites until restart.
                if memory_full:
                    if membership != "invite":
                        self._conn.execute("DELETE FROM as_invitations WHERE key = ?", (key,))
                    return
                event_key = self.room_persona_key(key, event_id)
                inserted = self._conn.execute("INSERT OR IGNORE INTO as_invitation_events VALUES (?)", (event_key,))
                if not inserted.rowcount:
                    return
            if membership == "invite":
                now = self._clock()
                self._conn.execute("DELETE FROM as_invitations WHERE created_at < ?",
                                   (now - self._invite_ttl,))
                self._conn.execute("INSERT OR REPLACE INTO as_invitations (key, encrypted, created_at) "
                                   "VALUES (?, ?, ?)", (key, int(encrypted), now))
                # Bound the fallback cache; eviction requires authoritative sync confirmation.
                if not self._conn.execute("PRAGMA database_list").fetchone()[2]:
                    self._conn.execute("DELETE FROM as_invitations WHERE rowid NOT IN "
                                       "(SELECT rowid FROM as_invitations ORDER BY rowid DESC LIMIT 1000)")
            elif membership in ("join", "leave", "ban"):
                self._conn.execute("DELETE FROM as_invitations WHERE key = ?", (key,))

    def set_invite_ttl(self, seconds: int) -> None:
        with self._lock:
            self._invite_ttl = seconds

    def invitation(self, persona: str, room: str) -> Optional[bool]:
        """None means no fresh pending invite; bool reports stripped encryption state.

        Entries older than the TTL are expired (deleted) and reported as None,
        so the caller falls back to the authoritative homeserver check.
        """
        key = self.room_persona_key(persona, room)
        with self._lock, self._conn:
            row = self._conn.execute("SELECT encrypted, created_at FROM as_invitations WHERE key = ?",
                                     (key,)).fetchone()
            if row is None:
                return None
            if row[1] < self._clock() - self._invite_ttl:
                self._conn.execute("DELETE FROM as_invitations WHERE key = ?", (key,))
                return None
            return bool(row[0])

    def cleanup_enqueue(self, persona: str, room: str) -> None:
        # Raw routing IDs are necessary for leave after restart. Like transaction
        # bodies, these are private SQLite data, never log fields. No token stored.
        with self._lock, self._conn:
            self._conn.execute("INSERT OR IGNORE INTO as_pending_cleanup VALUES (?, ?, ?, 0, 0)",
                               (self.room_persona_key(persona, room), persona, room))

    def cleanup_pending(self, persona: str, room: str) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1 FROM as_pending_cleanup WHERE key = ?",
                                      (self.room_persona_key(persona, room),)).fetchone() is not None

    def cleanup_due(self, now: float, limit: int = 20):
        with self._lock:
            return self._conn.execute("SELECT * FROM as_pending_cleanup WHERE next_at <= ? "
                                      "ORDER BY next_at LIMIT ?", (now, limit)).fetchall()

    def cleanup_done(self, key: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM as_pending_cleanup WHERE key = ?", (key,))

    def cleanup_failed(self, key: str, now: float) -> None:
        with self._lock, self._conn:
            row = self._conn.execute("SELECT attempts FROM as_pending_cleanup WHERE key = ?", (key,)).fetchone()
            if row is not None:
                attempts = row[0] + 1
                self._conn.execute("UPDATE as_pending_cleanup SET attempts = ?, next_at = ? WHERE key = ?",
                                   (attempts, now + min(5 * 2 ** min(attempts - 1, 10), 3600), key))

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
        # 发言去重账本按同一保留期清理（B 线 PR2）：过期键对应的旧事务也已被
        # 清理，重放不会再发生；不删则会无限增长。失败只记日志。
        try:
            removed = self.purge_speech_dedupe(retention)
        except Exception as exc:  # noqa: BLE001 - 清理是尽力而为
            _log.warning(
                "AS speech dedupe purge failed: %s", type(exc).__name__
            )
        else:
            if removed:
                _log.info(
                    "AS speech dedupe purge removed %d expired key(s)", removed
                )
        return result

    def start_purge_scheduler(self, interval_seconds: float) -> threading.Thread:
        """后台守护线程：每隔 interval_seconds 跑一次 `run_purge_safely`。

        首次在等待一个间隔后执行（启动时那次由 `build_as_txn_store` 同步执行）。
        `stop_purge_scheduler()` 可让线程尽快退出。

        内部 `_wait_fn(stop_event, timeout)` 是可注入的等待函数（仅测试用；
        默认 `Event.wait`）。注入时语义须与之一致：**返回 True 表示收到停止
        信号**、False 表示超时该跑下一轮。
        """
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be > 0")
        self.stop_purge_scheduler()
        stop = threading.Event()
        self._purge_stop = stop
        wait_fn = self._wait_fn

        def _loop() -> None:
            while True:
                stopped = (
                    wait_fn(stop, interval_seconds)
                    if wait_fn is not None
                    else stop.wait(interval_seconds)
                )
                if stopped:
                    return
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

    # ------------------------------------------------- speech dedupe ledger
    def speech_dedupe_seen(self, key: str) -> bool:
        """该发言幂等键是否已在账本里（只在成功发言后写入）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM as_speech_dedupe WHERE key = ?", (key,)
            ).fetchone()
            return row is not None

    def speech_dedupe_mark(self, key: str) -> None:
        """成功发言后写入幂等键；重复写入保持幂等。"""
        # Actual persistence time for audit/retention, independent of consumer scheduling.
        now_str = iso_utc(utcnow())
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO as_speech_dedupe(key, created_at, sent_at)"
                    " VALUES (?, ?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET"
                    " sent_at = excluded.sent_at",
                    (key, now_str, now_str),
                )

    def purge_speech_dedupe(self, retention_days: int, now: Optional[datetime] = None) -> int:
        """删除超过保留期的发言去重行（与事务保留期同一套口径）。

        只删 `created_at < cutoff` 的行，返回删除条数。**不删未过期的行**：
        过早删除会让重放的旧事务重新发言。
        """
        if retention_days < 1:
            raise ValueError("retention_days must be >= 1")
        current = now if now is not None else utcnow()
        cutoff = iso_utc(current - timedelta(days=retention_days))
        with self._lock:
            with self._conn:
                cur = self._conn.execute(
                    "DELETE FROM as_speech_dedupe WHERE created_at < ?", (cutoff,)
                )
                return cur.rowcount

    def speech_dedupe_count(self) -> int:
        """账本行数（仅供测试 / 运维检查）。"""
        with self._lock:
            return int(
                self._conn.execute("SELECT COUNT(*) FROM as_speech_dedupe").fetchone()[0]
            )


def build_as_txn_store(
    db_path: Optional[str],
    *,
    purge_retention_days: Optional[int] = None,
    purge_interval_seconds: int = 0,
    invite_ttl_seconds: int = DEFAULT_INVITE_LEDGER_TTL_SECONDS,
) -> Optional[AsTransactionStore]:
    """按配置装配 AS 事务存储；db_path 为空/None → None（调用方需处理 500）。

    `purge_retention_days` 非 None 时启用保留期清理：启动阶段同步跑一次
    （失败只记日志），`purge_interval_seconds > 0` 时再启动后台守护线程按间隔
    执行。只读地重开同一库（测试 / 运维检查）不传该参数即可，不会启动线程。
    """
    if not db_path:
        return None
    conn = connect(db_path)
    store = AsTransactionStore(conn, purge_retention_days=purge_retention_days,
                               invite_ttl_seconds=invite_ttl_seconds)
    if purge_retention_days is not None:
        store.run_purge_safely()
        if purge_interval_seconds > 0:
            store.start_purge_scheduler(purge_interval_seconds)
    return store


class PersistentSpeechDedupe:
    """基于 `as_speech_dedupe` 表的发言去重账本（B 线 PR2，contract §1.7.8 ④）。

    与 `as_consumer.InMemorySpeechDedupe` 接口相同（`seen` / `mark`），但状态
    落在 AS 事务库里：**进程重启 / 换一个 store 实例打开同一个文件**都不会
    重新发言（at-least-once 消费 + 持久化幂等键 = 实际效果至多一次发言）。

    只在成功投递后写入：模型调用前占位会在进程崩溃时吞掉未送达的回复。
    """

    def __init__(self, store: AsTransactionStore) -> None:
        self._store = store

    def seen(self, key: str) -> bool:
        return self._store.speech_dedupe_seen(key)

    def mark(self, key: str) -> None:
        self._store.speech_dedupe_mark(key)

    def __len__(self) -> int:
        return self._store.speech_dedupe_count()
