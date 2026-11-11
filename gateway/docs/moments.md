# 动态（Moments）设计 — A2.2 / issue #7

状态：**PR1 契约已合入；PR2 同服实现已合入 main（#184，`e2dcf50`）**——存储与迁移、
`can_view` / `can_see_interaction`、11 个同服操作、评论/点赞、软删清理任务均已上线代码。
跨服只读（PR3）尚未开始。好友图在 A2.3（#8）落地前**默认为空**，影响见 §7.1。
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
- **身份不自报**：App **只**发 `Authorization: MatrixOpenID <token>`（`matrixOpenIdToken`，
  网关经 homeserver `openid/userinfo` 取 `sub` 换出 `user_id`），无 feature flag、无回退，
  **禁止**拿用户 Matrix access token 当 `Bearer` 兜底。网关在 `/moments*` 上**继续接受**
  `Authorization: Bearer <Matrix access token>`（`matrixUserToken`→whoami），那是留给
  **App 以外其他调用方**的阶段 1 共存路径（未标 `deprecated`，仍有效）；退场节奏见
  `contract.md` §1.2.3。共享 gateway token 不能用于以用户身份读写动态。完整身份规则见
  §3 与 `contract.md` §1.2.3。
- 好友图归 A2.3（#8）。本项只定义 `RelationProvider.is_friend(viewer, author)` 接口，
  PR2 已提供可测试的临时实现 `StaticRelationProvider`（配置文件
  `GATEWAY_MOMENTS_FRIENDS_FILE` 或测试注入，见 §7.1），A2.3 落地后替换，本模块代码不变。

原方案评论见 issue #7（方案 §0–§8）；本文在其基础上补充评论/点赞的可见性
（`can_see_interaction`）与契约细节。

## 2. 数据模型

SQLite，DDL：`gateway/migrations/moments/0001_moments.sql`（表名全部 `moments_` 前缀；
`IF NOT EXISTS` 幂等，可在同一库执行两遍）。迁移编号空间 `moments/NNNN` 独立于其他
网关模块（B 线 #117 无持久化迁移）；网关启动时（`GATEWAY_MOMENTS_DB_PATH` 非空）按
目录内文件名顺序执行，已应用编号记在 `moments_schema_migrations` 表（PR2 已实现）。

| 表 | 说明 |
|---|---|
| `moments_posts` | 动态正文。`id`（ULID）、`author_id`/`author_kind`（`ai`=persona id / `human`=Matrix user id）、`owner_id`（恒为 Matrix user id）、`body`、`media_json`、`visibility`、`allow_list_json`（JSON 数组或 NULL，只收窄）、`revision`（乐观并发）、`created_at`/`updated_at`、`deleted_at`（软删，非 NULL 即对外不存在）。索引：`(author_id, created_at)`、`(created_at)`。 |
| `moments_comments` | 评论。`id`（ULID）、`moment_id`、`author_id`/`author_kind`/`owner_id`、`body`（1..500）、`reply_to_comment_id`（同动态内引用）、`created_at`/`updated_at`、`deleted_at`（软删；删除后不再出现在列表与可见计数）。索引：`(moment_id, created_at)`。 |
| `moments_likes` | 点赞。主键 `(moment_id, actor_id)`——每人对每条动态至多一个赞，重复点赞幂等；`actor_kind`、`owner_id`（恒为 Matrix user id，AI 点赞者的主人；human 时等于 `actor_id`）、`created_at`。 |
| `moments_events` | 审计（append-only）：`at`、`moment_id`（可空）、`actor`、`action`（create/update_visibility/delete/comment/delete_comment/like/unlike/…）、`detail_json`。记录发布、改可见范围、删除，以及节流后的越权尝试。索引：`(at)`。 |

与 schema 的对应：`Moment.like_count` / `comment_count` / `liked_by_me` **不落库**，
相对查看者计算（见 §4）；`Moment.author` 由 `author_id`/`author_kind` 加展示名快照组成。

