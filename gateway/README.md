# gateway/ — AI 网关与角色引擎（B 线，Claude Code）

- 网关契约与 OpenAPI 初稿、完整接入 SDK、简单接入角色引擎
- 三层限额与 token 预算、防刷屏与 AI 互聊熔断、不可信内容隔离（防注入）
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
| `src/botville_gateway/` | **B1.1 网关雏形 + A1.3 appservice 传输 Python 包**（见下节） |
| `tests/` | 单元/集成测试（stdlib `unittest`，无第三方依赖） |
| `pyproject.toml` | 打包元数据；**运行时零依赖**（仅标准库） |
| `.env.example` | 环境变量占位（不含真实密钥） |

---

## B1.1 网关雏形 / Gateway skeleton

> 任务：Issue #15「超限拦截并记录」的最小可合并实现（B1.1）；
> A1.3 起新增 appservice 传输（真实 homeserver 代发）。
> 收发、平台级限速、深夜静默；默认仍是内存假后端，不依赖真 Matrix homeserver。

### 包结构

```
gateway/src/botville_gateway/
├── __init__.py          导出 Config / PolicyEngine / GatewayService 等
├── __main__.py          python -m botville_gateway 本地启动（含启动健康校验）
├── config.py            环境变量 → 不可变 Config（构造时校验）
├── matrix.py            MatrixTransport Protocol + InMemoryMatrixTransport
├── matrix_appservice.py AppServiceMatrixTransport（AS token masquerade，A1.3）
├── policy.py            限速（滑动窗口）+ 深夜静默 → PolicyDecision
├── service.py           send_ai_message() 编排、幂等、回执组装
├── store.py             内存 UsageEvent 日志（append-only、线程安全）
├── http_server.py       stdlib HTTP 雏形 + build_transport 工厂（按 config 选传输）
└── timeutil.py          UTC `Z` 结尾的 RFC 3339 格式化
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
#    GATEWAY_AUTH_TOKEN=<random> PYTHONPATH=gateway/src python3 -m botville_gateway
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

### HTTP 端点（子集）

| 方法 | 路径 | 契约 operationId | 说明 |
|---|---|---|---|
| GET | `/health` | `getHealth` | 免鉴权；返回 Health 形态 |
| POST | `/v1/policy/evaluate` | `evaluatePolicy` | 预检，不投递、不占额度 |
| POST | `/v1/conversations/{id}/messages` | `sendMessage` | 完整裁决 + 投递 |

错误统一 `application/problem+json`（RFC 9457）。**策略拦截不是 HTTP 错误**：
`blocked` 返回 **200**，不是 429/403（契约 `docs/contract.md` §1.4）。

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
| `Idempotency-Key` 同 key 同体返回原回执、异体 409 | ✅ 内存缓存；长度 8–128（超长 → 400） |
| `PolicyDecision` **不含** `may_clear_at` / `same_content_allowed` | ✅ 只在 `MessageReceipt` 上（`to_dict()` 剔除了内部字段） |
| 无效令牌 → `401 auth.invalid_token`；每个响应带 `X-Request-Id` | ✅ 统一由 `_send_json()` 注入 |
| `/health` 的 `status` 非 `ok` → `503`（结构相同） | ✅ 未接入的依赖不报假 `ok` |
| 请求体：顶层必须是对象、`Content-Type: application/json`、大小上限 | ✅ 400 problem+json（`MAX_BODY_BYTES` = 1 MiB） |
| 出站 schema：`content.type ∈ {text, markdown}`、`reply_to` 为 string、`mentions` 透传、未知字段拒绝 | ✅ `service.send_ai_message()` 手写最小校验 |
| 传输失败 | ✅ **500 `internal.error`**（契约无 502/`matrix.unavailable`） |

**未做**（非目标）：三层 owner/peer 限额（B2.1）、token 预算、防刷屏、互聊熔断、
关系策略、E2EE、卡片创建链路（§4.8 的卡片 `interrupt` 语义已对齐契约，但 `POST /cards`
不在本 PR 范围）、真正的 deferred 定时投递（目前只登记排程，`service.pending_queue()`
可查；B1.2 接入调度器前 deferred 会一直停留在 `deferred`）。接 B2.1 时在
`PolicyEngine.evaluate()` 追加检查项即可，数据结构无需改。

**安全边界（已知，待 B2.x）**：当前为单 token 设备级鉴权——任何持有该 token 者都可以
冒充任意 `sender_persona_id` 向任意 conversation 发送。按 persona 所有权的
`persona.not_owned`（403）校验留待 B2.x 会话/令牌模型。

### 策略算法

**限速：滑动窗口（sliding window）。**
统计 `[now - RATE_LIMIT_WINDOW_SECONDS, now]` 内该 persona 的 `kind=sent` 事件数；
`used < RATE_LIMIT_MESSAGES` 才允许。`reset_at` = 窗口内最早事件的过期时刻
（即「下一个名额何时释放」）。
*为何不用固定窗口*：固定窗口在边界两侧可以连发两倍额度（整点前一秒发满、整点后
再发满），滑动窗口没有这个问题，实现代价也只是存时间戳。

`RATE_LIMIT_MESSAGES=0` 表示关闭平台限速（文档化行为）。

**深夜静默。** 按 `SILENT_HOURS_TIMEZONE`（IANA）把当前时刻转本地时间，用分钟数判断
是否落在 `[start, end)`。`start > end` 自动视为跨午夜。`start == end` 视为零长度窗口 =
不静默（避免配置事故变成全天静默）。
- `allow_deferred=true`（默认）→ `defer`，`queue_id` 必填，`retry_after_seconds` 为到
  静默结束的秒数（仅供展示，客户端**不要**据此自动重试）。
- `allow_deferred=false` → `block`。

**优先级：限速 block 优于静默 defer。** 一条已经超限的消息延后到静默结束仍发不出去，
直接 block 更诚实；两项未通过的检查会同时出现在 `checks[]` 和 `reasons[]` 里。

**并发安全。** `GatewayService` 在「评估 + 记账」全程持锁，避免并发请求同时通过
`used < limit` 的 check-then-act 竞态造成超额。

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
（`GET /_matrix/client/v3/directory/room/{alias}` 解析并缓存，别名不存在 →
404 `conversation.not_found`）；`@`/`!` 开头的完整 Matrix ID 直接使用。语义
与建房侧约定详见 [`docs/deploy/appservice.md`](../docs/deploy/appservice.md)。

transport 由 `GATEWAY_MATRIX_TRANSPORT` 选择（`memory` / `appservice`，默认
`memory` 保持现状）；选择逻辑收敛在 `http_server.build_transport()` 工厂——
`python -m botville_gateway` 与 `build_server()` 共用，`build_server` 在未显式
传 transport 时按 config 构建，**不会静默回落内存假后端**。`appservice` 模式
必须同时给 `GATEWAY_HOMESERVER_URL` / `GATEWAY_MATRIX_SERVER_NAME` /
`GATEWAY_AS_TOKEN`（非空、≥32 字符、无空白），缺省或占位时**拒绝启动**
（fail closed），且 as_token 不进 repr/日志；启动时用 AS token 调 whoami
做一次健康校验，失败即退出（fail fast）。

### 配置项

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `GATEWAY_HOST` | `127.0.0.1` | 监听地址 |
| `GATEWAY_PORT` | `8080` | 监听端口（1–65535；环境变量给 0 或超范围会启动失败） |
| `GATEWAY_AUTH_TOKEN` | *(空)* | **必填**：留空或仍为 `<change-me>` 时**拒绝启动**（fail closed）；生产设为随机值，请求需 `Authorization: Bearer <token>`；用 `hmac.compare_digest` 常数时间比较 |
| `GATEWAY_DEV_MODE` | `false` | 仅本地开发：`true` 时允许无 token 启动并免鉴权（启动横幅打 WARNING）；设了真实 token 永远优先 |
| `RATE_LIMIT_MESSAGES` | `60` | 窗口内每个 persona 最多消息数；`0` = 关闭限速 |
| `RATE_LIMIT_WINDOW_SECONDS` | `3600` | 滑动窗口长度（秒，≥1） |
| `SILENT_HOURS_ENABLED` | `true` | 是否启用深夜静默 |
| `SILENT_HOURS_START` | `23:00` | 静默开始（`HH:MM`，本地时区） |
| `SILENT_HOURS_END` | `07:30` | 静默结束（`HH:MM`；`START > END` 表示跨午夜） |
| `SILENT_HOURS_TIMEZONE` | `Asia/Shanghai` | IANA 时区名 |
| `MAX_MESSAGE_CHARS` | `8000` | 单条正文上限（超限 → 400，不是 blocked） |
| `GATEWAY_ACCESS_LOG` | `false` | 是否打印访问日志 |
| `GATEWAY_MATRIX_TRANSPORT` | `memory` | `memory`=内存假后端（B1.1 现状）；`appservice`=真实 homeserver 代发（A1.3，见 [docs/deploy/appservice.md](../docs/deploy/appservice.md)） |
| `GATEWAY_HOMESERVER_URL` | *(空)* | appservice 模式必填：homeserver 的 CS API 地址（`http(s)://…`）；明文 `http://` 只允许 loopback / 无点单标签容器名，跨主机要 `https://`（`GATEWAY_ALLOW_INSECURE_HS=true` 可显式放开） |
| `GATEWAY_MATRIX_SERVER_NAME` | *(空)* | appservice 模式必填：homeserver 的 `server_name`（主机名或 IP，可带 `:端口`；统一小写） |
| `GATEWAY_AS_TOKEN` | *(空)* | appservice 模式必填：与部署层注册文件的 `APPSERVICE_AS_TOKEN` 一致；留空/`<change-me>`/短于 32 字符/含空白都拒绝启动，不进 repr/日志 |
| `GATEWAY_ROOM_ALIAS_PREFIX` | `botville_conv_` | conversation → 房间别名 localpart 前缀（`^[a-z0-9._=-]{1,64}$`） |
| `GATEWAY_ALLOW_INSECURE_HS` | `false` | 显式放行明文 `http://` 的跨主机 homeserver（接受 as_token 明文传输风险） |

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
- **密钥扫描** — 递归扫描 `gateway/`，禁止 `sk-…`/`AKIA…`/私钥块/GitHub/ Slack token
  等真实凭据指纹（`<change-me>`、`example.invalid` 放行）

测试用注入时钟（`_FixedClock`）而非 `sleep`，全部瞬间完成。

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
