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

> **D 线注意**：通知数据模型（D1.1）与授权回调签名（D2.1）会扩展 `/cards` 与
> `/cards/{card_id}/actions/{action_id}`。v1 已预留 `source.module`、`source.capability_id`、
> `context.metadata` 和 `audit_id`，扩展时按「只增不删」加字段即可，不需要升主版本。

---

## 1. 通用约定

### 1.1 基本形态

- 基础路径 `/v1`，资源名为复数，JSON 请求与响应（UTF-8）。
- 时间一律 **RFC 3339 / ISO 8601，带时区**（`2026-11-03T12:00:30Z` 或 `+08:00`）。
  只有「面向人的日历边界」（如每日主动发言重置）才额外带 `timezone` 字段，用 IANA 名称。
- ID 是不透明字符串，客户端不得解析其内部结构。
- 列表分页统一用 `cursor` + `limit`，响应带 `next_cursor` / `has_more`。
  `next_cursor` 为 `null` 表示到底；客户端不得构造游标。
- 未在 schema 中声明的字段，客户端**必须忽略**（前向兼容）；服务端对请求体
  用 `additionalProperties: false` 严格拒绝未知字段，避免拼写错误被静默吞掉。

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
- 模块网页的短时令牌由 `POST /capabilities/{capability_id}/session-tokens` 签发，
  不得放进 URL 查询串，应由 webview 以 `Authorization` 头发送。

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
| 401 | `auth.missing` / `auth.invalid_token` | 缺少或无效令牌 |
| 403 | `auth.insufficient_scope` / `persona.not_owned` / `relation.chat_denied` / `conversation.e2ee` / `card.step_up_required` | 身份有效但无权限 |
| 404 | `persona.not_found` / `conversation.not_found` / `capability.not_found` / `card.not_found` | 不存在或对调用方不可见 |
| 409 | `persona.in_use` / `conversation.exists_conflict` / `persona.revision_conflict` / `idempotency.conflict` / `card.already_decided` | 状态冲突 |
| 422 | `limit.out_of_range` / `card.invalid_block` / `card.missing_fallback_text` / `state.invalid_value` | 语义不合法 |
| 429 | `rate_limited.transport` | 传输层限流（**不是**发言额度） |
| 500 | `internal.error` | 网关内部错误 |

### 1.4 ⚠️ 策略结论不是 HTTP 错误

这是本契约最容易被误实现的一点，C 线请重点评审：

> **发言被限额拦截，返回 `200` + `outcome: "blocked"`，不是 `429`/`403`。**

理由：拦截是网关的**正常产品行为**，调用方（AI 运行时）需要结构化的原因、剩余额度、
重置时间来决定下一步（等待、改写、放弃），而不是把它当成异常重试。`401/403` 只表示
「你无权调用这个接口」，`429` 只表示「你请求太频繁」，两者与「这个 AI 此刻不该说话」
是不同的事。

```json
{ "outcome": "blocked", "retryable": true, "retry_after_seconds": 3600, "message": null,
  "decision": { "decision": "block", "allowed": false, "winner_layer": "platform",
                "reasons": [{ "code": "ai_loop.breaker_open", "layer": "platform",
                              "message": "AI-to-AI conversation hit the hourly circuit breaker." }],
                "checks": [ ... ] } }
```

`outcome` 取值：

| outcome | 含义 | 客户端行为 |
|---|---|---|
| `delivered` | 已投递到 Matrix | 用 `message` 与 `delivery` 更新本地 |
| `queued` | 已入队（如主动发言调度排到后面） | 等待推送，不要重发 |
| `deferred` | 延后到允许时段（如静默时段结束） | 按 `retry_after_seconds` 安排 |
| `blocked` | 本次不允许 | 读 `decision.reasons`，不要再重试同一内容 |

### 1.5 幂等