可见范围枚举 `Visibility: self | friends | server | paired_servers`，与
`RelationDefaults.moments_visibility` 的映射：`nobody → self`、`friends → friends`、
`paired_servers → paired_servers`；`server` 为 A2.2 新增档位（同服登录用户可见），
只能显式指定。`allow_list`（≤200 个 Matrix user id）在可见范围之上**只收窄、不放宽**。PATCH 语义：字段**省略**=保持；`[]`=名单为空（仅作者/所有者可见）；`null`=清除收窄（回到仅按 `visibility`）。

响应实体 `MomentAuthor`（动态作者、评论作者、点赞 actor）在 `kind=ai` 时**必须**带
`owner_id`（主人 Matrix user id），供 `friend_subject` / `can_see_interaction` 使用；
`kind=human` 时 `owner_id` 等于 `id`（可回显）。落库：`moments_posts.owner_id`、
`moments_comments.owner_id`、`moments_likes.owner_id` 同源，不依赖读路径再查 persona。

## 3. 身份

- **同服用户 / App（两种凭据，按调用方选用）**：`/moments*` 的 OpenAPI `security`
  同时列出两个方案，校验结果语义相同——换出的都是**本服 Matrix user id**：
  - **App 只走 `Authorization: MatrixOpenID <token>`**（契约方案 `matrixOpenIdToken`，
    #124 阶段 1）：App 经标准 C-S API
    `POST /_matrix/client/v3/user/{userId}/openid/request_token` 取得**短时** OpenID token
    （长期 access token 留在 App，**不发给网关、不作 `Bearer` 兜底**）；网关经 compose 内网
    **直连**本服 homeserver `GET /_matrix/federation/v1/openid/userinfo?access_token=…`
    （**不带 X-Matrix 签名**，不经 fedgate、不走公网；直连地址取自
    `GATEWAY_HOMESERVER_FEDERATION_URL`，空则回落 `GATEWAY_HOMESERVER_URL`），取返回的 `sub`
    作为 `user_id`。`sub` 的域必须等于本服 `GATEWAY_MATRIX_SERVER_NAME`，否则 401。缓存按
    token 哈希（不缓存明文、不写日志），TTL ≤ 60 秒且不超过剩余有效期；过期以 `expires_in`
    为准，网关不自行延长。不另做网关侧 nonce 防重放（持有者令牌的协议固有限制；跨服
    moments 的 HMAC+ts+nonce 与本项无关）。
  - **`Authorization: Bearer <Matrix access token>`**（契约方案 `matrixUserToken`，其他
    调用方的阶段 1 共存路径）：网关调用本服 homeserver
    `GET /_matrix/client/v3/account/whoami` 换出 `user_id`，按 token 哈希缓存 ≤ 60 秒。
    本路径**留给 App 以外的其他调用方**，**App 不使用**；**未标 `deprecated`，仍完全
    有效**。退场节奏（详见 `contract.md` §1.2.3）：T1 本 PR（#7 moments CRUD）上线后观察
    App 流量 → T2 清点 Bearer 调用方（届时「按 scheme 统计」PR）→ T3 已知调用方迁完且 Bearer
    占比 <1%（计划值）后进**阶段 2**（标 `deprecated`，须先公告、先报总管）→ T4 至少 90 天
    公告并加 `Deprecation`/`Sunset` 头后进**阶段 3**（从 `/moments*` security 移除
    `matrixUserToken`，破坏性变更须另行批准）。阶段 2/3 均须先公告、先报总管。
  - **不信任调用方自报身份**；两种凭据由请求头 scheme（`MatrixOpenID` vs `Bearer`）显式
    区分，**禁止「先试 OpenID 再试 whoami」**。无效 / 过期 token → 401（`auth.missing` /
    `auth.invalid_token`，不新增错误码）。完整契约见 `contract.md` §1.2.3。
- **AI persona（作者 / 行为者）**：沿用 appservice 身份。`POST /moments` 带
  `author_persona_id` 时，网关校验该 persona 归调用者所有（否则 403
  `persona.not_owned`）；persona 的常规代发经 appservice 的**内部调用**完成（不走
  Matrix 用户令牌，不走 `matrixUserToken`，也不走 `matrixOpenIdToken`）。
