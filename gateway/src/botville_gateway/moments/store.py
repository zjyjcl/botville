# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""动态模块 SQLite 访问层（posts / comments / likes / events）。

- 单连接 + 进程内锁：`ThreadingHTTPServer` 的并发请求在锁下串行访问
  （连接以 `check_same_thread=False` 打开，WAL + foreign_keys=ON 见
  `migrations.connect`）。每次操作独立提交，锁外只读不可变 dataclass 行。
- `media` / `allow_list` 以 JSON 文本落库（DDL 的 `*_json` 列），读取时
  反序列化为 tuple；时间戳为 ISO 8601 UTC（`Z` 结尾）文本，字典序即时间序。
- 软删：`deleted_at` 非空即对外不存在；**本层不过滤**，由可见性层统一判定
  （读单条 / 列表 / 计数复用同一函数）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .migrations import connect

# 列表分页的批量读取上限：可见性过滤在服务层进行，这里一次多拉一些，
# 服务层循环拉取直到凑满 limit+1 条可见项或库耗尽。
_PAGE_QUERY_BATCH = 200


@dataclass(frozen=True)
class MomentRow:
    """moments_posts 一行。`allow_list` 为 None（未收窄）或 tuple（收窄）。"""

    id: str
    author_id: str
    author_kind: str          # 'ai' | 'human'
    owner_id: str             # 恒为 Matrix user id
    body: str
    media: Tuple[Dict[str, Any], ...]
    visibility: str
    allow_list: Optional[Tuple[str, ...]]
    revision: int
    created_at: str
    updated_at: str
    deleted_at: Optional[str]


@dataclass(frozen=True)
class CommentRow:
    """moments_comments 一行。"""

    id: str
    moment_id: str
    author_id: str
    author_kind: str
    owner_id: str
    body: str
    reply_to_comment_id: Optional[str]
    created_at: str
    updated_at: str
    deleted_at: Optional[str]


@dataclass(frozen=True)
class LikeRow:
    """moments_likes 一行（主键 (moment_id, actor_id)，无软删）。"""

    moment_id: str
    actor_id: str
    actor_kind: str
    owner_id: str
    created_at: str


def _load_json_list(raw: Optional[str]) -> Tuple[Any, ...]:
    if raw is None:
        return ()
    value = json.loads(raw)
    return tuple(value)


