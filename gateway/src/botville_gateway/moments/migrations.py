# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""动态模块迁移执行器（A2.2 / issue #7，moments.md §2）。

启动时按 `gateway/migrations/moments/` 目录内**文件名顺序**执行 `*.sql`，
并在簿记表 `moments_schema_migrations`（本执行器私有，不属于 0001 DDL）
记录已应用编号；已应用的跳过，DDL 本身 `IF NOT EXISTS` 幂等，因此同一库
重复执行安全。

连接统一以 WAL + foreign_keys=ON 打开（`connect`）；并发由上层（MomentStore
的进程内锁）串行化，本模块函数假定独占连接。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import List, Optional, Sequence

from ..timeutil import iso_utc, utcnow

# 源码树布局：本文件位于 gateway/src/botville_gateway/moments/，
# 仓库 gateway/ 目录是其上三级；迁移目录固定为 gateway/migrations/moments。
# 打包 / 测试场景可通过参数注入其他目录。
DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations" / "moments"

# 执行器自己的簿记表（moments.md §2：编号空间 moments/NNNN 独立记录）。
_BOOKKEEP_DDL = """
CREATE TABLE IF NOT EXISTS moments_schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
)
"""


def connect(db_path: str) -> sqlite3.Connection:
    """打开（必要时创建）moments SQLite 库：WAL + foreign_keys=ON。"""
    path = Path(db_path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def apply_migrations(
    conn: sqlite3.Connection,
    migrations_dir: Optional[Path] = None,
) -> List[str]:
    """按文件名顺序执行未应用的迁移，返回本次新应用的版本列表。"""
    directory = Path(migrations_dir) if migrations_dir is not None else DEFAULT_MIGRATIONS_DIR
    if not directory.is_dir():
        raise FileNotFoundError(f"moments migrations directory not found: {directory}")
    with conn:
        conn.execute(_BOOKKEEP_DDL)
    applied = {
        row[0]
        for row in conn.execute("SELECT version FROM moments_schema_migrations")
    }
    newly: List[str] = []
    for sql_file in sorted(directory.glob("*.sql")):
        version = sql_file.stem
        if version in applied:
            continue
        sql = sql_file.read_text(encoding="utf-8")
        with conn:
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO moments_schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, iso_utc(utcnow())),
            )
        newly.append(version)
    return newly


def applied_versions(conn: sqlite3.Connection) -> Sequence[str]:
    """已应用的迁移版本（测试 / 运维检查用）。"""
    return [row[0] for row in conn.execute(
        "SELECT version FROM moments_schema_migrations ORDER BY version"
    )]
