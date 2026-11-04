<!-- SPDX-License-Identifier: Apache-2.0 -->

# AI 网关契约 v1（草案 0.1.0）

> 任务：B0.1 · 里程碑 M0 · 评审：C 线（App）/ D 线（通知与开放 API）
> 机器可读规范：[`gateway/openapi/gateway.v1.yaml`](../openapi/gateway.v1.yaml)
> 校验命令：`python3 gateway/scripts/validate_openapi.py`

本文是网关契约的**人类可读说明**。凡是与 OpenAPI 文件冲突的地方，以 `gateway.v1.yaml`
为准（它是唯一事实源），并应视为本文的 bug 提 issue 修正。

---

## 0. 范围与职责边界

网关夹在 Matrix homeserver 与 AI 运行时之间，一句话概括产品原则：

> **AI 决定想不想说，网关决定能不能说。**

| 归本契约管 | 不归本契约管 |
|---|---|
| 角色配置与状态、会话在网关侧的登记 | Matrix Client-Server / Application Service 协议本身 |
| 发言裁决（三层限额、静默、防刷屏、互聊熔断、关系、E2EE 策略） | 消息在房间内的加密算法与密钥分发 |
| token 预算与用量账单 | 具体模型提供方的 API 形态 |
| 功能清单（capability manifest）与通用卡片 | App 的渲染实现、通知渠道插件内部协议 |
| 健康检查 | 令牌签发/轮换的服务端实现（v1 只声明 bearer 占位） |

传输层仍是标准 Matrix：网关以 appservice 身份代 AI 发消息。本契约里的 `/conversations`
只是把 Matrix room 在网关侧登记（限额、记忆范围、关系都挂在会话上），不替代 Matrix 房间。

### 会话加密与 AI 参与

`Conversation.encryption` 表示 Matrix 房间的加密状态：`none` 为未加密，`e2ee` 为端到端加密。
botville 的 AI 接入目前不支持加密会话。含 AI 的会话使用 `none`，App 不提供开启加密；
`encryption=e2ee` 时，契约要求 `ai_participation=disabled`，网关不得代表 AI 发言。
`disabled` 表示禁止网关 AI 参与，**不表示已撤销 Matrix 成员资格或密钥**。

**硬约束，不是策略拦截。** 向 `encryption=e2ee` 或 `ai_participation=disabled` 的会话请求
AI 发言（`POST /conversations/{id}/messages`）或预检（`POST /policy/evaluate`）时，网关返回
`403` `conversation.e2ee`（调用方无权在此会话代 AI 发言）。该码**不可重试**，也**不是**
§1.4 的 `200` + `outcome: "blocked"`：这是所有权/会话约束，不是限额或静默等产品策略结论。

**登记会话。** `POST /conversations`：`encryption=e2ee` 且 `ai_participation=enabled` 返回
`422` `request.invalid`（语义组合不合法）；省略 `ai_participation` 时按 `encryption` 推导
（`e2ee` → `disabled`，`none` → `enabled`）。

客户端侧限制与已知局限见[苹果端加密策略](../../app-apple/docs/encryption-policy.md)。
当前网关参考实现尚未校验房间加密状态；后续接入须按真实房间状态执行上述限制。

> **D 线注意**：通知数据模型（D1.1）与授权回调签名（D2.1）会扩展 `/cards` 与
> `/cards/{card_id}/actions/{action_id}`。v1 已预留 `source.module`、`source.capability_id`、
> `context.metadata` 和 `audit_id`，扩展时按「只增不删」加字段即可，不需要升主版本。

---

## 1. 通用约定

### 1.1 基本形态

- 基础路径 `/v1`，资源名为复数，JSON 请求与响应（UTF-8）。
- 时间一律 **RFC 3339 / ISO 8601，UTC（`Z` 结尾）**（如 `2026-11-03T12:00:30Z`）。
  带偏移的写法（`+08:00`）一律不接受，示例与校验脚本都强制 `Z`，避免同一天在不同文件里
  出现 8 小时偏差。「面向人的日历边界」（如每日主动发言重置）额外带 `timezone` 字段，
  用 IANA 名称表示含义，时间值本身仍是 UTC。
- ID 是不透明字符串，客户端不得解析其内部结构。
- 列表分页统一用 `cursor` + `limit`，响应带 `next_cursor` / `has_more`。
  `next_cursor` 为 `null` 表示到底；客户端不得构造游标。
- 未在 schema 中声明的字段，客户端**必须忽略**（前向兼容）；服务端对请求体
  用 `additionalProperties: false` 严格拒绝未知字段，避免拼写错误被静默吞掉。

#### 1.1.1 请求体上限（B2.1 / #92）

任何 `POST` / `PUT` 请求体不得超过 **1 MiB（1048576 字节）**。

- 超限返回 **`400 request.invalid`**，`errors[]` 含一项
  `{ "field": "body", "code": "too_large", "message": … }`。
- **不新增 413 状态码**：契约的状态码集合保持不变（见 §1.3），客户端只需按
  `code` 分支即可。
- 服务端在读取 `Content-Length` 后即可拒绝，不必把超大请求体读入内存；
  `Transfer-Encoding: chunked` 不被支持（同样 400 `request.invalid`）。
- 单条正文另有更小的业务上限 `content.text` ≤ 8000 字符（`OutgoingContent.maxLength`）。

### 1.2 鉴权（占位）

```http
Authorization: Bearer <gateway-token>
```

- 安全方案 `bearerAuth`（`type: http, scheme: bearer`）**仅作占位**，具体令牌格式、
  签发、轮换与 scope 管理不在 v1 契约内。
- 所有示例只出现 `<change-me>`。**任何真实令牌、密钥、密码不得进入仓库、PR、
  issue、日志或截图**（见 `CONTRIBUTING.md`）。
- 角色模型凭据只接受**引用**（`credential_ref: "env:LLM_API_KEY"`），网关拒绝明文密钥，
  也不会在响应里回显。
- `/health` 不需要鉴权。

#### 1.2.1 模块网页会话：一次性 code → HttpOnly cookie（C3）

**不能用 `Authorization` 头**：WKWebView 只能给首个 `URLRequest` 加头，页面内的后续
导航、子资源与 XHR 都带不上。因此模块网页的身份传递改为一次性 code 换 cookie：

```
App                          网关                        模块网页（WKWebView）
 │                             │                              │
 ├─ POST /capabilities/{id}/session-tokens ─►│                │
 │◄── { code, url, expires_at, allowed_origins }             │
 │                             │                              │
 ├─ 在内置浏览器打开 url ───────────────────────────────────►│
 │                             │◄─ POST .../session-cookies ──┤  (body: {code})
 │                             ├── 204 + Set-Cookie ─────────►│  HttpOnly; Secure; SameSite
 │                             │                              │
 │                             │◄── 后续同源请求自动带 cookie ─┤
 │                             │                              │
 │◄── JS bridge: botville.session.renew ─────────────────────┤  (会话将过期时)
 │  重新走第 1 步               │                              │
```

| 规则 | 值 |
|---|---|
| 一次性 `code` | 单次使用，TTL **≤ 60 秒**；只能出现在 `url` 查询串或兑换请求体 |
| 会话有效期 | `ttl_seconds` **≤ 3600 秒**，默认 300 |
| cookie | `HttpOnly` + `Secure` + `SameSite`；JSON 响应**不回传** cookie 值 |
| `url` | 必填，与能力 `entry.web_url` 同源；由网关附加 `code`，App 不得自行拼接 |
| `allowed_origins[]` | 内置浏览器放行的 origin 白名单；超出即改用外部浏览器 |
| 续期 | 页面发 JS bridge 消息 `botville.session.renew` → App 重新签发 |

`code` 与 cookie 值都不得写入日志、localStorage 或任何脚本可读的位置。


### 1.3 错误模型（RFC 9457）

所有 4xx / 5xx 响应体为 `application/problem+json`：

```json
{
  "type": "urn:botville:error:card.step_up_required",
  "title": "Step-up authentication required",
  "status": 403,
  "detail": "This action has risk=high and needs a fresh biometric assertion.",
  "instance": "/v1/cards/card_demo/actions/approve",
  "code": "card.step_up_required",
  "request_id": "req_demo_403",
  "errors": [{ "field": "assertion", "code": "missing", "message": "required for risk=high" }],
  "retry_after_seconds": null
}
```

`type` 固定为 `urn:botville:error:<code>`；`code` 是稳定的机器可读标识，客户端按 `code`
分支，**不要**匹配 `title` 或 `detail` 文案。每个响应带 `X-Request-Id`。

| HTTP | code（示例） | 含义 |
|---|---|---|
| 400 | `request.invalid` / `request.malformed_json` | 语法或参数错误 |
| 401 | `auth.missing` / `auth.invalid_token` | 缺少或无效令牌（含一次性 code 重复使用） |
| 403 | `auth.insufficient_scope` / `persona.not_owned` / `relation.chat_denied` / `conversation.e2ee` / `card.step_up_required` / `card.assertion_invalid` | 身份有效但无权限 / 断言无效 |
| 404 | `persona.not_found` / `conversation.not_found` / `capability.not_found` / `card.not_found` / `audit.not_found` | 不存在或对调用方不可见 |
| 409 | `persona.in_use` / `conversation.exists_conflict` / `persona.revision_conflict` / `card.revision_conflict` / `idempotency.conflict` / `card.already_decided` / `card.challenge_invalid` | 状态冲突 |
| 410 | `card.challenge_expired` | challenge 或一次性 code 已过期 |
| 422 | `limit.out_of_range` / `card.invalid_block` / `card.missing_fallback_text` / `card.source_scope_mismatch` / `card.missing_event_id` / `card.missing_expires_at` / `state.invalid_value` / `request.invalid`（createConversation 语义不合法 + peer_id 校验） | 语义不合法 |
| 429 | `rate_limited.transport` | 传输层限流（**不是**发言额度） |
| 500 | `internal.error` | 网关内部错误 |

卡片操作相关错误码（C4 / D3）：

| code | HTTP | 何时返回 |
|---|---|---|
| `card.step_up_required` | 403 | 有效 risk=high 但缺 `assertion` / `challenge` |
| `card.assertion_invalid` | 403 | 签名校验失败，或 assertion 绑定的字段与请求不一致 |
| `card.challenge_invalid` | 409 | challenge 不属于该卡片/操作，或已被使用 |
| `card.challenge_expired` | 410 | challenge 超过 `expires_at` |
| `card.revision_conflict` | 409 | `revision` 与当前值不一致（TOCTOU），响应附带最新卡片 |
| `card.already_decided` | 409 | 卡片已被其他意图处理（同 key 幂等重放不会走到这里） |
| `card.expired` | 409 | 卡片已过期 |

