# 开源通知数据模型（D1.1）

> License: Apache-2.0（与仓库一致）。本文是通知域数据模型的人类可读规范，
> 与 `notify/openapi/notify.v1.yaml`、notify/openapi/examples/ 下的示例保持一致，
> 三者一起作为 D1.1 的 Schema 评审对象。

## 1. 范围与边界

### 1.1 本模型管什么

- **通知卡片（Notification/Card）**的结构语义：`type`、`priority`、`risk`、
  `blocks[]`、`actions[]`、`fallback_text` 等，wire 形态复用 B0.1 网关契约的
  `Card` / `CreateCardRequest`（`gateway/openapi/gateway.v1.yaml`，行为描述见
  `gateway/docs/contract.md` §4）。
- **可靠投递语义**：`delivery[]` 四态状态机、确认失败重试与模糊结果不重放规则。
- **静默、优先级与合并**：`deliver_after` / `scheduled_at`、`interrupt`、
  `collapse_key` / `group_id` 的语义表。
- **授权卡片衔接**：approval 卡的必填约束与授权回调链（challenge / assertion /
  revision / audit_id）在通知模型中的引用关系。
- **生产者约定**：能力 ID、事件 ID 与去重键的取名规则；wire 时间的 UTC 约定。

### 1.2 本模型不管什么

- **私人渠道实现**：任何具体推送厂商 SDK、个人消息渠道、个人部署的桥接实现。
  渠道只抽象到 `push` / `in_app` / `email` / `webhook` **开放枚举**
  （`CardDelivery.channel`，未知值照常显示状态）。
- **中枢迁移与并行期**：D1.2（并行期双发与对账）已按所有者决定搁置归档
  （见 `docs/migration/deferred.md`），本模型**不依赖**它，也不推进它。
- **签名算法实现**：assertion 的字节编码与算法细节属 D2.1；本模型只固定
  需要绑定的字段。
- **通知插件实现**：外部通知插件接口属 D2.5；本模型只保证字段层可扩展。

### 1.3 与 B0.1 的关系

- 对接网关 `POST /cards` / `GET /cards` / `GET /cards/{card_id}`；
  D1.1 **不改写** B0.1 契约，只做文档化、类型化、示例化。
- `notify/openapi/notify.v1.yaml` 以 `$ref` 引用网关 schema，
  以 `allOf` **只增**补充通知域语义；网关校验
  （`python3 gateway/scripts/validate_openapi.py`）不受影响。
- 后续 D2.1（授权回调签名）与 D2.5（通知插件）在本模型的字段之上扩展。

### 1.4 开源语义来源

本模型吸收 `docs/migration/D0.1-hub-inventory.md` 中 F01（统一完成通知）、
F03（可靠投递：确认失败重试、模糊尝试绝不重放）、F06（一次性定时提醒）、
F15（webhook 出口的开放接口语义）的**开源替代语义**；仅取语义，不引入任何
中枢路径、配置值或部署细节。

### 1.5 与 B0.1 缺口清单（已知分歧与归属）

通知域相对 B0.1 契约的已知缺口/分歧（含已关闭记录），评审对齐用：