- **查看者恒为 Matrix user id**：公开契约的 `viewer` 只来自
  `matrixOpenIdToken` 的 userinfo `sub` / `matrixUserToken`→whoami，或跨服 HMAC 请求里
  由 origin 担保且属于 origin 域的 `viewer`。**persona 不作为公开契约的查看者**（权限
  矩阵 8 类查看者均为 Matrix 用户）。若实现层需要以 AI 身份代读，必须先 `friend_subject`
  解到 `owner_id` 再进入 `can_view` / `can_see_interaction`；公开 API 从不接受 persona 令牌。
- **AI 评论 / 点赞的创建路径**：公开 `POST /moments/{id}/comments` 与
  `PUT/DELETE /moments/{id}/likes/me` 的行为者**恒为**调用者本人（human，即
  `matrixOpenIdToken` 的 userinfo `sub` 或 `matrixUserToken`→whoami 的 `user_id`）；
  `CreateCommentRequest` 不接受 `author_persona_id`。
  AI 评论/点赞与 AI 发动态一样，只经 appservice **内部调用**写入（校验
  persona 归属后落库 `author_kind`/`actor_kind=ai` 与对应 `owner_id`），不暴露为
  任一用户凭据（`matrixUserToken` / `matrixOpenIdToken`）的公开路径。
- **跨服**（PR3 入契约，本文先描述）：对端网关经 fedgate 调
  `/_matrix/botville/moments/v1/feed`、`/_matrix/botville/moments/v1/moments/{id}`，
  用配对时协商的 per-peer 密钥做 HMAC 签名（复用 #116 notify-severed 格式：
  origin + ts + nonce + body 哈希，±300s，nonce 防重放）。请求里的 `viewer` 是对端
  担保的用户 ID，必须属于 origin 域（`@x:origin`）；origin 必须在**当前 active** 的
  配对表（A2.1 pairing DB，与 fedgate 同源）。未配对 / 已切断 / 签名失败 → 401。
- **共享 gateway token**（`bearerAuth`）只保留给运维与内部服务，**不得**用于以用户
  身份读写动态。

发布缺省可见范围：
- 带 `author_persona_id`（AI 作者）：取该 persona 的 `RelationDefaults.moments_visibility`
  （`nobody` → `self`）。
- **不带** `author_persona_id`（真人作者）：缺省 `friends`（与契约里
  `RelationDefaults.moments_visibility` 的默认值一致）；也可显式传入任意 `Visibility`。

## 4. 可见性定义（单一函数，供读单条 / 列表 / 评论 / 点赞 / 跨服共用）

```
# 好友判定永远是 Matrix user ↔ Matrix user。AI persona 没有独立好友图。
# friend_subject 用于 author / actor（以及任何需要把 AI 解到主人的实体）；
# 公开契约的 viewer 本身已是 Matrix user id，不再对 viewer 做 persona 映射。
friend_subject(entity):
    # entity 有 kind 与 id；AI 必有 owner_id（Matrix user id，响应 MomentAuthor 带回）
    if entity.kind == 'ai': return entity.owner_id
    return entity.id   # kind == 'human'：id 本身就是 Matrix user id
                       # （响应里 human 的 owner_id 等于 id，可有可无）

can_view(viewer, moment):
    # viewer: Matrix user id（whoami / 跨服担保）；绝不是 persona_id
    if moment.deleted_at is set:        return False   # 软删 = 不存在
    if viewer is None:                  return False   # 匿名/无效 token（层面前置：401）
    if viewer == moment.author_id
       or viewer == moment.owner_id:    return True    # 作者/所有者短路面，allow_list 不适用
    if moment.visibility == 'self':     return False
    if moment.allow_list is not None
       and viewer not in moment.allow_list: return False   # 只收窄，不放宽；null/省略 = 不收窄
    if moment.visibility == 'friends':
        return RelationProvider.is_friend(
            viewer, friend_subject(moment.author))       # AI → owner_id
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
    # viewer / actor.owner_id 均为 Matrix user id；actor 来自响应 MomentAuthor
    viewer == moment.author_id or viewer == moment.owner_id   # 动态作者/所有者
    or viewer == actor.id or viewer == friend_subject(actor) # 查看者就是该评论/点赞者（或其主人）
    or moment.visibility ∈ {server, paired_servers}           # 动态本身全服（或跨服）可见
    or RelationProvider.is_friend(viewer, friend_subject(actor))  # AI 行为者 → 其 owner_id
```

