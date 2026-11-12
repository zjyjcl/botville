# gateway/ — AI 网关与角色引擎（B 线，Claude Code）

- 网关契约与 OpenAPI 初稿、完整接入 SDK、简单接入角色引擎
- 三层限额与 token 预算、防刷屏与 AI 互聊熔断（B2.2）、不可信内容隔离（防注入）
- 心情、需求驱动、主动发言；四层记忆；默认性格人设模板
- AI 关系策略与多 AI 群发言调度
- 通知多口吻改写；中枢合并/静默/续办逻辑
- 小镇行为：规则作息、社交判断、事件联动、素材生成管线

## 目录 / Layout

| 路径 | 内容 |
|---|---|
| `openapi/gateway.v1.yaml` | 网关契约（OpenAPI 3.1，唯一事实源） |
| `openapi/examples/` | 26 个请求/响应示例，全部对 schema 校验 |
| `docs/contract.md` | 人类可读契约说明：功能清单规范、卡片描述规范、版本规则、评审清单 |
| `scripts/validate_openapi.py` | 离线校验脚本（文档结构 + 示例 schema） |
| `scripts/coverage_check.py` | 零依赖行覆盖率脚本（`sys.settrace`，阈值默认 80%） |
| `src/botville_gateway/` | **B2.1 网关实现 + A1.3 appservice 传输**（见下节） |
| `tests/` | 单元/集成测试（stdlib `unittest`，无第三方依赖） |
| `pyproject.toml` | 打包元数据；**运行时零依赖**（仅标准库） |
| `.env.example` | 环境变量占位（不含真实密钥） |

---

## B2.1 网关实现 / Gateway

> 任务：Issue #15「超限拦截并记录」+ Issue #17「三层限额与 token 预算」；
> A1.3 起新增 appservice 传输（真实 homeserver 代发）。
> 收发、三层限额（platform/owner/peer）、token 预算、深夜静默、用量账单；
> 默认仍是内存假后端，appservice 模式下对接真实 homeserver。

### 包结构

```
gateway/src/botville_gateway/
├── __init__.py          导出 Config / LimitStore / PolicyEngine / GatewayService 等
├── __main__.py          python -m botville_gateway 本地启动（含启动健康校验）
├── as_txn_store.py      AS 事务存储：持久化、幂等、保留期清理（SQLite WAL，B 线）
├── caller_identity.py   调用方身份（caller token / legacy token，#124 §1.2.2）
├── config.py            环境变量 → 不可变 Config（构造时校验）
├── errors.py            异常类（ValidationError / LimitError / IdempotencyConflict）
├── isolation.py         §7.2 上下文四区隔离（nonce 包裹，B2.3）
├── limits.py            三层限额存储、规则校验、逐项取最严（EffectiveLimits）
├── matrix.py            MatrixTransport Protocol + InMemoryMatrixTransport
├── matrix_appservice.py AppServiceMatrixTransport（AS token masquerade，A1.3）
├── policy.py            三层限速（消息/token/间隔）+ 按日预算 + 静默 → PolicyDecision
├── service.py           send_ai_message() 编排、幂等、/limits 与 /usage 查询
├── store.py             内存 UsageEvent 日志（append-only、容量有界、不存正文）
├── http_server.py       stdlib HTTP + build_transport 工厂（/health、/policy、/messages、/limits、/usage、/_matrix/app/v1/*）
├── moments/             动态模块（存储 / 可见性 / HTTP / 迁移，A2.2）
├── timeutil.py          UTC `Z` 结尾的 RFC 3339 格式化
├── trust.py             §7.1 trust 判定规则（B2.3）
└── user_identity.py     终端用户身份校验（MatrixOpenID / Matrix Bearer，#124 §1.2.3）
```

**包位置**：`gateway/src/botville_gateway/`（src-layout）。测试通过 `sys.path` 注入
`gateway/src`，无需 `pip install` 即可运行。

### 快速开始

```bash
# 1) 跑测试（在仓库根目录）
python3 -m unittest discover -s gateway/tests -v

# 2) 起雏形（显式开发模式，不校验 Authorization）
GATEWAY_DEV_MODE=true PYTHONPATH=gateway/src python3 -m botville_gateway
#    生产/联调：必须设置真实 token
#    GATEWAY_AUTH_TOKEN=<random> GATEWAY_HMAC_SECRET=<random-hex> PYTHONPATH=gateway/src python3 -m botville_gateway
#    或安装后：pip install -e gateway && botville-gateway

# 3) 冒烟
curl -s localhost:8080/health | python3 -m json.tool
curl -s -X POST localhost:8080/v1/policy/evaluate \
  -H 'Content-Type: application/json' \
  -d '{"persona_id":"persona_demo","conversation_id":"conv_demo","content":"在吗"}'
curl -s -X POST localhost:8080/v1/conversations/conv_demo/messages \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-key-0001' \
  -d '{"sender_persona_id":"persona_demo","content":{"type":"text","text":"你好"}}'
```