class MomentStore:
    """线程安全的 moments SQLite 存储。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.RLock()

    @classmethod
    def open(cls, db_path: str) -> "MomentStore":
        """打开（不执行迁移；迁移由 `build_moments_service` 负责）。"""
        return cls(connect(db_path))

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------- posts
    def insert_post(
        self,
        *,
        id: str,
        author_id: str,
        author_kind: str,
        owner_id: str,
        body: str,
        media: Sequence[Dict[str, Any]],
        visibility: str,
        allow_list: Optional[Sequence[str]],
        created_at: str,
    ) -> MomentRow:
        allow_json = (
            json.dumps(list(allow_list)) if allow_list is not None else None
        )
        media_json = json.dumps(list(media))
        with self._lock:
            self._conn.execute(
                "INSERT INTO moments_posts (id, author_id, author_kind, owner_id,"
                " body, media_json, visibility, allow_list_json, revision,"
                " created_at, updated_at, deleted_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, NULL)",
                (id, author_id, author_kind, owner_id, body, media_json,
                 visibility, allow_json, created_at, created_at),
            )
            self._conn.commit()
        return MomentRow(
            id=id, author_id=author_id, author_kind=author_kind, owner_id=owner_id,
            body=body, media=tuple(media), visibility=visibility,
            allow_list=tuple(allow_list) if allow_list is not None else None,
            revision=1, created_at=created_at, updated_at=created_at,
            deleted_at=None,
        )

    def _post_from_row(self, row: sqlite3.Row) -> MomentRow:
        return MomentRow(
            id=row["id"],
            author_id=row["author_id"],
            author_kind=row["author_kind"],
            owner_id=row["owner_id"],
            body=row["body"],
            media=_load_json_list(row["media_json"]),
            visibility=row["visibility"],
            allow_list=(
                tuple(json.loads(row["allow_list_json"]))
                if row["allow_list_json"] is not None else None
            ),
            revision=row["revision"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            deleted_at=row["deleted_at"],
        )

    _POST_COLUMNS = (
        "id, author_id, author_kind, owner_id, body, media_json, visibility,"
        " allow_list_json, revision, created_at, updated_at, deleted_at"
    )

    def get_post(self, moment_id: str) -> Optional[MomentRow]:
        """按 id 取动态（含软删行；对外可见性由服务层判定）。"""
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._POST_COLUMNS} FROM moments_posts WHERE id = ?",
                (moment_id,),
            ).fetchone()
        return self._post_from_row(row) if row is not None else None

    def update_post(
        self,
        moment_id: str,
        *,
        visibility: str,
        allow_list: Optional[Sequence[str]],
        updated_at: str,
        expected_revision: Optional[int] = None,
    ) -> Optional[MomentRow]:
        """仅更新可见性 / allow_list；revision+1。

        `expected_revision` 非 None 时在同一条 UPDATE 里比对（`AND revision = ?`），
        避免「先读后写」的 TOCTOU：并发 PATCH 只有一个能成功，另一个返回 None，
        由服务层再读一次区分 409（revision 不一致）与 404（已删）。
        """
        allow_json = (
            json.dumps(list(allow_list)) if allow_list is not None else None
        )
        with self._lock:
            sql = (
                "UPDATE moments_posts SET visibility = ?, allow_list_json = ?,"
                " revision = revision + 1, updated_at = ?"
                " WHERE id = ? AND deleted_at IS NULL"
            )
            params: list = [visibility, allow_json, updated_at, moment_id]
            if expected_revision is not None:
                sql += " AND revision = ?"
                params.append(expected_revision)
            cur = self._conn.execute(sql, tuple(params))
            if cur.rowcount != 1:
                self._conn.commit()
                return None
            self._conn.commit()
        return self.get_post(moment_id)

    def soft_delete_post(self, moment_id: str, deleted_at: str) -> Optional[MomentRow]:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE moments_posts SET deleted_at = ?, updated_at = ?"
                " WHERE id = ? AND deleted_at IS NULL",
                (deleted_at, deleted_at, moment_id),
            )
            if cur.rowcount != 1:
                self._conn.commit()
                return None
            self._conn.commit()
        return self.get_post(moment_id)

    def page_posts_desc(
        self,
        *,
        after_key: Optional[Tuple[str, str]],
        author_id: Optional[str],
        limit: int,
    ) -> List[MomentRow]:
        """按 (created_at, id) 倒序取一页候选行（含软删；服务层过滤）。

        `after_key` 为上一页最后一条的 (created_at, id)，严格小于它继续。
        """
        conds, params = [], []
        if after_key is not None:
            conds.append("(created_at < ? OR (created_at = ? AND id < ?))")
            params.extend([after_key[0], after_key[0], after_key[1]])
        if author_id is not None:
            conds.append("author_id = ?")
            params.append(author_id)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._POST_COLUMNS} FROM moments_posts{where}"
                " ORDER BY created_at DESC, id DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._post_from_row(row) for row in rows]

    # ---------------------------------------------------------- comments
    _COMMENT_COLUMNS = (
        "id, moment_id, author_id, author_kind, owner_id, body,"
        " reply_to_comment_id, created_at, updated_at, deleted_at"
    )

    def insert_comment(
        self,
        *,
        id: str,
        moment_id: str,
        author_id: str,
        author_kind: str,
        owner_id: str,
        body: str,
        reply_to_comment_id: Optional[str],
        created_at: str,
    ) -> CommentRow:
        with self._lock:
            self._conn.execute(
                "INSERT INTO moments_comments (id, moment_id, author_id,"
                " author_kind, owner_id, body, reply_to_comment_id, created_at,"
                " updated_at, deleted_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (id, moment_id, author_id, author_kind, owner_id, body,
                 reply_to_comment_id, created_at, created_at),
            )
            self._conn.commit()
        return CommentRow(
            id=id, moment_id=moment_id, author_id=author_id,
            author_kind=author_kind, owner_id=owner_id, body=body,
            reply_to_comment_id=reply_to_comment_id, created_at=created_at,
            updated_at=created_at, deleted_at=None,
        )

    def _comment_from_row(self, row: sqlite3.Row) -> CommentRow:
        return CommentRow(
            id=row["id"], moment_id=row["moment_id"],
            author_id=row["author_id"], author_kind=row["author_kind"],
            owner_id=row["owner_id"], body=row["body"],
            reply_to_comment_id=row["reply_to_comment_id"],
            created_at=row["created_at"], updated_at=row["updated_at"],
            deleted_at=row["deleted_at"],
        )

    def get_comment(self, comment_id: str) -> Optional[CommentRow]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._COMMENT_COLUMNS} FROM moments_comments"
                " WHERE id = ?",
                (comment_id,),
            ).fetchone()
        return self._comment_from_row(row) if row is not None else None

    def comments_for_moment(self, moment_id: str) -> List[CommentRow]:
        """动态的全部评论（含软删；可见性由服务层过滤），按时间正序。"""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._COMMENT_COLUMNS} FROM moments_comments"
                " WHERE moment_id = ? ORDER BY created_at ASC, id ASC",
                (moment_id,),
            ).fetchall()
        return [self._comment_from_row(row) for row in rows]

    def soft_delete_comment(self, comment_id: str, deleted_at: str) -> Optional[CommentRow]:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE moments_comments SET deleted_at = ?, updated_at = ?"
                " WHERE id = ? AND deleted_at IS NULL",
                (deleted_at, deleted_at, comment_id),
            )
            if cur.rowcount != 1:
                self._conn.commit()
                return None
            self._conn.commit()
        return self.get_comment(comment_id)

    # ------------------------------------------------------------- likes
    def upsert_like(
        self,
        *,
        moment_id: str,
        actor_id: str,
        actor_kind: str,
        owner_id: str,
        created_at: str,
    ) -> bool:
        """点赞（幂等）：已存在返回 False，新插入返回 True。"""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO moments_likes (moment_id, actor_id, actor_kind,"
                " owner_id, created_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(moment_id, actor_id) DO NOTHING",
                (moment_id, actor_id, actor_kind, owner_id, created_at),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def remove_like(self, moment_id: str, actor_id: str) -> bool:
        """取消点赞（幂等）：删除了返回 True，本就不存在返回 False。"""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM moments_likes WHERE moment_id = ? AND actor_id = ?",
                (moment_id, actor_id),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def likes_for_moment(self, moment_id: str) -> List[LikeRow]:
        """动态的全部点赞（无软删；可见性由服务层过滤），按时间正序。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT moment_id, actor_id, actor_kind, owner_id, created_at"
                " FROM moments_likes WHERE moment_id = ?"
                " ORDER BY created_at ASC, actor_id ASC",
                (moment_id,),
            ).fetchall()
        return [LikeRow(*row) for row in rows]

    # ------------------------------------------------------------ events
    def append_event(
        self,
        *,
        at: str,
        moment_id: Optional[str],
        actor: str,
        action: str,
        detail_json: Optional[str] = None,
    ) -> None:
        """追加审计事件（append-only；不含任何 token / 凭据）。"""
        with self._lock:
            self._conn.execute(
                "INSERT INTO moments_events (at, moment_id, actor, action,"
                " detail_json) VALUES (?, ?, ?, ?, ?)",
                (at, moment_id, actor, action, detail_json),
            )
            self._conn.commit()

    def list_events(self) -> List[Dict[str, Any]]:
        """全部审计事件（时间正序）；测试 / 运维检查用。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT at, moment_id, actor, action, detail_json"
                " FROM moments_events ORDER BY at ASC, rowid ASC"
            ).fetchall()
        return [
            {
                "at": row[0], "moment_id": row[1], "actor": row[2],
                "action": row[3],
                "detail": json.loads(row[4]) if row[4] else None,
            }
            for row in rows
        ]

    # ------------------------------------------------------------- purge
    def purge(
        self, *, soft_delete_cutoff: str, audit_cutoff: str
    ) -> Dict[str, int]:
        """硬删超期软删行与过期审计（moments.md §7 保留天数）。

        外键顺序：先删动态的 likes / comments 再删 post；孤儿评论（存活动态
        下软删超期）可能互为 reply 链，循环删「无子回复」的直到收敛。
        """
        events_deleted = 0
        posts_deleted = 0
        comments_deleted = 0
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM moments_events WHERE at < ?", (audit_cutoff,)
            )
            events_deleted = cur.rowcount
            stale_posts: Iterable[str] = [
                row[0]
                for row in self._conn.execute(
                    "SELECT id FROM moments_posts"
                    " WHERE deleted_at IS NOT NULL AND deleted_at < ?",
                    (soft_delete_cutoff,),
                )
            ]
            for post_id in stale_posts:
                self._conn.execute(
                    "DELETE FROM moments_likes WHERE moment_id = ?", (post_id,)
                )
                self._conn.execute(
                    "DELETE FROM moments_comments WHERE moment_id = ?", (post_id,)
                )
                cur = self._conn.execute(
                    "DELETE FROM moments_posts WHERE id = ?", (post_id,)
                )
                posts_deleted += cur.rowcount
            while True:
                cur = self._conn.execute(
                    "DELETE FROM moments_comments"
                    " WHERE deleted_at IS NOT NULL AND deleted_at < ?"
                    " AND id NOT IN (SELECT reply_to_comment_id FROM moments_comments"
                    "                WHERE reply_to_comment_id IS NOT NULL)",
                    (soft_delete_cutoff,),
                )
                if not cur.rowcount:
                    break
                comments_deleted += cur.rowcount
            self._conn.commit()
        return {
            "events": events_deleted,
            "posts": posts_deleted,
            "comments": comments_deleted,
        }
