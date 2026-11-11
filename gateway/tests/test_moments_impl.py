#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""A2.2（issue #7）PR2 动态实现单元测试。

覆盖（不启动 HTTP，HTTP 层见 test_moments_http.py）：
  1. ULID：形状 / 时间有序 / 唯一；
  2. 迁移执行器：幂等、版本记录、表与索引存在、注入目录按文件名顺序；
  3. StaticRelationProvider：文件加载 / 对称 / 空图 fail-closed / 畸形拒绝；
  4. PersonaDirectory：空目录 fail-closed（persona.not_owned）、静态映射
     与 RelationDefaults→Visibility 默认映射；
  5. visibility：can_view / can_see_interaction 逐分支（对照 moments.md §4）；
  6. 存储层：CRUD / 软删 / 点赞幂等 / 事件；
  7. 服务层：发布校验、PATCH 三态与 revision 冲突、幂等缓存（TTL/容量/
     跨 viewer 隔离）、denied 审计节流、分页游标、purge、AI 内部调用；
  8. 配置：env 解析与保留天数校验、空 DB_PATH → 模块关闭。

全部使用临时目录 SQLite 与明显假身份，不含真实凭据。
运行：python3 -m unittest gateway.tests.test_moments_impl -v
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

GATEWAY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(GATEWAY_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from botville_gateway.config import Config, ConfigError, load_config  # noqa: E402
from botville_gateway.errors import (  # noqa: E402
    IdempotencyConflict,
    UnprocessableError,
    ValidationError,
)
from botville_gateway.moments.migrations import (  # noqa: E402
    DEFAULT_MIGRATIONS_DIR,
    applied_versions,
    apply_migrations,
    connect,
)
from botville_gateway.moments.service import (  # noqa: E402
    MomentNotFound,
    MomentsService,
    PersonaNotOwned,
    RevisionConflict,
    build_moments_service,
    decode_cursor,
    encode_cursor,
)
from botville_gateway.moments.store import MomentStore  # noqa: E402
from botville_gateway.moments.ulid import ULID_RE, is_ulid, new_ulid  # noqa: E402
from botville_gateway.moments.visibility import (  # noqa: E402
    EmptyPersonaDirectory,
    StaticPersonaDirectory,
    StaticRelationProvider,
    can_see_interaction,
    can_view,
    friend_subject,
)
from botville_gateway.moments.store import CommentRow, MomentRow  # noqa: E402

SERVER = "localhost"
ALICE = "@alice:localhost"
OWNER = "@owner:localhost"
BOB = "@bob:localhost"
STRANGER = "@stranger:localhost"
PERSONA = "persona_demo"

_HMAC = b"test-hmac-secret-0123456789abcdef"


class FakeClock:
    """可推进的假时钟：datetime（业务时间）+ monotonic（缓存/节流）。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.wall = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
        self.mono = start

    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.wall = self.wall + timedelta(seconds=seconds)
        self.mono += seconds


def _service(
    *,
    pairs: Optional[List[List[str]]] = None,
    personas: Optional[Dict[str, tuple]] = None,
    server_name: Optional[str] = SERVER,
    **kwargs,
) -> MomentsService:
    clock = kwargs.pop("clock", None)
    tmp = tempfile.mkdtemp(prefix="moments-impl-")
    conn = connect(os.path.join(tmp, "m.db"))
    apply_migrations(conn, DEFAULT_MIGRATIONS_DIR)
    store = MomentStore(conn)
    relations = StaticRelationProvider(pairs or [])
    svc = MomentsService(
        store=store,
        relations=relations,
        persona_dir=StaticPersonaDirectory(personas or {}),
        server_name=server_name,
        **kwargs,
    )
    if clock is not None:
        svc._now = clock.now
        svc._monotonic = clock.monotonic
    svc._tmpdir = tmp  # tearDown 侧由调用方清理（这里挂在实例上便于调试）
    return svc


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.tmpdirs: List[str] = []
        self.service = self.make_service()

    def make_service(self, **kwargs) -> MomentsService:
        kwargs.setdefault("clock", self.clock)
        svc = _service(**kwargs)
        self.tmpdirs.append(svc._tmpdir)
        return svc

    def tearDown(self) -> None:
        for tmp in self.tmpdirs:
            shutil.rmtree(tmp, ignore_errors=True)


# ============================================================================
# 1. ULID
# ============================================================================
class TestUlid(unittest.TestCase):
    def test_shape(self):
        for _ in range(200):
            value = new_ulid()
            self.assertTrue(ULID_RE.fullmatch(value))
            self.assertTrue(is_ulid(value))

    def test_is_ulid_rejects(self):
        self.assertFalse(is_ulid(None))
        self.assertFalse(is_ulid(""))
        self.assertFalse(is_ulid("01JDYW7ZQ9GKJ4TMMB6RT8X2VTx"))  # 27 位
        self.assertFalse(is_ulid("0IJDYW7ZQ9GKJ4TMMB6RT8X2VT"))   # 含 I
        self.assertFalse(is_ulid("01JDYW7ZQ9GKJ4TMMB6RT8X2Vt"))   # 小写

    def test_time_ordered_and_unique(self):
        values = [new_ulid() for _ in range(2000)]
        self.assertEqual(len(set(values)), len(values))
        self.assertEqual(values, sorted(values))  # 同批生成严格递增


# ============================================================================
# 2. 迁移执行器
# ============================================================================
class TestMigrations(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="moments-mig-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_apply_twice_idempotent(self):
        conn = connect(os.path.join(self.tmp, "m.db"))
        self.addCleanup(conn.close)
        first = apply_migrations(conn, DEFAULT_MIGRATIONS_DIR)
        second = apply_migrations(conn, DEFAULT_MIGRATIONS_DIR)
        self.assertEqual(first, ["0001_moments"])
        self.assertEqual(second, [])
        self.assertEqual(list(applied_versions(conn)), ["0001_moments"])
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertIn("moments_posts", names)
        self.assertIn("moments_comments", names)
        self.assertIn("moments_likes", names)
        self.assertIn("moments_events", names)
        self.assertIn("moments_schema_migrations", names)
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        for index in (
            "idx_moments_posts_author_created",
            "idx_moments_posts_created",
            "idx_moments_comments_moment_created",
            "idx_moments_events_at",
        ):
            self.assertIn(index, indexes)

    def test_wal_and_foreign_keys(self):
        conn = connect(os.path.join(self.tmp, "m.db"))
        self.addCleanup(conn.close)
        apply_migrations(conn, DEFAULT_MIGRATIONS_DIR)
        self.assertEqual(
            conn.execute("PRAGMA journal_mode").fetchone()[0], "wal"
        )
        self.assertEqual(
            conn.execute("PRAGMA foreign_keys").fetchone()[0], 1
        )

    def test_injected_directory_in_filename_order(self):
        custom = Path(self.tmp) / "custom"
        custom.mkdir()
        (custom / "0002_extra.sql").write_text(
            "CREATE TABLE IF NOT EXISTS moments_extra (id TEXT);", "utf-8"
        )
        (custom / "0001_base.sql").write_text(
            "CREATE TABLE IF NOT EXISTS moments_base (id TEXT);", "utf-8"
        )
        conn = connect(os.path.join(self.tmp, "custom.db"))
        self.addCleanup(conn.close)
        applied = apply_migrations(conn, custom)
        self.assertEqual(applied, ["0001_base", "0002_extra"])  # 按文件名顺序

    def test_missing_directory_raises(self):
        conn = connect(os.path.join(self.tmp, "m.db"))
        self.addCleanup(conn.close)
        with self.assertRaises(FileNotFoundError):
            apply_migrations(conn, Path(self.tmp) / "nope")


# ============================================================================
# 3. RelationProvider / 4. PersonaDirectory
# ============================================================================
class TestRelationProvider(unittest.TestCase):
    def test_from_file_and_symmetry(self):
        path = os.path.join(tempfile.mkdtemp(prefix="friends-"), "friends.json")
        self.addCleanup(shutil.rmtree, os.path.dirname(path), True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"friends": [[ALICE, BOB]]}, fh)
        provider = StaticRelationProvider.from_file(path)
        self.assertTrue(provider.is_friend(ALICE, BOB))
        self.assertTrue(provider.is_friend(BOB, ALICE))  # 对称
        self.assertFalse(provider.is_friend(ALICE, STRANGER))

    def test_empty_graph_fail_closed(self):
        provider = StaticRelationProvider()
        self.assertFalse(provider.is_friend(ALICE, BOB))

    def test_malformed_file_rejected(self):
        base = tempfile.mkdtemp(prefix="friends-")
        self.addCleanup(shutil.rmtree, base, True)
        cases = [
            "not json",
            json.dumps({"friends": "nope"}),
            json.dumps({"friends": [[ALICE]]}),          # 单元素
            json.dumps({"friends": [[ALICE, 123]]}),      # 非字符串
            json.dumps({"friends": [[ALICE, ""]]}),       # 空串
        ]
        for index, content in enumerate(cases):
            path = os.path.join(base, f"bad{index}.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
            with self.assertRaises(ConfigError, msg=content):
                StaticRelationProvider.from_file(path)
        with self.assertRaises(ConfigError):
            StaticRelationProvider.from_file(os.path.join(base, "missing.json"))

    def test_replace_takes_effect_immediately(self):
        provider = StaticRelationProvider([[ALICE, BOB]])
        self.assertTrue(provider.is_friend(ALICE, BOB))
        provider.replace([[ALICE, STRANGER]])
        self.assertFalse(provider.is_friend(ALICE, BOB))
        self.assertTrue(provider.is_friend(ALICE, STRANGER))


class TestPersonaDirectory(unittest.TestCase):
    def test_empty_directory_fail_closed(self):
        directory = EmptyPersonaDirectory()
        self.assertIsNone(directory.owner_of(PERSONA))
        self.assertIsNone(directory.display_name(PERSONA))
        self.assertEqual(directory.moments_visibility_default(PERSONA), "friends")

    def test_static_directory_mapping(self):
        directory = StaticPersonaDirectory(
            {PERSONA: (OWNER, "大肥鱼", "nobody")}
        )
        self.assertEqual(directory.owner_of(PERSONA), OWNER)
        self.assertEqual(directory.display_name(PERSONA), "大肥鱼")
        self.assertEqual(directory.moments_visibility_default(PERSONA), "nobody")
        self.assertEqual(
            directory.moments_visibility_default("unknown"), "friends"
        )
        fallback = StaticPersonaDirectory(
            {"p2": (OWNER, None, "bogus")}
        )
        self.assertEqual(fallback.moments_visibility_default("p2"), "friends")


# ============================================================================
# 5. visibility 函数
# ============================================================================
def _moment(
    *,
    visibility: str = "friends",
    author_id: str = ALICE,
    author_kind: str = "human",
    owner_id: Optional[str] = None,
    allow_list: Optional[tuple] = None,
    deleted: bool = False,
) -> MomentRow:
    return MomentRow(
        id="01JDYW7ZQ9GKJ4TMMB6RT8X2VT",
        author_id=author_id,
        author_kind=author_kind,
        owner_id=owner_id or author_id,
        body="x",
        media=(),
        visibility=visibility,
        allow_list=allow_list,
        revision=1,
        created_at="2026-11-01T00:00:00Z",
        updated_at="2026-11-01T00:00:00Z",
        deleted_at="2026-11-02T00:00:00Z" if deleted else None,
    )


class TestVisibility(unittest.TestCase):
    def setUp(self) -> None:
        self.relations = StaticRelationProvider([[ALICE, BOB]])

    def test_friend_subject(self):
        self.assertEqual(friend_subject("human", ALICE, ALICE), ALICE)
        self.assertEqual(friend_subject("ai", PERSONA, OWNER), OWNER)

    def test_can_view_deleted_and_anonymous(self):
        moment = _moment(visibility="server", deleted=True)
        self.assertFalse(can_view(ALICE, moment, self.relations, server_name=SERVER))
        self.assertFalse(can_view(None, _moment(), self.relations, server_name=SERVER))

    def test_can_view_author_owner_shortcut(self):
        # 作者 / 所有者短路面：self 档 + 空 allow_list 也可见。
        self.assertTrue(
            can_view(ALICE, _moment(visibility="self"), self.relations, server_name=SERVER)
        )
        ai = _moment(visibility="self", author_id=PERSONA, author_kind="ai", owner_id=OWNER)
        self.assertTrue(can_view(OWNER, ai, self.relations, server_name=SERVER))

    def test_can_view_self_denies_everyone_else(self):
        moment = _moment(visibility="self")
        for viewer in (BOB, STRANGER):
            self.assertFalse(can_view(viewer, moment, self.relations, server_name=SERVER))

    def test_can_view_allow_list_narrows_only(self):
        # friends 档：好友在名单外 → False（收窄）；在名单内的陌生人仍 False（不放宽）。
        friend_moment = _moment(visibility="friends", allow_list=(STRANGER,))
        self.assertFalse(can_view(BOB, friend_moment, self.relations, server_name=SERVER))
        self.assertFalse(
            can_view(STRANGER, friend_moment, self.relations, server_name=SERVER)
        )
        # server 档：陌生人在名单内 → True；好友不在名单内 → False。
        server_moment = _moment(visibility="server", allow_list=(STRANGER,))
        self.assertTrue(
            can_view(STRANGER, server_moment, self.relations, server_name=SERVER)
        )
        self.assertFalse(can_view(BOB, server_moment, self.relations, server_name=SERVER))
        # 空数组 = 仅作者 / 所有者。
        empty = _moment(visibility="server", allow_list=())
        self.assertFalse(can_view(BOB, empty, self.relations, server_name=SERVER))
        # None = 不收窄。
        no_list = _moment(visibility="server", allow_list=None)
        self.assertTrue(can_view(BOB, no_list, self.relations, server_name=SERVER))

    def test_can_view_friends_binds_ai_to_owner(self):
        ai_moment = _moment(
            visibility="friends", author_id=PERSONA, author_kind="ai", owner_id=OWNER
        )
        # BOB 与 ALICE 是好友、与 OWNER 不是 → AI 动态不可见（绑主人）。
        self.assertFalse(can_view(BOB, ai_moment, self.relations, server_name=SERVER))
        self.relations.replace([[BOB, OWNER]])
        self.assertTrue(can_view(BOB, ai_moment, self.relations, server_name=SERVER))

    def test_can_view_server_domain(self):
        moment = _moment(visibility="server")
        self.assertTrue(can_view(BOB, moment, self.relations, server_name=SERVER))
        # 域不匹配 / server_name 未知 → fail-closed。
        self.assertFalse(
            can_view("@bob:other.example", moment, self.relations, server_name=SERVER)
        )
        self.assertFalse(can_view(BOB, moment, self.relations, server_name=None))
        self.assertFalse(can_view("bob-no-at", moment, self.relations, server_name=SERVER))

    def test_can_view_paired_servers_same_server_viewer(self):
        moment = _moment(visibility="paired_servers")
        self.assertTrue(can_view(STRANGER, moment, self.relations, server_name=SERVER))

    def test_can_see_interaction_branches(self):
        moment = _moment(visibility="friends")  # 作者 ALICE
        kwargs = {"relations": self.relations}
        # 动态作者 / 所有者。
        self.assertTrue(can_see_interaction(ALICE, moment, "human", BOB, BOB, **kwargs))
        # 查看者就是行为者（或其主人）。
        self.assertTrue(can_see_interaction(BOB, moment, "human", BOB, BOB, **kwargs))
        self.assertTrue(can_see_interaction(OWNER, moment, "ai", PERSONA, OWNER, **kwargs))
        # 共同好友（BOB 与行为者主人 OWNER 是好友）。
        self.relations.replace([[BOB, OWNER]])
        self.assertTrue(can_see_interaction(BOB, moment, "ai", PERSONA, OWNER, **kwargs))
        # 非共同好友 → False。
        self.relations.replace([[ALICE, BOB]])
        self.assertFalse(can_see_interaction(BOB, moment, "human", STRANGER, STRANGER, **kwargs))
        # server / paired_servers 档全可见。
        server_moment = _moment(visibility="server")
        self.assertTrue(
            can_see_interaction(BOB, server_moment, "human", STRANGER, STRANGER, **kwargs)
        )
        # 匿名 → False。
        self.assertFalse(can_see_interaction(None, moment, "human", BOB, BOB, **kwargs))


# ============================================================================
# 6. 存储层
# ============================================================================
class TestStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="moments-store-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        conn = connect(os.path.join(self.tmp, "m.db"))
        apply_migrations(conn, DEFAULT_MIGRATIONS_DIR)
        self.store = MomentStore(conn)

    def test_post_crud_and_missing_paths(self):
        row = self.store.insert_post(
            id=new_ulid(), author_id=ALICE, author_kind="human", owner_id=ALICE,
            body="b", media=[{"mxc_uri": "mxc://h/a", "mimetype": "image/jpeg"}],
            visibility="friends", allow_list=[BOB], created_at="2026-11-01T00:00:00Z",
        )
        self.assertEqual(row.revision, 1)
        loaded = self.store.get_post(row.id)
        assert loaded is not None
        self.assertEqual(loaded.allow_list, (BOB,))
        self.assertEqual(loaded.media[0]["mxc_uri"], "mxc://h/a")
        updated = self.store.update_post(
            row.id, visibility="server", allow_list=None,
            updated_at="2026-11-01T01:00:00Z",
        )
        assert updated is not None
        self.assertEqual(updated.revision, 2)
        self.assertIsNone(updated.allow_list)
        self.assertEqual(updated.updated_at, "2026-11-01T01:00:00Z")
        # 带 expected_revision：不一致不写入。
        self.assertIsNone(self.store.update_post(
            row.id, visibility="self", allow_list=None,
            updated_at="2026-11-01T01:30:00Z", expected_revision=1,
        ))
        same = self.store.update_post(
            row.id, visibility="server", allow_list=None,
            updated_at="2026-11-01T01:30:00Z", expected_revision=2,
        )
        assert same is not None
        self.assertEqual(same.revision, 3)
        deleted = self.store.soft_delete_post(row.id, "2026-11-01T02:00:00Z")
        assert deleted is not None
        self.assertIsNotNone(deleted.deleted_at)
        # 不存在的行：各操作安全返回 None / False。
        self.assertIsNone(self.store.get_post(new_ulid()))
        self.assertIsNone(self.store.update_post(new_ulid(), visibility="self", allow_list=None, updated_at="x"))
        self.assertIsNone(self.store.soft_delete_post(new_ulid(), "x"))
        # 幂等软删：第二次不再命中。
        self.assertIsNone(self.store.soft_delete_post(row.id, "2026-11-01T03:00:00Z"))

    def test_likes_idempotent(self):
        moment = self.store.insert_post(
            id=new_ulid(), author_id=ALICE, author_kind="human", owner_id=ALICE,
            body="b", media=[], visibility="friends", allow_list=None,
            created_at="2026-11-01T00:00:00Z",
        )
        self.assertTrue(self.store.upsert_like(
            moment_id=moment.id, actor_id=BOB, actor_kind="human", owner_id=BOB,
            created_at="2026-11-01T00:01:00Z"))
        self.assertFalse(self.store.upsert_like(
            moment_id=moment.id, actor_id=BOB, actor_kind="human", owner_id=BOB,
            created_at="2026-11-01T00:02:00Z"))  # 幂等
        self.assertTrue(self.store.remove_like(moment.id, BOB))
        self.assertFalse(self.store.remove_like(moment.id, BOB))

    def test_comments_and_events(self):
        moment = self.store.insert_post(
            id=new_ulid(), author_id=ALICE, author_kind="human", owner_id=ALICE,
            body="b", media=[], visibility="friends", allow_list=None,
            created_at="2026-11-01T00:00:00Z",
        )
        first = self.store.insert_comment(
            id=new_ulid(), moment_id=moment.id, author_id=BOB, author_kind="human",
            owner_id=BOB, body="c1", reply_to_comment_id=None,
            created_at="2026-11-01T00:01:00Z",
        )
        second = self.store.insert_comment(
            id=new_ulid(), moment_id=moment.id, author_id=ALICE, author_kind="human",
            owner_id=ALICE, body="c2", reply_to_comment_id=first.id,
            created_at="2026-11-01T00:02:00Z",
        )
        comments = self.store.comments_for_moment(moment.id)
        self.assertEqual([c.id for c in comments], [first.id, second.id])
        deleted = self.store.soft_delete_comment(first.id, "2026-11-01T00:03:00Z")
        assert deleted is not None
        self.assertIsNotNone(deleted.deleted_at)
        self.assertIsNone(self.store.soft_delete_comment(first.id, "x"))
        self.assertIsNone(self.store.get_comment(new_ulid()))
        self.store.append_event(
            at="2026-11-01T00:00:00Z", moment_id=moment.id, actor=ALICE,
            action="create", detail_json=json.dumps({"visibility": "friends"}),
        )
        events = self.store.list_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["detail"], {"visibility": "friends"})

    def test_page_posts_desc_with_after_key(self):
        ids = []
        for index in range(5):
            row = self.store.insert_post(
                id=new_ulid(), author_id=ALICE if index % 2 else BOB,
                author_kind="human",
                owner_id=ALICE if index % 2 else BOB,
                body="b", media=[], visibility="friends", allow_list=None,
                created_at="2026-11-01T00:00:0%dZ" % index,
            )
            ids.append(row.id)
        page1 = self.store.page_posts_desc(after_key=None, author_id=None, limit=2)
        self.assertEqual([r.id for r in page1], list(reversed(ids))[:2])
        key = (page1[-1].created_at, page1[-1].id)
        page2 = self.store.page_posts_desc(after_key=key, author_id=None, limit=2)
        self.assertEqual([r.id for r in page2], list(reversed(ids))[2:4])
        only_alice = self.store.page_posts_desc(
            after_key=None, author_id=ALICE, limit=10
        )
        self.assertTrue(all(r.author_id == ALICE for r in only_alice))
        self.assertEqual(len(only_alice), 2)


# ============================================================================
# 7. 服务层
# ============================================================================
class TestServiceCreateValidation(ServiceTestBase):
    def test_human_default_visibility_friends(self):
        moment = self.service.create_moment(ALICE, {"body": "hi"})
        self.assertEqual(moment["visibility"], "friends")
        self.assertEqual(moment["author"]["kind"], "human")
        self.assertEqual(moment["author"]["owner_id"], ALICE)
        self.assertEqual(moment["owner_id"], ALICE)
        self.assertIsNone(moment["allow_list"])
        self.assertNotIn("media", moment)  # 空媒体省略
        self.assertTrue(is_ulid(moment["id"]))
        self.assertTrue(moment["created_at"].endswith("Z"))

    def test_media_and_allow_list_persisted(self):
        moment = self.service.create_moment(ALICE, {
            "body": "hi",
            "media": [{"mxc_uri": "mxc://h/a", "mimetype": "image/png", "width": 8}],
            "visibility": "server",
            "allow_list": [BOB],
        })
        self.assertEqual(moment["media"][0]["width"], 8)
        self.assertEqual(moment["allow_list"], [BOB])

    def test_validation_errors(self):
        cases: List[tuple] = [
            ({"body": ""}, "body"),
            ({"body": "x" * 2001}, "body"),
            ({"body": "x", "visibility": "everyone"}, "visibility"),
            ({"body": "x", "unknown": 1}, "unknown"),
            ({"body": "x", "media": ["oops"]}, "media"),
            ({"body": "x", "media": [{"mxc_uri": "https://x", "mimetype": "image/png"}]}, "media"),
            ({"body": "x", "media": [{"mxc_uri": "mxc://x"}]}, "media"),
            ({"body": "x", "media": [{"mxc_uri": "mxc://x", "mimetype": "image/png", "width": 0}]}, "media"),
            ({"body": "x", "media": [{"mxc_uri": "mxc://x", "mimetype": "i"}] * 10}, "media"),
            ({"body": "x", "allow_list": ["a"] * 201}, "allow_list"),
            ({"body": "x", "allow_list": [1]}, "allow_list"),
            ({"body": 1}, "body"),
            ({"body": "x", "author_persona_id": 5}, "author_persona_id"),
        ]
        for payload, field in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError) as ctx:
                    self.service.create_moment(ALICE, payload)
                self.assertEqual(ctx.exception.field, field)


class TestServiceVisibilityReads(ServiceTestBase):
    def test_list_subset_of_get(self):
        # server 档全员可见、self 档仅作者：列表只含会 200 的。
        visible = self.service.create_moment(
            ALICE, {"body": "a", "visibility": "server"}
        )
        hidden = self.service.create_moment(
            ALICE, {"body": "b", "visibility": "self"}
        )
        page = self.service.list_moments(STRANGER)
        ids = [item["id"] for item in page["items"]]
        self.assertIn(visible["id"], ids)
        self.assertNotIn(hidden["id"], ids)
        self.assertEqual(page["has_more"], False)
        self.assertIsNone(page["next_cursor"])

    def test_pagination_desc_and_cursor(self):
        created = [
            self.service.create_moment(ALICE, {"body": "m%d" % i, "visibility": "server"})
            for i in range(5)
        ]
        page1 = self.service.list_moments(STRANGER, limit=2)
        self.assertEqual([i["id"] for i in page1["items"]], [created[4]["id"], created[3]["id"]])
        self.assertTrue(page1["has_more"])
        page2 = self.service.list_moments(STRANGER, limit=2, cursor=page1["next_cursor"])
        self.assertEqual([i["id"] for i in page2["items"]], [created[2]["id"], created[1]["id"]])
        page3 = self.service.list_moments(STRANGER, limit=2, cursor=page2["next_cursor"])
        self.assertEqual([i["id"] for i in page3["items"]], [created[0]["id"]])
        self.assertFalse(page3["has_more"])
        self.assertIsNone(page3["next_cursor"])

    def test_author_filter_and_bad_cursor(self):
        by_bob = self.service.create_moment(BOB, {"body": "b", "visibility": "server"})
        self.service.create_moment(ALICE, {"body": "a", "visibility": "server"})
        page = self.service.list_moments(STRANGER, author=BOB)
        self.assertEqual([i["id"] for i in page["items"]], [by_bob["id"]])
        with self.assertRaises(ValidationError):
            self.service.list_moments(STRANGER, cursor="@@not-base64@@")
        with self.assertRaises(ValidationError):
            self.service.list_moments(STRANGER, limit=0)
        with self.assertRaises(ValidationError):
            self.service.list_moments(STRANGER, limit=51)
        # 跨端点 cursor（kind 不符）→ 400。
        foreign = encode_cursor({"k": "c", "c": "x", "i": "y"})
        with self.assertRaises(ValidationError):
            self.service.list_moments(STRANGER, cursor=foreign)

    def test_non_ulid_and_missing_ids_are_not_found(self):
        for bad in ("not-ulid", "01JDYW7ZQ9GKJ4TMMB6RT8X2VTX", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(MomentNotFound):
                    self.service.get_moment(ALICE, bad)
        with self.assertRaises(MomentNotFound):
            self.service.get_moment(ALICE, new_ulid())


class TestServicePatch(ServiceTestBase):
    def test_patch_semantics(self):
        self.service.relations.replace([[ALICE, BOB]])
        moment = self.service.create_moment(
            ALICE, {"body": "a", "allow_list": [BOB]}
        )
        # 省略 allow_list → 保持 [BOB]。
        first = self.service.update_moment(
            ALICE, moment["id"], {"revision": 1, "visibility": "friends"}
        )
        self.assertEqual(first["allow_list"], [BOB])
        self.assertEqual(first["revision"], 2)
        # [] → 仅作者 / 所有者。
        second = self.service.update_moment(
            ALICE, moment["id"], {"revision": 2, "allow_list": []}
        )
        self.assertEqual(second["allow_list"], [])
        self.assertEqual(second["visibility"], "friends")
        # null → 清除收窄。
        third = self.service.update_moment(
            ALICE, moment["id"], {"revision": 3, "allow_list": None}
        )
        self.assertIsNone(third["allow_list"])
        # 修改时间推进、revision 递增。
        self.assertGreaterEqual(third["updated_at"], moment["created_at"])
        self.assertEqual(third["revision"], 4)
        # 新可见性立即生效（读取时计算）。
        self.assertIsNotNone(self.service.get_moment(BOB, moment["id"]))

    def test_patch_revision_conflict(self):
        moment = self.service.create_moment(ALICE, {"body": "a"})
        with self.assertRaises(RevisionConflict):
            self.service.update_moment(
                ALICE, moment["id"], {"revision": 9, "visibility": "self"}
            )
        # 校验错误：缺 revision / 未知字段 / 非法 revision。
        for payload in (
            {"visibility": "self"},
            {"revision": 1, "body": "nope"},
            {"revision": 0},
            {"revision": "1"},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    self.service.update_moment(ALICE, moment["id"], payload)

    def test_patch_explicit_null_visibility_rejected(self):
        """`"visibility": null` 不是合法 Visibility（省略才是保持）→ 400。"""
        moment = self.service.create_moment(ALICE, {"body": "a"})
        with self.assertRaises(ValidationError):
            self.service.update_moment(
                ALICE, moment["id"], {"revision": 1, "visibility": None}
            )

    def test_patch_concurrent_write_is_revision_conflict(self):
        """比对与写入之间有人先改：UPDATE 带 revision 条件，后到者 409 而非覆盖。"""
        moment = self.service.create_moment(ALICE, {"body": "a"})
        store = self.service.store
        original = store.update_post

        def racing_update(moment_id, **kwargs):
            # 模拟另一个请求在本请求比对 revision 之后、写入之前抢先成功。
            original(
                moment_id, visibility="self", allow_list=None,
                updated_at="2026-11-01T00:00:00Z", expected_revision=1,
            )
            return original(moment_id, **kwargs)

        store.update_post = racing_update  # type: ignore[assignment]
        try:
            with self.assertRaises(RevisionConflict):
                self.service.update_moment(
                    ALICE, moment["id"], {"revision": 1, "visibility": "server"}
                )
        finally:
            store.update_post = original  # type: ignore[assignment]
        current = self.service.get_moment(ALICE, moment["id"])
        self.assertEqual(current["revision"], 2)
        self.assertEqual(current["visibility"], "self")

    def test_patch_delete_denied_for_non_owner(self):
        self.service.relations.replace([[ALICE, BOB]])
        moment = self.service.create_moment(
            ALICE, {"body": "a", "visibility": "friends"}
        )
        with self.assertRaises(MomentNotFound):
            self.service.update_moment(BOB, moment["id"], {"revision": 1})
        with self.assertRaises(MomentNotFound):
            self.service.delete_moment(BOB, moment["id"])
        # 作者本人可删（软删）。
        self.service.delete_moment(ALICE, moment["id"])
        with self.assertRaises(MomentNotFound):
            self.service.get_moment(ALICE, moment["id"])
        page = self.service.list_moments(ALICE)
        self.assertEqual(page["items"], [])

    def test_delete_then_everything_404(self):
        moment = self.service.create_moment(
            ALICE, {"body": "a", "visibility": "server"}
        )
        self.service.delete_moment(ALICE, moment["id"])
        for call in (
            lambda: self.service.get_moment(STRANGER, moment["id"]),
            lambda: self.service.list_comments(STRANGER, moment["id"]),
            lambda: self.service.list_likes(STRANGER, moment["id"]),
            lambda: self.service.like_moment(STRANGER, moment["id"]),
            lambda: self.service.create_comment(STRANGER, moment["id"], {"body": "x"}),
            lambda: self.service.delete_moment(STRANGER, moment["id"]),
        ):
            with self.assertRaises(MomentNotFound):
                call()


class TestServiceComments(ServiceTestBase):
    def _friends_setup(self):
        # ALICE 与 BOB、CAROL 均为好友；BOB 与 CAROL 互不为好友。
        self.service.relations.replace([[ALICE, BOB], [ALICE, "@carol:localhost"]])
        moment = self.service.create_moment(
            ALICE, {"body": "m", "visibility": "friends"}
        )
        return moment

    def test_comment_flow_and_visibility(self):
        moment = self._friends_setup()
        by_bob = self.service.create_comment(BOB, moment["id"], {"body": "bob"})
        by_carol = self.service.create_comment(
            "@carol:localhost", moment["id"], {"body": "carol"}
        )
        reply = self.service.create_comment(
            ALICE, moment["id"],
            {"body": "reply", "reply_to_comment_id": by_bob["id"]},
        )
        self.assertEqual(reply["reply_to_comment_id"], by_bob["id"])
        self.assertIsNone(by_bob["reply_to_comment_id"])
        # 作者看到全部评论与计数。
        author_view = self.service.get_moment(ALICE, moment["id"])
        self.assertEqual(author_view["comment_count"], 3)
        # BOB 看不到 CAROL 的评论（非共同好友），看得到自己的与作者的。
        bob_comments = self.service.list_comments(BOB, moment["id"])
        bob_ids = {c["id"] for c in bob_comments["items"]}
        self.assertEqual(bob_ids, {by_bob["id"], reply["id"]})
        bob_view = self.service.get_moment(BOB, moment["id"])
        self.assertEqual(bob_view["comment_count"], 2)
        # server 档全部可见。
        server_moment = self.service.create_moment(
            ALICE, {"body": "s", "visibility": "server"}
        )
        self.service.create_comment(BOB, server_moment["id"], {"body": "b1"})
        self.service.create_comment(STRANGER, server_moment["id"], {"body": "b2"})
        stranger_page = self.service.list_comments(STRANGER, server_moment["id"])
        self.assertEqual(len(stranger_page["items"]), 2)

    def test_reply_target_invalid_cases(self):
        moment = self._friends_setup()
        by_bob = self.service.create_comment(BOB, moment["id"], {"body": "bob"})
        other = self.service.create_moment(ALICE, {"body": "o", "visibility": "friends"})
        other_comment = self.service.create_comment(BOB, other["id"], {"body": "other"})
        self.service.delete_comment(BOB, moment["id"], by_bob["id"])
        cases = [
            new_ulid(),                       # 不存在
            by_bob["id"],                     # 已软删
            other_comment["id"],              # 不同动态
        ]
        for target in cases:
            with self.subTest(target=target):
                with self.assertRaises(UnprocessableError) as ctx:
                    self.service.create_comment(
                        "@carol:localhost", moment["id"],
                        {"body": "r", "reply_to_comment_id": target},
                    )
                self.assertEqual(ctx.exception.field, "reply_to_comment_id")
                self.assertEqual(ctx.exception.http_status, 422)
        # 结构类型错（非字符串 / 空串）→ 400（契约 §1.3），不属于 §6 的四类语义 422。
        for bad in (12345, {"x": 1}, ["a"], ""):
            with self.subTest(type_error=bad):
                with self.assertRaises(ValidationError) as ctx:
                    self.service.create_comment(
                        "@carol:localhost", moment["id"],
                        {"body": "r", "reply_to_comment_id": bad},
                    )
                self.assertNotIsInstance(ctx.exception, UnprocessableError)
                self.assertEqual(ctx.exception.http_status, 400)
        # 不可见评论（CAROL 看不到 BOB 的评论）→ 422。
        hidden = self.service.create_comment(BOB, moment["id"], {"body": "h"})
        with self.assertRaises(UnprocessableError):
            self.service.create_comment(
                "@carol:localhost", moment["id"],
                {"body": "r", "reply_to_comment_id": hidden["id"]},
            )
        # 显式 null = 顶层评论。
        top = self.service.create_comment(
            BOB, moment["id"], {"body": "t", "reply_to_comment_id": None}
        )
        self.assertIsNone(top["reply_to_comment_id"])

    def test_comment_body_validation_and_delete(self):
        moment = self._friends_setup()
        for payload, field in (({}, "body"), ({"body": "x" * 501}, "body"), ({"body": "x", "author_persona_id": PERSONA}, "author_persona_id")):
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError) as ctx:
                    self.service.create_comment(BOB, moment["id"], payload)
                self.assertEqual(ctx.exception.field, field)
        comment = self.service.create_comment(BOB, moment["id"], {"body": "ok"})
        # 动态作者可删他人评论；删除后计数减一、列表不再出现。
        self.service.delete_comment(ALICE, moment["id"], comment["id"])
        view = self.service.get_moment(ALICE, moment["id"])
        self.assertEqual(view["comment_count"], 0)
        # 评论不可见 / 不存在 / 无权 → 404。
        with self.assertRaises(MomentNotFound):
            self.service.delete_comment(BOB, moment["id"], comment["id"])
        with self.assertRaises(MomentNotFound):
            self.service.delete_comment(ALICE, moment["id"], new_ulid())
        with self.assertRaises(MomentNotFound):
            self.service.delete_comment(ALICE, moment["id"], "not-ulid")

    def test_comment_delete_by_comment_author_only(self):
        # 动态作者以外的第三方：即使是好友也不能删别人评论。
        moment = self._friends_setup()
        comment = self.service.create_comment(BOB, moment["id"], {"body": "b"})
        with self.assertRaises(MomentNotFound):
            self.service.delete_comment(
                "@carol:localhost", moment["id"], comment["id"]
            )
        self.service.delete_comment(BOB, moment["id"], comment["id"])

    def test_comment_pagination_asc(self):
        moment = self._friends_setup()
        created = [
            self.service.create_comment(BOB, moment["id"], {"body": "c%d" % i})
            for i in range(3)
        ]
        page1 = self.service.list_comments(BOB, moment["id"], limit=2)
        self.assertEqual(
            [c["id"] for c in page1["items"]], [created[0]["id"], created[1]["id"]]
        )
        self.assertTrue(page1["has_more"])
        page2 = self.service.list_comments(
            BOB, moment["id"], limit=2, cursor=page1["next_cursor"]
        )
        self.assertEqual([c["id"] for c in page2["items"]], [created[2]["id"]])
        self.assertFalse(page2["has_more"])


class TestServiceLikes(ServiceTestBase):
    def test_like_flow_and_relative_counts(self):
        self.service.relations.replace([[ALICE, BOB], [ALICE, "@carol:localhost"]])
        moment = self.service.create_moment(
            ALICE, {"body": "m", "visibility": "friends"}
        )
        self.assertEqual(self.service.like_moment(BOB, moment["id"])["liked"], True)
        self.assertEqual(
            self.service.like_moment(BOB, moment["id"])["liked"], True  # 幂等
        )
        self.service.like_moment(ALICE, moment["id"])
        self.service.like_moment("@carol:localhost", moment["id"])
        # BOB：自己的赞 + 好友（作者 ALICE）的赞；CAROL 非 BOB 好友 → 不可见。
        bob_view = self.service.get_moment(BOB, moment["id"])
        self.assertEqual(bob_view["like_count"], 2)
        self.assertTrue(bob_view["liked_by_me"])
        likes_page = self.service.list_likes(BOB, moment["id"])
        self.assertEqual(
            sorted(i["actor"]["id"] for i in likes_page["items"]),
            sorted([BOB, ALICE]),
        )
        # 作者看到全部。
        author_view = self.service.get_moment(ALICE, moment["id"])
        self.assertEqual(author_view["like_count"], 3)
        # 取消幂等。
        self.assertEqual(
            self.service.unlike_moment(BOB, moment["id"])["liked"], False
        )
        self.assertEqual(
            self.service.unlike_moment(BOB, moment["id"])["liked"], False
        )
        bob_view = self.service.get_moment(BOB, moment["id"])
        self.assertEqual(bob_view["like_count"], 1)
        self.assertFalse(bob_view["liked_by_me"])

    def test_likes_pagination(self):
        moment = self.service.create_moment(
            ALICE, {"body": "m", "visibility": "server"}
        )
        for actor in (BOB, STRANGER, "@x:localhost"):
            self.service.like_moment(actor, moment["id"])
        page1 = self.service.list_likes(ALICE, moment["id"], limit=2)
        self.assertEqual(len(page1["items"]), 2)
        self.assertTrue(page1["has_more"])
        page2 = self.service.list_likes(
            ALICE, moment["id"], limit=2, cursor=page1["next_cursor"]
        )
        self.assertEqual(len(page2["items"]), 1)
        actors = [i["actor"]["id"] for i in page1["items"] + page2["items"]]
        self.assertEqual(sorted(actors), sorted([BOB, STRANGER, "@x:localhost"]))


class TestServiceIdempotency(ServiceTestBase):
    def test_same_key_same_body_replays(self):
        first = self.service.create_moment(
            ALICE, {"body": "x"}, idempotency_key="key-12345678"
        )
        second = self.service.create_moment(
            ALICE, {"body": "x"}, idempotency_key="key-12345678"
        )
        self.assertEqual(first, second)
        page = self.service.list_moments(ALICE)
        self.assertEqual(len(page["items"]), 1)
        # 键序无关（规范化 JSON 指纹）。
        third = self.service.create_moment(
            ALICE, {"body": "x"}, idempotency_key="key-12345678"
        )
        self.assertEqual(third["id"], first["id"])

    def test_same_key_different_body_conflicts(self):
        self.service.create_moment(ALICE, {"body": "x"}, idempotency_key="key-12345678")
        with self.assertRaises(IdempotencyConflict):
            self.service.create_moment(
                ALICE, {"body": "y"}, idempotency_key="key-12345678"
            )

    def test_key_scoped_per_viewer_and_endpoint(self):
        self.service.create_moment(ALICE, {"body": "x"}, idempotency_key="key-12345678")
        # 其他 viewer 用同 key 互不干扰。
        other = self.service.create_moment(BOB, {"body": "x"}, idempotency_key="key-12345678")
        self.assertNotEqual(other["author"]["id"], ALICE)
        # 评论与动态的幂等键空间独立。
        moment = self.service.create_moment(
            ALICE, {"body": "m", "visibility": "server"}
        )
        comment = self.service.create_comment(
            BOB, moment["id"], {"body": "c"}, idempotency_key="key-12345678"
        )
        replay = self.service.create_comment(
            BOB, moment["id"], {"body": "c"}, idempotency_key="key-12345678"
        )
        self.assertEqual(comment["id"], replay["id"])

    def test_ttl_expiry_and_capacity(self):
        service = self.make_service(idempotency_ttl_seconds=10, max_idempotency_entries=2)
        a = service.create_moment(ALICE, {"body": "a"}, idempotency_key="key-aaaaaaa")
        self.clock.advance(11)  # 超过 TTL
        b = service.create_moment(ALICE, {"body": "a"}, idempotency_key="key-aaaaaaa")
        self.assertNotEqual(a["id"], b["id"])
        # 容量淘汰：2 条上限，插入第 3 条挤掉最旧。
        c = service.create_moment(ALICE, {"body": "c"}, idempotency_key="key-bbbbbbb")
        service.create_moment(ALICE, {"body": "d"}, idempotency_key="key-ccccccc")
        replay = service.create_moment(ALICE, {"body": "a"}, idempotency_key="key-aaaaaaa")
        self.assertNotEqual(replay["id"], a["id"])  # a 已被淘汰 → 新建


class TestDeniedAudit(ServiceTestBase):
    def test_denied_throttled_per_viewer_moment(self):
        moment = self.service.create_moment(
            ALICE, {"body": "m", "visibility": "friends"}
        )
        for _ in range(3):
            with self.assertRaises(MomentNotFound):
                self.service.get_moment(STRANGER, moment["id"])
        denied = [e for e in self.service.store.list_events() if e["action"] == "denied"]
        self.assertEqual(len(denied), 1)  # 60 秒窗口内只记一次
        # 超过窗口再记一次。
        self.clock.advance(61)
        with self.assertRaises(MomentNotFound):
            self.service.get_moment(STRANGER, moment["id"])
        denied = [e for e in self.service.store.list_events() if e["action"] == "denied"]
        self.assertEqual(len(denied), 2)
        # 不同 viewer 分别记录。
        with self.assertRaises(MomentNotFound):
            self.service.get_moment(BOB, moment["id"])
        denied = [e for e in self.service.store.list_events() if e["action"] == "denied"]
        self.assertEqual(len(denied), 3)
        # 不存在的 id 不记 denied。
        with self.assertRaises(MomentNotFound):
            self.service.get_moment(STRANGER, new_ulid())
        denied = [e for e in self.service.store.list_events() if e["action"] == "denied"]
        self.assertEqual(len(denied), 3)

    def test_audit_actions_recorded(self):
        self.service.relations.replace([[ALICE, BOB]])
        moment = self.service.create_moment(ALICE, {"body": "m"})
        self.service.update_moment(ALICE, moment["id"], {"revision": 1})
        comment = self.service.create_comment(BOB, moment["id"], {"body": "c"})
        self.service.like_moment(BOB, moment["id"])
        self.service.unlike_moment(BOB, moment["id"])
        self.service.delete_comment(BOB, moment["id"], comment["id"])
        self.service.delete_moment(ALICE, moment["id"])
        actions = [e["action"] for e in self.service.store.list_events()]
        self.assertEqual(
            actions,
            ["create", "update_visibility", "comment", "like", "unlike",
             "delete_comment", "delete"],
        )
        # 审计不含任何凭据字段。
        for event in self.service.store.list_events():
            self.assertNotIn("token", json.dumps(event).lower())


class TestPurge(ServiceTestBase):
    def test_purge_respects_retention_and_fk_order(self):
        service = self.make_service(soft_delete_retention_days=30, audit_retention_days=90)
        service.relations.replace([[ALICE, BOB]])
        moment = service.create_moment(
            ALICE, {"body": "m", "visibility": "friends"}
        )
        comment = service.create_comment(BOB, moment["id"], {"body": "c"})
        service.like_moment(BOB, moment["id"])
        # 10 天后软删动态；再创建一条存活动态。
        self.clock.advance(10 * 86400)
        service.delete_moment(ALICE, moment["id"])
        live = service.create_moment(ALICE, {"body": "live"})
        live_comment = service.create_comment(BOB, live["id"], {"body": "ok"})
        # 20 天后 purge：软删 10 天 < 保留 30 天 → 不删。
        self.clock.advance(20 * 86400)
        counts = service.purge(self.clock.now())
        self.assertEqual(counts, {"events": 0, "posts": 0, "comments": 0})
        row = service.store.get_post(moment["id"])
        assert row is not None
        self.assertIsNotNone(row.deleted_at)
        # 再过 25 天（软删已 35 天 > 30）→ 动态连同评论 / 点赞硬删。
        self.clock.advance(25 * 86400)
        counts = service.purge(self.clock.now())
        self.assertEqual(counts["posts"], 1)
        self.assertIsNone(service.store.get_post(moment["id"]))
        self.assertIsNone(service.store.get_comment(comment["id"]))
        self.assertEqual(service.store.likes_for_moment(moment["id"]), [])
        # 存活动态不受影响。
        self.assertIsNotNone(service.store.get_post(live["id"]))
        self.assertIsNotNone(service.store.get_comment(live_comment["id"]))

    def test_purge_orphan_comments_and_audit_expiry(self):
        service = self.make_service(soft_delete_retention_days=30, audit_retention_days=90)
        service.relations.replace([[ALICE, BOB]])
        moment = service.create_moment(
            ALICE, {"body": "m", "visibility": "friends"}
        )
        parent = service.create_comment(BOB, moment["id"], {"body": "p"})
        reply = service.create_comment(
            ALICE, moment["id"],
            {"body": "r", "reply_to_comment_id": parent["id"]},
        )
        service.delete_comment(BOB, moment["id"], parent["id"])
        self.clock.advance(31 * 86400)
        # parent 已软删超期，但仍有存活的 reply 引用 → 外键下保守保留。
        counts = service.purge(self.clock.now())
        self.assertEqual(counts["comments"], 0)
        self.assertIsNotNone(service.store.get_comment(parent["id"]))
        # 软删 reply 并超期后，两者都可清理（循环删除直到收敛）。
        service.delete_comment(ALICE, moment["id"], reply["id"])
        self.clock.advance(31 * 86400)
        counts = service.purge(self.clock.now())
        self.assertEqual(counts["comments"], 2)
        self.assertIsNone(service.store.get_comment(parent["id"]))
        self.assertIsNone(service.store.get_comment(reply["id"]))
        # 审计过期：90 天前的事件被清。
        self.clock.advance(91 * 86400)
        counts = service.purge(self.clock.now())
        self.assertGreater(counts["events"], 0)
        self.assertEqual(service.store.list_events(), [])


class TestAiInternalCalls(ServiceTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.service.persona_dir = StaticPersonaDirectory(
            {
                PERSONA: (OWNER, "大肥鱼", "friends"),
                "persona_nobody": (OWNER, None, "nobody"),
                "persona_paired": (OWNER, None, "paired_servers"),
            }
        )

    def test_ownership_enforced(self):
        with self.assertRaises(PersonaNotOwned):
            self.service.create_moment_as_persona(
                PERSONA, ALICE, {"body": "x"}  # OWNER 名下，不是 ALICE 的
            )
        with self.assertRaises(PersonaNotOwned):  # 未知 persona
            self.service.create_moment_as_persona("ghost", OWNER, {"body": "x"})
        with self.assertRaises(PersonaNotOwned):
            self.service.comment_as_persona(PERSONA, ALICE, new_ulid(), {"body": "x"})
        with self.assertRaises(PersonaNotOwned):
            self.service.like_as_persona(PERSONA, ALICE, new_ulid())

    def test_create_as_persona_defaults(self):
        friends = self.service.create_moment_as_persona(
            PERSONA, OWNER, {"body": "x"}
        )
        self.assertEqual(friends["visibility"], "friends")
        self.assertEqual(friends["author"]["kind"], "ai")
        self.assertEqual(friends["author"]["owner_id"], OWNER)
        self.assertEqual(friends["author"]["display_name"], "大肥鱼")
        nobody = self.service.create_moment_as_persona(
            "persona_nobody", OWNER, {"body": "x"}
        )
        self.assertEqual(nobody["visibility"], "self")  # nobody → self
        paired = self.service.create_moment_as_persona(
            "persona_paired", OWNER, {"body": "x"}
        )
        self.assertEqual(paired["visibility"], "paired_servers")
        # 校验沿用公开路径：author_persona_id 在内部 payload 中是未知字段。
        with self.assertRaises(ValidationError):
            self.service.create_moment_as_persona(
                PERSONA, OWNER, {"body": "x", "author_persona_id": PERSONA}
            )

    def test_comment_and_like_as_persona_bound_to_owner(self):
        carol = "@carol:localhost"
        moment = self.service.create_moment(
            ALICE, {"body": "m", "visibility": "friends"}
        )
        # AI 点赞 / 评论以 owner 的可见性为前提：OWNER 不是 ALICE 好友 → 404。
        with self.assertRaises(MomentNotFound):
            self.service.like_as_persona(PERSONA, OWNER, moment["id"])
        with self.assertRaises(MomentNotFound):
            self.service.comment_as_persona(PERSONA, OWNER, moment["id"], {"body": "c"})
        # OWNER 与 ALICE 成为好友（AI 经主人视角 can_view）；
        # BOB 同时是 ALICE 与 OWNER 的好友；CAROL 只是 ALICE 的好友。
        self.service.relations.replace(
            [[ALICE, BOB], [ALICE, carol], [OWNER, BOB], [ALICE, OWNER]]
        )
        state = self.service.like_as_persona(PERSONA, OWNER, moment["id"])
        self.assertEqual(state["liked"], True)
        comment = self.service.comment_as_persona(
            PERSONA, OWNER, moment["id"], {"body": "c"}
        )
        self.assertEqual(comment["author"]["id"], PERSONA)
        self.assertEqual(comment["author"]["kind"], "ai")
        self.assertEqual(comment["author"]["owner_id"], OWNER)
        # BOB（OWNER 的好友）看得到 AI 的评论 / 点赞；
        # CAROL（非 OWNER 好友）看得到动态但看不到 AI 交互。
        bob_view = self.service.get_moment(BOB, moment["id"])
        self.assertEqual(bob_view["comment_count"], 1)
        self.assertEqual(bob_view["like_count"], 1)
        carol_view = self.service.get_moment(carol, moment["id"])
        self.assertEqual(carol_view["comment_count"], 0)
        self.assertEqual(carol_view["like_count"], 0)
        # OWNER（主人）能看到。
        owner_view = self.service.get_moment(OWNER, moment["id"])
        self.assertEqual(owner_view["comment_count"], 1)


# ============================================================================
# 8. 配置
# ============================================================================
# load_config 的最小安全环境（dev 模式 + 假 HMAC，避免与鉴权默认值纠缠）。
_DEV_ENV = {
    "GATEWAY_DEV_MODE": "true",
    "GATEWAY_HMAC_SECRET": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
}


class TestMomentsConfig(unittest.TestCase):
    def test_defaults_and_disabled(self):
        config = load_config(_DEV_ENV)
        self.assertIsNone(config.moments_db_path)
        self.assertIsNone(config.moments_friends_file)
        self.assertEqual(config.moments_soft_delete_retention_days, 30)
        self.assertEqual(config.moments_audit_retention_days, 90)
        self.assertIsNone(build_moments_service(config))  # 模块关闭

    def test_env_parsing(self):
        tmp = tempfile.mkdtemp(prefix="moments-cfg-")
        self.addCleanup(shutil.rmtree, tmp, True)
        db_path = os.path.join(tmp, "m.db")
        config = load_config(dict(
            _DEV_ENV,
            GATEWAY_MOMENTS_DB_PATH=db_path,
            GATEWAY_MOMENTS_SOFT_DELETE_RETENTION_DAYS="7",
            GATEWAY_MOMENTS_AUDIT_RETENTION_DAYS="180",
        ))
        self.assertEqual(config.moments_db_path, db_path)
        self.assertEqual(config.moments_soft_delete_retention_days, 7)
        self.assertEqual(config.moments_audit_retention_days, 180)
        service = build_moments_service(config)
        self.assertIsNotNone(service)
        self.assertEqual(service._soft_days, 7)
        self.assertEqual(service._audit_days, 180)
        versions = applied_versions(service.store._conn)
        self.assertEqual(list(versions), ["0001_moments"])
        service.stop_purge_scheduler()
        service.store.close()

    def test_env_friends_file(self):
        tmp = tempfile.mkdtemp(prefix="moments-cfg-")
        self.addCleanup(shutil.rmtree, tmp, True)
        friends = os.path.join(tmp, "friends.json")
        with open(friends, "w", encoding="utf-8") as fh:
            json.dump({"friends": [[ALICE, BOB]]}, fh)
        config = load_config(dict(
            _DEV_ENV,
            GATEWAY_MOMENTS_DB_PATH=os.path.join(tmp, "m.db"),
            GATEWAY_MOMENTS_FRIENDS_FILE=friends,
        ))
        service = build_moments_service(config)
        assert service is not None
        self.assertTrue(service.relations.is_friend(ALICE, BOB))
        service.store.close()

    def test_invalid_retention_rejected(self):
        for name in (
            "GATEWAY_MOMENTS_SOFT_DELETE_RETENTION_DAYS",
            "GATEWAY_MOMENTS_AUDIT_RETENTION_DAYS",
        ):
            with self.subTest(name=name):
                with self.assertRaises(ConfigError):
                    load_config(dict(_DEV_ENV, **{name: "0"}))
        with self.assertRaises(ConfigError):  # 直接构造同样校验
            Config(
                host="127.0.0.1", port=0, dev_mode=True, auth_token=None,
                silent_hours_enabled=False, hmac_secret=_HMAC,
                moments_soft_delete_retention_days=0,
            )

    def test_friends_file_invalid_fails_startup(self):
        tmp = tempfile.mkdtemp(prefix="moments-cfg-")
        self.addCleanup(shutil.rmtree, tmp, True)
        bad = os.path.join(tmp, "bad.json")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write("[]")
        config = load_config(dict(
            _DEV_ENV,
            GATEWAY_MOMENTS_DB_PATH=os.path.join(tmp, "m.db"),
            GATEWAY_MOMENTS_FRIENDS_FILE=bad,
        ))
        with self.assertRaises(ConfigError):
            build_moments_service(config)


# ============================================================================
# #184 审查建议：purge 定时调用（启动一次 + 后台按间隔，可关闭，异常只记日志）
# ============================================================================
class TestPurgeScheduler(ServiceTestBase):
    def _wait_until(self, predicate, timeout=3.0):
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()

    def test_scheduler_runs_repeatedly_and_survives_errors(self):
        service = self.make_service()
        calls = []

        def flaky_purge(now=None):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("disk hiccup")
            return {"events": 0, "posts": 0, "comments": 0}

        service.purge = flaky_purge  # type: ignore[assignment]
        with self.assertLogs("botville_gateway.moments.service", level="WARNING") as logs:
            service.start_purge_scheduler(0.02)
            self.addCleanup(service.stop_purge_scheduler)
            # 第一次抛异常只记日志，线程继续跑后续轮次。
            self.assertTrue(self._wait_until(lambda: len(calls) >= 3))
        self.assertTrue(any("moments purge failed" in line for line in logs.output))
        thread = service._purge_thread
        self.assertTrue(thread.daemon)
        service.stop_purge_scheduler()
        self.assertFalse(thread.is_alive())
        stopped_at = len(calls)
        import time
        time.sleep(0.1)
        self.assertEqual(len(calls), stopped_at)

    def test_run_purge_safely_swallows_errors(self):
        service = self.make_service()

        def boom(now=None):
            raise ValueError("x")

        service.purge = boom  # type: ignore[assignment]
        with self.assertLogs("botville_gateway.moments.service", level="WARNING"):
            self.assertIsNone(service.run_purge_safely())

    def test_invalid_interval_rejected(self):
        service = self.make_service()
        with self.assertRaises(ValueError):
            service.start_purge_scheduler(0)

    def _config(self, tmp, interval):
        return load_config(dict(
            _DEV_ENV,
            GATEWAY_MOMENTS_DB_PATH=os.path.join(tmp, "m.db"),
            GATEWAY_MOMENTS_PURGE_INTERVAL_SECONDS=interval,
        ))

    def test_build_runs_startup_purge_and_starts_scheduler(self):
        tmp = tempfile.mkdtemp(prefix="moments-purge-")
        self.addCleanup(shutil.rmtree, tmp, True)
        # 先用同一个库造一条早已过保留期的软删动态。
        seed = build_moments_service(self._config(tmp, "0"))
        assert seed is not None
        old = seed.create_moment(ALICE, {"body": "old"})
        seed.delete_moment(ALICE, old["id"])
        seed.store._conn.execute(
            "UPDATE moments_posts SET deleted_at = '2000-01-01T00:00:00Z'"
        )
        seed.store._conn.commit()
        seed.store.close()
        service = build_moments_service(self._config(tmp, "3600"))
        assert service is not None
        self.addCleanup(service.store.close)
        self.addCleanup(service.stop_purge_scheduler)
        # 启动阶段同步 purge 已经清掉过期软删行。
        self.assertIsNone(service.store.get_post(old["id"]))
        self.assertIsNotNone(service._purge_thread)
        self.assertTrue(service._purge_thread.is_alive())

    def test_interval_zero_disables(self):
        tmp = tempfile.mkdtemp(prefix="moments-purge-")
        self.addCleanup(shutil.rmtree, tmp, True)
        seed = build_moments_service(self._config(tmp, "0"))
        assert seed is not None
        old = seed.create_moment(ALICE, {"body": "old"})
        seed.delete_moment(ALICE, old["id"])
        seed.store._conn.execute(
            "UPDATE moments_posts SET deleted_at = '2000-01-01T00:00:00Z'"
        )
        seed.store._conn.commit()
        self.assertIsNone(getattr(seed, "_purge_thread", None))
        # 关闭时启动不 purge：过期软删行仍在。
        self.assertIsNotNone(seed.store.get_post(old["id"]))
        seed.store.close()

    def test_config_interval_parsing(self):
        self.assertEqual(load_config(_DEV_ENV).moments_purge_interval_seconds, 86400)
        self.assertEqual(
            load_config(dict(_DEV_ENV, GATEWAY_MOMENTS_PURGE_INTERVAL_SECONDS="0"))
            .moments_purge_interval_seconds, 0,
        )
        self.assertEqual(
            load_config(dict(_DEV_ENV, GATEWAY_MOMENTS_PURGE_INTERVAL_SECONDS="60"))
            .moments_purge_interval_seconds, 60,
        )
        for bad in ("30", "-1", "abc", "1.5"):
            with self.subTest(value=bad):
                with self.assertRaises(ConfigError):
                    load_config(dict(_DEV_ENV, GATEWAY_MOMENTS_PURGE_INTERVAL_SECONDS=bad))


if __name__ == "__main__":
    unittest.main()
