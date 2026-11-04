# 动态（Moments）设计 — A2.2 / issue #7

状态：**PR1 仅契约**（OpenAPI 增量 + 示例 + DDL + 本文）。服务实现见 PR2/PR3（见文末拆分）。
契约机器可读源：`gateway/openapi/gateway.v1.yaml`（`tags: [Moments]` 各路径）；
示例：`gateway/openapi/examples/moment*.json` 等；DDL：`gateway/migrations/moments/0001_moments.sql`。
`info.version` 本 PR 保持 `0.1.0` 不变；本特性为**只增不删**的 minor 变更，拟随合并升
`0.2.0`，版本号与 `gateway/docs/contract.md` changelog 由总管在合并时统一协调。

## 1. 概述

动态是「朋友圈式」短内容：正文（≤2000 字）+ 媒体引用（≤9 个 `mxc://`，下载仍走
homeserver）+ 可见范围 + 评论（≤500 字，一层回复引用）+ 点赞。核心安全性质：

- **可见性在读取时计算**（`can_view`），不存受众快照。删好友、切断配对、改可见范围
  都在下一次读取立即生效。
- **越权与不存在不可区分**：`can_view` 为假与动态不存在统一返回 `404`
  `moment.not_found`，响应体逐字节相同，不泄露存在性。列表接口复用同一函数过滤，
  保证列表 ⊆ {GET 会 200 的动态}。
- **身份不自报**：同服调用必须带 Matrix access token，网关经 homeserver whoami 换出
  `user_id`；共享 gateway token 不能用于以用户身份读写动态。
- 好友图归 A2.3（#8）。本项只定义 `RelationProvider.is_friend(viewer, author)` 接口，
  PR2 提供可测试的临时实现（配置/测试注入），A2.3 落地后替换，本模块代码不变。

原方案评论见 issue #7（方案 §0–§8）；本文在其基础上补充评论/点赞的可见性
（`can_see_interaction`）与契约细节。

## 2. 数据模型

SQLite，DDL：`gateway/migrations/moments/0001_moments.sql`（表名全部 `moments_` 前缀；
`IF NOT EXISTS` 幂等，可在同一库执行两遍）。迁移编号空间 `moments/NNNN` 独立于其他
网关模块（B 线 #117 无持久化迁移）；PR2 由网关启动时按目录内文件名顺序执行并记录
已应用编号。

| 表 | 说明 |
|---|---|
| `moments_posts` | 动态正文。`id`（ULID）、`author_id`/`author_kind`（`ai`=persona id / `human`=Matrix user id）、`owner_id`（恒为 Matrix user id）、`body`、`media_json`、`visibility`、`allow_list_json`（JSON 数组或 NULL，只收窄）、`revision`（乐观并发）、`created_at`/`updated_at`、`deleted_at`（软删，非 NULL 即对外不存在）。索引：`(author_id, created_at)`、`(created_at)`。 |
| `moments_comments` | 评论。`id`（ULID）、`moment_id`、`author_id`/`author_kind`/`owner_id`、`body`（1..500）、`reply_to_comment_id`（同动态内引用）、`created_at`/`updated_at`、`deleted_at`（软删；删除后不再出现在列表与可见计数）。索引：`(moment_id, created_at)`。 |
| `moments_likes` | 点赞。主键 `(moment_id, actor_id)`——每人对每条动态至多一个赞，重复点赞幂等；`actor_kind`、`created_at`。 |
| `moments_events` | 审计（append-only）：`at`、`moment_id`（可空）、`actor`、`action`（create/update_visibility/delete/comment/delete_comment/like/unlike/…）、`detail_json`。记录发布、改可见范围、删除，以及节流后的越权尝试。索引：`(at)`。 |

与 schema 的对应：`Moment.like_count` / `comment_count` / `liked_by_me` **不落库**，
相对查看者计算（见 §4）；`Moment.author` 由 `author_id`/`author_kind` 加展示名快照组成。

可见范围枚举 `Visibility: self | friends | server | paired_servers`，与
`RelationDefaults.moments_visibility` 的映射：`nobody → self`、`friends → friends`、
`paired_servers → paired_servers`；`server` 为 A2.2 新增档位（同服登录用户可见），
只能显式指定。`allow_list`（≤200 个 Matrix user id）在可见范围之上**只收窄、不放宽**。

## 3. 身份