| # | 项 | 现状与归属 |
|---|---|---|
| 1 | 响应侧无 `scheduled_at` | **已定**（所有者 2026-11-10 拍板，[contract-gaps.md](contract-gaps.md) B9）：暂不改 wire；客户端用 `deliver_after`（及可选 `context.metadata`）展示。若日后必须区分「期望触发时刻」与「最早投递时刻」，再走网关 minor 给 `Card` 增加可选 `scheduled_at`。 |
| 2 | `DeliveryRetryPolicy` / `DeliveryFailureCode` 未上 wire | **已定**（所有者 2026-11-10 拍板，[contract-gaps.md](contract-gaps.md) B10）：保持实现侧建议值（总尝试 ≤ 3 含首次、间隔 ≥ 300 秒、仅 `failed`），不上卡片 wire。 |
| 3 | approval 回调签名 | 绑定字段已定。字节编码与设备密钥注册接口为**已定方向、待 D2.1 专门 PR 落地**（不在本文改网关 YAML），见 [contract-gaps.md](contract-gaps.md) A6、B4 与下文 §2.6。 |
| 4 | gateway `CreateCardRequest.description` 静默检查一句 | **已关闭**（[#74](https://github.com/zjyjcl/botville/issues/74)，合入 #76）：gateway YAML 已与 contract.md §4.8 对齐——`urgent` 默认穿透静默，显式 `interrupt: false` 时连 `urgent` 也不穿透；`interrupt` 无 `default`。 |

D 线契约缺口清单见 [contract-gaps.md](contract-gaps.md)。B1–B10 已于 2026-11-10
由所有者按推荐拍板，定论写在本文对应章节；A6 与 B4 为已定方向、待 D2.1
专门 PR 落地（本文只记录方向，不改网关 YAML）。

## 2. 核心对象

### 2.1 Card 与 type 语义

一张卡片 = 结构化数据，不是 HTML（contract §4.1）。通知域使用
`type` 开放枚举中的五个值（未知 type → 只用 `fallback_text` 降级渲染）：

| type | 语义 | 典型场景 |
|---|---|---|
| `notification` | 普通通知 | 状态变更、构建结果、新消息摘要 |
| `approval` | 授权卡 | 需要所有者拍板的操作（见 2.6） |
| `reminder` | 提醒卡 | 定时触发的一次性提醒（见 2.5） |
| `result` | 任务结果 | AI 任务/回合的完成结果 |
| `system` | 系统通知 | 网关/应用级公告、维护窗口 |

（网关 `Card.type` 另有 `moment` 取值，属社交域，不在通知域范围。）

### 2.2 Source：去重键与幂等

`source` 必填，`source.event_id` 与 `source.dedupe_key` **二选一必填**，
`source.capability_id` **必填**（去重元组的第一分量，contract §4.6 规则 8）：

- 有稳定事件 ID 时用 `event_id`，通知域约定（MUST）：`evt_<功能>_<语义slug>`，
  小写字母/数字/`._-`，≤ 128 字符（见 `NotifyEventId`）。
- 没有稳定事件 ID 时（桥接、外部 webhook、轮询结果）用 `dedupe_key`，
  通知域约定（MUST）：≤ 192 字符，可含 `:`（见 `NotifyDedupeKey`）。
- 两个都不给 → `422 card.missing_event_id`。

> 三个来源键在 notify 层由 `NotificationSource` 以 pattern / 枚举**硬约束**
> （MUST）：这是对 gateway 开放 string 的**收敛**，跨通知域生产者统一键空间；
> 网关 schema 本身保持不变。

服务端按 **`(capability_id, event_id ?? dedupe_key)`** 去重：重复创建返回已有卡片
（`200`，首次创建 `201`），不重复投递。生产者重启、多实例、多渠道重放都安全。

**业务级去重 ≠ HTTP 幂等**：

| 机制 | 层 | 管什么 | 有效期 |
|---|---|---|---|
| `Idempotency-Key` | 传输层 | 同一个 HTTP 请求被网络重试 | 24h 缓存 |
| `event_id` / `dedupe_key` | 业务层 | 同一**事件**被重新发起（生产者重启、重放） | 事件生命周期 |

二者互补，不能互相替代。

### 2.3 Delivery：投递状态

`delivery[]` 表达各渠道投递状态，字段与状态机见第 3 章。要点：

- `channel` 开放枚举：`push` / `in_app` / `email` / `webhook`。
- `state` 开放枚举：`pending` / `confirmed` / `unconfirmed` / `failed`
  （未知 state 按 `unconfirmed` 处理，不重放该渠道）。
- `attempts` 只增；`last_attempt_at`、`failure_code`、`provider_message_id`
  用于排查与对账。

### 2.4 Merge / collapse

| 字段 | 语义 |
|---|---|
| `collapse_key` | 合并键；同键的**未读**卡片由客户端折叠（新卡替换旧卡的展示） |
| `group_id` | 合并展示组 ID；同组卡片折叠为一条摘要入口 |

折叠只影响**展示**，不删除卡片、不改变 `Card.status`。摘要场景示例见
`examples/digest-collapsed.json`。

### 2.5 Quiet / priority / interrupt

`POST /cards` 参与静默检查（contract §4.8，此处引用不重复定义）：

| 字段 | 语义 |
|---|---|
| `deliver_after` | 服务端最早投递时间；`null` 表示立即（仍受静默检查） |
| `scheduled_at` | 期望展示时间（提醒触发时刻），创建时与 `deliver_after` 二选一 |
| `interrupt` | 是否穿透静默立即打断；**无默认值**（三态：省略 / `true` / `false`），省略时按 `priority` 决定 |

`priority` → 打断级别：

| priority | 静默时段内 | 打断级别 |
|---|---|---|
| `urgent` | 默认穿透静默（等效 `interrupt: true`） | 立即通知，可响铃 |
| `high` | 延后，但置顶 | 静默结束后优先 |
| `normal` | 延后 | 常规 |
| `low` | 延后 | 静默/摘要里出现 |

`interrupt` 是三态字段，与 contract.md §4.8 一致，**不得补默认值**：

- 省略时由 `priority` 决定：`urgent` → 穿透；其余优先级 → 顺延。
- 显式 `true` → 无论优先级都穿透。
- 显式 `false` → 即使 `priority: urgent` 也不打断，静默期间顺延到 `deliver_after`。

静默时段规则本体（`SilentHours`：start/end/timezone，跨午夜合法）属网关
B 线配置，通知域只引用其 timezone 语义，见第 5 章。

### 2.6 授权卡片衔接

approval 卡在通知模型中的约束：

- 创建时 `expires_at` **必填**（contract §4.6 规则 9：approval 或有效
  risk=high 必填）；notify 层在 `ApprovalCreateRequest` 以 schema
  `required: [expires_at]` 强制。过期卡片 `status: expired` 且不可再操作。
- `actions[]` 至少包含一个决策操作（`kind: approve` / `deny`，或模块自定义的
  `callback`）。决策操作是开放枚举、无法在 JSON Schema 穷举，属**运行时校验**
  （schema description 与本文档同步标注）。有效风险 = `action.risk ?? card.risk`；
  有效 risk=high 时 App 强制二次确认 + 生物识别。
- 授权回调引用链（字段层固定，算法细节属 D2.1）：

```
POST /cards/{id}/actions/{action_id}/challenge
  → CardActionChallenge {challenge, expires_at, card_id, action_id, revision, algorithm}
App 用设备密钥对 card_id + action_id + revision + challenge 签名 → assertion
POST /cards/{id}/actions/{action_id}  {assertion, challenge, revision, Idempotency-Key}
  → CardActionReceipt {audit_id, actor, auth_method, revision_before/after, card}
GET /audit/{audit_id} → AuditRecord（append-only 审计）
```

`revision` 贯穿全程用于防 TOCTOU；`audit_id` 是通知模型引用审计记录的
唯一入口；回执中的 `card` 反映拍板后的最新状态。

通知域 **不**规定全局业务 `payload` / `result` schema，二者结构都由发出该卡
的模块定义。`payload` 与 `context.metadata` 适用既有禁令（不得含密钥或个人
敏感信息，服务端扫描、命中拒绝，contract §4.4 / §4.6 规则 5）。回执/审计中的
`result` 同样**建议**不要放密钥或个人敏感信息，但网关目前没有对应扫描规则，
责任在来源模块与 D2.1，本文不把「命中则拒绝」扩到 `result`。

有效 risk=high 时，设备上生物识别不可用必须 **fail-closed**（所有者
2026-11-10 拍板，[contract-gaps.md](contract-gaps.md) B3）：不降级为仅点按
或密码代替签名，不提交操作；提示改用已登记生物识别的设备，或取消。

断言字节编码与设备密钥注册为**已定方向、待 D2.1 专门 PR 落地**（本次不改
网关 YAML，[contract-gaps.md](contract-gaps.md) A6、B4）：

- 签名输入为四个绑定字段的规范 JSON 对象（键序固定为 `card_id`、
  `action_id`、`revision`、`challenge`；`revision` 为 JSON 数字，字符串
  按 UTF-8，无无意义空白），整体 UTF-8 编码后签名。
- `assertion` 为 **base64url（无填充）**，内容是 P-256 ECDSA 的 raw
  `r || s`（各 32 字节，共 64 字节）；算法标识继续用已有 `p256`。
- 设备密钥：用网关 minor 增加「注册 / 列举 / 吊销本所有者设备签名公钥」
  接口；每台设备各自注册，吊销后该设备 assertion 立即失败；密钥物料不得
  进仓库或日志。接口路径与请求体由 D2.1 PR 写入 gateway 契约。

### 2.7 列表展示的默认映射（不新增 API）

通知列表与详情复用 `Card` 与 `GET /cards`（基路径 `/v1`，含 `updated_since`），
**不**另建通知对象或通知列表接口。未读与折叠可由既有 wire 语义推出；分类、
排序与偏好默认由所有者于 2026-11-10 拍板（[contract-gaps.md](contract-gaps.md)
B2）。下列均为客户端展示约定，不新增 wire 字段；`GET /cards` 仍只规定过滤、
不规定服务端排序。

| 展示问题 | 默认映射 |
|---|---|
| 未读 | `Card.status == active` 计为未读；`read` / `dismissed` / `expired` / `acted` 不计。未读与 `delivery[].state` 无关。 |
| 折叠 | 同 `group_id` 收成一条摘要入口；同 `collapse_key` 的未读卡由新卡替换旧卡展示（见 §2.4）。折叠只影响展示，不改 `Card.status`。 |
| 分类 | 按 `type` 与 `source.capability_id` 分组；不另作分类枚举。 |
| 排序 | 客户端按 `priority` 序（`urgent > high > normal > low`），同级按 `updated_at` 降序。 |
| 偏好 | M2/M3 **不**做独立通知偏好 API。系统级打扰交给操作系统通知设置与已有 `SilentHours`。若日后要做每能力开关，再走网关 minor。 |

### 2.8 评论事件到通知卡（已定）

所有者 2026-11-10 拍板（[contract-gaps.md](contract-gaps.md) B1）。不新增通知
对象或专用关联字段：

- 仅当评论对接收者**可见之后**才创建卡片（遵守动态可见性，不可见者不通知）。
- `type=notification`，`source.capability_id` 使用已有 `notify.inbox`
  （不必为此先新增 `notify.moments`）。若社交域日后希望能力归 moments 模块，
  再单开网关 minor 增加能力 ID。
- 关联放在已有 `context.metadata`（如 `moment_id` / `comment_id`）与
  `context.deep_link`；推送打开走该 deep link，由 C 线映射到动态详情。

### 2.9 与消息推送的边界（已定）

所有者 2026-11-10 拍板（[contract-gaps.md](contract-gaps.md) B5），回应
`contract.md` §8.3 第 2 条：

- **不另建**一条 D 线推送通道。会话消息的到达继续走 Matrix 推送（A1.2
  Sygnal + App NSE）。
- `queued` / `deferred` 的客户端行为不变：拿 `queue_id` 等待，不自行重试。
- 仅当需要可操作的应用内卡片（授权、结果、提醒）时，再按本模型创建 `Card`。

## 3. 投递状态机

### 3.1 状态与迁移

```
                 ┌─ confirmed（渠道回执确认送达）
  pending ───────┤
  ▲  │           ├─ unconfirmed（模糊：超时 / 回执缺失，无法确认）
  │  │           └─ failed（确认失败：渠道明确拒绝，如 token 失效、地址退信）
  │  │
  └──┴─ 同渠道重试：failed → pending（attempts+1；总尝试 ≤3 含首次，间隔 ≥5 分钟）
```

重试是状态机的一条边：重试时 `state` 从 `failed` 回到 `pending`，`attempts`
只增（+1），与 §3.2 的重试规则自洽。

| state | 语义 | 后续允许 |
|---|---|---|
| `pending` | 已受理、尚未尝试、等待排程，或重试后重新排队 | 进入任意终态 |
| `confirmed` | 渠道确认送达 | 终态 |
| `unconfirmed` | 模糊结果，无法确认送达与否 | **同渠道不得重放**；可换渠道或由所有者主动触发 |
| `failed` | 确认失败 | 同渠道可按 3.2 重试（回到 `pending`，`attempts+1`）；也可换渠道 |

### 3.2 重试规则（规范建议值）

- **计数口径**：**同渠道总尝试 ≤ 3（含首次）**，即**可重试 ≤ 2 次**；
  相邻尝试间隔 ≥ 5 分钟。notify.v1.yaml 的 `DeliveryRetryPolicy.max_attempts`
  即总尝试上限（`maximum: 3`），可重试次数 = `max_attempts − 1`。
  示例 `card-delivery-states.json` 中 email 渠道 `attempts: 3` 即
  「首次 + 2 次重试」达到上限。
- **只对 `failed` 重试**：确认失败可**同渠道**重试；重试时按 §3.1 状态机
  从 `failed` 回到 `pending`（`attempts+1`）。
- **模糊结果绝不重放**：`unconfirmed`（超时、回执缺失）不得在同一渠道重放——
  宁可漏一次，也不重复打扰。要补投必须**换渠道**，或由**所有者**在 App 内主动
  触发重新发送。
- 超过重试上限仍 `failed` → 保持 `failed` 并保留 `failure_code`，等待
  所有者处理或渠道修复。

### 3.3 所有者生命周期 vs 渠道投递

两者严格分离，互不覆盖。术语约定：本文统一用「**所有者**」（owner，卡片的
接收方与处理者）；如个别处出现「用户」，仅为「接收方」的泛指，语义同「所有者」。

| | `Card.status` | `delivery[].state` |
|---|---|---|
| 回答 | 所有者对卡片做了什么 | 渠道是否真的送达 |
| 取值 | `active` / `read` / `dismissed` / `expired` / `acted` | `pending` / `confirmed` / `unconfirmed` / `failed` |
| 变化方 | 所有者操作、过期、授权拍板 | 渠道投递与回执 |

例如：所有者在 App 内直接处理了卡片（`status: acted`），`push` 渠道仍可能
`unconfirmed`；反之推送已 `confirmed`，卡片仍可 `active` 未读。

## 4. 事件类型与 capability 约定

通知域能力 ID 约定为点分小写 `notify.<功能>`（开放枚举，新增能力走网关
manifest 的 minor 升级）：

| capability_id | 用途 |
|---|---|
| `notify.approvals` | 授权卡（approval） |
| `notify.inbox` | 收件/完成通知类卡片 |
| `notify.reminders` | 一次性提醒（reminder） |
| `notify.digest` | 订阅式摘要/晨报类合并通知 |

生产者选择键的指引：

1. 事件有自然稳定 ID（任务 ID、构建号、回合 ID）→ `event_id`，
   如 `evt_reminders_standup_20261104`。
2. 桥接外部来源没有稳定 ID → `dedupe_key`，把外部消息 ID、内容哈希等
   编码进去，**同一业务事实必须复用同一键**。
3. 同一 `capability_id` 内键空间独立；不同 capability 的键即使相同也互不冲突。

### 4.1 完成事件与派工授权（已定方向，接口细节随 D2.2）

所有者 2026-11-10 拍板（[contract-gaps.md](contract-gaps.md) B7）。开源侧
App 内闭环（不涉及已搁置的中枢迁移）：

- 完成/失败事件 → `type=notification` + `notify.inbox`。
- 需要所有者批准的派工 → `type=approval` + `notify.approvals`，走既有
  challenge / assertion 链。
- `payload` 只放不透明业务句柄（如 `run_id`），不设全局派工 schema（见 §2.6）。
- 订阅方式（事件推入优先，轮询仅作兜底）在 D2.2 实现 PR 中写明。

### 4.2 桥接能力 ID（已定，属 `bridge` 模块）

所有者 2026-11-10 拍板（[contract-gaps.md](contract-gaps.md) B8）。下列 ID
预留给桥接模块，**不**进入 `notify.*` 枚举：`bridge.feishu`、
`bridge.telegram`、`bridge.qq`。开源默认支持飞书与 Telegram；QQ/NapCat 为
可选桥，须直连、不走境外代理（验收含代理检查）。私人渠道实现不进开源仓库。
先做进/出站消息与 Matrix 会话映射；只有需要拍板或摘要时才创建 `Card`。
入口映射的实现细节随 D2.3。

### 4.3 外部通知插件（已定方向，接口细节随 D2.5）

所有者 2026-11-10 拍板（[contract-gaps.md](contract-gaps.md) B6）。v1 只做
出站 webhook：对已投递或状态变化的 `Card` 快照 POST 到所有者配置的 URL。
鉴权使用部署侧密钥**引用**（真实密钥不得写入仓库或开源示例）。重试遵循
§3.2（仅 `failed` 可同渠道重试，`unconfirmed` 不重放）。示例插件保持中性，
不含具体私人渠道；入站私人渠道不进开源仓库。`source.module` /
`capability_id` 足以区分来源，注册与鉴权的请求体由 D2.5 PR 补充。

## 5. 时区与时间

- **wire 一律 UTC**：所有 `date-time` 字段为 RFC 3339、带 `Z` 后缀
  （如 `2026-11-04T12:00:00Z`）；不允许 `+08:00` 这类偏移。
- **展示时区**不进 wire 时间：`Card` 侧放 `context.metadata`（如
  `display_timezone`），静默时段配置放 `SilentHours.timezone`；
  客户端据此本地化渲染。

## 6. OpenAPI 纳入方式

- `notify/openapi/notify.v1.yaml`：
  - 以只读 `$ref` 聚合网关的 `Card*` / `Actor` / `SilentHours` 等 schema，
    作为通知域评审的单一入口；
  - 以 `allOf` **只增**定义 `NotificationCard` / `ReminderCard` /
    `ApprovalCard` / `*CreateRequest`、`NotifyCapabilityId` / `NotifyEventId` /
    `NotifyDedupeKey` / `DeliveryFailureCode` / `DeliveryRetryPolicy`；
  - 不定义新接口（`paths: {}`），不改网关契约。
- 本任务未对 `gateway/openapi/gateway.v1.yaml` 做任何改动；
  网关校验保持绿。

## 7. 示例与校验

`notify/openapi/examples/`：

| 文件 | 覆盖点 |
|---|---|
| `notification-card.json` | 普通通知卡（完整 Card 响应，in_app confirmed） |
| `reminder-create-request.json` | 提醒卡创建请求（`scheduled_at`、collapse） |
| `approval-create-request.json` | 授权卡创建请求（`expires_at`、action risk、challenge 前置字段） |
| `bridge-create-request.json` | 桥接/webhook 创建请求（仅 `dedupe_key`、无 `event_id`，中性名） |
| `card-delivery-states.json` | 四渠道四态 `delivery[]`（confirmed / pending / failed / unconfirmed） |
| `digest-collapsed.json` | 合并摘要场景（`collapse_key` + `group_id`） |
| `delivery-retry-policy.json` | 重试策略对象（总尝试 ≤3 含首次、间隔 ≥300s、仅 failed） |

校验命令（脚本校验 OpenAPI 结构与示例，**不解析本文档**）：

```bash
python3 notify/scripts/validate_examples.py   # notify OpenAPI + 7 个示例（交叉校验独立重读 gateway.v1.yaml）
python3 gateway/scripts/validate_openapi.py   # 网关契约（D1.1 未改动，保持绿）
```

所有示例 date-time 均为 UTC `Z`，无真实密钥，显示名用中性词。

## 8. 非目标

- 不实现通知发送服务 / 推送 SDK 对接（消息推送边界见 §2.9）。
- 不做 D1.2（并行期）的任何内容。
- D2.1 的 YAML/实现、D2.2 / D2.3 / D2.5 的接口细节与实现不在本文展开；
  已定方向见 §2.6、§4.1–4.3。
- 不含中枢路径、私人渠道实现、私有部署细节。
