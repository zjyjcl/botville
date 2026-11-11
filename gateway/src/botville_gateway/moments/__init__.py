# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 botville contributors
"""动态（Moments）模块（A2.2 / issue #7，PR2 同服实现）。

子模块：

  ulid        — ULID 生成（Crockford base32，时间有序）
  migrations  — 迁移执行器（gateway/migrations/moments/*.sql，幂等）
  store       — SQLite 访问层（posts / comments / likes / events）
  visibility  — RelationProvider / PersonaDirectory / can_view /
                can_see_interaction（moments.md §4 单一判定函数）
  service     — 业务服务（HTTP 公开操作 + AI 内部调用 + 幂等 + 审计 + 清理）
  http_api    — HTTP 分发（挂在 http_server 的 /v1/moments* 鉴权之后）

未配置 `GATEWAY_MOMENTS_DB_PATH` 时模块整体关闭（build 返回 None），
`/v1/moments*` 鉴权后行为与本模块合入前完全一致（404/405）。
"""