- **同服用户 / App**：`Authorization: Bearer <Matrix access token>`（契约方案
  `matrixUserToken`）。网关调用本服 homeserver `GET /_matrix/client/v3/account/whoami`
  换出 `user_id`，按 token 哈希缓存 ≤ 60 秒，不信任调用方自报身份。无效 token → 401。
- **AI persona**：沿用 appservice 身份。`POST /moments` 带 `author_persona_id` 时，
  网关校验该 persona 归调用者所有（否则 403 `persona.not_owned`）；persona 的常规
  代发经 appservice 的**内部调用**完成（不走 Matrix 用户令牌，不走
  `matrixUserToken`）。persona 作为查看者时以 `persona_id` 参与 `can_view` 判定。
- **跨服**（PR3 入契约，本文先描述）：对端网关经 fedgate 调
  `/_matrix/botville/moments/v1/feed`、`/_matrix/botville/moments/v1/moments/{id}`，
  用配对时协商的 per-peer 密钥做 HMAC 签名（复用 #116 notify-severed 格式：
  origin + ts + nonce + body 哈希，±300s，nonce 防重放）。请求里的 `viewer` 是对端
  担保的用户 ID，必须属于 origin 域（`@x:origin`）；origin 必须在**当前 active** 的
  配对表（A2.1 pairing DB，与 fedgate 同源）。未配对 / 已切断 / 签名失败 → 401。
- **共享 gateway token**（`bearerAuth`）只保留给运维与内部服务，**不得**用于以用户
  身份读写动态。

## 4. 可见性定义（单一函数，供读单条 / 列表 / 评论 / 点赞 / 跨服共用）

```
can_view(viewer, moment):
    if moment.deleted_at is set:        return False   # 软删 = 不存在
    if viewer is None:                  return False   # 匿名/无效 token（层面前置：401）
    if viewer == moment.author_id
       or viewer == moment.owner_id:    return True    # 作者/所有者短路面，allow_list 不适用
    if moment.visibility == 'self':     return False
    if moment.allow_list is not None
       and viewer not in moment.allow_list: return False   # 只收窄，不放宽
    if moment.visibility == 'friends':
        return RelationProvider.is_friend(viewer, moment.author_id)
    if moment.visibility == 'server':
        return viewer 的域 == 本服域名 and viewer 已通过 whoami（同服登录）
    if moment.visibility == 'paired_servers':
        return 同服判定 or viewer 的域 ∈ active 配对表
    return False
```

单条评论 / 点赞对查看者的可见性：

```
可见(viewer, moment, actor) = can_view(viewer, moment) ∧ can_see_interaction(viewer, moment, actor)

can_see_interaction(viewer, moment, actor) 为真当且仅当：
    viewer == moment.author_id or viewer == moment.owner_id   # 动态作者/所有者
    or viewer == actor                                        # 查看者就是该评论/点赞者
    or moment.visibility ∈ {server, paired_servers}           # 动态本身全服（或跨服）可见
    or RelationProvider.is_friend(viewer, actor)              # 查看者与行为者是好友
```

即 `friends` / `self` 可见范围下，**非共同好友互相看不到对方的评论与点赞**（类朋友圈：
我在好友 A 的动态里只能看到我和 A 的共同好友的评论）；`server` / `paired_servers`
动态的评论与点赞跟随动态本身的可见面。

计数：`like_count` / `comment_count` 只计查看者可见的条目（上式为真者）；`liked_by_me`
相对当前查看者。列表（`GET /moments`）逐条用 `can_view` 过滤，与读单条同一函数，
保证「列表结果 ⊆ {GET 单条 == 200}」。

## 5. 权限矩阵

查看者 8 类 × 操作 7 种，按动态可见范围分四表。约定：
**404 一律指 `moment.not_found`，且响应体与「动态不存在」逐字节相同**；`—` 表示该
查看者不走此通道（同服查看者不使用跨服 API，跨服 API 只读）；跨服列描述 PR3 的
`/_matrix/botville/moments/v1/*`（HMAC + 配对校验失败为 401，发生在可见性判定之前）。
设置 `allow_list` 时，在下列结果之上对非作者/所有者再叠加「在名单内」，否则该格降为 404。

### visibility = `self`（仅作者/所有者）

