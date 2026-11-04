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
4. [注册方式差异（三家 homeserver）](#注册方式差异)
5. [网关配置](#网关配置)
6. [token 轮换](#token-轮换)
7. [安全须知](#安全须知)
8. [验收与排障](#验收与排障)

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
  | `APPSERVICE_HS_TOKEN` | HS → 网关：A2 接收事务时校验（当前未使用） |

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
- `url: null` 是有意为之：M1 网关不接收 HS 推送的事务，null 让 HS 无处可推、
  也不会重试。A2 网关开始接收事务时改为 `http://gateway:<port>`。

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
| `GATEWAY_HOMESERVER_URL` | — | homeserver 的 Client-Server API 地址（如 `http://127.0.0.1:8008`）；明文 `http://` 只允许 loopback（`localhost`/`127.0.0.1`/`::1`）与无点单标签容器名（如 `synapse`/`tuwunel`），其余（含任意非 `::1` 的 IPv6、空 host）拒绝；跨主机必须 `https://` |
| `GATEWAY_MATRIX_SERVER_NAME` | — | homeserver 的 `server_name`（主机名或 IP，可带 `:端口`；统一小写），用于命名空间校验 |
| `GATEWAY_AS_TOKEN` | — | 与 `server/compose/.env` 的 `APPSERVICE_AS_TOKEN` 一致；非空、≥32 字符、不含空白，否则拒绝启动 |
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
3. 重启 homeserver 栈（`make down && make up`；Synapse/Tuwunel 都是重启生效）；
4. 同步更新网关侧 `GATEWAY_AS_TOKEN` 并重启网关。

注意：轮换是**全量替换**，没有灰度；网关与 homeserver 必须同批切换。

## 安全须知

- **`as_token` 等同整个 `@ai_*` 命名空间的管理员**：持有者可以免密注册命名
  空间内任意账号、以任意 `@ai_*` 身份发言。它与 `.env` 里其他密钥同级管理：
  渲染产物 0600、gitignore、绝不入库/入日志/入工单。
- `hs_token` 当前未被任何组件校验（网关不收事务），但仍按密钥保管：A2 启用
  事务接收后它就是 HS→网关的鉴权凭据。
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