需要 Python **3.9+**（用到 `zoneinfo` 与 `str.removeprefix`）。无第三方运行时依赖。
在无系统时区库的平台（如精简容器）上需安装 `tzdata`，否则启动时校验 IANA 时区会失败。

容器方式（A2.2 / #7）：`gateway/Dockerfile` 基于固定 digest 的 `python:3.12-slim`（自带
时区库），按仓库布局放入 `src/` 与 `migrations/`，非 root（uid 10001）运行，不含密钥。
部署栈里用 `make up GATEWAY=1` 起（compose `gateway` profile，宿主端口只绑
`127.0.0.1:${BOTVILLE_GATEWAY_PORT:-8020}`，数据在卷 `gateway-data`）。
synapse / tuwunel 用 `appservice` 传输（`GATEWAY_HOMESERVER_URL=http://<服务名>:8008`，
`GATEWAY_AS_TOKEN` / `GATEWAY_HS_TOKEN` 即 `APPSERVICE_AS_TOKEN` / `APPSERVICE_HS_TOKEN`，注册 `url` 为 `http://gateway:8080`，事务库 `as_transactions.db` 在 `gateway-data` 卷；只跑一个网关）；
continuwuity 仍用 `memory`（尚无文件注册）。运维注意见
[docs/deploy/appservice.md](../docs/deploy/appservice.md) 与
[docs/deploy/z423-synapse.md](../docs/deploy/z423-synapse.md)；卷与备份见
[docs/moments.md](docs/moments.md) §8。`make gateway-hs-smoke` 可对真实 HS
做 whoami + OpenID userinfo 冒烟（不打印 token）。

### HTTP 端点（子集）

| 方法 | 路径 | 契约 operationId | 说明 |
|---|---|---|---|
| GET | `/health` | `getHealth` | 免鉴权；返回 Health 形态 |
| POST | `/v1/policy/evaluate` | `evaluatePolicy` | 预检，不投递、不占额度 |
| POST | `/v1/conversations/{id}/messages` | `sendMessage` | 完整裁决 + 投递 |
| GET | `/v1/limits` | `getLimits` | 三层限额原始规则与逐项生效值 |
| PUT | `/v1/limits` | `putLimits` | 写入 owner / peer 层（平台层只读） |
| GET | `/v1/usage` | `getUsage` | 用量汇总、预算与分桶 |
| GET | `/v1/usage/events` | `listUsageEvents` | 用量账单明细分页（不含正文） |
| PUT | `/_matrix/app/v1/transactions/{txnId}` | *（不在 OpenAPI）* | Matrix AS 事务接收（幂等落库）。**根路径**，不带 `GATEWAY_HTTP_PATH_PREFIX`；鉴权只用 `Authorization: Bearer <GATEWAY_HS_TOKEN>` 请求头（不认 `?access_token=`）；错误体为 Matrix `{"errcode","error"}`。见 [contract.md](docs/contract.md) §1.7 |
| POST | `/_matrix/app/v1/ping` | *（不在 OpenAPI）* | Matrix AS 连通性探测，返回 `{}`。同样**根路径**、`Authorization: Bearer <GATEWAY_HS_TOKEN>`、Matrix 错误格式。见 [contract.md](docs/contract.md) §1.7 |

错误统一 `application/problem+json`（RFC 9457）。**策略拦截不是 HTTP 错误**：
`blocked` 返回 **200**，不是 429/403（契约 `docs/contract.md` §1.4）。

上表最后两行是 **homeserver → 网关**的 Matrix AS 回调，不是面向 App 的 `/v1` API，
因此**不写入** `gateway/openapi/gateway.v1.yaml`、错误体也不用 problem+json。它们的
鉴权凭据是 `GATEWAY_HS_TOKEN`（文档只写变量名、不写任何值，见配置表）。

**请求体上限 1 MiB**：超限返回 `400 request.invalid`，`errors[].code` 为 `too_large`
（契约 §1.1.1，不新增 413）。

### 与 B0.1 契约的对齐

