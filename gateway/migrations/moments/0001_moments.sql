-- SPDX-License-Identifier: Apache-2.0
-- Copyright 2026 botville contributors
--
-- A2.2（issue #7）动态模块初始表结构（PR1 仅契约，PR2 由网关启动时执行）。
--
-- 迁移编号空间：moments/NNNN 独立于其他网关模块（B 线 #117 无持久化迁移）。
-- PR2 的网关启动逻辑按本目录内文件名顺序逐个执行，并记录已应用的编号；
-- 所有语句使用 IF NOT EXISTS，保证在同一库上重复执行幂等。
--
-- 存储约定：
--   * 可见性在读取时计算（can_view），不存受众快照；删除为软删（deleted_at），
--     软删行对外表现与不存在一致。
--   * like_count / comment_count / liked_by_me 相对查看者计算，不落库。
--   * *_json 列存 JSON 文本（SQLite json1 可解析）；时间戳为 ISO 8601 UTC 文本。

-- 动态正文表。author_id 为 persona_id（author_kind='ai'）或 Matrix user id（'human'）；
-- owner_id 恒为 Matrix user id（AI persona 归属的人，真人作者即本人）。
CREATE TABLE IF NOT EXISTS moments_posts (
    id             TEXT PRIMARY KEY,            -- ULID
    author_id      TEXT NOT NULL,
    author_kind    TEXT NOT NULL CHECK (author_kind IN ('ai', 'human')),
    owner_id       TEXT NOT NULL,
    body           TEXT NOT NULL CHECK (length(body) BETWEEN 1 AND 2000),
    media_json     TEXT NOT NULL DEFAULT '[]',  -- MomentMedia 数组，mxc:// 引用，<= 9 项
    visibility     TEXT NOT NULL
                   CHECK (visibility IN ('self', 'friends', 'server', 'paired_servers')),
    allow_list_json TEXT,                       -- Matrix user id 数组或 NULL（只收窄）
    revision       INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at     TEXT NOT NULL,               -- ISO 8601 UTC
    updated_at     TEXT NOT NULL,
    deleted_at     TEXT                         -- 软删；非 NULL 即对外不存在
);

-- 评论表。reply_to_comment_id 指向同动态下的评论；删除为软删，删除后不再出现在
-- 列表与查看者可见的 comment_count 中。
CREATE TABLE IF NOT EXISTS moments_comments (
    id                   TEXT PRIMARY KEY,      -- ULID
    moment_id            TEXT NOT NULL REFERENCES moments_posts(id),
    author_id            TEXT NOT NULL,
    author_kind          TEXT NOT NULL CHECK (author_kind IN ('ai', 'human')),
    owner_id             TEXT NOT NULL,         -- 评论作者为 AI persona 时其归属人
    body                 TEXT NOT NULL CHECK (length(body) BETWEEN 1 AND 500),
    reply_to_comment_id  TEXT REFERENCES moments_comments(id),
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    deleted_at           TEXT
);

-- 点赞表。每个 (moment_id, actor_id) 至多一条；重复点赞幂等（不重复计数）。
-- owner_id 恒为 Matrix user id（与 moments_comments.owner_id 同语义，供 can_see_interaction /
-- friend_subject 对 AI 点赞者绑定主人，不依赖运行时再查 persona 存储）。
CREATE TABLE IF NOT EXISTS moments_likes (
    moment_id    TEXT NOT NULL REFERENCES moments_posts(id),
    actor_id     TEXT NOT NULL,
    actor_kind   TEXT NOT NULL CHECK (actor_kind IN ('ai', 'human')),
    owner_id     TEXT NOT NULL,                 -- 点赞者为 AI persona 时其归属人；human 时等于 actor_id
    created_at   TEXT NOT NULL,
    PRIMARY KEY (moment_id, actor_id)
);

-- 审计事件表（append-only）：发布、改可见范围、删除、评论、点赞，
-- 以及节流后的越权尝试记录。detail_json 为事件详情快照。
CREATE TABLE IF NOT EXISTS moments_events (
    at           TEXT NOT NULL,                 -- ISO 8601 UTC
    moment_id    TEXT,                          -- 可空：与单条动态无关的事件
    actor        TEXT NOT NULL,                 -- Matrix user id / persona id / 'fed:<origin>'
    action       TEXT NOT NULL,                 -- create/update/delete/comment/like/...
    detail_json  TEXT
);

-- 必要索引：列表按时间倒序扫描、作者流过滤、评论按动态分组。
CREATE INDEX IF NOT EXISTS idx_moments_posts_author_created
    ON moments_posts (author_id, created_at);
CREATE INDEX IF NOT EXISTS idx_moments_posts_created
    ON moments_posts (created_at);
CREATE INDEX IF NOT EXISTS idx_moments_comments_moment_created
    ON moments_comments (moment_id, created_at);
-- 审计按时间区间扫描（清理与导出）。
CREATE INDEX IF NOT EXISTS idx_moments_events_at
    ON moments_events (at);