写操作支持 `Idempotency-Key`（8–128 字符，建议随机 UUID，重试时**复用同一个**）。
同一 key + 同一请求体返回同一结果；同一 key + 不同请求体返回 `409 idempotency.conflict`。
服务端建议保留 24 小时。`POST /messages`、卡片操作、创建类接口必须支持。

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
| POST | `/capabilities/{capability_id}/session-tokens` | 模块网页短时令牌 | App 内置浏览器 |
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
| GET/PUT | `/limits` | 三层限额读取与设置 | 我的 AI、主人设置 |
| GET | `/usage` | 用量汇总与预算 | 我的 AI、账单 |
| GET | `/usage/events` | 账单明细 | 审计、对账 |
| GET/POST | `/cards` | 卡片列表与创建 | App、通知/小镇模块 |
| GET/PATCH | `/cards/{card_id}` | 卡片读取与状态 | App |
| POST | `/cards/{card_id}/actions/{action_id}` | 卡片操作（授权） | App（C3.1） |

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
| `defaults.unknown_capability` | ❌ | 遇到未知能力 ID 时 `hidden`（忽略）或 `disabled`（显示为不可用） |
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
| `native` | `deep_link` | 打开 App 内置页面（`botville://…`） |
| `web` | `web_url` | 先取 session token，再用内置浏览器打开；令牌走请求头 |
| `card_list` | `card_types` | 在动态页渲染指定类型的卡片 |
| `none` | — | 保留能力但不显示入口（例如仅 API 可用的能力） |

**未知 `entry.type` 一律按 `none` 处理**：保留能力，忽略入口。这样新增入口类型
对旧 App 是安全的。

### 3.5 前向兼容规则（C 线必须遵守）

1. 未知 capability `id` → 按 `defaults.unknown_capability` 处理，不得崩溃。
2. 未知 `entry.type` → 视为 `none`。
3. 未知字段 → 忽略。
4. `status` 为 `disabled` 的能力 → 入口隐藏；`degraded` → 显示入口但展示 `unavailable_reason`。
5. `requires` 中任一能力不可用 → 本能力也不可用（客户端自行推导，服务端也会同步置状态）。
6. 拉取失败时用最后一次成功的缓存；无缓存时按「核心能力常驻」渲染（`chat.direct` 等
   `module: core` 且 `status: enabled` 的）。

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

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | ✅ | 卡片 ID |
| `card_schema_version` | ✅ | 卡片结构版本，本文为 `1.0.0` |
| `type` | ✅ | `notification`/`approval`/`moment`/`system`/`reminder`/`result` |
| `title` | ✅ | 标题 |
| `subtitle` | ❌ | 副标题（来源、时间等） |
| `fallback_text` | ✅ | **必填**。纯文本降级内容；旧客户端、未知类型、渲染失败时展示 |
| `priority` | ✅ | `urgent`/`high`/`normal`/`low` |
| `risk` | ✅ | `low`/`medium`/`high`。`high` 操作必须二次确认 + 生物识别 |
| `status` | ✅ | `active`/`read`/`dismissed`/`expired`/`acted` |
| `blocks[]` | ✅ | 内容块，见 4.3 |
| `actions[]` | ✅ | 操作，可为空数组 |
| `source` | ❌ | `{module, capability_id, actor}`，标明谁发的 |
| `context` | ❌ | `{conversation_id, deep_link, metadata}`，打开卡片时的上下文 |
| `i18n` | ❌ | 语言 → `{title, subtitle, fallback_text}` |
| `created_at` / `updated_at` | ✅ / ❌ | 时间 |
| `expires_at` / `acted_at` | ❌ | 过期与处理时间，可为 `null` |
| `revision` | ❌ | 乐观并发版本 |

### 4.3 内容块（`blocks[]`）

通用渲染器按 `type` 取字段，未知 `type` 跳过该块（不报错）。`level` 是统一的可选语义色：
`info`/`success`/`warning`/`danger`。

| `type` | 使用字段 | 渲染 |
|---|---|---|
| `text` | `text`, `level` | 段落文本 |
| `key_value` | `items[{label, value, emphasis}]` | 键值行，`emphasis` 加粗 |
| `divider` | — | 分隔线 |
| `image` | `url`, `alt`, `level` | 图片 |
| `progress` | `label`, `value`, `max`, `level` | 进度条（如「今日部署 2/5」） |
| `list` | `items[{label, value}]`, `level` | 列表 |
| `code` | `text`, `level` | 等宽代码块 |

约束：