| 契约语义 | 本雏形 |
|---|---|
| 拦截 → `200 + outcome: "blocked"` | ✅ `service._receipt()` |
| `blocked` → `retryable: false`、`retry_after_seconds: null` | ✅ |
| `blocked` → `may_clear_at`（额度/静默恢复时刻） | ✅ 取窗口最早事件过期时刻 / 静默结束时刻 |
| 静默 → `outcome: "deferred"` + 必填 `queue_id` | ✅ 默认 `allow_deferred: true` |
| `allow_deferred: false` → 静默时直接 `blocked` | ✅ |
| `deferred` / `blocked` → `same_content_allowed: true`（额度类，不是内容类拒绝） | ✅ |
| 拦截记 `usage_events kind=blocked` | ✅ `UsageStore`，可查询 |
| `decision.reasons[]` / `checks[]` 字段名与取值 | ✅ 对齐 `PolicyReason` / `PolicyCheck` |
| `date-time` 一律 UTC、`Z` 结尾 | ✅ `timeutil.iso_utc()` |
| `Idempotency-Key` 同 key 同体返回原回执、异体 409 | ✅ 内存缓存（有界，LRU 淘汰）；长度 8–128（超长 → 400） |
| `PolicyDecision` **不含** `may_clear_at` / `same_content_allowed` | ✅ 只在 `MessageReceipt` 上 |
| 无效令牌 → `401 auth.invalid_token`；每个响应带 `X-Request-Id` | ✅ 统一由 `_send_json()` 注入 |
| `/health` 的 `status` 非 `ok` → `503`（结构相同） | ✅ 未接入的依赖不报假 `ok` |
| 请求体：顶层必须是对象、`Content-Type: application/json`、大小上限 1 MiB | ✅ 400 problem+json，`errors[].code: too_large` |
| 出站 schema：`content.type ∈ {text, markdown}`、`reply_to` 为 string、`mentions` 透传、未知字段拒绝 | ✅ `service.send_ai_message()` 手写最小校验 |
| 传输失败 | ✅ **500 `internal.error`**（契约无 502/`matrix.unavailable`） |
| **B2.1：三层限额**（platform/owner/peer）逐项取最严 | ✅ `LimitStore`；`GET/PUT /v1/limits` |
| **B2.1：token 预算**预扣 + 结算 | ✅ `tokens.window` check；`usage_delta.tokens_out` |
| **B2.1：限速窗口开区间 + 释放时刻向上取整** | ✅ `store.events_in_window` 左开右闭；`_ceil_to_second` |
| **B2.1：UsageEvent 不存正文** | ✅ `text_length` + `text_sha256`；无 `text_preview` |
| **B2.1：`GET /usage` + `GET /usage/events`** | ✅ 含 budget、breakdown、分页 |
| **B2.1：内存容量上限 + LRU 淘汰** | ✅ store / idempotency / queue 均有界 |
| `422 limit.out_of_range`（owner/peer 比平台宽松） | ✅ `LimitStore._check_not_looser()` |
| `min_seconds_between_messages`（逐层取最严）| ✅ `min_interval` check |
| `proactive.per_day` / `town.tokens_per_day` 生效值 | ✅ 参与 `GET /limits`；评估 kind=proactive/town_action 时 check |

**未做**（非目标）：防刷屏（`anti_spam.consecutive`）与互聊熔断（`ai_loop.per_hour`）
的执行——两者的**生效值**已在 `GET /limits` 中逐项计算，但策略评估尚未据此拦截，
留待 B2.2；关系策略、E2EE、卡片创建链路（§4.8 的卡片 `interrupt` 语义已对齐契约，
但 `POST /cards` 不在本 PR 范围）、真正的 deferred 定时投递（目前只登记排程，
`service.pending_queue()` 可查；B1.2 接入调度器前 deferred 会一直停留在 `deferred`）。

**安全边界（已知，待 B2.x）**：`/v1/context/assemble` 的 persona 区按 caller token 携带的
`owner_id` 鉴权（#124）：非 owner 或未证明 owner 时 persona 区为空，状态码仍 `200`（不改
任何字段集合与状态码，见契约 §1.2.2）。`persona.not_owned`（403）只用于 assemble 以外的
persona 写操作，发送路径 `sender_persona_id` 的所有权校验不在本期，留待 B2.x。

### 策略算法

**限速：滑动窗口（左开右闭区间）。**
统计 `(now - WINDOW, now]` 内该 persona 的 `kind=sent` 事件数；`used < LIMIT` 才允许。
`reset_at` = 「第 `used − limit + 1` 早」的事件过期时刻（`used == limit` 时即最早那条），
**向上取整到秒**——23:59:59.5 释放报 00:00:00，否则调用方在 23:59:59 重试仍会被拦，
形成差一秒的活锁（#92）。
*为何不用固定窗口*：固定窗口在边界两侧可以连发两倍额度（整点前一秒发满、整点后
再发满），滑动窗口没有这个问题，实现代价也只是存时间戳。

`RATE_LIMIT_MESSAGES=0` 表示关闭平台限速（文档化行为）；
而 `PUT /limits` 写入的 `messages_per_window: 0` 是「一条都不允许」——
两者语义不同，前者由 `LimitStore` 在平台层**省略**该键来表达。

**token 预算。** `tokens.window` 用窗口内 `tokens_in + tokens_out` 之和加本次
`token_estimate`（预扣）与生效预算比较。投递成功后实际入账到 `UsageEvent.tokens_out`。
恢复时刻 = 最早让「剩余额度 ≥ 本次请求」的事件过期时刻；单次请求就超过总预算时
`reset_at` 省略（等多久都不会恢复）。

**最小发言间隔。** `min_seconds_between_messages` 取三层中最严（**越大越严**）；
距上一条 `SENT` 不足时 block，`reset_at` = 上一条 + 间隔。

**主动发言 / 小镇 token（按日）。** `kind: proactive` 评估 `proactive.per_day`，
`kind: town_action` 评估 `town.tokens_per_day`；日界按生效静默时区的日历日计算，
`reset_at` 为次日零点。