即 `friends` / `self` 可见范围下，**非共同好友互相看不到对方的评论与点赞**（类朋友圈：
我在好友 A 的动态里只能看到我和 A 的共同好友的评论）。**AI 动态**（`author.kind=ai`）的
`friends` 判定绑到**主人** `owner_id`：查看者与主人是好友即可看到该 AI 动态；
**AI 评论/点赞**同理，共同好友按「查看者 ↔ 该 AI 的主人」判定。`server` /
`paired_servers` 动态的评论与点赞跟随动态本身的可见面。

`RelationProvider.is_friend(a, b)` 的两个参数均为 Matrix user id；PR2 临时实现按
配置/测试注入的 user↔user 图回答，A2.3 落地后换成真实好友图，本模块调用点不变。

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

0. **`friends` 矩阵单元格（含「好友 → 读单条/列表/评论/点赞 = 200」）的判定对象**：
   矩阵 8 类查看者均为 Matrix user id（persona 不作为公开契约查看者）。动态
   `author.kind=ai` 时，「好友」= 与 `owner_id`（主人）是好友的查看者；
   `author.kind=human` 时，「好友」= 与 `author.id` 是好友的查看者。评论/点赞行中
   「共同好友」同理：AI 行为者绑到其 `owner_id`（响应 `MomentAuthor.owner_id` /
   落库 `moments_comments.owner_id` / `moments_likes.owner_id`）。示例
   `moment.json` 即 AI 作者路径。
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
- `PATCH /moments/{id}` 必带当前 `revision`，冲突 `409` `moment.revision_conflict`（复用既有
  Conflict 响应组件；比对与写入原子完成；响应不附带当前动态，客户端重新 `GET`）。
  错误码已登记在 `contract.md` §1.3。
- 匿名或 Matrix token 无效：401（`auth.invalid_token`）；传输层限速 429。
- `reply_to_comment_id`：目标评论不存在、已软删、或不在同一动态、或对当前查看者不可见（`can_view ∧ can_see_interaction` 为假）时，**统一**返回 `422` `request.invalid`（`errors[].field = reply_to_comment_id`），不区分原因，避免靠 404/422 差异探评论 ULID。
  **格式不像 ULID 的字符串**同样按「目标不存在」返回 422（类型对、值找不到属语义错误）；
  只有**类型错误**（数字、布尔、对象、数组、空串）返回 `400` `request.invalid`。
  显式 `null` 视同省略（顶层评论，`201`）；OpenAPI 请求 schema 仍声明为 `string`，
  客户端应省略该字段而不是发 `null`。
- **幂等作用域与重放**（实现口径，登记于 `contract.md` §1.5「动态端点」）：缓存键为
  `(操作, 调用者, 目标资源, key)`，评论的目标资源是 `moment_id`；persona 归属与目标动态
  可见性**先于**幂等缓存判定。因此动态被删除、或与作者解除好友后，用原
  `Idempotency-Key` 重放评论返回 `404` `moment.not_found`（不是原回执）；persona 归属
  被收回后重放 `POST /moments` 返回 `403` `persona.not_owned`。
- 幂等缓存为进程内存，TTL 沿用 `IDEMPOTENCY_TTL_SECONDS`（默认 86400），与全局约定一致：
  `0` = 不过期（只受 `MAX_IDEMPOTENCY_ENTRIES` 容量淘汰约束）；网关重启后清空。
