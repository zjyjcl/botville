#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""A2.2（issue #7）动态契约测试：DDL 幂等 + OpenAPI 契约形状。

只依赖标准库 + PyYAML（CI 已安装），不启动任何服务：
  1. 迁移 DDL 可在 sqlite3 内存库上执行两遍（IF NOT EXISTS 幂等），
     且所有 moments_ 表与必要索引存在；
  2. 契约中 Moments 路径齐全，评论/点赞的读写操作 description 均以 can_view
     为前提（字面包含 "can_view"），且交互描述含 can_see_interaction /
     friend_subject 或 owner_id 绑定说明；安全方案为 matrixUserToken；
  3. Visibility 枚举与 moment 示例一致；UpdateMomentRequest / Moment.allow_list 可 null；
     真人发布缺省 visibility 写明为 friends；MomentAuthor / moments_likes 含 owner_id；
     文档明确 persona 不作公开契约查看者、AI 评论/点赞走 appservice 内部。
"""

from __future__ import annotations

import json
import os
import sqlite3
import unittest

import yaml

GATEWAY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTRACT_PATH = os.path.join(GATEWAY_ROOT, "openapi", "gateway.v1.yaml")
EXAMPLES_DIR = os.path.join(GATEWAY_ROOT, "openapi", "examples")
DDL_PATH = os.path.join(GATEWAY_ROOT, "migrations", "moments", "0001_moments.sql")

MOMENT_PATHS = [
    "/moments",
    "/moments/{moment_id}",
    "/moments/{moment_id}/comments",
    "/moments/{moment_id}/comments/{comment_id}",
    "/moments/{moment_id}/likes",
    "/moments/{moment_id}/likes/me",
]

# 评论/点赞的读、写、删操作：description 必须写明以 can_view(viewer, moment) 为前提。
INTERACTION_OPERATIONS = [
    ("/moments/{moment_id}/comments", "get"),
    ("/moments/{moment_id}/comments", "post"),
    ("/moments/{moment_id}/comments/{comment_id}", "delete"),
    ("/moments/{moment_id}/likes", "get"),
    ("/moments/{moment_id}/likes/me", "put"),
    ("/moments/{moment_id}/likes/me", "delete"),
]

MOMENTS_TABLES = [
    "moments_posts",
    "moments_comments",
    "moments_likes",
    "moments_events",
]

MOMENTS_INDEXES = [
    "idx_moments_posts_author_created",
    "idx_moments_posts_created",
    "idx_moments_comments_moment_created",
    "idx_moments_events_at",
]

VISIBILITY_ENUM = ["self", "friends", "server", "paired_servers"]


def _load_contract():
    with open(CONTRACT_PATH, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _sqlite_names(conn, kind):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = ?", (kind,)
    ).fetchall()
    return {name for (name,) in rows}


class TestMomentsDdl(unittest.TestCase):
    """迁移 DDL 在 sqlite3 内存库上可执行两遍且对象齐全。"""

    def test_ddl_executes_twice_and_creates_objects(self):
        with open(DDL_PATH, "r", encoding="utf-8") as handle:
            ddl = handle.read()
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(ddl)
            conn.executescript(ddl)  # 第二遍：IF NOT EXISTS 幂等
            tables = _sqlite_names(conn, "table")
            indexes = _sqlite_names(conn, "index")
        finally:
            conn.close()
        for table in MOMENTS_TABLES:
            self.assertIn(table, tables, "missing table %s" % table)
        for index in MOMENTS_INDEXES:
            self.assertIn(index, indexes, "missing index %s" % index)


class TestMomentsContract(unittest.TestCase):
    """OpenAPI 契约中的动态路径、鉴权与可见性语义。"""

    @classmethod
    def setUpClass(cls):
        cls.contract = _load_contract()

    def test_moments_paths_present(self):
        paths = self.contract["paths"]
        for path in MOMENT_PATHS:
            self.assertIn(path, paths, "missing path %s" % path)

    def test_moments_tag_declared(self):
        names = [tag["name"] for tag in self.contract["tags"]]
        self.assertIn("Moments", names)

    def test_moments_operations_use_matrix_user_token(self):
        for path in MOMENT_PATHS:
            for method in ("get", "post", "put", "patch", "delete"):
                operation = self.contract["paths"][path].get(method)
                if operation is None:
                    continue
                schemes = operation.get("security") or []
                self.assertIn(
                    "matrixUserToken",
                    {name for entry in schemes for name in entry},
                    "%s %s must require matrixUserToken" % (method.upper(), path),
                )

    def test_moments_operations_accept_matrix_openid_token(self):
        """#124 阶段 1：#169 契约加法——/moments* 的 security OR 里也必须有
        matrixOpenIdToken；matrixUserToken 仍必须同时存在（并存期，不许删）。
        """
        for path in MOMENT_PATHS:
            for method in ("get", "post", "put", "patch", "delete"):
                operation = self.contract["paths"][path].get(method)
                if operation is None:
                    continue
                schemes = operation.get("security") or []
                names = {name for entry in schemes for name in entry}
                self.assertIn(
                    "matrixOpenIdToken",
                    names,
                    "%s %s must list matrixOpenIdToken in its OR security"
                    % (method.upper(), path),
                )
                self.assertIn(
                    "matrixUserToken",
                    names,
                    "%s %s must keep matrixUserToken (coexistence, not removed)"
                    % (method.upper(), path),
                )
                # OR 语义：两条各自都是独立的 security 要求项，不能合成 AND。
                self.assertIn(
                    {"matrixOpenIdToken": []},
                    schemes,
                    "%s %s must list matrixOpenIdToken as its own OR alternative"
                    % (method.upper(), path),
                )
                self.assertIn(
                    {"matrixUserToken": []},
                    schemes,
                    "%s %s must list matrixUserToken as its own OR alternative"
                    % (method.upper(), path),
                )

    def test_interaction_operations_mention_can_view(self):
        for path, method in INTERACTION_OPERATIONS:
            operation = self.contract["paths"][path][method]
            description = operation.get("description") or ""
            self.assertIn(
                "can_view",
                description,
                "%s %s description must state the can_view(viewer, moment) precondition"
                % (method.upper(), path),
            )


    def test_interaction_operations_mention_friend_binding(self):
        """AI 好友判定绑到 owner_id / friend_subject 须写进评论与点赞描述。"""
        for path, method in INTERACTION_OPERATIONS:
            if method == "delete" and path.endswith("/likes/me"):
                continue  # unlike 不列 can_see_interaction 细节也可
            operation = self.contract["paths"][path][method]
            description = operation.get("description") or ""
            if method in ("get",) or (method == "post" and "comments" in path):
                self.assertTrue(
                    "can_see_interaction" in description
                    or "friend_subject" in description
                    or "owner_id" in description,
                    "%s %s must document AI friend binding"
                    % (method.upper(), path),
                )

    def test_update_moment_allow_list_accepts_null(self):
        schema = self.contract["components"]["schemas"]["UpdateMomentRequest"]
        allow = schema["properties"]["allow_list"]
        variants = allow.get("oneOf") or allow.get("anyOf") or [allow]
        types = set()
        for variant in variants:
            t = variant.get("type")
            if isinstance(t, list):
                types.update(t)
            elif t:
                types.add(t)
        self.assertIn("null", types)
        self.assertIn("array", types)

    def test_create_documents_human_default_visibility(self):
        post = self.contract["paths"]["/moments"]["post"]["description"]
        self.assertIn("friends", post)
        self.assertRegex(post, r"不带.*author_persona_id|真人")
        create = self.contract["components"]["schemas"]["CreateMomentRequest"][
            "description"
        ]
        self.assertIn("friends", create)

    def test_visibility_friends_documents_ai_owner_binding(self):
        vis = self.contract["components"]["schemas"]["Visibility"]["description"]
        self.assertIn("owner_id", vis)

    def test_visibility_enum(self):
        enum = self.contract["components"]["schemas"]["Visibility"]["enum"]
        self.assertEqual(enum, VISIBILITY_ENUM)

    def test_moment_example_visibility_is_legal(self):
        with open(
            os.path.join(EXAMPLES_DIR, "moment.json"), "r", encoding="utf-8"
        ) as handle:
            moment = json.load(handle)
        self.assertIn(moment["visibility"], VISIBILITY_ENUM)


    def test_moment_allow_list_accepts_null(self):
        """响应 Moment.allow_list 与描述一致：可 array 或 null。"""
        schema = self.contract["components"]["schemas"]["Moment"]
        allow = schema["properties"]["allow_list"]
        t = allow.get("type")
        if isinstance(t, list):
            types = set(t)
        else:
            variants = allow.get("oneOf") or allow.get("anyOf") or [allow]
            types = set()
            for variant in variants:
                vt = variant.get("type")
                if isinstance(vt, list):
                    types.update(vt)
                elif vt:
                    types.add(vt)
        self.assertIn("null", types)
        self.assertIn("array", types)

    def test_moment_author_documents_owner_id(self):
        author = self.contract["components"]["schemas"]["MomentAuthor"]
        self.assertIn("owner_id", author["properties"])
        desc = author.get("description") or ""
        self.assertIn("owner_id", desc)
        self.assertRegex(desc, r"persona|查看者")

    def test_likes_ddl_has_owner_id(self):
        with open(DDL_PATH, "r", encoding="utf-8") as handle:
            ddl = handle.read()
        # moments_likes 表定义内必须有 owner_id 列
        start = ddl.index("CREATE TABLE IF NOT EXISTS moments_likes")
        end = ddl.index(";", start)
        block = ddl[start:end]
        self.assertIn("owner_id", block)

    def test_docs_viewer_is_matrix_user(self):
        docs = os.path.join(GATEWAY_ROOT, "docs", "moments.md")
        with open(docs, "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("persona 不作为公开契约的查看者", text)
        self.assertIn("AI 评论 / 点赞的创建路径", text)
        self.assertIn("moments_likes.owner_id", text)



if __name__ == "__main__":
    unittest.main()