**深夜静默。** 按生效层的 IANA 时区把当前时刻转本地时间，用分钟数判断是否落在
`[start, end)`。`start > end` 自动视为跨午夜；`start == end` 视为零长度窗口 = 不静默
（避免配置事故变成全天静默）。**任一层开启即生效**，平台已开启时 owner/peer 不得关闭。
- `allow_deferred=true`（默认）→ `defer`，`queue_id` 必填，`retry_after_seconds` 为到
  静默结束的秒数（仅供展示，客户端**不要**据此自动重试）。
- `allow_deferred=false` → `block`。

**三层取最严（逐项）。** `messages.window` / `tokens.window` / `min_interval` /
`silent_hours` / `proactive.per_day` / `town.tokens_per_day` 各自比较，`PolicyCheck.layer`
记录该项的胜出层，`winner_layer` 记录导致 block/defer 的那一项。同值时归 platform
（结果稳定、可复现）。

**优先级：block 优于 defer。** 一条已经超限的消息延后到静默结束仍发不出去，
直接 block 更诚实；所有未通过的检查会同时出现在 `checks[]` 和 `reasons[]` 里。

**并发安全。** `GatewayService` 在「评估 + 记账」全程持锁，避免并发请求同时通过
`used < limit` 的 check-then-act 竞态造成超额。

**容量与淘汰（#92）。** 用量事件（最旧优先）、幂等缓存（LRU + TTL）、deferred
队列（LRU）都有可配置上限；长时间运行的进程不会因请求量线性增长而耗尽内存。
`UsageEvent` **不存正文**，只存 `text_length` 与 `text_sha256`。

### Matrix Transport

`matrix.MatrixTransport` 是 `Protocol`，网关只依赖 `send_text()` 与 `healthy()`：

```python
class MatrixTransport(Protocol):
    def send_text(self, *, room_id: str, sender_id: str, text: str,
                  reply_to: str | None = None) -> SentEvent: ...
    def healthy(self) -> bool: ...
```

两个实现，service / policy / 测试都不感知差异：

- `InMemoryMatrixTransport`（默认）：消息进 `sent` 列表，测试断言它「有没有发」；
  `fail=True` 可模拟传输故障（抛 `DeliveryError`）。
- `AppServiceMatrixTransport`（A1.3，`matrix_appservice.py`）：以部署层注册的
  application service 身份对接真实 homeserver——`m.login.application_service`
  在 exclusive 命名空间内免密建号、`?user_id=` masquerade 以 `@ai_*` 身份
  join / send（`PUT /_matrix/client/v3/rooms/{roomId}/send/m.room.message/{txnId}`）、
  未加入房间（`M_FORBIDDEN`）自动 join 后重试一次、命名空间外 sender 客户端侧
  直接拒绝。只用 Matrix 标准接口；部署侧注册方式与 token 约定见
  [`docs/deploy/appservice.md`](../docs/deploy/appservice.md)。对真实 Synapse /
  Tuwunel 的验收是 `make appservice-e2e`（CI `appservice-e2e` job）。

**身份与房间映射**（appservice 模式，映射在 transport 内完成，service 层只给
persona/conversation id）：`sender_persona_id` 是 persona id
（`^[a-z0-9._=-]{1,64}$`，大写即非法）→ 用户 `@ai_<persona>:<server_name>`；
`conversation_id` → 房间别名 `#botville_conv_<conversation_id>:<server_name>`
（`GET /_matrix/client/v3/directory/room/{alias}` 解析并按 TTL 缓存，默认 60s
（`GATEWAY_ROOM_ALIAS_CACHE_TTL`），别名不存在 →
404 `conversation.not_found`）；`@`/`!` 开头的完整 Matrix ID 直接使用。语义
与建房侧约定详见 [`docs/deploy/appservice.md`](../docs/deploy/appservice.md)。

transport 由 `GATEWAY_MATRIX_TRANSPORT` 选择（`memory` / `appservice`，默认
`memory` 保持现状）；选择逻辑收敛在 `http_server.build_transport()` 工厂——
`python -m botville_gateway` 与 `build_server()` 共用，`build_server` 在未显式
传 transport 时按 config 构建，**不会静默回落内存假后端**。`appservice` 模式
必须同时给 `GATEWAY_HOMESERVER_URL` / `GATEWAY_MATRIX_SERVER_NAME` /
`GATEWAY_AS_TOKEN`（非空、≥32 字符、无空白），缺省或占位时**拒绝启动**
（fail closed），且 as_token 不进 repr/日志；此外 `GATEWAY_HMAC_SECRET` 在
**所有**非 dev 模式下都是必填（十六进制、≥64 hex 字符、拒绝全零），
缺省或占位时同样拒绝启动。启动时用 AS token 调 whoami
做一次健康校验，失败即退出（fail fast）。