- 非法请求目标（authority-form、其他 scheme、IPv6 括号未闭合、`*` 搭配非 `OPTIONS`）
  在路由前即返回 `400` `request.invalid`，对所有端点生效，见 `contract.md` §1.1。
- 结构/类型错误（未知字段、类型不符、非法 JSON、含孤立代理项的 JSON 字符串、畸形
  `cursor`）一律 `400` `request.invalid`，不会 500。

## 7. 配置变量

全部变量已登记在 `gateway/.env.example`（「动态 Moments」一节）与 `gateway/README.md`
配置表，由 `gateway/src/botville_gateway/config.py` 读取并在启动时校验（非法值拒绝启动）。
`GATEWAY_OPENID_CACHE_TTL_SECONDS`（#124/#170）见 `contract.md` §1.2.3。

| 变量 | 默认 | 说明 |
|---|---|---|
| `GATEWAY_MOMENTS_DB_PATH` | *(空)* | 动态 SQLite 路径（WAL、`foreign_keys=ON`）；启动时自动执行 `gateway/migrations/moments/` 迁移（幂等）。**留空 = 关闭动态模块**：`/v1/moments*` 鉴权后仍返回模块合入前的 404/405。 |
| `GATEWAY_MOMENTS_FRIENDS_FILE` | *(空)* | 临时好友图 JSON 文件，见 §7.1。留空 = 空好友图。 |
| `GATEWAY_MOMENTS_SOFT_DELETE_RETENTION_DAYS` | `30` | 软删的动态（连同其评论、点赞）与软删评论，超过该天数后由清理任务硬删。 |
| `GATEWAY_MOMENTS_AUDIT_RETENTION_DAYS` | `90` | `moments_events` 审计事件**默认保留 90 天**，超期由清理任务删除。 |
| `GATEWAY_MOMENTS_PURGE_INTERVAL_SECONDS` | `86400` | 定时清理间隔（秒），见下。`0` = 关闭；非 0 时**至少 60**，否则拒绝启动。 |

**定时清理**：`GATEWAY_MOMENTS_PURGE_INTERVAL_SECONDS` 非 0（默认 86400，即每天）且动态模块
开启时，网关启动先同步跑一次清理，之后由后台守护线程按间隔执行；按上面两个保留天数
硬删软删行与过期审计。单次失败只记日志（`botville_gateway.moments.service` logger），
不影响请求处理，下个周期重试。网关关闭（`server_close`）时停止该线程。设为 `0` 则不跑
清理，两个保留天数**不生效**，软删行与审计会一直累积，需另行运维。

### 7.1 好友图为空时的影响（A2.3 / #8 落地前）

好友关系归 A2.3（#8）。在它落地前，网关只用 `StaticRelationProvider` 这个临时实现：

- **默认（`GATEWAY_MOMENTS_FRIENDS_FILE` 留空）好友图为空**，`is_friend` 恒为假。于是：
  - `visibility = friends` 的动态**等同「仅自己」**：只有作者本人（AI 动态则为其主人
    `owner_id`）能看到，其他人一律 `404 moment.not_found`（fail-closed，不会误放行）；
  - 评论/点赞的可见性里「与行为者是好友」一项也恒假：`friends` / `self` 动态上的交互只
    对动态作者/所有者及行为者本人（或其主人）可见；
  - `server` / `paired_servers` 档不依赖好友图，同服登录用户照常可见（同服判定依赖
    `GATEWAY_MATRIX_SERVER_NAME`，未配置时 fail-closed；跨服读取待 PR3）。
- **用文件注入好友关系**：把 `GATEWAY_MOMENTS_FRIENDS_FILE` 指向一个 JSON 文件：

  ```json
  {"friends": [["@alice:example.invalid", "@bob:example.invalid"]]}
  ```

  - 每项是两个**非空**字符串组成的数组，表示一条**无向**好友边（写一次即双向）；
  - **不校验 user id 格式**：只要求非空字符串，不检查 `@localpart:server` 形态、大小写或
    所属服务器，按字符串原样精确比较。写错（如漏 `@`、大小写不一致）不会报错，只是该边
    永远匹配不上，等同没写；
  - 两端都是 Matrix user（AI persona 没有独立好友图，AI 按其 `owner_id` 判定）；
  - 文件只在**启动时读取一次**，改了要重启网关才生效；
  - 文件不存在、不是合法 JSON、缺 `friends` 数组或条目畸形 → **拒绝启动**（不会静默
    退化成空图）。