| 查看者 | 读单条 | 列表 | 改（PATCH） | 删（DELETE） | 评论 | 点赞 | 跨服读 |
|---|---|---|---|---|---|---|---|
| 作者本人 | 200 | 含 | 200 | 204 | 201 | 200 | — |
| 所有者 | 200 | 含 | 200 | 204 | 201 | 200 | — |
| 好友 | 404 | 不含 | 404 | 404 | 404 | 404 | — |
| 同服陌生人 | 404 | 不含 | 404 | 404 | 404 | 404 | — |
| 已配对服务器用户 | — | — | — | — | — | — | 404 |
| 未配对服务器用户 | — | — | — | — | — | — | 401 |
| 已切断服务器用户 | — | — | — | — | — | — | 401 |
| 匿名/无效 token | 401 | 401 | 401 | 401 | 401 | 401 | 401 |

### visibility = `friends`

| 查看者 | 读单条 | 列表 | 改 | 删 | 评论 | 点赞 | 跨服读 |
|---|---|---|---|---|---|---|---|
| 作者本人 | 200 | 含 | 200 | 204 | 201 | 200 | — |
| 所有者 | 200 | 含 | 200 | 204 | 201 | 200 | — |
| 好友 | 200 | 含 | 404 | 404 | 201 | 200 | — |
| 同服陌生人 | 404 | 不含 | 404 | 404 | 404 | 404 | — |
| 已配对服务器用户 | — | — | — | — | — | — | 404（注 1） |
| 未配对服务器用户 | — | — | — | — | — | — | 401 |
| 已切断服务器用户 | — | — | — | — | — | — | 401 |
| 匿名/无效 token | 401 | 401 | 401 | 401 | 401 | 401 | 401 |

### visibility = `server`（同服登录用户）

| 查看者 | 读单条 | 列表 | 改 | 删 | 评论 | 点赞 | 跨服读 |
|---|---|---|---|---|---|---|---|
| 作者本人 | 200 | 含 | 200 | 204 | 201 | 200 | — |
| 所有者 | 200 | 含 | 200 | 204 | 201 | 200 | — |
| 好友 | 200 | 含 | 404 | 404 | 201 | 200 | — |
| 同服陌生人 | 200 | 含 | 404 | 404 | 201 | 200 | — |
| 已配对服务器用户 | — | — | — | — | — | — | 404（server 不跨服） |
| 未配对服务器用户 | — | — | — | — | — | — | 401 |
| 已切断服务器用户 | — | — | — | — | — | — | 401 |
| 匿名/无效 token | 401 | 401 | 401 | 401 | 401 | 401 | 401 |

### visibility = `paired_servers`

| 查看者 | 读单条 | 列表 | 改 | 删 | 评论 | 点赞 | 跨服读 |
|---|---|---|---|---|---|---|---|
| 作者本人 | 200 | 含 | 200 | 204 | 201 | 200 | — |
| 所有者 | 200 | 含 | 200 | 204 | 201 | 200 | — |
| 好友 | 200 | 含 | 404 | 404 | 201 | 200 | — |
| 同服陌生人 | 200 | 含 | 404 | 404 | 201 | 200 | — |
| 已配对服务器用户 | — | — | — | — | — | — | 200（只读，注 2） |
| 未配对服务器用户 | — | — | — | — | — | — | 401 |
| 已切断服务器用户 | — | — | — | — | — | — | 401 |
| 匿名/无效 token | 401 | 401 | 401 | 401 | 401 | 401 | 401 |

注：

1. `friends` 级跨服可见需要跨服好友判定，归 A2.3；届时若 `is_friend` 判定为跨服好友，
   该格升为 200。
2. 跨服 API **只读**（feed / 单条），不提供评论、点赞、改、删；跨服用户对
   `paired_servers` 动态只能读到动态本身与其可见交互（`can_see_interaction` 同式计算，
   `server/paired_servers` 档全量可见）。
3. 「改/删」列对非作者/所有者一律 404（含可见者）：可见但无权与不可见统一不可区分。
   评论删除额外允许**动态作者/所有者**删他人评论（仍为 404 语义，不泄露）。
4. 所有 401（含跨服 HMAC/配对失败）发生在可见性判定之前，不进入 404 分支。

## 6. 错误模型与幂等

- 错误一律 RFC 9457 `application/problem+json`，复用既有 `components.responses`
  （`BadRequest`/`Unauthorized`/`Forbidden`/`NotFound`/`Conflict`/
  `UnprocessableEntity`/`TooManyRequests`/`InternalError`），不新增响应组件。
- 越权与不存在：`404` `moment.not_found`，响应体逐字节相同（操作 description 已写明）。
- `POST /moments`、`POST …/comments` 遵循全局 `Idempotency-Key` 约定（同 key 同体返回
  同一结果；同 key 异体 409）。点赞 `PUT/DELETE …/likes/me` 天然幂等。