### 配置项

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `GATEWAY_HOST` | `127.0.0.1` | 监听地址 |
| `GATEWAY_PORT` | `8080` | 监听端口（1–65535；环境变量给 0 或超范围会启动失败） |
| `GATEWAY_HTTP_PATH_PREFIX` | *(空)* | 部署路径前缀（如 `/_botville/gateway`）：网关整体挂在某前缀之后时填写，请求可带前缀发出。默认空 = 现行行为不变。必须以 `/` 开头，不含 `//` `..` `?` `#` `%` 空白与控制字符，尾部 `/` 归一、单独 `/` 视为空；非法值拒绝启动。**未带前缀的请求仍被接受**（兼容已去前缀的反代），按整段边界匹配（`/_botville/gatewayX` 不算） |
| `GATEWAY_AUTH_TOKEN` | *(空)* | **必填**（`GATEWAY_LEGACY_TOKEN_ENABLED=false` 时除外）：留空或仍为 `<change-me>` 时**拒绝启动**（fail closed）；生产设为随机值，请求需 `Authorization: Bearer <token>`；进程内对 token 做 HMAC-SHA256 后等长 `hmac.compare_digest` 比较 |
| `GATEWAY_CALLER_TOKENS` | *(空)* | #124 §1.2.2：per-caller 凭据表，每条形如 `caller_id:token:owner_id`（token ≥32 字节随机值），多条用 `;` **或换行**分隔（可混用）。命中后 `owner_id` 用于 `/v1/context/assemble` 的 persona 区鉴权。字段缺/空/数不为 3/含 `:` `;` 换行 → 拒绝启动；token 短于 32 字节或重复 → 非 dev 拒绝、dev 仅 WARNING；caller token 等于 `GATEWAY_AUTH_TOKEN`、或 `caller_id` 重复 → 一律拒绝启动。留空 = 不启用 |
| `GATEWAY_LEGACY_TOKEN_ENABLED` | `true` | 是否仍接受站点级 `GATEWAY_AUTH_TOKEN`。配置了 caller token 后建议设为 `false`（否则启动打 WARNING：旧 token 仍可绕过 per-caller 边界）；设为 `false` 且 caller token 为空时，需鉴权的请求一律 `401 auth.invalid_token`（启动 WARNING），且不再要求 `GATEWAY_AUTH_TOKEN` |
| `GATEWAY_HMAC_SECRET` | *(空)* | **必填**（#114 PR-a / N10）：**只接受十六进制**，至少 **64 个 hex 字符**（= 32 字节），拒绝全零；用于 txnId 与 `text_sha256` 的 HMAC。非 dev 模式留空或为 `<change-me>` 时拒绝启动，dev 模式未设置则每进程随机生成。生成：`python3 -c 'import secrets;print(secrets.token_hex(32))'` **（破坏性变更：以前接受任意字符串）** |
| `GATEWAY_DEV_MODE` | `false` | 仅本地开发：`true` 时允许无 token 启动并免鉴权（启动横幅打 WARNING）；设了真实 token 永远优先 |
| `RATE_LIMIT_MESSAGES` | `60` | 窗口内每个 persona 最多消息数；`0` = 关闭限速 |
| `RATE_LIMIT_WINDOW_SECONDS` | `3600` | 滑动窗口长度（秒，**≥60**，与 OpenAPI `window_seconds` 的 `minimum: 60` 一致） |
| `SILENT_HOURS_ENABLED` | `true` | 是否启用深夜静默 |
| `SILENT_HOURS_START` | `23:00` | 静默开始（`HH:MM`，本地时区） |
| `SILENT_HOURS_END` | `07:30` | 静默结束（`HH:MM`；`START > END` 表示跨午夜） |
| `SILENT_HOURS_TIMEZONE` | `Asia/Shanghai` | IANA 时区名 |
| `MAX_MESSAGE_CHARS` | `8000` | 单条正文上限（超限 → 400，不是 blocked） |
| `PLATFORM_TOKEN_BUDGET` | `200000` | 平台层 token 预算（窗口同 `RATE_LIMIT_WINDOW_SECONDS`）；`0` = 平台不设该限额 |
| `PLATFORM_PROACTIVE_PER_DAY` | `6` | 平台层主动发言/日上限；`0` = 平台不设该限额 |
| `PLATFORM_MAX_CONSECUTIVE_AI_MESSAGES` | `0` | 平台层 `anti_spam.max_consecutive_ai_messages`（N7）；`0` = 平台不设该项，非 0 取 **1–50**，owner/peer 更宽松 → `422 limit.out_of_range` |
| `PLATFORM_ANTI_SPAM_COOLDOWN_SECONDS` | `0` | 平台层 `anti_spam.cooldown_seconds`；`0` = 平台不设该项，非 0 取 **1–86400**（越大越严） |
| `PLATFORM_MAX_AI_MESSAGES_PER_HOUR` | `0` | 平台层 `ai_loop.max_ai_messages_per_hour`；`0` = 平台不设该项（**不是** API 的「一条都不允许」），非 0 取 **1–1000** |
| `PLATFORM_AI_LOOP_ON_BREAK` | `stop` | 平台层 `ai_loop.on_break`（`stop`/`notify`，大小写不敏感）；枚举**无**「不设」语义，默认 `stop` 即平台底线。`notify` 当前等价 `stop`，故平台 `stop` 时 owner/peer 写 `notify` 不报 422 |
| `USAGE_RETENTION_SECONDS` | `2592000` | 用量事件保留期（秒，≥3600）。启动校验必须 **≥ `max(RATE_LIMIT_WINDOW_SECONDS, PLATFORM_ANTI_SPAM_COOLDOWN_SECONDS)`**（平台冷却 0 不参与），否则拒绝启动（N5：保留期短于窗口/冷却会静默 fail-open） |
| `MAX_USAGE_EVENTS` | `10000` | 内存用量事件条数上限（超出按最旧优先淘汰，≥1） |
| `MAX_IDEMPOTENCY_ENTRIES` | `2000` | 幂等缓存条目上限（LRU 淘汰，≥1） |
| `IDEMPOTENCY_TTL_SECONDS` | `86400` | 幂等缓存保留时长（秒，`0` = 不过期，只受容量淘汰；网关重启后清空）。全局写接口与动态端点同样生效，已在 `.env.example` 登记 |
| `MAX_QUEUE_ENTRIES` | `1000` | deferred 排程条目上限（LRU 淘汰，≥1） |
| `GATEWAY_ACCESS_LOG` | `false` | 是否打印访问日志 |
| `GATEWAY_MATRIX_TRANSPORT` | `memory` | `memory`=内存假后端（B1.1 现状）；`appservice`=真实 homeserver 代发（A1.3，见 [docs/deploy/appservice.md](../docs/deploy/appservice.md)） |
| `GATEWAY_HOMESERVER_URL` | *(空)* | appservice 模式必填：homeserver 的 CS API 地址（`http(s)://…`）；明文 `http://` 只允许 loopback / 无点单标签容器名，跨主机要 `https://`（`GATEWAY_ALLOW_INSECURE_HS=true` 可显式放开） |
| `GATEWAY_MATRIX_SERVER_NAME` | *(空)* | appservice 模式必填：homeserver 的 `server_name`（主机名或 IP，可带 `:端口`；统一小写）。**OpenID userinfo 校验时**，`sub` 的域须等于该值，否则 401 |
| `GATEWAY_OPENID_CACHE_TTL_SECONDS` | `60` | 契约 §1.2.3（#124 OpenID）：OpenID userinfo 结果缓存 TTL（秒），**上限 60**，且不超过 token 剩余有效期（响应未带 `expires_in` 时不收紧，只受该上限与配置约束）。缓存键只用 token 哈希（不缓存明文、不入日志）。失败负缓存 5 秒、且不超过成功 TTL，**仅对 401/403（及域不匹配）负缓存**；其他 4xx / 5xx / 超时 / 不可达不缓存，可重试。值为 0 表示不缓存；`<0` 或 `>60` 拒绝启动。**自本 PR 起生效** |
| `GATEWAY_HOMESERVER_FEDERATION_URL` | *(空)* | 契约 §1.2.3：**仅**用于 `GET /_matrix/federation/v1/openid/userinfo` 的**内网直连** homeserver 基址（如 compose 内网 `http://synapse:8008`）。留空则回落 `GATEWAY_HOMESERVER_URL`。不得经 fedgate / 公网；只有这条 userinfo 走直连，其他联邦流量仍走 fedgate，且**不得把该端点暴露到公网入口**。**自本 PR 起生效** |
| `GATEWAY_AS_TOKEN` | *(空)* | appservice 模式必填：与部署层注册文件的 `APPSERVICE_AS_TOKEN` 一致；留空/`<change-me>`/短于 32 字符/含空白都拒绝启动，不进 repr/日志 |
| `GATEWAY_ROOM_ALIAS_PREFIX` | `botville_conv_` | conversation → 房间别名 localpart 前缀（`^[a-z0-9._=-]{1,64}$`）；改前缀须同步改部署模板的 exclusive aliases 正则（`server/compose/appservice/botville-gateway.yaml.tmpl`） |
| `GATEWAY_ROOM_ALIAS_CACHE_TTL` | `60` | 别名 → room_id 进程内缓存 TTL（秒），过期重新 directory lookup；`0` = 不缓存，负数拒绝启动 |
| `GATEWAY_ALLOW_INSECURE_HS` | `false` | 显式放行明文 `http://` 的跨主机 homeserver（接受 as_token 明文传输风险） |
| `GATEWAY_MOMENTS_DB_PATH` | *(空)* | 动态（A2.2 / #7）SQLite 路径，WAL；启动时自动执行 `gateway/migrations/moments/` 迁移（幂等）。**留空 = 关闭动态模块**（`/v1/moments*` 鉴权后仍 404/405）。compose `gateway` 服务（`make up GATEWAY=1`）固定为卷 `gateway-data` 上的 `/var/lib/botville-gateway/moments.db`，见 [docs/moments.md](docs/moments.md) §7 / §8 |
| `GATEWAY_MOMENTS_FRIENDS_FILE` | *(空)* | 临时好友图 JSON（`{"friends": [["@a:hs","@b:hs"], ...]}`，无向边，启动时读一次，结构非法拒绝启动）。**留空 = 空好友图**：A2.3（#8）落地前「好友可见」等同「仅自己」，见 moments.md §7.1 |
| `GATEWAY_MOMENTS_SOFT_DELETE_RETENTION_DAYS` | `30` | 软删动态/评论硬删前的保留天数（由定时清理执行） |
| `GATEWAY_MOMENTS_AUDIT_RETENTION_DAYS` | `90` | `moments_events` 审计默认保留 90 天（由定时清理执行） |
| `GATEWAY_MOMENTS_PURGE_INTERVAL_SECONDS` | `86400` | 定时清理间隔（秒）：启动先跑一次，之后后台按间隔跑，失败只记日志。`0` = 关闭（两个保留天数不生效）；非 0 时 **≥60**，否则拒绝启动 |
| `GATEWAY_HS_TOKEN` | *(空)* | Matrix AS 回调（B 线 as-transactions）的共享密钥：必须与注册文件里的 `hs_token`（= `server/compose/.env` 的 `APPSERVICE_HS_TOKEN`）同值。只从 `Authorization: Bearer` 请求头校验（不认 `?access_token=`），恒定时间比较，不入日志。**留空 = 网关照常启动但 `/_matrix/app/v1/transactions/*` 与 `/ping` 一律 403 `M_FORBIDDEN`**（部署：init.sh 生成 `APPSERVICE_HS_TOKEN` 并渲染进注册文件的 `hs_token`，`make up GATEWAY=1` 时 compose 把同一个值注入为本变量）。设置后要求非占位符、**≥32 字符**、无空白，且**不能等于 `GATEWAY_AS_TOKEN`**，否则拒绝启动。见 [docs/contract.md](docs/contract.md) §1.7 |
| `GATEWAY_AS_TXN_DB_PATH` | *(空)* | AS 事务 SQLite 路径（WAL + `synchronous=FULL`，返回 200 前提交；新建文件以 **0600** 创建）。**留空 = 关闭接收**（transactions 返回 `500 M_UNKNOWN`，HS 重试），与 `GATEWAY_MOMENTS_DB_PATH` 的「空 = 关闭」约定一致。`make up GATEWAY=1` 时 compose 设为 `/var/lib/botville-gateway/as_transactions.db`（与 `moments.db` 共用 `gateway-data` 卷，#193）。**库含原始事件请求体**，备份按敏感数据处理。见 [docs/contract.md](docs/contract.md) §1.7 / §1.7.7 |
| `GATEWAY_AS_MAX_BODY_BYTES` | `16777216` | AS 回调请求体上限（字节，**16 MiB**；正整数，最大 256 MiB）。与 `/v1` 的 1 MiB 上限**相互独立**；超限 → `413 M_TOO_LARGE` |
| `GATEWAY_AS_TXN_RETENTION_DAYS` | `90` | AS 事务库保留天数（正整数，`<1` 拒绝启动）。超过保留期的事务**不删行**：只清空原始请求体并写 `purged_at`（墓碑保留 `txn_id` / 接收时间 / `processed_at`），重放仍 `200 {}` 且不重落库；过期未消费事务按丢弃处理。**校验与 `GATEWAY_AS_TXN_DB_PATH` 是否配置无关**——非法值在任何情况下都拒绝启动。清理只在同时配置了库路径且间隔非 0 时运行。见 [docs/contract.md](docs/contract.md) §1.7.7 |
| `GATEWAY_AS_TXN_PURGE_INTERVAL_SECONDS` | `86400` | AS 事务保留期清理间隔（秒）：启动先跑一次，之后后台按间隔跑，失败只记日志（不记 token / 事件体）。`0` = 关闭（启动不跑、不起线程，库会持续增长）；非 0 时 **≥60**，否则拒绝启动。**校验与 `GATEWAY_AS_TXN_DB_PATH` 是否配置无关**。见 [docs/contract.md](docs/contract.md) §1.7.7 |