### 1.4 ⚠️ 策略结论不是 HTTP 错误

这是本契约最容易被误实现的一点，C 线请重点评审：

> **发言被限额拦截，返回 `200` + `outcome: "blocked"`，不是 `429`/`403`。**

理由：拦截是网关的**正常产品行为**，调用方（AI 运行时）需要结构化的原因、剩余额度、
重置时间来决定下一步（等待、改写、放弃），而不是把它当成异常重试。`401/403` 只表示
「你无权调用这个接口」，`429` 只表示「你请求太频繁」，两者与「这个 AI 此刻不该说话」
是不同的事。

```json
{ "outcome": "blocked", "retryable": false, "retry_after_seconds": null,
  "may_clear_at": "2026-11-03T14:00:00Z", "same_content_allowed": true, "queue_id": null,
  "message": null, "delivery": null,
  "decision": { "decision": "block", "allowed": false, "winner_layer": "platform",
                "reasons": [{ "code": "ai_loop.tripped", "layer": "platform",
                              "message": "AI-to-AI conversation hit the hourly circuit breaker." }],
                "checks": [ ... ] } }
```

`outcome` 取值：

| outcome | 含义 | 客户端行为 |
|---|---|---|
| `delivered` | 已投递到 Matrix | 用 `message` 与 `delivery` 更新本地 |
| `queued` | 已入队（如主动发言调度排到后面，带 `queue_id`） | 等待推送，不要重发 |
| `deferred` | 延后到允许时段（服务端排程，带 `queue_id`） | 不要自行重试，等服务端在允许时段投递 |
| `blocked` | 本次不允许 | 读 `decision.reasons`，**不重试同一内容**；`may_clear_at` 仅表示额度恢复时间 |

#### 1.4.1 投递三分表（D1）

| 类别 | outcome / HTTP | 重试 | usage_events | 说明 |
|---|---|---|---|---|
| **成功** | `delivered` / `queued` / `deferred`，2xx | 不重试 | — | `queued` / `deferred` 是服务端排程，客户端只等推送 |
| **策略拦截** | `blocked`，2xx | **不重试**（`retryable: false`） | `kind: blocked` | 同一内容不再重试；`may_clear_at` 表示额度何时恢复，用户可换内容后再试 |
| **失败** | 4xx / 5xx，`application/problem+json` | 按 `code` 决定 | — | 传输层或调用方错误，与策略拦截严格区分 |

关键规则：
- `retryable` / `retry_after_seconds` **只以 `MessageReceipt` 为准**；`PolicyDecision`
  内的同名字段仅在 `POST /policy/evaluate` 单独使用时有意义，两处语义相同但不互相引用。
- **200 响应不附带 `Retry-After` HTTP 头**；重试时机放 body 里。`Retry-After` 头只在
  4xx / 5xx 传输层错误（`TooManyRequests` 等）出现。
- `blocked` 始终 `retryable: false`；若额度会恢复，用 `may_clear_at` 表达（额度恢复后
  同一内容可重发，`same_content_allowed: true`）；若是关系策略或内容本身被拒，则
  `same_content_allowed: false`——两者都不要把 `retryable` 改成 `true`。
- `deferred` 始终 `retryable: false`（服务端排程，客户端不自行重试），并返回 `queue_id`。
- 幂等缓存对 `queued` / `deferred` **同样生效**：重用同一 `Idempotency-Key` 返回同一
  `queue_id` 与当前状态，不会新建排程；排程进入终态后，重放返回终态回执
  （`delivered` / `blocked`）。

### 1.5 幂等

写操作支持 `Idempotency-Key`（8–128 字符，建议随机 UUID）。统一规则如下：

- **同一次用户意图复用同一个 key**：用户点一次「批准」，以及这次点击的超时重试，
  都用同一个 key。同 key 重放返回**原回执**，不会返回 `409`。
- **新的用户意图用新的 key**：用户第二次点击（哪怕是同一张卡片的另一个操作）
  必须换新 key。同 key + 不同请求体返回 `409 idempotency.conflict`。
- 服务端建议保留 24 小时。缓存覆盖**所有结果**，不区分是否终态：
  - 请求已被接受但仍在排程（`queued` / `deferred`）：重放返回**同一 `queue_id`** 与
    当前状态，不会新建第二个排程。
  - 已到终态（`delivered` / `blocked` / `accepted` / `rejected` / `failed`）：重放返回
    终态回执，不会重新执行。
  - 因此客户端可以用同一个 key 安全重试，无需先判断上一次走到哪一步。
- 卡片操作（`POST /cards/{card_id}/actions/{action_id}`）的 `Idempotency-Key`
  **必填**，其余写接口强烈建议。`POST /messages`、创建类接口必须支持。
- 本规则取代此前「卡片操作必须用新 key」的说法——那条与重试复用相矛盾，已作废。
- **例外：`PUT /limits`**。整层替换天然幂等，服务端**不做**幂等缓存；`Idempotency-Key`
  对该端点**可选**，同一 key 不同请求体**不返回** `409 idempotency.conflict`（每次重放
  均执行整层替换）。
- **Matrix 事务 ID（#114 PR-a）**：网关把 `Idempotency-Key` 推导为
  `txnId = hex(HMAC-SHA256(部署密钥 GATEWAY_HMAC_SECRET, b"txn\x00" + UTF-8(key)))[:64]`
  ——取前 64 个十六进制字符。`b"txn\x00"` 是**域分隔前缀**，与 `text_sha256` 的
  `b"text\x00"` 隔开，两者不存在构造上的交叉（第三方按契约复算时必须带上该前缀，
  否则算出的值与网关不同）。txnId 出现在 homeserver / 反代日志的 URL 中，但没有
  部署密钥无法反推 key，也无法跨部署关联。
- **缓存淘汰后不重复计费**：幂等缓存因容量或 TTL 被淘汰后，同 key 重试会再次投递；
  homeserver 按 `(room, sender, txnId)` 去重并返回**同一 `event_id`**。网关另保留
  `txnId → event_id` 账本（容量与 TTL 均为幂等缓存的 2 倍；账本键是网关自己算出的
  txnId，**不带** `Idempotency-Key` 的请求不写账本、照常计费），命中同一 `event_id` 时
  **不再记 SENT 用量、不重复扣 token**，回执照常返回该 `event_id`。账本也被淘汰后的
  极端重试仍可能重复计费，属已知边界。

### 1.6 版本与乐观并发

- 角色与卡片带 `revision`（从 1 起递增）。写操作可选传 `revision`（或 `If-Match`），
  不一致返回 `409` 并附带当前值——避免两端覆盖。