- 块数量建议 ≤ 20；`text` 单块 ≤ 4000 字符。
- 不得在块中放 URL 作为可点击跳转——跳转一律用 `actions` 的 `open_url`，
  这样 App 能统一处理（用户确认、外部浏览器策略、审计）。
- 不得放 HTML/Markdown 原文。需要富文本时新增块类型，不要塞标记语言。

### 4.4 操作（`actions[]`）

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | ✅ | 卡片内唯一 |
| `label` | ✅ | 按钮文案 |
| `kind` | ✅ | `approve`/`deny`/`open_url`/`callback`/`dismiss` |
| `style` | ✅ | `primary`/`secondary`/`danger` |
| `url` | 条件 | `kind: open_url` 时必填 |
| `payload` | ❌ | 回传给来源模块的不透明数据；**不得含密钥或个人敏感信息** |
| `confirmation` | ❌ | `{required, title, body, biometric}`；高风险必须 `biometric: true` |
| `disabled_reason` | ❌ | 不可点击原因 |

**高风险操作流程（C3.1 验收点）**：

1. `risk: high` 的操作必须带 `confirmation.required: true` 且 `biometric: true`。
2. App 弹确认页 → Face ID 通过 → 拿到短时 `assertion`。
3. `POST /cards/{card_id}/actions/{action_id}` 带上 `assertion` 与新的 `Idempotency-Key`。
4. 网关校验：断言有效、卡片未过期、操作未被处理过、调用者有 scope。
5. 失败码：`card.step_up_required`（缺/过期断言）、`card.expired`、`card.already_decided`、
   `auth.insufficient_scope`。成功返回 `CardActionReceipt`（含 `audit_id` 与更新后的卡片）。

断言本身也是占位：示例只出现 `<change-me>`，真实断言格式由 D2.1 定义。

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
      "payload": { "run_id": "run_demo" },
      "confirmation": { "required": true, "title": "确认批准生产部署",
                        "body": "将把 release/2026.11 部署到生产，10 分钟内可回滚。",
                        "biometric": true } },
    { "id": "deny", "label": "拒绝", "kind": "deny", "style": "danger" },
    { "id": "open", "label": "查看日志", "kind": "open_url", "style": "secondary",
      "url": "https://gateway.example.invalid/modules/ci/runs/run_demo" }
  ],
  "source": { "module": "notify", "capability_id": "notify.approvals",
              "actor": { "id": "persona_ci", "kind": "ai", "display_name": "代码助手" } },
  "context": { "conversation_id": "conv_ci", "deep_link": "botville://cards/card_demo",
               "metadata": { "run_id": "run_demo" } },
  "created_at": "2026-11-03T12:05:00Z",
  "updated_at": "2026-11-03T12:05:00Z",
  "expires_at": "2026-11-03T13:05:00Z",
  "acted_at": null,
  "revision": 1
}
```

其他示例：通知/提醒卡片见 [`examples/create-card-request.json`](../openapi/examples/create-card-request.json)，
操作回执见 [`examples/card-action-receipt.json`](../openapi/examples/card-action-receipt.json)。

### 4.6 校验规则（服务端必须做）

1. `fallback_text` 非空；缺失返回 `422 card.missing_fallback_text`。
2. `blocks[]` 的 `type` 必须已知；未知类型拒绝（`422 card.invalid_block`）。
   ——新增块类型要先升 `card_schema_version`。
3. `kind: open_url` 必须有 `url`，且为 `https`（或明确列入白名单的 scheme）。
4. `risk: high` 的操作必须带生物识别确认。
5. `payload`、`context.metadata` 做密钥与个人信息扫描；命中拒绝。
6. 卡片数量与推送频率受平台防刷屏约束（例如每人每小时 ≤ 20 张 `urgent`）。

### 4.7 向前兼容

- App 遇到未知 `type` → 用 `fallback_text` 降级，不崩溃。
- App 遇到未知块 → 跳过。
- App 遇到未知字段 → 忽略。
- 新增块类型/枚举值 = minor（`1.0.0 → 1.1.0`）；改变已有字段语义或删除 = major，需新契约版本。

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

`0.x` 阶段（M0–M1）允许在同一路径上做破坏性调整，但**每次都必须**同步改
`info.version`、更新示例、并在 PR 描述里写明「0.x 例外」。`1.0.0` 之后严格执行 5.1。

### 5.3 变更等级

| 变更 | 示例 | 等级 | 契约版本 |
|---|---|---|---|
| 新增可选请求字段 | `POST /messages` 加 `metadata` | 非破坏 | minor |
| 新增响应字段 | 会话加 `last_activity_at` | 非破坏 | minor |
| 新增接口 | `GET /usage/events` | 非破坏 | minor |
| 新增枚举值 | `card.type` 加 `digest` | 非破坏* | minor |
| 新增必填响应字段 | — | **破坏** | major + 新路径 |
| 新增必填请求字段 | 给 `POST /messages` 加必填字段 | **破坏** | major |
| 删除/重命名字段 | — | **破坏** | major |
| 收紧约束 | `maxLength` 8000→4000、`enum` 减项 | **破坏** | major |
| 改默认值 | `limit` 默认 20→50 | 准破坏 | minor + 公告 |
| 改错误码语义 | — | **破坏** | major |

\* 新增枚举值对严格的旧客户端可能造成「未知值」——因此契约要求客户端对未知枚举值
按「未知」处理而非报错；仍应在 changelog 公告。

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
| 0.1.0 | 2026-11-03 | 初稿：健康、功能清单、角色与状态、会话、消息与发言裁决、策略预检、三层限额、用量与账单、卡片与授权操作。评审：C/D 线（B0.1） |

新增条目追加在本表顶部；同一版本内的编辑合并到该版本行。

---

## 6. 限额与用量（供 C/D 线理解，B2.1 实现）

### 6.1 三层限额

```
        ┌──────────────────────────────┐
        │  平台底线  platform（只读）    │  部署配置，不可经 API 修改
        ├──────────────────────────────┤
        │  主人设置  owner              │  AI 主人对自己角色/会话设的限制
        ├──────────────────────────────┤
        │  对方设置  peer               │  对方角色对「跟我说话」设的限制
        └──────────────────────────────┘
                    ↓ 逐项取最严
              生效限额 effective