非法配置（端口非数字、时区不存在、`HH:MM` 格式错）在 `Config` 构造时抛 `ConfigError`，
进程以退出码 `2` 结束。

`.env.example` 只是占位，本雏形**不读取 `.env` 文件**（避免引入 python-dotenv）；
由部署侧注入环境变量。

### 测试

```bash
python3 -m unittest discover -s gateway/tests -v
```

覆盖：

- **超限拦截** — `outcome=blocked`、`retryable=false`、`may_clear_at` 非空、
  transport 未发送、`store` 有 `kind=blocked` 记录（Issue #15 验收点）
- **静默延后** — `outcome=deferred` + `queue_id` + `reason=silent_hours.active`；
  `allow_deferred=false` → `blocked`；跨午夜窗口；零长度窗口不静默
- **正常投递** — `outcome=delivered`，transport 收到消息，回执含 `message`/`delivery`
- **滑动窗口过期** — 发满后推进时钟，名额恢复
- **幂等** — 同 key 同体返回缓存回执，异体抛 `IdempotencyConflict`
- **校验** — 缺字段、超长正文
- **appservice 传输**（`test_matrix_appservice.py`，http.server 假 HS）—
  `m.login.application_service` 注册与缓存 / `M_USER_IN_USE` 幂等 /
  `M_FORBIDDEN` 自动 join 重试 / 命名空间外零请求拒绝（send/ensure_user/join）/
  persona→`@ai_*` 与 conversation→房间别名映射（directory/room 解析、缓存、
  `conversation.not_found`）/ HTTP 错误映射 `DeliveryError(code=errcode)` /
  非 JSON 2xx 与重定向不跟随 / masquerade 参数 / markdown 与回复字段 /
  `healthy()`（whoami，token 错不误报）/ `GATEWAY_MATRIX_TRANSPORT`
  fail-closed、as_token 强度、server_name 归一化、明文 URL 主机白名单、
  别名前缀校验与 token 不进 repr