- 接口版本规则见 [第 5 节](#5-版本规则)。

---

## 2. 接口总览

| 方法 | 路径 | 说明 | 主要使用方 |
|---|---|---|---|
| GET | `/health` | 健康检查（免鉴权） | 部署探针、App 状态页 |
| GET | `/capabilities` | 功能清单 | App（C2.5、C3.2） |
| GET | `/capabilities/{capability_id}` | 单个能力 | App |
| POST | `/capabilities/{capability_id}/session-tokens` | 签发模块网页一次性 code | App 内置浏览器 |
| POST | `/capabilities/{capability_id}/session-cookies` | 一次性 code 换 HttpOnly cookie | 模块网页 |
| GET | `/personas` | 角色列表 | 我的 AI（C2.4） |
| POST | `/personas` | 创建角色 | 简单接入、我的 AI |
| GET/PATCH/DELETE | `/personas/{persona_id}` | 角色读写 | 我的 AI |
| GET/PATCH | `/personas/{persona_id}/state` | 心情/需求/主动发言预算 | 我的 AI、群调度 |
| GET | `/conversations` | 会话列表 | App |
| POST | `/conversations` | 登记 Matrix room | 服务端（A 线） |
| GET | `/conversations/{conversation_id}` | 会话详情 | App |
| GET | `/conversations/{conversation_id}/messages` | 读消息（带 trust 标记） | App |
| POST | `/conversations/{conversation_id}/messages` | **请求以 AI 身份发言（核心）** | AI 运行时 |
| POST | `/policy/evaluate` | 发言前预检（不投递、不扣额度） | AI 运行时 |
| GET/PUT | `/limits` | 三层限额读取与设置 | 我的 AI、所有者设置 |
| GET | `/usage` | 用量汇总与预算 | 我的 AI、账单 |
| GET | `/usage/events` | 账单明细 | 审计、对账 |
| GET/POST | `/cards` | 卡片列表与创建 | App、通知/小镇模块 |
| GET/PATCH | `/cards/{card_id}` | 卡片读取与状态 | App |
| POST | `/cards/{card_id}/actions/{action_id}` | 卡片操作（授权，幂等键必填） | App（C3.1） |
| POST | `/cards/{card_id}/actions/{action_id}/challenge` | 高风险操作 challenge | App（C3.1 / C4） |
| GET | `/audit/{audit_id}` | 卡片操作审计记录 | 审计、D 线 |

### 2.1 一次发言的完整链路（参考实现）

```
AI 运行时                        网关                        Matrix
   │                              │                            │
   ├─ POST /policy/evaluate ─────►│  三层限额 / 静默 / 熔断      │
   │◄──── PolicyDecision ─────────┤  （不扣额度）               │
   │                              │                            │
   ├─ 调模型（花 token）           │                            │
   │                              │                            │
   ├─ POST /messages ────────────►│  再裁决一次（并发下额度可能变）│
   │   Idempotency-Key: …         ├─ appservice 代发 ─────────►│
   │◄──── 200 MessageReceipt ─────┤                            │
   │                              ├─ 写 usage_events            │
```

两个接口都用 `POST /messages` 的 `outcome` 说话：预检只是「提前知道」，不是「预订额度」。

---

## 3. 功能清单（Capability Manifest）规范

### 3.1 目的

App 不得硬编码模块列表。所有模块入口由网关下发，这样：

- 未安装/未启用的模块在 App 里**没有入口**（C3.2 验收点）；
- 新模块只要在网关注册 capability，App **不改代码**就能显示入口与卡片（C2.5 验收点）；
- 同一套清单驱动网页版（D4.1）与内置浏览器。

### 3.2 清单结构

```json
{
  "manifest_version": "1.2.0",
  "contract_version": "0.1.0",
  "generated_at": "2026-11-03T12:00:00Z",
  "cache_ttl_seconds": 300,
  "defaults": { "unknown_capability": "hidden", "card_fallback": "text" },
  "capabilities": [ { ... }, { ... } ]
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `manifest_version` | ✅ | semver。**内容**变化就升版本；App 可据此判断缓存是否过期 |
| `contract_version` | ✅ | 生成该清单的网关契约版本 |
| `generated_at` | ✅ | 生成时间 |
| `cache_ttl_seconds` | ❌ | 建议缓存时长，默认 300 |
| `defaults.unknown_capability` | ❌ | 遇到未知能力 ID 时 `hidden`（忽略）或 `disabled`（显示为不可用）。仅适用 native 入口（无法路由到对应能力时） |
| `defaults.card_fallback` | ❌ | 卡片渲染失败时降级为 `text` |
| `capabilities[]` | ✅ | 能力数组，按 `id` 唯一 |

### 3.3 单个 capability

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | ✅ | 小写点分 ID，匹配 `^[a-z][a-z0-9]*(\.[a-z][a-z0-9_]*)*$`，如 `memory.search`。**发布后永不复用** |
| `module` | ✅ | `core`/`role`/`memory`/`social`/`notify`/`bridge`/`town`，与仓库模块一致 |
| `version` | ✅ | 该能力自身的 semver |
| `title` / `summary` | ✅ | 默认语言下的标题与一句话说明 |
| `description` | ❌ | 详细说明 |
| `icon` | ❌ | `{type: sf_symbol|url, value}` |
| `status` | ✅ | `enabled` / `disabled` / `degraded` |
| `unavailable_reason` | ❌ | 不可用/降级原因，如 `module_disabled`、`push_channel_degraded` |
| `entry` | ✅ | 入口类型，见 3.4 |
| `scopes` | ✅ | 调用所需网关 scope |
| `requires` | ❌ | 依赖的其他能力 ID；依赖不可用时本能力也不可用 |
| `session_token.supported` | ❌ | 该能力网页是否支持短时令牌 |
| `session_token.ttl_seconds` | ❌ | 建议 TTL，60–86400，默认 300 |
| `min_app_version` | ❌ | 低于此版本的 App 不显示该能力（由服务端按 `?app_version=` 过滤） |
| `i18n` | ❌ | 语言 → `{title, summary, description}`；缺失时回退顶层字段 |

### 3.4 四种入口

| `entry.type` | 需要的字段 | App 行为 |
|---|---|---|
| `native` | `deep_link` | 打开 App 内置页面（`botville://…`）；无法路由时按 `defaults.unknown_capability` |
| `web` | `web_url` | 先取一次性 code，再用内置浏览器打开，页面换 HttpOnly cookie（见 1.2.1） |
| `card_list` | `card_types` | 在动态页渲染指定类型的卡片 |
| `none` | — | 保留能力但不显示入口（例如仅 API 可用的能力） |

**开放枚举**：`entry.type` 是开放枚举。**未知 `entry.type` 一律按 `none` 处理**：
保留能力，忽略入口。这样新增入口类型对旧 App 是安全的，且不会导致清单解码失败。

**未知 capability ID 的正确含义**（C 线评审批注）：App 本来就从清单读能力，
「未知 ID」指的是 **App 没有原生实现的 ID**，不是网关不认识的 ID。规则：

1. `entry` 是 App 认识的类型（`web` / `card_list`）时，**未知 ID 也照常显示入口**。
   这正是「新模块不改 App 即可显示入口」（C2.5）的落点。
2. `defaults.unknown_capability` **只在 entry 需要原生实现时生效**：`native` 但
   `deep_link` 无法路由时，按 `hidden`（默认）或 `disabled` 处理。
3. `requires` 引用了清单里**不存在**的能力 ID → 视为该依赖不可用 → **本能力不可用**。

### 3.5 前向兼容规则（C 线必须遵守）

1. 未知 capability `id` → 若 `entry.type` 为 `web` / `card_list` 则照常显示；
   仅 `native` 无法路由时才按 `defaults.unknown_capability` 处理。不得崩溃。
2. 未知 `entry.type` → 视为 `none`。
3. 未知 `status` → 按 `disabled` 处理（入口隐藏）。
4. 未知 `module` → 照常显示能力，按「其他」分组。
5. 未知字段 → 忽略；**任何未知枚举值都不得导致整份清单解码失败**。
6. `status` 为 `disabled` 的能力 → 入口隐藏；`degraded` → 显示入口但展示 `unavailable_reason`。
7. `requires` 中任一能力不可用（或不存在）→ 本能力也不可用（客户端自行推导，服务端也会同步置状态）。
8. 拉取失败时用最后一次成功的缓存；无缓存时按「核心能力常驻」渲染（`chat.direct` 等
   `module: core` 且 `status: enabled` 的）。
9. 支持 `ETag` / `If-None-Match` 与 `platform=ios|macos`、semver 形式的 `app_version`：
   iOS 与 macOS 可用入口可能不同，服务端可按平台过滤。

### 3.6 示例

完整示例见 [`examples/capability-manifest.json`](../openapi/examples/capability-manifest.json)。
片段：

```json
{
  "id": "memory.search",
  "module": "memory",
  "version": "0.9.0",
  "title": "记忆检索",
  "summary": "按语义检索角色的四层记忆。",
  "status": "enabled",
  "entry": { "type": "web", "web_url": "https://gateway.example.invalid/modules/memory/" },
  "scopes": ["memory.read"],
  "requires": ["chat.direct"],
  "session_token": { "supported": true, "ttl_seconds": 300 },
  "min_app_version": "0.4.0"
}
```

### 3.7 命名约定

- 能力 ID 按「模块.功能」两段，必要时三段的点分小写：`notify.approvals`、`town.enter`。
- 已发布的能力 ID 不复用、不重命名；废弃走 [5.4](#54-弃用流程)。
- `manifest_version` 用 semver：新增能力 = minor；修复元数据/文案 = patch；
  删除能力或改变既有能力语义 = major（在新契约版本里做）。

---

## 4. 卡片描述规范（Card Schema）

### 4.1 目标

一张卡片 = **结构化数据**，不是 HTML。App 用固定的通用渲染器画出所有卡片，
模块只描述内容与动作。这样新模块不必改 App 就能出卡片（C2.5），旧客户端遇到
不认识的块或类型也能退化为纯文本（`fallback_text`）。

### 4.2 顶层字段

> **响应侧枚举都是开放枚举**。客户端遇到未知值必须按 4.7 的降级表处理，
> 不得因未知值导致整卡解码失败。请求侧（`POST /cards`）的枚举仍是封闭集合。

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | ✅ | 卡片 ID |
| `card_schema_version` | ✅ | 卡片结构版本，本文为 `1.0.0`；与契约版本解耦，见 4.7 |
| `type` | ✅ | 开放枚举：`notification`/`approval`/`moment`/`system`/`reminder`/`result`。未知 → 只用 `fallback_text` |
| `title` | ✅ | 标题 |
| `subtitle` | ❌ | 副标题（来源、时间等） |
| `fallback_text` | ✅ | **必填**。纯文本降级内容；旧客户端、未知类型、渲染失败时展示 |
| `priority` | ✅ | 开放枚举：`urgent`/`high`/`normal`/`low`。未知 → 按 `normal` |
| `risk` | ✅ | 开放枚举：`low`/`medium`/`high`。**未知 → 按 `high`**（二次确认 + Face ID） |
| `status` | ✅ | 开放枚举：`active`/`read`/`dismissed`/`expired`/`acted`。未知 → 按 `disabled`（不可操作） |
| `blocks[]` | ✅ | 内容块，见 4.3；`maxItems: 50` |
| `actions[]` | ✅ | 操作，可为空数组；`maxItems: 10` |
| `source` | ✅ | `{module, event_id, dedupe_key, capability_id, actor}`，标明谁发的；`event_id` 与 `dedupe_key` 二选一必填，见 4.8 |
| `context` | ❌ | `{conversation_id, deep_link, metadata}`，打开卡片时的上下文 |
| `i18n` | ❌ | 语言 → `{title, subtitle, fallback_text}` |
| `delivery[]` | ❌ | 各渠道投递状态，见 4.8 |
| `group_id` / `collapse_key` | ❌ | 合并展示组 / 合并键 |
| `deliver_after` / `interrupt` | ❌ | 最早投递时间 / 是否穿透静默 |
| `created_at` / `updated_at` | ✅ / ❌ | 时间 |
| `expires_at` / `acted_at` | ❌ | 过期与处理时间，可为 `null` |
| `revision` | ❌ | 乐观并发版本 |

### 4.3 内容块（`blocks[]`）

通用渲染器按 `type` 取字段，未知 `type` **跳过该块**（不报错，也不影响其余块）。
`level` 是统一的可选语义色：`info`/`success`/`warning`/`danger`（未知 `level` 按无颜色）。

| `type` | 使用字段 | 渲染 |
|---|---|---|
| `text` | `text`, `level` | 段落文本 |
| `key_value` | `items[{label, value, emphasis}]` | 键值行，`emphasis` 加粗 |
| `divider` | — | 分隔线 |
| `image` | `url`, `alt`, `aspect_ratio`, `level` | 图片；只接受 `https`，加载失败显示 `alt` |
| `progress` | `label`, `value`, `max`, `level` | 进度条（如「今日部署 2/5」） |
| `list` | `items[{label, value}]`, `level` | 列表 |
| `code` | `text`, `level` | 等宽代码块 |

约束：

- **硬上限**：`blocks` ≤ 50 块，单块 `text` ≤ 4000 字符，`items` ≤ 50 项。
  服务端超限返回 422；客户端收到超限内容时**截断**，不报错。
- `image` 只用 `https`；建议给 `aspect_ratio`（如 `1.777`）避免列表跳动。
- 不得在块中放 URL 作为可点击跳转——跳转一律用 `actions` 的 `open_url`，
  这样 App 能统一处理（用户确认、外部浏览器策略、审计）。
- 不得放 HTML/Markdown 原文。需要富文本时新增块类型，不要塞标记语言。

### 4.4 操作（`actions[]`）

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | ✅ | 卡片内唯一 |
| `label` | ✅ | 按钮文案 |
| `kind` | ✅ | 开放枚举：`approve`/`deny`/`open_url`/`callback`/`dismiss`。**未知 → 隐藏按钮** |
| `style` | ✅ | 开放枚举：`primary`/`secondary`/`danger`。**未知 → 按 `secondary`** |
| `risk` | ❌ | 可选，缺省继承卡片；**有效 risk = `action.risk ?? card.risk`**。未知 → 按 `high` |
| `target` | ❌ | `open_url` 用：`in_app`（内置浏览器，可带会话 cookie）/ `external`（系统浏览器），缺省 `external` |
| `capability_id` | ❌ | `target: in_app` 时指明用哪个能力的会话打开 |
| `url` | 条件 | `kind: open_url` 时必填，且为 `https` |
| `payload` | ❌ | 回传给来源模块的不透明数据；**不得含密钥或个人敏感信息** |
| `confirmation` | ❌ | `{required, title, body, biometric}`；高风险应 `biometric: true` |
| `disabled_reason` | ❌ | 不可点击原因 |

**有效风险等级与强制生物识别（C4）**：

- 有效 risk 按操作级覆盖卡片级：`effective_risk = action.risk ?? card.risk`。
  这样同一张 high 卡上的 `deny` / `dismiss` 可以标 `risk: low` 而不误伤。
- **只要有效 risk 为 `high`，App 一律强制二次确认 + Face ID**，不论服务端是否下发
  `confirmation`。服务端漏配不得导致绕过。
- 未知 `risk` 按 `high` 处理，属于安全侧的失败保护。

**高风险操作流程（C3.1 / C4 验收点）**：

1. App 弹确认页 → Face ID 通过。
2. App 调用 `POST /cards/{card_id}/actions/{action_id}/challenge`，拿到
   `{challenge, expires_at, card_id, action_id, revision, algorithm}`。
3. App 用 Secure Enclave 设备密钥（或 App Attest 密钥）对
   **`card_id + action_id + revision + challenge`** 签名，得到 `assertion`；
   assertion 因此绑定卡片、操作、版本与一次性 challenge，不能跨卡/跨操作重放。
4. `POST /cards/{card_id}/actions/{action_id}` 带 `assertion`、`challenge`、`revision`
   与**必填的** `Idempotency-Key`（同一次用户意图复用同一个 key）。
5. 网关校验：challenge 未过期且匹配、assertion 有效、`revision` 与当前一致（防 TOCTOU）、
   卡片未过期、操作未被处理过、调用者有 scope。
6. 失败码见 1.3；成功返回 `CardActionReceipt`（含 `audit_id`、`actor`、认证方式、
   决策前后 `revision`、来源模块与更新后的卡片）。

断言格式本身仍是占位（示例只出现 `<change-me>`），字节编码由 D2.1 定义；
契约只固定**绑定字段**与上述错误码。

### 4.5 完整示例（授权卡片）

```json
{
  "id": "card_demo",
  "card_schema_version": "1.0.0",
  "type": "approval",
  "title": "授权：部署到生产",
  "subtitle": "来自 代码助手 · 5 分钟前",
  "fallback_text": "代码助手请求授权：把 staging 分支部署到生产。回复「批准」或「拒绝」。",
  "priority": "urgent",
  "risk": "high",
  "status": "active",
  "blocks": [
    { "type": "text", "text": "代码助手想执行一次生产部署，请确认变更内容。", "level": "info" },
    { "type": "key_value", "items": [
      { "label": "分支", "value": "release/2026.11" },
      { "label": "提交", "value": "a1b2c3d" },
      { "label": "影响", "value": "3 个服务", "emphasis": true }
    ]},
    { "type": "divider" },
    { "type": "progress", "label": "今日部署次数", "value": 2, "max": 5 },
    { "type": "code", "text": "botville deploy --env prod --ref release/2026.11", "level": "warning" }
  ],
  "actions": [
    { "id": "approve", "label": "批准", "kind": "approve", "style": "primary",
      "risk": "high", "payload": { "run_id": "run_demo" },
      "confirmation": { "required": true, "title": "确认批准生产部署",
                        "body": "将把 release/2026.11 部署到生产，10 分钟内可回滚。",
                        "biometric": true } },
    { "id": "deny", "label": "拒绝", "kind": "deny", "style": "danger", "risk": "low" },
    { "id": "open", "label": "查看日志", "kind": "open_url", "style": "secondary", "risk": "low",
      "target": "in_app", "capability_id": "notify.approvals",
      "url": "https://gateway.example.invalid/modules/ci/runs/run_demo" }
  ],
  "source": { "module": "notify", "capability_id": "notify.approvals",
              "event_id": "evt_ci_run_demo_approval",
              "actor": { "id": "persona_ci", "kind": "ai", "display_name": "代码助手" } },
  "context": { "conversation_id": "conv_ci", "deep_link": "botville://cards/card_demo",
               "metadata": { "run_id": "run_demo" } },
  "delivery": [
    { "channel": "push", "state": "confirmed", "attempts": 1,
      "last_attempt_at": "2026-11-03T12:05:02Z", "failure_code": null,
      "provider_message_id": "apns_demo_1" }
  ],
  "deliver_after": null,
  "interrupt": true,
  "created_at": "2026-11-03T12:05:00Z",
  "updated_at": "2026-11-03T12:05:02Z",
  "expires_at": "2026-11-03T13:05:00Z",
  "acted_at": null,
  "revision": 1
}
```

其他示例：通知/提醒卡片见 [`examples/create-card-request.json`](../openapi/examples/create-card-request.json)，
操作回执见 [`examples/card-action-receipt.json`](../openapi/examples/card-action-receipt.json)，
challenge 见 [`examples/card-action-challenge.json`](../openapi/examples/card-action-challenge.json)，
操作请求见 [`examples/card-action-request.json`](../openapi/examples/card-action-request.json)，
审计记录见 [`examples/audit-record.json`](../openapi/examples/audit-record.json)。

### 4.6 校验规则（服务端必须做）

1. `fallback_text` 非空；缺失返回 `422 card.missing_fallback_text`。
2. **生产者（CreateCard）不得提交未知块类型**，未知类型拒绝（`422 card.invalid_block`）；
   新增块类型要先升 `card_schema_version`。**这条只约束生产者**，与消费者侧的
   「跳过未知块」（4.7）是两件事。
3. `kind: open_url` 必须有 `url`，且为 `https`（或明确列入白名单的 scheme）；
   `target: in_app` 时 `capability_id` 必须在调用方 scope 内。
4. 有效 risk 为 `high` 的操作必须带生物识别确认（`confirmation.biometric: true`）。
5. `payload`、`context.metadata` 做密钥与个人信息扫描；命中拒绝。
6. 卡片数量与推送频率受平台防刷屏约束（例如每人每小时 ≤ 20 张 `urgent`）。
7. `source` 必填；`source.event_id` 与 `source.dedupe_key` 二选一必填；按
   `(capability_id, event_id ?? dedupe_key)` 去重。
8. 创建时校验 `source.capability_id` 与调用方 scope 一致，`source.module` 对齐
   Capability 的 module 枚举，防止伪造 `notify.approvals` 之类的高风险来源。
9. `approval` 卡片或有效 risk=high 时，`expires_at` 必填。
10. 去重命中已有卡片时返回 `200`（不是错误，也不是 `201`）；`201` 只用于首次创建。

### 4.7 向前兼容与版本协商

#### 未知值处理表（C1）

**响应侧字段都是开放枚举**：客户端遇到未知值按下表降级，**任何单个字段的未知值
都不得导致整卡或整份清单解码失败**。

| 字段 | 未知值处理 |
|---|---|
| `Card.type` | 只用 `fallback_text` 降级渲染 |
| `CardBlock.type` | 跳过该块，继续渲染其余块 |
| `CardBlock.level` | 按无颜色渲染 |
| `CardAction.kind` | 隐藏该按钮 |
| `CardAction.style` | 按 `secondary` |
| `CardAction.risk` / `Card.risk` | **按 `high`**（二次确认 + Face ID） |
| `Card.priority` | 按 `normal` |
| `Card.status` | 按 `disabled`（不可操作，仍可展示） |
| `CardDelivery.channel` | 照常显示投递状态 |
| `CardDelivery.state` | 按 `unconfirmed`（不重放该渠道） |
| `Capability.status` | 按 `disabled`（入口隐藏） |
| `Capability.entry.type` | 按 `none` |
| `Capability.module` | 照常显示，按「其他」分组 |
| `CapabilityIcon.type` | 按无图标渲染 |
| 其他未知字段 | 忽略 |

#### `card_schema_version` 与契约版本解耦（C2）

- 卡片结构的 **minor** 升级（新增块类型、可选字段）：契约 `info.version` **不必同步升**。
- 卡片结构的 **major** 升级：**可在同一个 `/v1` 内共存**，不要求开 `/v2`；
  前提是通过下面的协商机制给旧客户端下发 `fallback_text`。
- 卡片的 semver 只描述卡片渲染结构，不描述契约 API 形态。

#### 协商机制

| 客户端 | 服务端 |
|---|---|
| 请求头 `X-Card-Schema-Version: <major>.<minor>` | `GET /cards`、`GET /cards/{card_id}` 接收 |
| query `card_schema_max=<major>.<minor>`（优先级高于请求头） | 按 max 降级下发 |

客户端行为：

- **major 不匹配**（服务端卡片 major > 客户端支持）→ 整卡**只显示 `fallback_text`**
  （可附带按 actions 降级的按钮，或干脆不给按钮）。
- **minor 更高** → 照常渲染，跳过未知块/字段。

服务端行为：按 `card_schema_max` 降级到客户端可理解的版本；无法降级的卡片只下发
`fallback_text` 与最小元数据。

新块类型/枚举值 = 卡片 minor（`1.0.0 → 1.1.0`）；改变已有字段语义或删除 = 卡片
major（可在 `/v1` 内共存）。

### 4.8 通知与投递数据模型（D1）

#### 去重

- `source` 必填。`source.event_id` 与 `source.dedupe_key` **二选一必填**：
  有稳定事件 ID 时用 `event_id`；没有时（桥接、外部 webhook、轮询结果）用 `dedupe_key`。
  两个都不给 → `422 card.missing_event_id`。
- 服务端按 **`(capability_id, event_id ?? dedupe_key)`** 去重：重复创建返回**已有卡片**
  （`200`，首次创建是 `201`），不重复投递。这样生产者重启、多实例、多渠道重放都不会
  重复打扰。
- `Idempotency-Key`（24h 缓存）是传输层幂等，**不能替代**来源键的业务级去重：
  前者只管同一个 HTTP 重试，后者管「同一事件被重新发起」。

#### 投递状态 `delivery[]`

`Card.status` 是用户生命周期（已读/已处理），不是投递状态。投递状态单独用
`delivery[]` 表达：

| 字段 | 说明 |
|---|---|
| `channel` | `push` / `in_app` / `email` / `webhook`（开放枚举） |
| `state` | `pending` / `confirmed` / `unconfirmed` / `failed` |
| `attempts` | 尝试次数（只增） |
| `last_attempt_at` | 最后一次尝试时间 |
| `failure_code` | 失败或未确认的机器可读原因 |
| `provider_message_id` | 渠道侧消息 ID，便于对账 |

**模糊结果处理**：超时、回执缺失等无法确认送达的情况记 `unconfirmed`，
**不得在同一渠道重放**——宁可漏一次，也不要重复打扰。要重投必须换渠道或由用户主动触发。

#### 静默、优先级与合并

`POST /cards` 参与静默检查（此前不参与，已修正）：

| 字段 | 说明 |
|---|---|
| `deliver_after` | 服务端最早投递时间；`null` 表示立即（仍受静默检查） |
| `scheduled_at` | 期望展示时间（如提醒触发时刻），与 `deliver_after` 二选一 |
| `interrupt` | 是否穿透静默立即打断；**无默认值**（见下），省略时按 `priority` 决定 |
| `collapse_key` / `group_id` | 合并键 / 合并展示组，同键未读卡片折叠为摘要 |

`priority` → 打断级别：

| priority | 静默时段内 | 打断级别 |
|---|---|---|
| `urgent` | 默认穿透静默（`interrupt: true` 效果） | 立即通知，可响铃 |
| `high` | 延后，但置顶 | 静默结束后优先 |
| `normal` | 延后 | 常规 |
| `low` | 延后 | 静默/摘要里出现 |

`interrupt` 是三态字段：**省略** / `true` / `false`。

- 省略时由 `priority` 决定：`urgent` → 穿透；其余优先级 → 顺延。
- 显式 `true` → 无论优先级都穿透。
- 显式 `false` → 即使 `priority: urgent` 也不打断，静默期间顺延到 `deliver_after`。

因此 `interrupt` **不得有 `default`**（`Card` 与 `CreateCardRequest` 两处均已移除
`default: false`）。若 schema 层补默认值，「未显式给出」与「显式 false」就不可区分，
`urgent` + 省略 `interrupt` 是否打断将取决于客户端实现——这正是本节要消除的歧义。
代码生成/校验器请把该字段当作可选三态（`boolean | undefined`）处理。

#### 增量同步

`GET /cards?updated_since=<RFC 3339>` 只返回该时间之后更新的卡片，供 App 做
「已处理」状态的多端同步。`card.already_decided` 的 409 响应会附带最新卡片。

#### queued / deferred 可追踪

- `outcome: queued` 与 `deferred` 的回执**必须带 `queue_id`**。
- `deferred` 是**服务端排程**（不是客户端重试）：客户端拿到 `queue_id` 后等待推送，
  不要自行重试。
- 幂等缓存对 `queued` / `deferred` 同样生效：重用同一 `Idempotency-Key` 返回同一
  `queue_id` 与当前状态，不会新建排程；进入终态后重放返回终态回执。

---

## 5. 版本规则

### 5.1 三条硬规则

1. **路径带版本**：所有接口在 `/v1` 下。`/v2` 是另一个契约文件，不是 `/v1` 的变形。
2. **只增不删**：同一主版本内，只能新增接口、字段、枚举值、能力；不得删除、重命名、
   改类型、收紧约束。这是 `CONTRIBUTING.md` 第 5、6 条与 `docs/README.md` 的要求。
3. **破坏性变更必须换主版本**：新主版本与新路径并存，旧版本至少保留一个迁移周期。

### 5.2 版本号落在哪里

| 版本 | 位置 | 语义 |
|---|---|---|
| 契约版本（`0.1.0`） | `info.version`、`/health.contract_version`、manifest 的 `contract_version` | 整份契约的 semver |
| 接口主版本（`v1`） | 路径前缀、`servers` | 破坏性变更的分界 |
| 功能清单版本 | `manifest_version` | 清单**内容**的变更 |
| 能力版本 | `capability.version` | 单个能力自身演进 |
| 卡片结构版本 | `card_schema_version` | 卡片渲染结构的变更 |

**卡片结构版本与契约版本解耦**（C2）：

- 卡片 minor（新增块类型、可选字段）**不要求** `info.version` 同升。
- 卡片 major **可在同一 `/v1` 内共存**，通过 `X-Card-Schema-Version` /
  `card_schema_max` 协商下发 `fallback_text`，不必开 `/v2`（见 4.7）。
- 只有**接口形态**的破坏性变更（路径、必填字段、枚举收紧）才换主版本。

`0.x` 阶段（M0–M1）允许在同一路径上做破坏性调整，但**每次都必须**同步改
`info.version`、更新示例、并在 PR 描述里写明「0.x 例外」。`1.0.0` 之后严格执行 5.1。

### 5.3 变更等级

| 变更 | 示例 | 等级 | 契约版本 |
|---|---|---|---|
| 新增可选请求字段 | `POST /messages` 加 `metadata` | 非破坏 | minor |
| 新增响应字段 | 会话加 `last_activity_at` | 非破坏 | minor |
| 新增接口 | `GET /usage/events` | 非破坏 | minor |
| 新增枚举值 | `card.type` 加 `digest` | 非破坏* | 卡片 minor（契约可不升） |
| 新增必填响应字段 | — | **破坏** | major + 新路径 |
| 新增必填请求字段 | 给 `POST /messages` 加必填字段 | **破坏** | major |
| 删除/重命名字段 | — | **破坏** | major |
| 收紧约束 | `maxLength` 8000→4000、`enum` 减项 | **破坏** | major |
| 改默认值 | `limit` 默认 20→50 | 准破坏 | minor + 公告 |
| 改错误码语义 | — | **破坏** | major |

\* 新增枚举值对严格的旧客户端可能造成「未知值」——因此契约要求**响应侧枚举按开放枚举
处理**、客户端对未知值按 4.7 降级而非报错；仍应在 changelog 公告。

C 线/D 线评审时特别关注：**新增枚举值**（要求客户端容错）、**收紧 `maxLength`**、
**改默认分页大小**。

### 5.4 弃用流程

```
公告（Deprecation 头）→ 至少一个 minor 周期（建议 ≥ 90 天）→ 移除（仅限新主版本）
```

1. 在 `gateway/openapi/gateway.v1.yaml` 对应元素标 `deprecated: true`，并在
   `description` 写替代方案与移除版本。
2. 响应加头：`Deprecation: true`、`Sunset: <RFC 3339 日期>`、`Link: <替代接口>; rel="successor-version"`。
3. 写进 CHANGELOG（[5.5](#55-changelog)）并在 PR 里 @ 受影响的 C/D 线负责者。
4. 同一主版本内**只能弃用、不能移除**。移除只在下一个主版本进行。
5. 能力废弃：把 capability 的 `status` 置 `disabled` 并设 `unavailable_reason`，
   保留 ID 不复用，≥ 90 天后再从清单移除（移除视为 manifest 的 major）。

### 5.5 Changelog

| 版本 | 日期 | 变更 |
|---|---|---|
| 0.1.0 | 2026-11-04 | [#114 PR-a] 安全加固，**不改字段集合与状态码**。(1) §1.5 txnId 改为 `HMAC-SHA256(部署密钥, Idempotency-Key)`；(2) §1.5 幂等缓存淘汰后同 key 重试按 `txnId → event_id` 去重计费，不重复扣额；(3) §6.2 / OpenAPI `UsageEvent.text_sha256` 改为 HMAC-SHA256（字段名不变）；(4) 新增部署配置 `GATEWAY_HMAC_SECRET`（≥32 字节，非 dev 模式未配置或为占位时拒绝启动；dev 模式每进程随机生成并告警）；(5) `/health` whoami 探测 single-flight（并发探针在缓存过期时只打一次 homeserver）；(6) appservice `SentEvent` 回填 `txn_id`。 |
| 0.1.0 | 2026-11-04 | [B2.2 PR2 实现] 防刷屏与 AI 互聊熔断落地，**只增不删、不改状态码集合**。(1) §1.3 错误码表补 `422 request.invalid`（createConversation 语义不合法 + peer_id 校验，属 §5.3 条件必填，0.x 例外）；(2) §6.3 `ai_loop.may_clear_at` 措辞改为「最早一条**被计入 count 的** AI 事件出窗」（窗口内有早于人类消息的旧事件时不偏早）；(3) §6.3 补「blocked 仍记 `kind=blocked` 不计入连续数；冷却锚定首次触发、重试不延长」；(4) §6.4 补 `peer_id` 须为会话成员且为对方 persona（≠ sender）；(5) §6.4 peer_id 缺失强制声明属 §5.3 条件必填、0.x 例外，无 peer 规则时省略同 B2.1。实现：`store.py` 增 `EventKind.HUMAN` + `UsageEvent.peer_id` + 保留期淘汰；`policy.py` 增 `anti_spam`/`ai_loop`/`message.length` 检查 + peer 层按 (persona, peer_id) 计数；`service.py` 增 `record_human_message`/`register_conversation`/`_check_peer_id`（422）；`limits.py` #114-4 `window_seconds` 单写 → 422；`http_server.py` peer_id 校验 422。测试 `test_b22_anti_spam_ai_loop.py` 42 例。**[B2.2 #117 审查修订]** (6) §6.4 会话成员来源（HTTP 注册/房间成员同步）尚未接入：**未登记会话（或 evaluate 无 `conversation_id`）按 B2.1 采信 `peer_id`、不校验成员**（带 `peer_id` 则 peer 层生效并计数）；仅**已登记**会话做成员校验（∈成员且≠sender，否则 422）与「**对方**（排除 sender）有 peer 规则时缺 `peer_id` → 422」，sender 自身有 peer 规则不触发；空串 `peer_id` → `400 request.invalid`（send 与 evaluate 一致）。(7) §6.3 **冷却状态独立于事件表保存**（进程内存按 `(persona, conversation_id)` 键，不受 blocked 事件容量淘汰影响，不再由事件复算 `may_clear_at`）；冷却期内同键 blocked 事件**只记一条**；`cooldown_seconds=0` 触发后冷却立即结束、计数起点取触发时刻（无 `reset_at == now` 活锁）；`kind=deferred`（静默顺延）**计入**连续数；保留期 `USAGE_RETENTION_SECONDS`（默认 30 天）**按服务时钟**（事件 `recorded_at`）淘汰。(8) §6.3 在途预留按会话计入 `anti_spam`（不跨会话误伤）。 |
| 0.1.0 | 2026-11-04 | [B2.2 PR1 审查修订] 补全冷却/peer_id/幂等/码表语义，**只增不删、不改状态码集合**。(1) §6.3 冷却结束时刻即清零该 persona 在该会话的连续计数，被拦（`blocked`）消息不计入连续数；选码时机——触发那条报 `anti_spam.consecutive`、冷却期内后续请求报 `anti_spam.cooldown`（check 口径 limit=`cooldown_seconds`/used=已冷却秒数/remaining=剩余秒数/reset_at=`may_clear_at`）；冷却期内出现经网关人类消息提前解除冷却并清零，首版无人类入站入口故实际只靠冷却到期解除（不会永久静音）；(2) §6.4 当会话成员中存在配置了 peer 层限额的对方 persona、而请求未带 `peer_id` 时返回 `422 request.invalid`（`errors[].field=peer_id`），无 peer 层规则时省略 `peer_id` 与 B2.1 一致，实发与预检 `peer_id` 必须一致；(3) §1.5 把 `PUT /limits` 列为幂等例外（整层替换天然幂等、服务端不缓存、同 key 不同 body 不返回 409）；(4) `PolicyCheck.key` 推荐列表补 `anti_spam.cooldown`，注明 `key` 与 `code` 是两套名字；(5) `PolicyReason.code` 描述注明旧码 `ai_loop.breaker_open` 视为 `ai_loop.tripped` 的别名；(6) 推荐码列表按 B2.1 实际常量写（带后缀 `.exceeded`/`.active`/`.too_soon` 等）；(7) §6.3 `ai_loop` 计数口径明确为「窗口内、最后一条经网关人类消息之后的 AI 消息数（任意 AI）」，会话级计数由被评估 persona 的生效 `ai_loop` 限额约束（三层取最严），只拦被评估这条；(8) 平台硬上限 `MAX_MESSAGE_CHARS` 仍 `400 request.invalid`，只有 owner/peer 分层更严 `max_message_chars` 走 `200 blocked` + `message.length`；(9) §6.3 写明计数依赖事件保留期，部署须保证 `max_usage_events` 足以保留 `max(window,cooldown)` 内事件，容量不足可能少算（已知局限，PR2 按保留期淘汰）。实现见 PR2。 |
| 0.1.0 | 2026-11-04 | [B2.2 + #114 契约] 防刷屏 / 互聊熔断与 peer 计数语义，**只增不删、不改状态码集合**。(1) `PolicyReason.code` / `PolicyCheck.key`·`code` 推荐取值新增 `anti_spam.consecutive`、`anti_spam.cooldown`、`ai_loop.tripped`（均为开放字符串，旧值仍有效）；(2) `UsageEvent` 新增可选 `peer_id`；(3) 新增 [§6.3 熔断语义](#63-熔断语义b22)：连续的定义（`ai_loop` 同会话任意 AI、`anti_spam` 同 persona）、人类消息重置、`may_clear_at` 口径（anti_spam=冷却结束、ai_loop=最早事件出窗，向上取整到秒）、结论 `blocked`+`retryable:false`+200、首版只统计经网关的人类消息、`on_break=notify` 未实现时按 `stop` 处理（fail-closed）；(4) 新增 [§6.4 peer 计数语义](#64-peer-计数语义与-b21-兼容性b22)：B2.2 起 peer 层按 `(persona, peer_id)` 计数，请求不带 `peer_id` 时 peer 层不生效、旧事件无 `peer_id` 只计入 persona 全局与 platform/owner 层，platform/owner 计数不变；(5) #114-4 `window_seconds` 必须与本层 `messages_per_window` / `tokens_per_window` 成对出现，单写→422 `limit.out_of_range`；(6) #114-5 等速率按 platform→owner→peer 归属（行为不变）；(7) #114-7.5 `UsageEvent` 描述改准：删除「不可反推原文」，说明短/低熵文本 SHA-256 可被字典猜测（后续改 HMAC），`text_length` = Unicode 字符数；(8) #114-7.8b 错误边界：结构/类型错=400 `request.invalid`，规则值语义越界=422 `limit.out_of_range`；(9) #114-7.8c `PUT /limits` 为整层替换、天然幂等，`Idempotency-Key` 可选且服务端不缓存；(10) 示例：`policy-decision` / `message-receipt-blocked` 改用 `ai_loop.tripped`，新增 `usage-event.json`（含 `peer_id`）。实现见 PR2。 |
| 0.1.0 | 2026-11-04 | 澄清会话加密与 AI 参与：E2EE/`ai_participation=disabled` 下 AI 发言与预检返回 `403 conversation.e2ee`（硬约束，非 §1.4 blocked）；`createConversation` 禁止 `e2ee`+`enabled`（`422 request.invalid`），省略 `ai_participation` 时按 `encryption` 推导；苹果端细节改为外链。属文档澄清，不改字段集合。 |
| 0.1.0 | 2026-11-04 | `Card.interrupt` 与 `CreateCardRequest.interrupt` 移除 `default: false`，改为三态（省略 / `true` / `false`）：省略时由 `priority` 决定（`urgent`→穿透，其余→顺延），显式 `false` 即使 `urgent` 也不打断。属「改默认值」=准破坏，按 §5.3 minor + 公告处理；同主版本内字段语义未变，仅澄清三态。 |
| 0.1.0 | 2026-11-04 | [B2.1] 修 DeepSeek B1–B4。**取舍：补契约、不删实现**——`Message.metadata` 是 #92 已实现并测试的透传字段，`peer_id` 是三层限额在实发路径生效的必需输入，删掉会回退功能，因此把两者补进 OpenAPI 而不是从实现里移除。(1) 窗口类限额改为速率对取最严，`window_seconds` 不独立比较（B1）；(2) `PolicyEvaluateRequest` / `OutgoingMessageRequest` 增加可选 `peer_id`，实发路径据此触发 peer 层（B2）；(3) `Message` 增加可选 `metadata`（string 值，maxProperties 8，B4）；(4) token 估算取 `max(client, estimate_tokens(text))`，`token_estimate:0` 不可绕过（B3）；同步修 §6.1。#99 加固：txnId 由 Idempotency-Key 稳定推导（重试命中同一事务）；/health whoami 短超时 + 5s 缓存；ensure_user 读写加锁（double-check，网络请求仍在锁外）。 |

新增条目追加在本表顶部；同一版本内的编辑合并到该版本行。

---

## 6. 限额与用量（供 C/D 线理解，B2.1 实现）

### 6.1 三层限额

```
        ┌──────────────────────────────┐
        │  平台底线  platform（只读）    │  部署配置，不可经 API 修改
        ├──────────────────────────────┤
        │  所有者设置  owner              │  AI 所有者对自己角色/会话设的限制
        ├──────────────────────────────┤
        │  对方设置  peer               │  对方角色对「跟我说话」设的限制
        └──────────────────────────────┘
                    ↓ 逐项取最严
              生效限额 effective
```

- 「取最严」是**逐项**的，不是整层覆盖：消息数可能由 owner 胜出，token 可能由
  platform 胜出。`EffectiveLimit.source_layer` 指明每一项的胜出层。
- **`0` 是有效限额**（`messages_per_window: 0` 表示一条都不允许），不是「不限」；
  「该层不限制」用**省略字段**表达。部署配置 `RATE_LIMIT_MESSAGES=0` /
  `PLATFORM_TOKEN_BUDGET=0` 才是「平台关闭该项限额」（历史语义，两者不可混用）。
- **严格方向**：`proactive_per_day` / `town_tokens_per_day` / `max_message_chars`
  越小越严；`min_seconds_between_messages` 越大越严；`silent_hours.enabled: true`
  比 `false` 严。
- **窗口类限额按「速率对」取最严**（B1）：`messages_per_window` 与
  `tokens_per_window` 各自与它的 `window_seconds` 是**一个整体**，比较的是
  `count / window`；胜出层**成对采用**自己的 `count` 与 `window`（该层省略
  `window_seconds` 时回退平台窗口）。**`window_seconds` 不单独比较**——否则只写
  `window_seconds: 60` 就能把平台「2 条/3600s」放大成「2 条/60s」。两个速率对的
  胜出层可能不同，因此生效值分别给出 `messages.window_seconds` 与
  `tokens.window_seconds`。
- **平台层是底线，只读**：`PUT /limits` 只能写 owner / peer；任何一项比平台层宽松
  → **422 `limit.out_of_range`**（`errors[]` 逐项给出字段与平台底线值）。
  窗口类按速率对校验：`count/window` 不得大于平台的 `count/window`，**只缩短
  窗口**（计数不变、`window_seconds` 变小）同样算放宽。平台已开启的静默，
  owner/peer 不得关闭。
- **`window_seconds` 必须成对出现**（#114-4）：某层写了 `window_seconds`，就必须
  同时写 `messages_per_window` 或 `tokens_per_window` 中的至少一个。只写
  `window_seconds` 不会生效，按 **422 `limit.out_of_range`** 拒绝（实现见 B2.2 PR2）。
- **等速率时按 platform → owner → peer 归属**（#114-5）：两项比较结果相等时，
  `EffectiveLimit.source_layer` 取更靠平台的那一层。这是归属约定，不改变任何
  拦截行为（方向安全）。
- **错误边界**（#114-7.8b，状态码集合不变）：**结构/类型错误**（缺必填字段、
  类型不符、非 JSON、未知属性）→ `400 request.invalid`；**规则值语义越界**
  （比平台宽松、只写 `window_seconds`、枚举值未知如 `on_break`、时区不合法）
  → `422 limit.out_of_range`。
- 静默时段、防刷屏、互聊熔断也各自参与，`checks[]` 里逐项给出 `allowed`/`limit`/`used`/`remaining`/`reset_at`。
- `PolicyCheck.key` 推荐取值：`messages.window`、`tokens.window`、`proactive.per_day`、
  `town.tokens_per_day`、`silent_hours`、`min_interval`、`anti_spam.consecutive`、
  `anti_spam.cooldown`、`ai_loop.per_hour`、`message.length`、`relation.chat_allowed`。
  **注意**：`PolicyCheck.key` 与 `PolicyReason.code` 是**两套名字**——前者是检查项标识，
  后者是未通过原因码；两者同域但取值不同（如 key=`messages.window` 对应 code=
  `messages.window.exceeded`）。
- **B2.1 已实现**的检查项：`messages.window`（code: `messages.window.exceeded`）、
  `tokens.window`（code: `tokens.window.exceeded`）、`min_interval`（code: `min_interval.too_soon`）、
  `silent_hours`（code: `silent_hours.active`）（每项都取三层中最严者，`source_layer` 落在
  `PolicyCheck.layer` 上）。`proactive.per_day`（code: `proactive.per_day.exceeded`）/
  `town.tokens_per_day`（code: `town.tokens_per_day.exceeded`）的生效值已在 `GET /limits` 中计算，
  执行留待 B2.2（主动发言调度）。
- **B2.2 契约已定、实现见 PR2** 的检查项：`anti_spam.consecutive`（同 persona 连续
  AI 消息达到 `max_consecutive_ai_messages`，code: `anti_spam.consecutive`）、`anti_spam.cooldown`
  （防刷屏冷却未结束，code: `anti_spam.cooldown`）、`ai_loop.per_hour`（同会话任意 AI 互聊
  达到 `max_ai_messages_per_hour`，code: `ai_loop.tripped`）、`message.length`（正文长度超过
  owner/peer 分层 `max_message_chars`，code: `message.length`）。熔断语义见 [§6.3](#63-熔断语义b22)。
- **token 预算**：评估时按 `token_estimate`（缺省按正文长度估算）**预扣**，
  投递成功后实际入账（`usage_delta.tokens_out`）。计量取
  `max(token_estimate, estimate_tokens(content))`（B3）：客户端给的值只是**下限**，
  不能低于按正文的估算，否则 `token_estimate: 0` 能绕过预算（`tokens_per_window: 0`
  也挡不住非空正文）。`tokens.window` 检查的 `used` 是窗口内已入账之和，
  `allowed = used + estimated <= limit`。

### 6.2 预算与账单

- 预算以 token 计，窗口默认 24h（`window_seconds`）。
- 账单是 append-only 的 `usage_events`，`kind` 覆盖模型调用、发送、拦截、主动发言、
  小镇行为、图像生成。
- **消息正文不落入账单**（B2.1 / #92）：`UsageEvent` **不存储**消息正文，只存
  `text_length`（Unicode 字符数，与 `len(text)` 一致）与 `text_sha256` 用于对账。
  自 #114 PR-a 起 `text_sha256` =
  **`hex(HMAC-SHA256(部署密钥 GATEWAY_HMAC_SECRET, b"text\x00" + UTF-8(正文)))`**
  （字段名不变）；`b"text\x00"` 是域分隔前缀，与 txnId 的 `b"txn\x00"` 隔开，
  第三方复算时必须带上。同一部署同内容同值，可对账；没有密钥无法字典反推；
  轮换密钥后新旧事件不可跨期比对。旧字段 `text_preview` 从 B2.1 起**不再写入**，
  客户端必须忽略历史事件中该字段（前向兼容）。
- **密钥轮换的影响（#114 PR-a）**：`GATEWAY_HMAC_SECRET` 同时决定 `text_sha256`
  **和 txnId**，因此轮换不止“新旧摘要不可比对”：
  - 轮换后同一 `Idempotency-Key` 推导出**新的 txnId**，homeserver 不再认为是同一
    事务。轮换前已投递、轮换后重试的请求会落到新事务上，可能产生**重复消息**。
    建议轮换前先排空在途重试（等客户端重试窗口过去再切换）。
  - 幂等缓存与 `txnId → event_id` 账本均为**进程内存**，不做持久化，重启即清空；
    其“淘汰后不重复计费”的语义**假设网关单进程**。多实例部署必须让所有实例使用
    同一 `GATEWAY_HMAC_SECRET`（否则 txnId 互不相同，HS 侧去重失效），但各实例的
    内存账本不共享，跨实例的重复投递仍可能各记一次 SENT。
- **限速窗口开区间 + 释放时刻向上取整**（#92）：窗口过滤使用左开右闭区间
  `(now - window_seconds, now]`，保证「过期」的名额在 `reset_at` 之后**严格**
  可用；`reset_at` 向上取整到秒，避免差一秒的活锁。`used > limit` 时
  `reset_at` 取第 `used − limit + 1` 早的事件的过期时刻（足够让一个名额腾出来）。
- **小镇消耗单列**（`town_tokens`、`kind: town_action`）：小镇行为受 `town_tokens_per_day`
  约束，预算用尽后只走动不聊天（B4.2）。D 线做账目展示时请把它与聊天分开。
- **`PUT /limits` 整层替换**（#114-7.8c）：请求体 `rules` 完整替换该层原有规则，未出现的
  字段即被清除。因此该端点**天然幂等**，`Idempotency-Key` 对其**可选**，服务端**不做**
  幂等缓存，重放按一次新的整层替换处理。

### 6.3 熔断语义（B2.2）

本节描述防刷屏（`anti_spam`）与互聊熔断（`ai_loop`）的语义，契约已定，实现见 PR2。

- **「连续」的两种口径**：
  - **`ai_loop`（互聊熔断）按同会话任意 AI 连续计数**：同一 `conversation_id` 内，
    `count` = 滑动窗口内、**最后一条经网关人类消息之后**的 AI 消息数（任意 AI，不限
    persona）。理由：互聊的本质是多个 AI 互相接话，与具体哪个 AI 无关，只看「房间里
    AI 一直在自己说」这件事是否超过人类允许的密度。会话级计数由**被评估 persona 的生效
    `ai_loop` 限额**约束（三层取最严），只拦被评估的这条发言，不影响其他 AI 的独立计数。
  - **`anti_spam`（防刷屏）按同 persona 连续计数**：同一 `conversation_id` 内，按时间序
    累计**同一 `persona_id`** 发出的、未被人类消息打断的 AI 消息数。理由：防刷屏关心
    单个 AI 是否在刷屏，因此绑定到具体 persona。
  - 计数都基于 `UsageStore` 中近期发送方序列（`ai` / `human`），不读消息正文。
  - **被拦（`blocked`）的消息不计入连续数**：`anti_spam` / `ai_loop` 触发后返回
    `blocked`，该条消息不写入连续序列，冷却期内多次请求不会进一步累积计数。
- **人类消息重置连续计数**：一旦该会话出现一条**经网关**的人类消息，`ai_loop` 的「任意
  AI 连续」与 `anti_spam` 的「同 persona 连续」计数**同时归零**；冷却期内出现经网关的
  人类消息时亦提前解除冷却并将连续计数归零。**首版局限**：当前网关只有 AI 发言入口
  （`POST /conversations/{id}/messages`），没有人类入站路由；因此冷却实际只靠到期解除，
  **不会永久静音**。此局限将在后续接入人类消息同步后消除，届时不升主版本（仅扩展行为）。
- **恢复时间 `may_clear_at`**：
  - `anti_spam` 达 `max_consecutive_ai_messages` 上限后进入 `cooldown_seconds` 冷却，
    `may_clear_at` = **冷却结束时刻**（触发时刻 + `cooldown_seconds`，向上取整到秒，
    与 B2.1 的 `reset_at` 口径一致）。**冷却结束时刻即将该 persona 在该会话的连续计数
    清零**，下次发言重新从 0 起计；与 `same_content_allowed: true` 一致（冷却后同一
    内容可重发）。
  - `ai_loop` 按小时滑动窗口（`max_ai_messages_per_hour`）：`may_clear_at` = **最早一条
    被计入 count 的 AI 事件滑出窗口的时刻**（向上取整到秒，与 B2.1 一致）。
    count 已定义为「窗口内、最后一条人类消息之后的 AI 消息数」，因此「被计入 count
    的最早一条」指该窗口内、人类消息之后最早的那条；窗口内有早于人类消息的旧事件时
    不偏早（那些旧事件不在 count 中）。
  - 两者结论均为 `outcome: blocked`、`retryable: false`、HTTP `200`（策略结论不是错误，
    见 §1.4）。`same_content_allowed: true`（额度/窗口恢复后同一内容可重发）。
- **选码时机**：
  - 触发那条（第 `max_consecutive_ai_messages + 1` 条）：`reasons[].code = anti_spam.consecutive`。
  - 冷却期内后续请求：`reasons[].code = anti_spam.cooldown`；对应 `PolicyCheck` 口径：
    `key = anti_spam.cooldown`、`limit = cooldown_seconds`、`used` = 已冷却秒数、
    `remaining` = 剩余秒数、`reset_at = may_clear_at`。
- **blocked 事件持久化与冷却锚点**：被拦消息仍以 `kind=blocked` 写入 `UsageStore`
  （与 `MessageReceipt` 语义一致），但**不计入连续数**（连续数只统计 `kind=sent` /
  `kind=deferred`）。
  - **冷却状态独立于事件表保存（#117 审查修订）**：冷却锚点由网关在**实发路径**单独
    记录（进程内存，按 `(persona, conversation_id)` 键），**不依赖** `UsageStore` 中的
    blocked 事件，也**不受**这些事件的容量淘汰影响。因此被拒请求的写放大不会提前解除
    冷却，`may_clear_at` 也不再由事件复算（先前「实现无状态、可由 blocked 事件复算」的
    说法已废弃）。
  - 冷却锚定**首次触发** `anti_spam.consecutive` 的时刻，冷却期内**重试不延长冷却**。
  - **冷却期内重复 blocked 只记一条事件**：同 `(persona, conversation_id)` 在一次冷却
    期间，`reason_code=anti_spam.cooldown` 的 blocked 事件只在首次写入一条，后续重试
    仍返回同样的 `200 blocked` 结论与 `may_clear_at`，但不再写事件，避免被拒请求挤占
    事件容量。
  - **`cooldown_seconds = 0`**：触发后冷却**立即结束**，计数起点取触发时刻；下一条请求
    重新从 0 起计（可再次发满 `max_consecutive_ai_messages` 条），不会出现
    `reset_at == now` 的活锁。即 0 秒冷却表示「不冷却，只做窗口外的连续计数」。
- **`message.length` 与平台硬上限区分**：平台固定的 `MAX_MESSAGE_CHARS` 上限在请求解析时
  直接拒绝，返回 `400 request.invalid`，**不走策略裁决**。只有 owner / peer 分层中配置的
  更严 `max_message_chars` 才走策略路径，返回 `200 blocked` + `reasons[].code = message.length`。
  两者严格区分，不改变状态码集合（§1.3）。
- **`on_break` 行为**：
  - `stop`（默认）：熔断触发即**拦截**，按上面 `blocked` 结论返回。
  - `notify`：**当前版本未实现通知通道**，按 `stop` 处理（fail-closed，先拦截再谈通知），
    即与 `stop` 行为一致。将来实现通知后，`notify` 仍会拦截，并**额外向所有者推送一条
    通知**（提醒人类介入），不会改成放行。
- **在途预留与 `deferred` 计入连续数**：与 B2.1 的 token 预扣一致，已发出但未入账
  （`queued` / `deferred`）的 AI 消息**计入**连续数，避免因排程而绕过熔断；
  `may_clear_at` 的计算同样把在途计入。**`deferred`（静默时段被顺延）的消息也计入
  `anti_spam` 连续数**：顺延不等于未发，仍算作一次连续 AI 消息（`kind=deferred`
  写入 `UsageStore`，与 `kind=sent` 同等对待）。
- **保留期淘汰（`USAGE_RETENTION_SECONDS`，默认 30 天）**：`UsageStore` 在每次
  `append` 时，**按服务时钟**（事件的 `recorded_at`，不读 `datetime.now()`）优先淘汰
  早于 `now - USAGE_RETENTION_SECONDS` 的旧事件，超出容量上限才按最旧 LRU 淘汰。
  因此：超过 30 天的事件不会被滑动窗口计入，行为一致；注入/回放时钟时边界不失真。
  部署须保证 `max_usage_events` 足以保留 `max(window_seconds, cooldown_seconds)` 内
  的所有事件；容量不足时事件可能被淘汰，导致计数偏低（fail-open，提前放行），属
  已知局限（冷却状态独立存储，不受此影响）。
  此外，部署**还须保证 `USAGE_RETENTION_SECONDS >= max(window_seconds, cooldown_seconds)`**
  （`usage_retention_seconds`）：保留期短于窗口/冷却时，窗口内事件会被保留期淘汰，
  同样导致计数偏低（fail-open，静默放行）。默认 30 天已覆盖所有可配窗口上限。
- **blocked 事件去重与容量淘汰（#117 B6）**：`(persona, conversation_id, reason_code)`
  的 blocked 事件**只记一条**；重试仍返回 `blocked` 回执，但不重复写 `kind=blocked`。
  成功 `delivered` 或出现经网关人类消息时清空该 (persona, conversation) 的去重记录。
  容量淘汰时**优先丢弃最旧的 `kind=blocked` 事件**，仍超才淘汰计数事件（`sent` /
  `deferred` / `human`），因此反复提交被拦请求**不能**挤掉计数、不能重置
  `rate` / `token` / `anti_spam` / `ai_loop` 的已用额度。

### 6.4 peer 计数语义与 B2.1 兼容性（B2.2）

B2.2 起 peer 层限额按 `(persona, peer_id)` 计数，即对每个对端角色单独计数。

- **计数维度**：peer 层的 `messages_per_window` / `tokens_per_window` / `min_interval`
  等窗口类限额，`used` 以 `(persona_id, peer_id)` 为键累加，来源是带 `peer_id` 的
  `UsageEvent`。
- **会话成员来源与校验范围（#117 审查修订）**：会话成员的真实来源（HTTP 会话注册 /
  Matrix 房间成员同步）**尚未接入**，当前只有网关内部登记（`register_conversation`，测试用）。
  因此校验按会话是否已登记分两种：
  - **未登记（或 `POST /policy/evaluate` 未带 `conversation_id`）**：按 **B2.1 行为**采信
    `peer_id`，**不校验成员**。带 `peer_id` 时 peer 层生效并按 `(persona, peer_id)` 计数；
    不带时 peer 层不生效。这样未登记会话的旧客户端与 B2.1 完全一致。
  - **已登记**：`peer_id` 须 ∈ 会话成员且 ≠ sender（否则 `422 request.invalid`）。
  - **`peer_id` 缺失强制（仅已登记会话）**：当已登记会话中**对方**（排除 sender 自身）存在
    配置了 peer 层限额的 persona、而请求（`OutgoingMessageRequest` 或
    `PolicyEvaluateRequest`）**未带 `peer_id`** 时，网关返回 `422 request.invalid`，
    `errors[].field = peer_id`（语义组合不合法，peer 层规则无法匹配）。
    此为 B2.2 对 peer 层绕过的修正，属 §5.3「收紧约束/条件必填」；0.x 例外。
    只有 sender 自己配置了 peer 层规则、对方没有时，省略 `peer_id` **不报错**。
    **无 peer 层规则**时省略 `peer_id` 的行为与 B2.1 完全相同（旧客户端无感）。
  - **空串 `peer_id`**：`""` 不是合法对端标识，实发与预检均返回 **`400 request.invalid`**
    （`errors[].field = peer_id`），不走策略路径（结构/类型错误，见 §6.6 错误边界）。
- **兼容条件**：会话成员中**无任何 peer 层规则**时，省略 `peer_id` 的行为与 B2.1 完全
  相同（peer 层不生效，计数仍为 persona 全局，旧客户端无感）。
  - 旧 `UsageEvent` 无 `peer_id` 字段的记录，只计入 **persona 全局**与 **platform / owner
    层**，**不计入任何 peer 层**计数（历史数据不回填）。
  - **platform / owner 层计数不变**：仍以 persona 全局为键，与 peer_id 无关。
- **`PolicyEvaluateRequest.peer_id` 与 `OutgoingMessageRequest.peer_id` 必须一致**：
  否则预检与实发结论可能不同（见 §6.1 与 OpenAPI 字段说明）。实发与预检的 `peer_id`
  由调用方保证一致；两者不一致不报错，但结论不可信（已在字段描述中标注）。带 `peer_id`
  时须为该会话的成员 persona 且**为对方 persona**（≠ sender_persona_id），否则 422
  （见上文「`peer_id` 缺失强制」）。

---

## 7. 安全与不可信内容

- 消息带 `trust`：`trusted` / `untrusted` / `system`。来自联邦、桥接、外部插件
  的内容为 `untrusted`；网关在交给模型前做隔离（B2.3）。App 展示时不得把
  `untrusted` 内容当指令，也不得据其渲染可执行操作。
- `PolicyReason.message` 只给面向调用方的原因，不含内部提示词或不可信内容原文。
- 卡片 `payload` / `context.metadata` 不得含密钥；服务端扫描后才投递。
- 本契约所有示例、占位均为 `<change-me>` 或 `example.invalid`，仓库不含任何真实凭据。

---

## 8. 校验与评审

### 8.1 本地校验

```bash
# 仓库根目录
python3 gateway/scripts/validate_openapi.py
```

检查项：YAML 合法（重复键报错）、OpenAPI 3.1 结构、`$ref` 全部可解析、
operationId 唯一、路径参数声明与必填、错误响应统一 `application/problem+json`、
bearer 占位、`info.license` 为 Apache-2.0、示例 JSON 逐个对 schema 校验、
占位扫描（契约与 `examples/*.json` 都扫描，疑似密钥即失败）、
`date-time` 必须是 UTC，**以 `Z` 结尾**；带 `+HH:MM` 偏移的写法视为失败（保证示例之间
不出现时区偏差）。

可选的外部 lint（需要网络，非 CI 必需）：

```bash
npx --yes @redocly/cli lint gateway/openapi/gateway.v1.yaml
```

### 8.2 C / D 线评审清单

请按此清单逐项确认，在本 issue 里回复「通过 / 需修改 + 原因」：

**C 线（App）**
- [ ] 功能清单能覆盖 C2.5 的「新模块不改 App 即可显示卡片」与 C3.2 的「未装模块无入口」
- [ ] 四种 `entry.type` 够用；未知入口按 `none` 的规则可实现
- [ ] 卡片 `blocks` 七种类型能表达现有设计稿；`fallback_text` 策略可接受
- [ ] 高风险授权的 assertion → `card.step_up_required` 流程可实现（C3.1：回执 <3s、防误触）
- [ ] `outcome: blocked` 作为 200 的语义可接受（不是 429）
- [ ] `revision` / `If-Match` 的并发模型够用
- [ ] 分页、时间格式、错误 `code` 处理方式无异议

**D 线（通知 / 开放 API）**
- [ ] 卡片 schema 能承载 D1.1 的通知数据模型；缺口明确列出
- [ ] 卡片操作回执 + `audit_id` 满足 D2.1 的审计、有效期、防重放需求
- [ ] `source.module` / `capability_id` 足以区分通知来源与外部插件（D2.5）
- [ ] `usage_events` 与小镇账单口径满足 D4.2 开放 API 文档
- [ ] 版本规则（只增不删、弃用流程）满足开放 API 对外承诺
- [ ] 本契约与未来 `docs/openapi/` 的对应关系清晰（D 线负责总文档）

### 8.3 已知未决事项（评审请关注）

1. 令牌签发/轮换与 scope 定义不在 v1，需在 B1.1 前后补。
2. 主动发言的调度与推送通道（`queued` 之后如何通知）待 D 线推送方案确定。
3. 关系策略（B2.8）的审批接口目前只在 `relation` 快照里体现，审批动作可能新增
   `/relations/*`，属新增接口（minor）。
4. 多 AI 群发言调度（B2.8）是否需要独立的 `/conversations/{id}/turn` 接口，待 M2 评估。
5. 小镇接口（A4.1/B4.2）未纳入本契约；按「新增接口」扩展。

---

## 附：文件清单

```
gateway/
├── openapi/
│   ├── gateway.v1.yaml              # 契约（唯一事实源）
│   └── examples/                    # 请求/响应示例，全部对 schema 校验（数量见脚本输出）
│       ├── health.json
│       ├── capability-manifest.json
│       ├── session-token.json
│       ├── persona.json
│       ├── create-persona-request.json
│       ├── persona-state.json
│       ├── conversation.json
│       ├── message.json
│       ├── send-message-request.json
│       ├── message-receipt.json
│       ├── message-receipt-deferred.json
│       ├── message-receipt-blocked.json
│       ├── message-receipt-queued.json
│       ├── policy-evaluate-request.json
│       ├── policy-decision.json
│       ├── effective-limits.json
│       ├── usage-summary.json
│       ├── card.json
│       ├── create-card-request.json
│       ├── card-action-request.json
│       ├── card-action-challenge.json
│       ├── card-action-receipt.json
│       ├── audit-record.json
│       └── problem.json
├── docs/
│   └── contract.md                  # 本文
├── scripts/
│   └── validate_openapi.py          # 离线校验
└── README.md                        # 如何运行校验
```