- `PATCH /moments/{id}` 必带当前 `revision`，冲突 409（复用既有 Conflict）。
- 匿名或 Matrix token 无效：401（`auth.invalid_token`）；传输层限速 429。

## 7. 配置变量（计划，PR2 加入 `.env.example`）

本 PR **不**修改 `gateway/.env.example`（避免与未合的 B 线 #117 冲突），以下变量在
PR2 实现时登记：

| 变量 | 说明 |
|---|---|
| `GATEWAY_MOMENTS_DB_PATH` | 动态 SQLite 路径（compose 卷 `gateway-data` 内，WAL 模式）。 |
| `GATEWAY_WHOAMI_CACHE_TTL_SECONDS` | Matrix token → user_id 缓存 TTL，上限 60，默认 60。 |
| `GATEWAY_MOMENTS_SOFT_DELETE_RETENTION_DAYS` | 软删行清理任务保留天数（默认 30）。 |
| `GATEWAY_MOMENTS_AUDIT_RETENTION_DAYS` | `moments_events` 审计保留天数。 |

跨服（PR3）复用 fedgate 的配对 DB 与 per-peer 密钥配置，不新增独立密钥文件；
网关间地址等沿用 A2.1 配置。

## 8. 部署（8845 小主机，Ubuntu Server 24.04）

部署目标为 8845 小主机（Ubuntu Server 24.04，**尚未装好**）。本 PR 与后续 PR 的交付
边界：只改仓库内容，**不连接、不部署任何机器**，不写任何真实密钥。

- compose 为网关新增持久化卷 `gateway-data`（挂载 `GATEWAY_MOMENTS_DB_PATH` 所在目录，
  SQLite + WAL，随容器重启保留）。
- 备份/恢复：计划把网关 DB 纳入既有 `backup-synapse.sh` / `restore-synapse.sh`
  （冻结 WAL 后整库拷贝），部署手册按 A1.1 流程在 8845 上执行时补「网关数据」一节。
- 跨服入口只经 fedgate 暴露 `/_matrix/botville/*`；网关 HTTP 端口只在 compose 内网或
  `127.0.0.1` 监听。

## 9. 与 #117（B2.2，未合）的边界

#117 修改：`gateway.v1.yaml` 中 `OutgoingMessageRequest` / `PolicyEvaluateRequest` 的
`peer_id` 描述（约 2239、2397 行附近）、`gateway/docs/contract.md`（约 795–1004 行）、
`gateway/.env.example` 末尾、根 `README.md`、`src/*.py`
（config/errors/http_server/limits/policy/service/store）。

本 PR 与之**零交集**：

- 不改 `.env.example`、`README.md`（根与 gateway）、`contract.md`、任何 `src/*.py`。
- `gateway.v1.yaml` 只在四处**插入**，未修改任何已有行：`tags` 末尾（Audit 之后）加
  `Moments`；`paths` 末尾（`/audit/{audit_id}` 之后、`components:` 之前）加 6 条新路径；
  `components.securitySchemes` 加 `matrixUserToken`；`components.schemas` 文件末尾追加
  动态 schema。`info.version` 不改。
- 新增文件均为独立路径（`openapi/examples/moment*.json` 等、`migrations/moments/`、
  `tests/test_moments_contract.py`、`docs/moments.md`、`scripts/validate_openapi.py` 的
  EXAMPLE_TARGETS 追加——该文件不在 #117 改动集内）。
- 合并顺序无论先后，两边均无文本冲突；`info.version` 升 `0.2.0` 由总管在两 PR 均合入
  后统一协调。

## 10. PR 拆分

| PR | 内容 |
|---|---|
| PR1（本 PR） | 契约：OpenAPI 路径 / securitySchemes / schema、示例与校验登记、DDL、本文、契约单测（`gateway/tests/test_moments_contract.py`）。 |
| PR2 | 存储（迁移执行器、SQLite 访问层）、`can_view` / `can_see_interaction` 与 `RelationProvider` 临时实现、同服 API 实现、权限矩阵与性质单测、CI `moments-e2e` job、`.env.example` 登记配置变量。 |
| PR3 | 跨服只读 API（`/_matrix/botville/moments/v1/*` 正式入契约）、fedgate HMAC 验签、双 HS 联邦 e2e（配对可读、切断被拒）。 |

每个 PR 单独送审，不自行合并。