- **传输工厂**（`test_gateway.py`）— `build_server` 在未显式传 transport 时按
  config 构建：appservice 配置不回落内存、memory 保持现状、显式注入优先；
  投递错误映射：`appservice.namespace`/`conversation.invalid` → 400、
  `conversation.not_found` → 404、其余 → 500
- **B2.1 三层限额**（`tests/test_b21_limits.py`）— 逐项取最严、`source_layer` 归属、
  平台底线只读、放宽 → 422 `limit.out_of_range`、token 预扣与结算、`min_interval`、
  `proactive.per_day`、`town.tokens_per_day`、`GET/PUT /limits`、`GET /usage`、
  `GET /usage/events`
- **#92 剩余项** — 窗口左开右闭边界、`reset_at` 向上取整（含 `used > limit`）、
  `UsageEvent` 不存正文（`text_length` + `text_sha256`）、store / 幂等 / 队列容量上限、
  1 MiB 请求体 → 400 `too_large`
- **密钥扫描** — 递归扫描 `gateway/`，禁止 `sk-…`/`AKIA…`/私钥块/GitHub/Slack token
  等真实凭据指纹（`<change-me>`、`example.invalid` 放行）

测试用注入时钟（`_FixedClock`）而非 `sleep`，全部瞬间完成。

