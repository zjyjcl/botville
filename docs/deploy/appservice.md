# Appservice：AI 账号命名空间与网关代发（A1.3）

任务 A1.3（issue #5）部署文档 · 对象：botville homeserver 栈（Synapse / Tuwunel）与
AI 网关（`gateway/`）。

> **验证状态**：本机制的验收（网关以 `@ai_*` 身份注册、进房、代发；命名空间
> exclusive 双向拒绝；网关 HTTP 代发链路）由 CI 的 `appservice-e2e (synapse)`
> 与 `appservice-e2e (tuwunel)` 两个 job 在每次提交时对真实 homeserver 完整
> 执行。桌面实测已通过：Synapse **v1.164.1** 与 Tuwunel **v1.9.4** 全链路
> 可用，Tuwunel 接受注册文件中的 `url: null`（见下文
> [注册方式差异](#注册方式差异)）。

本文只使用 Matrix 标准接口（Application Service API + Client-Server API
masquerade）；运行时不使用 `/_synapse/admin`，也不使用 admin room 命令。
示例中一律是占位符，不含真实密钥。

---

## 目录

1. [命名空间约定](#命名空间约定)
2. [persona / conversation 的 ID 语义（appservice 模式）](#persona--conversation-的-id-语义appservice-模式)
3. [注册文件与渲染](#注册文件与渲染)
4. [角色引擎（模型与人设）](#角色引擎模型与人设)
5. [注册方式差异（三家 homeserver）](#注册方式差异)
6. [网关配置](#网关配置)
7. [token 轮换](#token-轮换)
8. [安全须知](#安全须知)
9. [验收与排障](#验收与排障)

---

## 命名空间约定

- **所有 AI 账号的 localpart 一律以 `ai_` 开头**：`@ai_<persona>:<server_name>`。
  homeserver 侧把它们划入 appservice 的 **exclusive user namespace**：
  普通用户不能用 UIA 注册进该命名空间（HS 返回 `M_EXCLUSIVE`），网关也不能
  越过命名空间冒充其他账号（`M_FORBIDDEN` / `M_EXCLUSIVE`）。
- 正则来自部署模板 `server/compose/appservice/botville-gateway.yaml.tmpl`：

  ```yaml
  namespaces:
    users:
      - exclusive: true
        regex: '^@ai_.*:<server_name 的正则转义>$'
    aliases:
      - exclusive: true
        regex: '^#botville_conv_.*:<server_name 的正则转义>$'
  ```

  两端锚定（`^…$`）。`<server_name>` 由 `init.sh` 做**正则转义**后注入
  （`example.com` → `example\.com`），避免域名中的点被当成通配。
- **房间别名也是 exclusive 命名空间**（#107）：`#botville_conv_*:<server>`
  只能由 appservice 认领；人类用户 `createRoom` 带
  `room_alias_name=botville_conv_*` 会被 HS 拒绝（`M_EXCLUSIVE`，两家 HS
  由 `appservice-e2e` 断言同一 errcode）。别名前缀必须与网关
  `GATEWAY_ROOM_ALIAS_PREFIX`（默认 `botville_conv_`）一致：**改前缀须同步
  改模板里的 aliases 正则**，否则别名解析与命名空间排他会分叉。
- AS 的 `sender_localpart`（`botville-gateway`）与命名空间外的普通账号互不冲突。
- **启用 exclusive 命名空间的前置检查**：若部署里已经存在**普通方式**注册的
  `@ai_*` 用户，注册 appservice 后 AS（持 as_token 者）将取得这些账号的控制
  权（免密登录、以其身份发言）。启用前先确认：

  ```bash
  # 任何客户端/管理界面查看用户目录，确认没有意外的 @ai_* 账号；
  # 有的话先改名或注销，再渲染注册文件并重启 homeserver。
  ```

## persona / conversation 的 ID 语义（appservice 模式）

网关 API（`POST /v1/conversations/{conversation_id}/messages`）在 appservice
模式下按以下规则把业务 ID 映射成 Matrix 身份与房间（映射在传输层
`AppServiceMatrixTransport` 内完成，service/HTTP 层不感知）：

| 请求字段 | 传什么 | 映射结果 |
| --- | --- | --- |
| `sender_persona_id` | persona id：`^[a-z0-9._=-]{1,64}$`（**小写即合法**，大写/空白/特殊字符直接 400） | Matrix 用户 `@ai_<persona_id>:<server_name>`；首次使用时经 `m.login.application_service` 免密注册 |
| `sender_persona_id` | 或完整 Matrix 用户 ID（`@` 开头） | 必须已落在 `@ai_*:<server_name>` 命名空间内，否则 400（`appservice.namespace`） |
| 路径 `conversation_id` | conversation id：字符集同 persona | 房间别名 `#<GATEWAY_ROOM_ALIAS_PREFIX><conversation_id>:<server_name>`（默认前缀 `botville_conv_`），网关经标准接口 `GET /_matrix/client/v3/directory/room/{alias}` 解析成 room id 并进程内缓存（TTL `GATEWAY_ROOM_ALIAS_CACHE_TTL`，默认 60s，过期重新解析）；别名不存在 → **404 `conversation.not_found`**；字符集非法 → 400 |
| 路径 `conversation_id` | 或完整 Matrix room ID（`!` 开头） | 直接使用，不做目录解析 |

**房间别名约定**：`#botville_conv_*` 是 appservice 的 exclusive 别名命名
空间，**会话房间由 appservice 一侧创建**（人类账号无法认领该前缀的别名，
`createRoom` 会得到 `M_EXCLUSIVE`）：以 as_token 加 `?user_id=@ai_<persona>`
masquerade 建房、设置 `room_alias_name = botville_conv_<conversation_id>` 并
邀请人类用户；之后只拿 `conversation_id` 调网关即可。示例：

```json
POST /_matrix/client/v3/createRoom?user_id=@ai_persona_demo:localhost
Authorization: Bearer <as_token>
{ "room_alias_name": "botville_conv_conv_demo",
  "invite": ["@human:localhost"], "preset": "private_chat" }
```

```json
POST /v1/conversations/conv_demo/messages
{ "sender_persona_id": "persona_demo",
  "content": { "type": "text", "text": "你好" } }
```

错误映射（problem+json）：非法 persona / conversation 字符集、命名空间外的
sender → **400 `request.invalid`**；房间别名不存在 → **404
`conversation.not_found`**（两者都是契约已声明的错误码，不是 500）。
`memory` 模式（默认）不做任何映射，行为与 B1.1 一致。

**注意（B 线遗留）**：A1.3 的鉴权边界在网关 token 一层——任何持有网关
`GATEWAY_AUTH_TOKEN` 的调用方都能指定**任意** persona / conversation。
按 persona 所有权的 `persona.not_owned`（403）校验留给 B2.x 会话/令牌模型。

## 注册文件与渲染

- 模板：`server/compose/appservice/botville-gateway.yaml.tmpl`（入库，只有占位符）。
- `server/scripts/init.sh` 把它渲染到
  `server/compose/generated/appservice/botville-gateway.yaml`（gitignore，
  **0600**），token 取自 `server/compose/.env` 的：

  | 键 | 用途 |
  | --- | --- |
  | `APPSERVICE_AS_TOKEN` | 网关 → HS：Bearer 代发/建号的凭据 |
  | `APPSERVICE_HS_TOKEN` | HS → 网关：校验事务接收（compose 注入为 `GATEWAY_HS_TOKEN`，同一份值） |

  `.env` 首次由 init.sh 随机生成；`.env.example` 只放 `<change-me>`（CI 有
  allowlist 守卫）。渲染产物不入库。
- **token 三层校验（fail closed）**：init.sh 渲染前校验两个 token 都匹配
  `^[0-9a-f]{64}$` 且互不相同——空值或 `<change-me>` 占位会自动随机生成写回
  `.env`（打印提示），其他非法值直接报错退出（不覆盖手填值）；渲染器写文件
  前再对生成 YAML 里的 `as_token`/`hs_token` 做同样校验（双保险）；网关侧
  `GATEWAY_AS_TOKEN` 也会在启动时做强度校验（≥32 字符、无空白）。空 token
  渲染进注册文件曾被证实会让 Synapse 接受**无鉴权**的 `@ai_*` 注册，因此
  三层都不放行。init.sh 全程 `umask 077` 并无条件 `chmod 600` `.env` 与渲染
  产物。
- **注册 `url` 按开关切换**（A2.2，接 #192）：注册文件的 `url:` 由 init.sh 从
  `BOTVILLE_APPSERVICE_URL` 渲染，只允许两个值：
  - `http://gateway:8080`：**仅当** `make up GATEWAY=1` 且 HS 是 synapse 或
    tuwunel。不带路径前缀——网关的 AS 回调
    `PUT /_matrix/app/v1/transactions/{txnId}` / `POST /_matrix/app/v1/ping`
    挂在根路径，设了 `GATEWAY_HTTP_PATH_PREFIX` 也一样（见
    `gateway/docs/contract.md` §1.7）。
  - `null`：其余一律如此——没开 `GATEWAY=1`，或 HS 是 continuwuity。否则 HS 会
    往一个不存在的网关推事务，连不上就无限退避重试。
  - 其他值 init.sh 直接报错退出。没设该变量时（`make smoke` / `make e2e` 等
    也会跑 init）保留生成文件里已有的值，首次默认 `null`。
  - **改了 url 必须重启 HS**（注册文件只在启动时读取）。url 变化时 init.sh 会
    留下 `generated/appservice/.url-changed`，`make up` 看到它就先
    `restart` 当前 HS（容器还没建时是空操作）再 `up -d`。所以从
    `make up GATEWAY=1` 切到 `make up`（或反过来）时 HS 会自动读到新 url。绕过
    make 手动跑 `BOTVILLE_APPSERVICE_URL=... bash server/scripts/init.sh` 时，需自己
    `docker compose ... restart <hs>`。
  - `make down` 不改 url；下一次 `make up` 再决定。
- compose 的可选 `gateway` profile（`make up GATEWAY=1`）把网关接到 homeserver：
  代发走 Client-Server API，事务接收走 AS 回调。
  - synapse / tuwunel：`GATEWAY_MATRIX_TRANSPORT=appservice`，
    `GATEWAY_HOMESERVER_URL=http://${BOTVILLE_HOMESERVER}:8008`（服务名，不要写
    `127.0.0.1`），`GATEWAY_MATRIX_SERVER_NAME=${BOTVILLE_SERVER_NAME}`，
    `GATEWAY_AS_TOKEN=${APPSERVICE_AS_TOKEN}`、
    `GATEWAY_HS_TOKEN=${APPSERVICE_HS_TOKEN}`（两个 token 各自同一份值，不另造），
    `GATEWAY_HOMESERVER_FEDERATION_URL` 留空（OpenID userinfo 走内网直连）。
  - 收到的事务（原始 JSON，按 txnId 幂等）存进
    `GATEWAY_AS_TXN_DB_PATH=/var/lib/botville-gateway/as_transactions.db`，
    在 `gateway-data` 卷顶层，和 `moments.db` 一样随 `make backup`（synapse）/
    `make backup-gateway`（tuwunel、continuwuity）整卷备份，备份前对顶层
    `*.db` 做 WAL checkpoint；恢复同理。`GATEWAY_AS_MAX_BODY_BYTES` 用默认 16 MiB。
    **HS 与 gateway-data 要成对恢复**：txnId 是 HS 按自己的库分配的，HS 被清空重建
    后会从头编号；这时只恢复 gateway-data，新事务的 txnId 会撞上库里的旧 txnId，
    网关按幂等直接回 200 而不落库（数据不会坏，但新事件被当成已收到）。生产上
    HS 数据和 gateway-data 要来自同一时间点；只想清空网关事务时，连同 HS 一起重建。
  - **保留期与清理**（contract.md §1.7.7）：`GATEWAY_AS_TXN_RETENTION_DAYS`（默认
    90）与 `GATEWAY_AS_TXN_PURGE_INTERVAL_SECONDS`（默认 86400，`0` = 关闭，非 0
    须 ≥60）用网关默认值即可。**两个参数的校验与库路径无关**：即使不配置库路径，
    非法值（如 `GATEWAY_AS_TXN_PURGE_INTERVAL_SECONDS=59`）也会让网关拒绝启动。
    清理**不删行**，只把超过保留期事务的原始请求体清空并保留墓碑，所以重放语义
    不变、库仍会缓慢增长（墓碑永不删除；需要彻底回收时停机后 `VACUUM` 或连同 HS
    一起重建）。
  - **文件权限**：网关新建库文件时以 `0600` 创建；WAL 旁文件（`-wal` / `-shm`）
    含未 checkpoint 的原始事务体，同等敏感，网关会在主库 `0600` 时对存在的旁文件
    补 `chmod 0600`（尽力而为）。**库目录**权限不由网关修改——部署侧应设为 `0700`、
    属 gateway 进程用户独占（compose 里的 `gateway-data` 卷目录建议在首次
    `make up GATEWAY=1` 后确认权限）。该库与 `gateway-data.tar.gz` 备份都**含原始
    事件请求体**，按敏感用户数据保管（目录 700、文件 600），不入库、不入工单。
    清理日志只记条数，不记 txnId 或事件内容。
  - continuwuity：仍用 `memory`，注册 url 为 `null`。它还没有文件方式的
    appservice 注册，切到 appservice 后启动 whoami 会失败、容器会反复退出。
    （网关照样注入 `GATEWAY_HS_TOKEN`、打开事务库，只是没有 HS 会推过来。）
  - token 规则：init.sh 生成的两个值都是 64 位小写 hex，且互不相同。网关在
    `GATEWAY_HS_TOKEN` 与 `GATEWAY_AS_TOKEN` 相同、或 hs_token 不足 32 字符 /
    是占位值 / 含空白时拒绝启动；`GATEWAY_HS_TOKEN` 为空时照常启动，但
    `/_matrix/app/v1/*` 一律 403。
  - **compose 兜底值**（变量没设时才生效，例如 init 之前的 `compose config`，或
    绕过 make 直接 `docker compose up`）：

    | 网关变量 | compose 写法 | 兜底 |
    | --- | --- | --- |
    | `GATEWAY_MATRIX_TRANSPORT` | `${GATEWAY_MATRIX_TRANSPORT:-memory}` | `memory` |
    | `GATEWAY_HOMESERVER_URL` | `http://${BOTVILLE_HOMESERVER:-synapse}:8008` | `http://synapse:8008` |
    | `GATEWAY_HOMESERVER_FEDERATION_URL` | `""` | 空（回落到上一行） |
    | `GATEWAY_MATRIX_SERVER_NAME` | `${BOTVILLE_SERVER_NAME:-localhost}` | `localhost` |
    | `GATEWAY_AS_TOKEN` | `${APPSERVICE_AS_TOKEN:-}` | 空（appservice 模式会拒绝启动） |
    | `GATEWAY_HS_TOKEN` | `${APPSERVICE_HS_TOKEN:-}` | 空（AS 回调一律 403） |
    | `GATEWAY_AS_TXN_DB_PATH` | 固定 | `/var/lib/botville-gateway/as_transactions.db` |
    | `GATEWAY_MODEL_*` / `GATEWAY_PERSONAS_FILE` / `GATEWAY_CONSUMER_ENABLED` | 透传 | 见[角色引擎](#角色引擎模型与人设)的回退值表 |

  - **只通过 `make up` 重建网关**：`BOTVILLE_HOMESERVER` / `GATEWAY_MATRIX_TRANSPORT`
    只在 `make up` 里导出（`HS=` 可能与 `.env` 不同），注册 url 也只在
    `make up` 里切换。直接 `docker compose up -d` / `--force-recreate gateway`
    会回到上表的兜底值（memory 传输），HS 却可能还在往网关推事务。
    `make gateway-hs-smoke` 会检查容器里的 `GATEWAY_MATRIX_TRANSPORT`，与 HS
    不符时报错并提示重新 `make up`。
  - **只能跑一个网关**：网关是单进程（SQLite 库加进程内锁）。不要
    `--scale gateway=2`，不要让第二个网关挂同一个卷或使用同一份注册文件。
  - 运维注意：appservice 模式下网关启动会 whoami，失败即退出（`restart:
    unless-stopped`，并对当前 HS `depends_on`）；改 `BOTVILLE_SERVER_NAME` 时
    注册正则和 `GATEWAY_MATRIX_SERVER_NAME` 要同步改；轮换 `APPSERVICE_AS_TOKEN`
    或 `APPSERVICE_HS_TOKEN` 时 HS（重新渲染注册文件并重启）与网关
    （`make up GATEWAY=1` 重建）必须同时切换；明文 `http://` 只允许 loopback
    或单标签服务名。
  - 验收：`make gateway-hs-smoke`——whoami、OpenID userinfo，以及 AS 事务：HS→
    网关 ping，`@ai_*` 所在房间的消息被推送并落库，网关重启后重放同一 txnId 只存
    一份，错误 hs_token 得 403。不打印 token；失败只报 HTTP 状态和完整 URL；
    ping 遇 401/403（token 不对、未注册）立即失败，429 / 5xx 才重试。
    夹具不会越积越多：人类测试用户（固定 device）、`@ai_gwsmoke` 和测试房间跨次复用，
    状态记在 `server/compose/generated/gateway-hs-smoke.json`（gitignore，0600，
    按 HS 地址分开）；HS 不认识它们时（如 `down -v` 之后）才重建，被替换的房间会
    先退出并忘记。

## 角色引擎（模型与人设）

B 线 PR2（角色引擎回复）读的变量，compose 原样透传给 `gateway` 服务；init.sh
**一个都不生成**（`.env.example` 里是空值或默认值，`make up` 时追加进 `.env`，
已有值不动）。改完后 `make up GATEWAY=1` 重建网关才生效。

**只有三件事同时满足，AI 才会开口**：`GATEWAY_CONSUMER_ENABLED=true`；配了模型
（`GATEWAY_MODEL_BASE_URL` 与 `GATEWAY_MODEL_NAME`）；配了人设
（`GATEWAY_PERSONAS_FILE` 指向存在的文件）。缺任何一项都只记录、不发言。

| 变量 | compose 写法 | 回退值 | 说明 |
| --- | --- | --- | --- |
| `GATEWAY_CONSUMER_ENABLED` | `${GATEWAY_CONSUMER_ENABLED:-false}` | `false` | 事务消费者总开关，默认关 |
| `GATEWAY_MODEL_BASE_URL` | `${GATEWAY_MODEL_BASE_URL:-}` | 空 | OpenAI 兼容接口地址；空 = 未配模型，只记录不发言 |
| `GATEWAY_MODEL_NAME` | `${GATEWAY_MODEL_NAME:-}` | 空 | 模型名 |
| `GATEWAY_MODEL_API_KEY` | `${GATEWAY_MODEL_API_KEY:-}` | 空 | 密钥，只写在部署方自己的 `server/compose/.env` |
| `GATEWAY_MODEL_TIMEOUT_SECONDS` | `${GATEWAY_MODEL_TIMEOUT_SECONDS:-30}` | `30` | 单次模型调用超时（秒） |
| `GATEWAY_PERSONAS_FILE` | `${GATEWAY_PERSONAS_FILE:-}` | 空 | 空 = 不加载人设；建议 `/etc/botville-gateway/personas.json` |
| `BOTVILLE_GATEWAY_PERSONAS_DIR` | 挂载源 `${BOTVILLE_GATEWAY_PERSONAS_DIR:-./gateway-personas}` | `server/compose/gateway-personas` | 只读挂到容器 `/etc/botville-gateway` |

**人设文件（只读挂载）**

- compose **总是**把 `server/compose/gateway-personas/` 只读挂到
  `/etc/botville-gateway`。这个目录随仓库提供，里面只有 `README.md` 和虚构的
  `personas.example.json`，所以没配人设时挂载、`compose config` 和网关启动都不会
  报错（选这种做法而不是可选 override 文件，是为了不让 `make up` 多一套参数）。
- 真实人设放同目录的 `personas.json`（`.gitignore` 排除了该目录下示例和 README
  以外的所有文件），再在 `.env` 里设
  `GATEWAY_PERSONAS_FILE=/etc/botville-gateway/personas.json`。也可以用
  `BOTVILLE_GATEWAY_PERSONAS_DIR` 换成仓库外的宿主机目录（绝对路径）。
- 网关以 uid 10001 运行：Linux 宿主机上文件需对其他用户可读（`0644`）或属主为
  10001。人设文件里不要放 token / API key。文件格式以 B 线 PR2 契约为准。

**`GATEWAY_MODEL_API_KEY` 不外泄**

- 只经 compose 从 `--env-file` 插值进容器；Makefile 不引用它，`make` 的命令回显
  和 `make -n` 都不会出现它；init.sh 不生成、不打印（追加缺失键时只打印键名）；
  冒烟脚本不读它，也不打印任何 token。
- **不要贴 `docker compose config` 的输出**：不带 `--quiet` 时它会把插值后的值
  原样打印出来。CI 与文档一律用 `config --quiet`；CI 另有一步用哨兵值验证
  12 种 HS × profile 组合都不会打印它，`server/tests` 里也有对应断言。
- 轮换：改 `.env` 后 `make up GATEWAY=1`。

## 注册方式差异

| | Synapse | Tuwunel | continuwuity |
| --- | --- | --- | --- |
| 机制 | `homeserver.yaml` 的 `app_service_config_files` 列出注册文件 | `TUWUNEL_APPSERVICE_DIR` 指向注册文件目录 | 仅 admin room（`!admin appservices register`），**无配置文件方式** |
| 本仓库接线 | compose 挂载 `./generated/appservice:/appservice:ro`，模板 `synapse/homeserver.yaml.tmpl` 引用 `/appservice/botville-gateway.yaml` | compose 同一目录挂载到 `/appservice`，环境变量 `TUWUNEL_APPSERVICE_DIR=/appservice` | **本任务未接线**（见下） |
| 生效方式 | 重启容器 | 重启容器（无热加载） | 管理室即时生效 |
| 注册文件格式 | 同一份 YAML | 同一份 YAML（Synapse 同款） | 同一份 YAML（粘贴到管理室） |

三家共用**同一份 YAML 注册文件**；差异只在 homeserver 从哪里读它。
Tuwunel 的 `appservice_dir`（`[global]` 配置项，容器环境变量
`TUWUNEL_APPSERVICE_DIR`）在官方文档 `docs/appservices.md` 中说明，注册文件
目录变更需重启；`url: null` 已在 Tuwunel v1.9.4 上实测可用（`appservice-e2e
(tuwunel)` job 每次提交复验）。

**continuwuity（后续）**：目前只能通过管理室消息注册 appservice（部署层
自动化要走管理员账号投递多行 YAML，或容器 `--execute`）。本任务不实现、CI
不跑 continuwuity 的 appservice job；接入时在部署层加对应分支并补
`appservice-e2e (continuwuity)` 矩阵项即可，网关与验收脚本无需改动。

## 网关配置

网关（`gateway/`，B 线）通过环境变量选择传输（`gateway/.env.example` 有占位）：

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `GATEWAY_MATRIX_TRANSPORT` | `memory` | `memory`=内存假后端（B1.1 现状）；`appservice`=真实代发 |
| `GATEWAY_HOMESERVER_URL` | — | homeserver 的 Client-Server API 地址。compose 里是 `http://${BOTVILLE_HOMESERVER}:8008`（容器内服务名；不要写 `127.0.0.1`）。明文 `http://` 只允许 loopback（`localhost`/`127.0.0.1`/`::1`）与无点单标签容器名（如 `synapse`/`tuwunel`），其余（含任意非 `::1` 的 IPv6、空 host）拒绝；跨主机必须 `https://` |
| `GATEWAY_HOMESERVER_FEDERATION_URL` | *(空)* | OpenID userinfo 专用基址；**留空则回落到 `GATEWAY_HOMESERVER_URL`**。compose 里刻意留空，走内网直连，不要指向 fedgate 或公网 |
| `GATEWAY_MATRIX_SERVER_NAME` | — | homeserver 的 `server_name`（主机名或 IP，可带 `:端口`；统一小写），用于命名空间校验 |
| `GATEWAY_AS_TOKEN` | — | 与 `server/compose/.env` 的 `APPSERVICE_AS_TOKEN` 一致；非空、≥32 字符、不含空白，否则拒绝启动 |
| `GATEWAY_HMAC_SECRET` | — | **必填**（#114 PR-a / N10）：**只接受十六进制**，至少 **64 个 hex 字符**（= 32 字节），拒绝全零；用于 txnId 与 `text_sha256` 的 HMAC。留空、为 `<change-me>` 或非 hex 时网关拒绝启动（fail-closed）。生成：`python3 -c 'import secrets;print(secrets.token_hex(32))'` |
| `GATEWAY_ROOM_ALIAS_PREFIX` | `botville_conv_` | conversation → 房间别名 localpart 前缀（见上文 ID 语义）；**改前缀须同步改注册模板的 aliases 正则** |
| `GATEWAY_ROOM_ALIAS_CACHE_TTL` | `60` | 别名 → room_id 进程内缓存 TTL（秒）；过期后重新 directory lookup 并刷新；`0` = 不缓存（每次 lookup），负数拒绝启动 |
| `GATEWAY_ALLOW_INSECURE_HS` | `false` | 显式放行明文 `http://` 的跨主机 homeserver（接受 as_token 明文传输风险） |

`appservice` 模式缺任一项（或 as_token 仍是 `<change-me>`/过短/含空白）时
网关**拒绝启动**（fail closed）。启动时还会用 AS token 调一次
`GET /_matrix/client/v3/account/whoami` 做健康校验，失败即退出（fail fast），
不让 `/health` 误报。token 不会出现在启动横幅、日志或异常里。

示例：

```bash
GATEWAY_MATRIX_TRANSPORT=appservice \
GATEWAY_HOMESERVER_URL=http://127.0.0.1:8008 \
GATEWAY_MATRIX_SERVER_NAME=localhost \
GATEWAY_AS_TOKEN=<与 server 侧一致的随机值> \
GATEWAY_HMAC_SECRET=<change-me> \
GATEWAY_AUTH_TOKEN=<网关自己的随机值> \
PYTHONPATH=gateway/src python3 -m botville_gateway
```

## token 轮换

`as_token` 泄漏 = `@ai_*` 命名空间整体失控，轮换步骤：

1. 重新生成 `server/compose/.env` 里的 `APPSERVICE_AS_TOKEN`（必要时连同
   `APPSERVICE_HS_TOKEN`）。只改这一个键，例如：

   ```bash
   new=$(openssl rand -hex 32)
   # macOS/BSD 与 GNU sed 的就地写法不同，以下用可移植方式：
   python3 - "$new" <<'EOF'
   import sys
   path = "server/compose/.env"
   token = sys.argv[1]
   lines = []
   for line in open(path, encoding="utf-8"):
       if line.startswith("APPSERVICE_AS_TOKEN="):
           line = "APPSERVICE_AS_TOKEN=" + token + "\n"
       lines.append(line)
   open(path, "w", encoding="utf-8").writelines(lines)
   EOF
   ```

2. `bash server/scripts/init.sh`（重新渲染注册文件，仍是 0600）；
3. 重启 homeserver 栈（`make down && make up`，用网关时 `make up GATEWAY=1`；
   Synapse/Tuwunel 都是重启生效）；
4. compose 网关从同一变量注入 `GATEWAY_AS_TOKEN` / `GATEWAY_HS_TOKEN`，第 3 步
   的 `make up GATEWAY=1` 会一起重建网关；仓库外单独运行的网关要手动同步并重启。

注意：轮换是**全量替换**，没有灰度；网关与 homeserver 必须同批切换。

## 安全须知

- **`as_token` 等同整个 `@ai_*` 命名空间的管理员**：持有者可以免密注册命名
  空间内任意账号、以任意 `@ai_*` 身份发言。它与 `.env` 里其他密钥同级管理：
  渲染产物 0600、gitignore、绝不入库/入日志/入工单。
- `hs_token` 已被网关的 AS 回调（B 线 as-transactions）使用：`GATEWAY_HS_TOKEN`
  与它同值，用于校验 `PUT /_matrix/app/v1/transactions/{txnId}` 与
  `POST /_matrix/app/v1/ping`（`Authorization: Bearer`，恒定时间比较，不入日志）。
  compose 把 `APPSERVICE_HS_TOKEN` 注入为 `GATEWAY_HS_TOKEN`；注册 url 只在
  `make up GATEWAY=1`（synapse/tuwunel）时是 `http://gateway:8080`，其余为
  `null`。网关在未配置该变量时对上述端点一律 403。轮换 `hs_token` 时须同时更新
  注册文件（重启 HS）与网关的 `GATEWAY_HS_TOKEN`（且不能与 `as_token` 相同，
  网关会拒绝启动）。
- 命名空间 exclusive 是**纵深防御的第一层**，不是唯一一层：网关在客户端侧
  也校验 sender（`@ai_` 前缀 + 本 server），homeserver 侧再拦一次越权
  masquerade。两侧拒绝行为都在 CI 断言。
- 注册文件对容器只读挂载（`/appservice:ro`）；容器以 root 读取 0600 文件，
  其他容器用户读不到。

## 验收与排障

- **一键验收**（对已启动的栈）：

  ```bash
  make up                        # 或 make up HS=tuwunel
  make appservice-e2e            # 注册→建房邀请→AI 代发→回读断言→网关 HTTP 链路→双向越权拒绝
  ```

脚本 `server/tests/appservice_e2e.py`：人类用户（UIA）建房并邀请
`@ai_e2e_<随机>`；`AppServiceMatrixTransport` 以该身份注册（
`m.login.application_service`）、自动进房并发消息；人类用户回读断言
sender/body。随后走**网关 HTTP 链路**：AS 侧以 `@ai_<persona>` 身份
（as_token masquerade）建房、认领别名 `room_alias_name=botville_conv_<随机>`
并邀请 alice（exclusive 别名只能 AS 认领，#107），alice 进房；进程内启动
真实网关（`build_server`，appservice 传输，端口 0），向
`/v1/conversations/<conversation_id>/messages` 发
`sender_persona_id=<persona>`，断言 2xx 且 alice 在房间读到
`sender=@ai_<persona>:<server>`、body 一致。同时断言渲染出的注册文件里
`hs_token` 非空（64 hex、与 as_token 不同）。最后断言普通 UIA 注册
`ai_*` 被拒、人类 `createRoom` 认领 `botville_conv_*` 别名被拒（两者
errcode 都是 `M_EXCLUSIVE`；若更换的 HS 对排他返回不同错误码，须在本文档
登记并同步调整断言，保持两家一致）、网关命名空间外发送被拒、直接拿
as_token masquerade `@bob` 被 HS 拒。
- **常见失败**：
  - `APPSERVICE_AS_TOKEN missing or placeholder`：没跑 `make init`，或手工
    `.env` 没补该键；
  - `init.sh: APPSERVICE_*_TOKEN ... is not 64 lowercase hex characters`：
    手工编辑的 token 非法，init.sh 拒绝渲染（不覆盖手填值）。清空该行后
    重跑 `bash server/scripts/init.sh` 即可自动重新生成；
  - masquerade 全部 401 `M_UNKNOWN_TOKEN`：homeserver 没有加载注册文件
    （Synapse：`app_service_config_files` 是否指向挂载路径；Tuwunel：
    `TUWUNEL_APPSERVICE_DIR` 是否设置），改完记得重启容器；
  - 普通 `ai_*` 注册居然成功：注册文件不是 exclusive（被手改过）或 homeserver
    没加载它；
  - 人类建房报 `M_EXCLUSIVE`：`#botville_conv_*` 是 appservice 的 exclusive
    别名命名空间（#107），会话房间须由 AS 侧创建（见上文房间别名约定）——
    这是预期拒绝，不是故障；
  - Tuwunel 起不来/不认注册文件：先看 `docker compose logs tuwunel`；若日志
    显示解析 `url: null` 失败，说明该版本要求字符串 URL——此时保持机制不变，
    在部署层给 Tuwunel 单独渲染一个 `url` 占位（如 `http://127.0.0.1:1`）并
    在本文档登记（HS 会重试连接但无功能影响），或升级 Tuwunel。
- CI：`appservice-e2e` matrix `[synapse, tuwunel]`，结构与 `smoke` /
  `backup-restore` 一致（init → 开放注册 → `make up` → 运行 → 失败 dump
  logs → `down -v`）。
