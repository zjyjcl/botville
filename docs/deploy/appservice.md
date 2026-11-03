# Appservice：AI 账号命名空间与网关代发（A1.3）

任务 A1.3（issue #5）部署文档 · 对象：botville homeserver 栈（Synapse / Tuwunel）与
AI 网关（`gateway/`）。

> **验证状态**：本机制的验收（网关以 `@ai_*` 身份注册、进房、代发；命名空间
> exclusive 双向拒绝）由 CI 的 `appservice-e2e (synapse)` 与
> `appservice-e2e (tuwunel)` 两个 job 在每次提交时对真实 homeserver 完整执行。
> Tuwunel 对注册文件中 `url: null` 的解析行为以该 job 的绿灯为准（见下文
> [注册方式差异](#注册方式差异)）。

本文只使用 Matrix 标准接口（Application Service API + Client-Server API
masquerade）；运行时不使用 `/_synapse/admin`，也不使用 admin room 命令。
示例中一律是占位符，不含真实密钥。

---

## 目录

1. [命名空间约定](#命名空间约定)
2. [注册文件与渲染](#注册文件与渲染)
3. [注册方式差异（三家 homeserver）](#注册方式差异)
4. [网关配置](#网关配置)
5. [token 轮换](#token-轮换)
6. [安全须知](#安全须知)
7. [验收与排障](#验收与排障)

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
  ```

  两端锚定（`^…$`）。`<server_name>` 由 `init.sh` 做**正则转义**后注入
  （`example.com` → `example\.com`），避免域名中的点被当成通配。
- AS 的 `sender_localpart`（`botville-gateway`）与命名空间外的普通账号互不冲突。

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
目录变更需重启；`url: null` 在 Tuwunel 上的解析以 CI `appservice-e2e
(tuwunel)` job 实测为准（该 job 全链路走真实容器）。

**continuwuity（后续）**：目前只能通过管理室消息注册 appservice（部署层
自动化要走管理员账号投递多行 YAML，或容器 `--execute`）。本任务不实现、CI
不跑 continuwuity 的 appservice job；接入时在部署层加对应分支并补
`appservice-e2e (continuwuity)` 矩阵项即可，网关与验收脚本无需改动。

## 网关配置

网关（`gateway/`，B 线）通过环境变量选择传输（`gateway/.env.example` 有占位）：

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `GATEWAY_MATRIX_TRANSPORT` | `memory` | `memory`=内存假后端（B1.1 现状）；`appservice`=真实代发 |
| `GATEWAY_HOMESERVER_URL` | — | homeserver 的 Client-Server API 地址（如 `http://127.0.0.1:8008`） |
| `GATEWAY_MATRIX_SERVER_NAME` | — | homeserver 的 `server_name`，用于命名空间校验 |
| `GATEWAY_AS_TOKEN` | — | 与 `server/compose/.env` 的 `APPSERVICE_AS_TOKEN` 一致 |

`appservice` 模式缺任一项（或 as_token 仍是 `<change-me>`）时网关**拒绝启动**
（fail closed）。token 不会出现在启动横幅、日志或异常里。

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
  make appservice-e2e            # 注册→建房邀请→AI 代发→回读断言→双向越权拒绝
  ```

  脚本 `server/tests/appservice_e2e.py`：人类用户（UIA）建房并邀请
  `@ai_e2e_<随机>`；`AppServiceMatrixTransport` 以该身份注册（
  `m.login.application_service`）、自动进房并发消息；人类用户回读断言
  sender/body；随后断言普通 UIA 注册 `ai_*` 被拒、网关命名空间外发送被拒、
  直接拿 as_token masquerade `@bob` 被 HS 拒。
- **常见失败**：
  - `APPSERVICE_AS_TOKEN missing or placeholder`：没跑 `make init`，或手工
    `.env` 没补该键；
  - masquerade 全部 401 `M_UNKNOWN_TOKEN`：homeserver 没有加载注册文件
    （Synapse：`app_service_config_files` 是否指向挂载路径；Tuwunel：
    `TUWUNEL_APPSERVICE_DIR` 是否设置），改完记得重启容器；
  - 普通 `ai_*` 注册居然成功：注册文件不是 exclusive（被手改过）或 homeserver
    没加载它；
  - Tuwunel 起不来/不认注册文件：先看 `docker compose logs tuwunel`；若日志
    显示解析 `url: null` 失败，说明该版本要求字符串 URL——此时保持机制不变，
    在部署层给 Tuwunel 单独渲染一个 `url` 占位（如 `http://127.0.0.1:1`）并
    在本文档登记（HS 会重试连接但无功能影响），或升级 Tuwunel。
- CI：`appservice-e2e` matrix `[synapse, tuwunel]`，结构与 `smoke` /
  `backup-restore` 一致（init → 开放注册 → `make up` → 运行 → 失败 dump
  logs → `down -v`）。