### 覆盖率

```bash
python3 gateway/scripts/coverage_check.py            # 报告 src 行覆盖率
python3 gateway/scripts/coverage_check.py --show-missing   # 附未覆盖行号
```

零依赖（`sys.settrace` + `threading.settrace`），统计 `gateway/src/botville_gateway/`
的行覆盖率，阈值默认 80%（`--threshold` 可调）。当前结果见 PR 描述。

---

## 契约校验 / Validate the contract

任务 B0.1 的验收方式：C/D 线评审前、以及每次改契约后，在**仓库根目录**运行：

```bash
python3 gateway/scripts/validate_openapi.py
```

需要 Python 3.9+ 与 PyYAML（`python3 -m pip install --user pyyaml`）。
脚本完全离线，不需要网络、不需要 Node。

退出码 `0` = 全部通过；`1` = 有检查失败（逐条打印原因）；`2` = 环境或路径问题。

它检查什么：

1. **文档结构** — OpenAPI 3.1、`info.license` 为 Apache-2.0、`servers` 在 `/v1` 下、
   bearer 占位鉴权、全部 `$ref` 可解析、operationId 唯一、每个操作有 summary/description/tag、
   路径参数已声明且 `required: true`、错误响应统一 `application/problem+json`
   （RFC 9457）、YAML 无重复键。
2. **示例** — `openapi/examples/*.json` 逐个对契约里声明的 schema 校验
   （映射表在脚本的 `EXAMPLE_TARGETS`，指向契约本身，所以 schema 一改示例就会暴露问题）；
   每个示例文件必须有映射，每个映射必须有文件。
3. **占位扫描** — 疑似真实密钥（`sk-…`、`AKIA…`、私钥块、长字面量密码等）即失败，
   契约与 `examples/*.json` 都会扫描。所有示例只允许 `<change-me>` 与 `example.invalid`。
   `date-time` 必须是 UTC 并以 `Z` 结尾（带 `+HH:MM` 偏移视为失败）。

常用变体：

```bash
# 指定别的契约文件
python3 gateway/scripts/validate_openapi.py --contract gateway/openapi/gateway.v1.yaml

# 可选：外部 lint（需要网络，非 CI 必需）
npx --yes @redocly/cli lint gateway/openapi/gateway.v1.yaml
```

示例：

```
[1/3] OpenAPI document structure
  PASS  all $refs resolve (293 found)
  PASS  path operations are well formed (26 ops, unique operationIds)
[2/3] Example payloads against contract schemas
  PASS  card.json matches GET /cards/{card_id} 200
  ...
[3/3] Summary
  All checks passed. Contract is consistent with its examples.
```

（实际检查项数量以脚本输出为准，不在此写死。）

## 约定 / Conventions

- 只增不删、破坏性变更换主版本，详见 [`docs/contract.md#版本规则`](docs/contract.md#5-版本规则)。
- 发言被限额拦截是 `200` + `outcome: "blocked"`，不是 `429`/`403`
  （见 [`docs/contract.md#14-️-策略结论不是-http-错误`](docs/contract.md#14-️-策略结论不是-http-错误)）。
- 不写任何真实密钥、token、密码，只用 `<change-me>` 占位（见 `CONTRIBUTING.md`）。
- 新文件加 Apache-2.0 SPDX 头。