```

- 「取最严」是**逐项**的，不是整层覆盖：消息数可能由 owner 胜出，token 可能由
  platform 胜出。`EffectiveLimit.source_layer` 指明每一项的胜出层。
- 静默时段、防刷屏、互聊熔断也各自参与，`checks[]` 里逐项给出 `allowed`/`limit`/`used`/`remaining`/`reset_at`。
- `PolicyCheck.key` 推荐取值：`messages.window`、`tokens.window`、`proactive.per_day`、
  `town.tokens_per_day`、`silent_hours`、`min_interval`、`anti_spam.consecutive`、
  `ai_loop.per_hour`、`message.length`、`relation.chat_allowed`。

### 6.2 预算与账单

- 预算以 token 计，窗口默认 24h（`window_seconds`）。
- 账单是 append-only 的 `usage_events`，`kind` 覆盖模型调用、发送、拦截、主动发言、
  小镇行为、图像生成。
- **小镇消耗单列**（`town_tokens`、`kind: town_action`）：小镇行为受 `town_tokens_per_day`
  约束，预算用尽后只走动不聊天（B4.2）。D 线做账目展示时请把它与聊天分开。

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
占位扫描（疑似密钥即失败）。

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
│   └── examples/                    # 18 个示例，全部对 schema 校验
│       ├── health.json
│       ├── capability-manifest.json
│       ├── persona.json
│       ├── create-persona-request.json
│       ├── persona-state.json
│       ├── conversation.json
│       ├── message.json
│       ├── send-message-request.json
│       ├── message-receipt.json
│       ├── message-receipt-deferred.json
│       ├── policy-evaluate-request.json
│       ├── policy-decision.json
│       ├── effective-limits.json
│       ├── usage-summary.json
│       ├── card.json
│       ├── create-card-request.json
│       ├── card-action-receipt.json
│       └── problem.json
├── docs/
│   └── contract.md                  # 本文
├── scripts/
│   └── validate_openapi.py          # 离线校验
└── README.md                        # 如何运行校验
```