- 该文件只是过渡与测试手段；A2.3 落地后替换 `RelationProvider` 实现，配置项随之下线，
  本模块其余代码不变。

另：persona 目录在 PR2 也是 fail-closed 的空实现，`POST /moments` 带 `author_persona_id`
目前一律 `403 persona.not_owned`；AI 发动态 / 评论 / 点赞走 appservice 内部调用（§3）。

跨服（PR3）复用 fedgate 的配对 DB 与 per-peer 密钥配置，不新增独立密钥文件；
网关间地址等沿用 A2.1 配置。

## 8. 部署（8845 小主机，Ubuntu Server 24.04）

部署目标为 8845 小主机（Ubuntu Server 24.04，**尚未装好**）。本 PR 与后续 PR 的交付
边界：只改仓库内容，**不连接、不部署任何机器**，不写任何真实密钥。

- **未做（计划）**：compose 为网关新增持久化卷 `gateway-data`（挂载
  `GATEWAY_MOMENTS_DB_PATH` 所在目录，SQLite + WAL，随容器重启保留）。当前 compose
  尚无该卷，与 §10「未做」一致；在它落地前若在容器内开启动态模块，数据随容器删除而丢失。
- 备份/恢复（**未做**）：计划把网关 DB 纳入既有 `backup-synapse.sh` / `restore-synapse.sh`
  （冻结 WAL 后整库拷贝），部署手册按 A1.1 流程在 8845 上执行时补「网关数据」一节。
- 跨服入口只经 fedgate 暴露 `/_matrix/botville/*`；网关 HTTP 端口只在 compose 内网或
  `127.0.0.1` 监听。

## 9. 与 #117（B2.2，已合入 main）的边界

#117 已合入 main，本分支已 `update branch` 吸收。#117 改动的文件（`src/*.py`、
`.env.example`、`gateway/README.md`、`gateway/docs/contract.md`、`test_b21_*` /
`test_b22_*`，以及 `gateway.v1.yaml` 中 `peer_id` 描述）**本 PR 的 Moments 提交不改动**；
Moments 仍只在 `tags` / `paths` 末尾 / `securitySchemes` / `schemas` 末尾**追加**，
与 #117 的 yaml hunk 区间不重叠。

- 本 PR 不修改 B2.2 实现代码与其单测。`moment.not_found` / `moment.revision_conflict`
  已在 #7 文档 PR 中登记到 `contract.md` §1.3。
- 配置变量已随 PR2（#184）写入 `.env.example` 与 `gateway/README.md`，见 §7。
- `info.version` 升 `0.2.0` 由总管在 Moments 合入后统一协调。

## 10. PR 拆分

| PR | 内容 |
|---|---|
| PR1（已合入） | 契约：OpenAPI 路径 / securitySchemes / schema、示例与校验登记、DDL、本文、契约单测（`gateway/tests/test_moments_contract.py`）。 |
| PR2（已合入，#184） | 存储（迁移执行器、SQLite 访问层）、`can_view` / `can_see_interaction` 与 `RelationProvider` 临时实现、同服 API 实现、权限矩阵与性质单测、`.env.example` 登记配置变量、软删/审计定时清理。**未做、留作后续**：CI `moments-e2e` job、compose `gateway-data` 卷与备份脚本（§8）、镜像内迁移目录定位。 |
| PR3 | 跨服只读 API（`/_matrix/botville/moments/v1/*` 正式入契约）、fedgate HMAC 验签、双 HS 联邦 e2e（配对可读、切断被拒）。 |

每个 PR 单独送审，不自行合并。
