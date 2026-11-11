# Synapse 与 Tuwunel/continuwuity 兼容性验证报告

| 项 | 内容 |
|---|---|
| 任务 | A0.1 —— Synapse 与 Tuwunel/continuwuity 兼容性验证（分支现状核实） |
| 里程碑 | M0（准备） |
| 文档性质 | 桌面核实（基于官方仓库 / 官方文档 / 官方变更日志），**接口行为需以 A0.2 CI 双服务器 smoke 实测为准** |
| 信息截止 | 2026-11-04（所有链接当日可访问） |
| 下一任务 | A0.2 仓库骨架、Compose profile、双服务器 CI |

**图例**：✅ 支持（官方文档/变更日志明确）｜🟡 支持（有配置差异或默认关闭，见备注）｜❓ 待 CI 实测（血统上支持但文档未逐条确认）｜❌ 不支持｜N/A 不适用（不依赖该实现）

---

## 0. 结论摘要（TL;DR）

1. **Conduit 基本停更**（最后常规版本 v0.7.0，2024-04，其后仅零星安全修复），**conduwuit 已于 2025-05-30 停止维护并归档**（GitHub 仓库只读）；两者都不进入 botville 安装向导。
2. **Tuwunel 与 continuwuity 是 conduwuit 的两个活跃继任分支**，截至 2026-11 都在频繁发版（Tuwunel v1.9.4 / 2026-10-10；continuwuity v26.9.1 / 2026-10-27，2026 年起改用日历版本号），均可作为 botville 的轻量服务器选项。
3. botville 依赖的全部标准接口（Client-Server / Application Service / Push Gateway）三家均宣称支持；**差异集中在部署与运维层**：appservice 注册方式、注册令牌、SSO、默认房间版本、媒体 legacy 端点默认值、数据库引擎（Postgres vs RocksDB）。
4. **`/_synapse/admin` 是 Synapse 私有 API，botville 运行时代码禁止依赖**（Tuwunel 1.7.0 起自带一个 Synapse 兼容 admin 子集，但覆盖可变、continuwuity 没有，同样不依赖）；Tuwunel/continuwuity 的 admin room 命令等私有管理面，只允许安装向导/运维脚本使用。
5. 对 A0.2 的核心约束：CI 需覆盖 **Synapse + 两个 Rust 分支共三份容器配置**（或至少 Synapse + 其一，见 §4.2）；注册引导、appservice 注册、配置文件三处必须按服务器分叉；示例配置只含占位符。

---

## 1. 分支现状核实

### 1.1 谱系

```
Conduit (timokoesters/conduit, Rust)
 └─ 已停更：v0.7.0 (2024-04) 后无常规发版，其后仅零星安全修复（最近 2025-12 修复 CVE-2025-49090）；
    开发托管移至 Famedly GitLab，主线为长期未出稳定版的 next 重写分支
    └─ conduwuit (girlbossceo/conduwuit) —— 归档：2025-05-30，仓库只读
       ├─ Tuwunel      (github.com/matrix-construct/tuwunel)     ← "官方继任者"，商业支持
       └─ continuwuity (forgejo.ellis.link/continuwuation/continuwuity) ← 社区延续
```

### 1.2 各分支现状（截至 2026-11-04）

| 服务器 | 状态 | 仓库 | 最新版本（日期） | 维护方 / 备注 |
|---|---|---|---|---|
| Synapse | ✅ 活跃（参考实现） | `github.com/element-hq/synapse`（2023-12 起从 matrix-org 迁移） | v1.164.1（2026-10）；支持的规范版本以 CI `/versions` 实测为准 | Element HQ 维护；Python + Rust；参考实现，接口覆盖最全 |
| Conduit | ❌ 停更 | `gitlab.com/famedly/conduit`（GitHub 为镜像） | v0.7.0（2024-04） | 最后常规版本；其后仅安全修复（2025-12 修复 CVE-2025-49090）；next 重写长期未出稳定版；**不进安装向导** |
| conduwuit | ❌ 已归档 | `github.com/girlbossceo/conduwuit` | — | 2025-05-30 宣布放弃维护，仓库转只读；**不进安装向导** |
| Tuwunel | ✅ 活跃 | `github.com/matrix-construct/tuwunel` | v1.9.4（2026-10-10） | 自称 conduwuit 官方继任者；专职团队维护，瑞士政府部署背书；conduwuit/Conduit 数据库可原地迁移，Synapse 迁移未支持 |
| continuwuity | ✅ 活跃 | `forgejo.ellis.link/continuwuation/continuwuity`（自建 Forgejo；`codeberg.org/continuwuity/continuwuity` 与 `github.com/continuwuity/continuwuity` 为镜像） | v26.9.1（2026-10-27） | conduwuit 社区延续（Continuwuation 社区）；2026 年起改用 `v年.月` 日历版本 |

### 1.3 部署要点对照

| 项 | Synapse | Tuwunel | continuwuity |
|---|---|---|---|
| 官方 Docker 镜像 | 官方双源：`docker.io/matrixdotorg/synapse`（Docker Hub）与 `ghcr.io/element-hq/synapse`（GHCR；旧 `ghcr.io/matrix-org/synapse` 已停用） | `ghcr.io/matrix-construct/tuwunel` 或 `docker.io/jevolk/tuwunel`；官方建议自动更新跟 `:latest`（`:preview`/`:main` 更频繁） | `forgejo.ellis.link/continuwuation/continuwuity`（主）；同步镜像 `ghcr.io/continuwuity/continuwuity`、`docker.io/jadedblueeyes/continuwuity`、`registry.gitlab.com/continuwuity/continuwuity` |
| 监听端口 | 单端口 `8008`（客户端 + 联邦同端口），容器内默认 `0.0.0.0:8008` | 单端口，默认 `port = 8008`（可向量多端口如 `[8080, 8448]`）；`address` 默认 `127.0.0.1`，**Docker 需改 `0.0.0.0`** | 单端口，默认 `8008`（`CONTINUWUITY_PORT`）；容器示例直接 `-e CONTINUWUITY_ADDRESS=0.0.0.0` |
| 对外联邦端口 | 反代 `8448 → 8008`（或 `443` + `.well-known` 委派） | 同左 | 同左；官方 Docker 文档主推 `443 + CONTINUWUITY_WELL_KNOWN` 委派，可省 8448 |
| 配置方式 | `homeserver.yaml`（生成于数据卷；`SYNAPSE_*` 环境变量支持有限） | `tuwunel.toml`（模板 `tuwunel-example.toml`，`[global]` 段；兼容 `CONDUWUIT_`/`CONDUIT_` 前缀环境变量） | `continuwuity.toml` 或全量 `CONTINUWUITY_*` 环境变量（`SERVER_NAME`/`DATABASE_PATH`/`ADDRESS`/`PORT`/`CONFIG`/`WELL_KNOWN`）；容器内可执行文件仍叫 `/sbin/conduwuit` |
| 数据库 | SQLite（默认，生产不建议）/ **PostgreSQL（推荐）** | **RocksDB 内嵌**（无外部 DB；支持 RocksDB Backup 引擎在线备份 `database_backup_path`） | **RocksDB 内嵌**（数据目录 `/var/lib/continuwuity`） |
| 管理面 | `/_synapse/admin` API（私有）+ CLI | admin room（`#admins`，`!admin` 命令）+ `--console` / `--execute`；另自 v1.7.0 起实现 **`/_synapse/admin` 兼容子集**（共享密钥注册、用户/房间/媒体等端点，覆盖逐步扩大） | admin room（`!admin` 命令）+ `uwu>` 控制台（`docker attach`）+ `--execute` |
| 注册控制 | `enable_registration` + UIA；多注册令牌仅经私有 admin API | `allow_registration` + `registration_token` / `registration_token_file`（静态多令牌）；**首个注册用户自动成为管理员** | 首次启动自动生成一次性注册令牌（打印在日志）；另有 `registration_token` / `registration_token_file` 静态令牌与管理室 `!admin token` 运行期增发；**首个注册用户自动成为管理员**并加入 `#admins` |
| 文档 | element-hq.github.io/synapse | matrix-construct.github.io/tuwunel（tuwunel.chat） | continuwuity.org |

### 1.4 「默认端口 8008/6167」核实结论

规划输入中的 `8008/6167` 是 **Conduit 时代的双端口模型**（8008 客户端 / 6167 联邦，Conduit 早期示例配置所示）。核实结果：

- **现行 Tuwunel 与 continuwuity 均为单监听端口模型**：客户端 API 与联邦 API 共用同一端口（默认 `8008`），`6167` 已不再使用；
- 对外联邦入口由**反向代理在 `8448`**（或经 `.well-known/matrix/server` 委派收敛到 `443`）暴露，与 Synapse 的部署惯例一致；
- 因此 A0.2 的 Compose 里三家统一为「容器内 8008 + 反代 8448/443」，无需为 Rust 分支单独开 6167。**以 CI 实测为准。**

### 1.5 分支结论

- botville 支持的服务器固定为三家：**Synapse（推荐，功能最全）、Tuwunel、continuwuity**，与根 README「选型」一致；
- Conduit / conduwuit 只读归档，**不出现在安装向导**；从 conduwuit 迁移到 Tuwunel 是官方支持的"二进制替换"（数据库原地迁移），未来如有用户诉求可在运维文档提及，不属于产品功能。

---

## 2. botville 依赖的标准接口清单

> 原则：botville 运行时**只依赖 Matrix 规范接口**（Client-Server API、Application Service API、Push Gateway 协议）；下表状态为文档层结论，逐行断言由 A0.2 CI smoke 落实（清单见 §4.2）。

### 2.1 Client-Server API

| 接口 / 端点 | Synapse | Tuwunel | continuwuity | 备注 |
|---|---|---|---|---|
| 版本协商 `GET /_matrix/client/versions` | ✅ | ✅ | ✅ | CI 健康检查第一步与能力协商；三家声明的规范版本不同（如 Tuwunel v1.8.2+ 已声明 v1.19），以 CI 实测回填 |
| 登录 `POST /_matrix/client/v3/login`、`POST /v3/logout` | ✅ | ✅ | ✅ | Tuwunel 额外支持密码登录开关（`login_with_password`）与 refresh token TTL 配置；continuwuity OAuth「新一代登录」模式下传统密码登录的可用性受 `compatibility_mode` 影响 |
| 注册 `POST /_matrix/client/v3/register`（UIA） | ✅ | ✅ | ✅ | 令牌机制差异见 §3.3；CI 用标准 UIA（`m.login.dummy` / `m.login.registration_token`）注册，不走任何 admin API |
| 身份自检 `GET /_matrix/client/v3/account/whoami` | ✅ | ✅ | ✅ | CI 断言返回的 `user_id` 与注册/登录一致 |
| 同步 `GET /_matrix/client/v3/sync` | ✅ | ✅ | ✅ | Tuwunel 有 sync 超时上下限配置；Rust 家族不做增量 state delta 的部分边缘行为需 CI 断言（`next_batch` 推进、`limited` 标志） |
| 建房 `POST /_matrix/client/v3/createRoom` | ✅ | ✅ | ✅ | **三家默认 room version 不同**（Tuwunel v1.9.2+ 新房默认 v12；continuwuity 参考配置明示默认 `"12"`、支持 6–12；Synapse 以其文档为准），botville 建房**显式指定 `room_version`**（建议 10/11），CI 断言三家一致 |
| 加入 `POST /v3/join/{roomIdOrAlias}`、`/_matrix/client/v3/rooms/{roomId}/join` | ✅ | ✅ | ✅ | |
| 发送 `PUT /_matrix/client/v3/rooms/{roomId}/send/{eventType}/{txnId}` | ✅ | ✅ | ✅ | 事务 ID 去重行为 CI 断言 |
| 拉取消息 `GET /_matrix/client/v3/rooms/{roomId}/messages`、`/context` | ✅ | ✅ | ✅ | 分页边界（`l/t/b`）行为 CI 断言 |
| 资料 `GET/PUT /_matrix/client/v3/profile/{userId}/{displayname\|avatar_url}` | ✅ | ✅ | ✅ | Tuwunel 可选 `require_auth_for_profile_requests`；全局改名对房间内 per-room 覆盖的传播策略两家实现有差异（Tuwunel `preserve_room_profile_overrides` 默认保留覆盖） |
| 媒体（认证）`/_matrix/client/v1/media/upload\|download\|thumbnail\|config\|preview_url` | ✅（1.111+，Matrix v1.11） | ✅（认证端点始终启用） | ✅（conduwuit 血统 2024 年即跟进 spec v1.11） | **Tuwunel/continuwuity 默认关闭旧版未认证 `/_matrix/media/*` 端点**（`allow_legacy_media` 默认 false）：2024-05 之前的旧客户端取不到媒体——botville 苹果端用 matrix-rust-sdk（新），不受影响；见 §3.6 |
| 推送器 `POST /_matrix/client/v3/pushers/set`、`GET /v3/pushers/` | ✅ | ✅ | ✅ | 三家都会按 pusher 的 `data.url` 调用推送网关 `/_matrix/push/v1/notify`（Tuwunel `notification_push_path` 默认即此路径）；推送规则计算细节 CI 断言 |
| `/.well-known/matrix/client`（客户端发现） | 🟡 自身仅可代管 `/.well-known/matrix/server`（`serve_server_wellknown`）；client well-known 需反代/静态站提供 | ✅ `[global.well_known]` 内置（client/server/support） | ✅ `[global.well_known]` 或 `CONTINUWUITY_WELL_KNOWN` 环境变量 | **botville 统一由反向代理层提供 well-known**，消除三家差异（A0.2 约束） |

### 2.2 Application Service API（botville 网关以 appservice 身份接入）

| 接口 / 端点 | Synapse | Tuwunel | continuwuity | 备注 |
|---|---|---|---|---|
| 注册文件（registration YAML：`id`/`url`/`as_token`/`hs_token`/`sender_localpart`/`namespaces`） | ✅ `app_service_config_files` 列表，改后**需重启** | ✅ 三种方式并存：admin room 注册（持久化进数据库）、`tuwunel.toml` 内联 `[global.appservice.<id>]`、`appservice_dir` 目录放 YAML（Synapse 同款格式，需重启） | ✅ 仅 admin room 注册（`!admin appservices register` + 粘贴 YAML，免重启）；当前参考配置**无** `appservice_dir`/内联注册键，即无配置文件方式 | 三家都接受**同一份 YAML 格式**；注册方式差异与自动化注入见 §3.1 |
| 事务推送 `PUT /_matrix/app/v1/transactions/{txnId}` | ✅ | ✅ | ✅ | HS→AS 事件批量推送，`hs_token` 鉴权；Tuwunel 支持透传 ephemeral（typing/回执，需注册文件声明）与 MSC3202 扩展；断点续传语义 CI 断言 |
| 用户查询 `GET /_matrix/app/v1/users/{userId}`、房间别名查询 `GET /_matrix/app/v1/rooms/{roomAlias}` | ✅ | ✅ | ✅ | botville 网关侧需实现（虚拟用户/别名由网关回答） |
| 命名空间独占（`exclusive: true`） | ✅ 普通用户不能注册冲突 ID | ✅（users/aliases 大小写不敏感；rooms 大小写敏感；exclusive 区间不得重叠） | ✅（同 conduwuit 语义） | botville 用 exclusive user namespace 承载 `@ai_*` 账号；两家 Rust 分支的 regex 为**非锚定**匹配，正则要自带 `^…$` |
| Ping（MSC2659，spec v1.7+）HS→AS `POST /_matrix/app/v1/ping`；AS→HS `POST /_matrix/client/v1/appservice/{appserviceId}/ping` | ✅（1.73 起以实验开关引入，规范 v1.7 声明后转正默认启用） | ✅（conduwuit v0.4.6 起实现并继承） | ✅（同血统） | **botville 网关必须实现接收 HS 的 ping 并正确应答**——ping 参与事务投递的健康判定（Tuwunel v1.8.3 起 stalled transaction 会在 ping 后重试），不应答有被误判下线、停发事务的风险；CI 加 ping 往返用例 |
| 虚拟用户代发（`?user_id=` masquerade）、`m.login.application_service` 命名空间内免密注册、设备管理 MSC4190 | ✅ 基本代发 | ✅（另有 `rate_limited`、`device_management` 开关） | ✅/❓（MSC4190 支持度以变更日志为准） | CI 断言：网关以虚拟用户身份 send、被 namespace 接受；AI 账号由 AS 持 `as_token` 走 `POST /register` + `m.login.application_service` 免密创建（规范路径，A1.3 建号方式） |

### 2.3 Push Gateway（Sygnal）

| 项 | 状态 | 备注 |
|---|---|---|
| Sygnal 组件 `element-hq/sygnal`（Docker Hub `matrixdotorg/sygnal`） | ✅ 独立组件 | Push Gateway 与 homeserver 实现无关：homeserver 侧只需 §2.1 的 `pushers/set` + 对 `/_matrix/push/v1/notify` 的调用，三家均支持。Sygnal 处于维护模式（安全/兼容性修复为主），当前满足 botville 用途；A1.2 接 APNs 实测送达 |
| `POST /_matrix/push/v1/notify`（Sygnal 侧接收） | ✅ | 苹果端 app_id/推送密钥在 Sygnal 配置；**本仓库文档不出现任何真实 APNs 密钥/团队号** |

### 2.4 Federation（仅说明：botville 只做「配对联邦」）

- botville 的联邦策略是**只与扫码配对过的服务器互通、任一方可单方面切断**（A2.1），配对闸门由 botville 自有组件（配对后端 + 反向代理/网络层）实现，**不依赖各 homeserver 的白名单能力**；
- 三家都实现标准 Server-Server API（`/_matrix/federation/v1/*`），互联邦本身可用。可作为辅助硬化的选项：Synapse 有 `federation_domain_whitelist`；Tuwunel 有 `allowed_remote_server_names_experimental`（白名单，实验性）与 `forbidden_remote_server_names`（黑名单）；continuwuity 有 `forbidden_remote_server_names` / `allowed_remote_server_names`（可设 `forbidden=[".*"]` 后仅放行配对方）——均不作为 botville 的功能依赖；
- **A2.1 已落地（2026-11）**：`server/pairing/`（标准库实现：pairing 服务 + fedgate 反代闸门 + SQLite 配对表）与独立 compose 项目 `botville-fed`（Synapse 双实例 hs-a/hs-b）进入 CI `federation-e2e` job，验收「未配对拒绝 / 配对后互发 / 双向切断即断流」；设计与运维细节见 [`docs/deploy/paired-federation.md`](../deploy/paired-federation.md)。Tuwunel/continuwuity 的配对联邦矩阵为后续扩展。
- **App 扫码配对首版只支持 Synapse（2026-11-10 决策 B3）**：[pairing-qr.md](../contracts/pairing-qr.md) 的一次性注册令牌首版只在 Synapse 上签发；Tuwunel / continuwuity 部署不提供 App 配对二维码，App 用密码登录。以后如要支持，需先核实两家一次性注册令牌的签发途径（目前只见静态 `registration_token` 配置）。

---

## 3. 差异与风险清单

### 3.1 appservice 注册方式差异（对 A1.3、A3.1 影响最大）

| | Synapse | Tuwunel | continuwuity |
|---|---|---|---|
| 注册途径 | `homeserver.yaml` 的 `app_service_config_files:` 列出 YAML 路径，**重启生效** | 三选一（并存）：① admin room `!admin appservices register` + 下方贴 YAML 代码块（入库、即时生效、免重启）；② `tuwunel.toml` 内联 `[global.appservice.<id>]`（随重启重载，不可用 unregister 移除）；③ `appservice_dir` 目录放 YAML（重启生效） | 官方文档：admin room `!admin appservices register` + 粘贴 YAML（免重启）；`!admin appservices list` / `!admin appservices unregister <name>`；**无配置文件注册方式**（参考配置无相关键） |
| 重复 ID | 重新注册需改配置重启 | **重复 ID 会替换旧条目**（配置文件来源的除外）；换 ID 前需先 `unregister` | ❓ 替换行为待CI；稳妥做法先 `unregister` 再注册 |
| 自动化注入 | 改 yaml + 重启容器，易脚本化 | admin room 本质是 Matrix 消息：CI/向导可用内联 TOML / `appservice_dir`（纯文件、无消息交互），或 `--execute` 启动参数 | 只能管理室消息或容器 `--execute`（continuwuity 亦支持）：CI 需以管理员账号登录后投递多行 YAML |

**风险与对策**：安装向导（A3.1）为三家各写一段「注册 appservice」步骤；CI 里 Synapse 用 `app_service_config_files`（改配置 + 重启），Tuwunel 优先**内联 TOML / `appservice_dir`**（纯文件、无消息交互），continuwuity 只能走**管理室消息 / `--execute`**——三家均可脚本化，只是路径不同。`as_token`/`hs_token` 一律 CI 生成、示例文件只放占位符。

**A1.3 落地（2026-11，issue #5）**：文件式注册已实装并纳入 CI——`server/compose/appservice/botville-gateway.yaml.tmpl` 由 init.sh 渲染（token 来自 `.env`，产物 0600）；Synapse 走 `app_service_config_files` + `/appservice` 只读挂载，Tuwunel 走 `TUWUNEL_APPSERVICE_DIR=/appservice`（同一份 YAML）。命名空间为 exclusive `^@ai_.*:<server_name>$`（server_name 校验后统一小写、正则转义）；注册文件 `url: null`（M1 不收事务）。验收由 CI `appservice-e2e` job（matrix `synapse, tuwunel`）对真实容器执行：网关以 `@ai_*` 身份注册（`m.login.application_service`）、加入被邀房间、以该身份发消息并被人类用户回读；网关 HTTP 链路（persona/conversation → `@ai_*` / 房间别名映射）同样在真实容器上断言；普通 UIA 注册 `ai_*` 被拒（断言 errcode 为 `M_EXCLUSIVE`）、命名空间外 masquerade 被 HS 拒（`M_EXCLUSIVE`/`M_FORBIDDEN`）。**桌面已实测通过：Synapse v1.164.1、Tuwunel v1.9.4（含 `url: null`）**；continuwuity 仍为后续（只能管理室注册，CI 不跑其 appservice job）。详见 [`docs/deploy/appservice.md`](../deploy/appservice.md)。

**A2.2 事务接线（2026-11，接 #192）**：`make up GATEWAY=1` 在 Synapse / Tuwunel 上把注册 `url` 渲染为 `http://gateway:8080`（其余情况仍为 `null`，url 变化时 `make up` 重启 HS），网关收 AS 事务并落库；CI 在两者上冒烟 HS→网关 ping、事务落库与重启后 txnId 幂等（Synapse v1.164.1、Tuwunel v1.9.4 本地也已跑通）。continuwuity 仍为 `null` + memory。

### 3.2 管理 API 边界（硬性规则）

- `/_synapse/admin/*` 是 **Synapse 私有 API**：注册令牌管理、用户管理、统计等都走它，无规范保证。Tuwunel 自 v1.7.0 起自行实现了一个 **Synapse 兼容 admin API 子集**（`/_synapse/admin/v1/register` 共享密钥注册、用户/房间/媒体/设备/访问令牌等端点，覆盖仍在扩大），continuwuity 没有兼容层——覆盖度各家不同且随时可变，**botville 运行时代码仍然禁止调用任何 `/_synapse/admin` 端点或 admin room 命令**；该兼容子集只作为安装向导/运维脚本在 Tuwunel 上复用 Synapse 工具链的便利项；
- 安装向导与运维脚本（部署期一次性动作）允许按服务器分叉使用各自管理面，但必须封装在 `server/` 部署层，不进入网关/App 代码路径；
- CI 冒烟全部走标准 API（注册用 UIA，管理员引导用「首个注册用户即管理员」的 Rust 分支惯例 / Synapse 则由向导预置管理员）。

### 3.3 注册令牌差异

- **Synapse**：多注册令牌的增删只存在于私有 admin API（`/_synapse/admin/v1/registration_tokens`）；标准路径只有 UIA（dummy/3PID/reCAPTCHA 等）与 `registration_shared_secret`（同样是私有注册接口）。向导若要发邀请码，需在部署层调私有 API 或预生成令牌写进配置。
- **Tuwunel**：`allow_registration` + 静态 `registration_token`（或 `registration_token_file` 存多个令牌）；**首个注册用户自动获得管理员**（`grant_admin_to_first_user` 默认 true）——向导须提醒用户第一个注册的必须是主人本人。
- **continuwuity**：首次启动在日志打印**一次性注册令牌**；之后可用 `registration_token` / `registration_token_file` 静态令牌，或管理室 `!admin token` 命令运行期增发；同样首用户即管理员并自动进 `#admins`。
- **实测（2026-11，#68）**：continuwuity 首启引导令牌**只出现在容器日志**（带 ANSI 色码），配置令牌（`CONTINUWUITY_REGISTRATION_TOKEN`）在首个账号创建前无效（UIA 返回 401 `M_FORBIDDEN` "Invalid registration token"，flows 仅 `["m.login.registration_token"]`）。
  botville 用 `server/scripts/bootstrap-continuwuity.sh`（`make up HS=continuwuity` 每次自动执行，幂等）从日志提取引导令牌，以标准 UIA 注册 `BOTVILLE_ADMIN_USER` 作为首个（管理员）账号，之后配置令牌才生效；日志无令牌时改用标准接口 `GET /_matrix/client/v1/register/m.login.registration_token/validity`（spec v1.2，MSC3231）核验引导状态，不再静默放行；一次性令牌被拒（`M_FORBIDDEN`）视为已有其他首个用户，成功退出（升级安全）。
  实测镜像锁定 `ghcr.io/continuwuity/continuwuity:v26.9.1@sha256:c9c62bc5c0a641f3f713945c701b069c7bc30d2612b54d4a5de80a5be19d5420`（docker-compose.yml 默认、`.env.example` 与 CI allowlist 同步此值）。
- **默认值差异（向导与 CI 都不得依赖默认）**：Tuwunel `allow_registration` 默认 **false**；continuwuity 默认 **true**（且无令牌的完全开放注册需显式确认开关，官方镜像另要求 `openreg` 编译特性）。三家配置一律显式写注册开关与令牌策略。
- 对 botville 的含义：「邀请注册」产品能力在三家的实现路径不同，统一方案：**向导预置静态注册令牌写进三家配置**（Synapse 用共享密钥/预生成令牌；Rust 家族用 `registration_token`），运行时 App 不感知差异。

### 3.4 SSO / 认证方式差异

- **Synapse**：CAS/SAML/OIDC SSO、密码、以及 MAS（Matrix Authentication Service，OAuth 2.0 新一代登录）可组合。
- **Tuwunel**：`[[global.identity_provider]]` OAuth/OIDC（品牌适配 GitHub/Google/Keycloak 等）、`[global.ldap]`、`[global.jwt]` 登录——能力已超 conduwuit 时代（conduwuit 无 SSO）。
- **continuwuity**：OAuth「新一代登录」（自带登录/注册页）+ 传统 UIA 密码登录，`[global.oauth] compatibility_mode` 默认 `hybrid`（两种并存，可选 `disabled`/`exclusive`）；认证源**内部认证 / 委托 OIDC 二选一**，启用委托 OIDC 后等效 `exclusive`——不支持 next-gen 登录的客户端将无法登录。
- botville 立场：核心流程只依赖**密码登录 + 注册令牌**（三家一致）；SSO 属于各部署的自选项，安装向导不做 SSO 配置（记入文档「不在范围内」），规避三家 SSO 语义差异。

### 3.5 空间（Spaces）、线程（Threads）、E2EE

| 项 | Synapse | Tuwunel/continuwuity | 风险 |
|---|---|---|---|
| Spaces（MSC1772/层级 MSC2946） | ✅ 完整 | 基本支持（Tuwunel 有 spaces 层级缓存配置项） | 层级分页/可见性细节需 CI + Element 实测；botville 的「群组/动态」自建存储，不把 Spaces 当依赖 |
| 线程（MSC3440/3856） | ✅ 完整 | 事件存储与 `/relations` 透传可用；线程订阅等细节 ❓ | botville 消息流不依赖线程聚合，App 侧暂不使用；若 M2+ 要用先补 CI 用例 |
| E2EE（Megolm） | ✅ 完整（密钥备份、交叉签名、secret storage） | 基础 E2EE 可用（keys query/claim、to-device、加密房间；Tuwunel `allow_encryption` 默认开） | **服务器端密钥备份等高级特性的实现完整度 ❓**：botville 产品规则「含 AI 会话默认不加密、人与人私聊可开」，建议 C2.6 只在 Synapse 上首发放开 E2EE 开关，Rust 分支待 CI + 客户端实测后放开 |
| 默认房间版本 | 以文档为准（较保守） | Tuwunel v1.9.2+ 新房默认 **v12**（`default_room_version = "11"` 可退回）；continuwuity 参考配置默认 `"12"`、支持 6–12（3–5 实验性） | botville 建房显式传 `room_version`（三家都支持 10/11），跨服房间版本一致由 CI 断言 |

### 3.6 媒体端点差异

- 三家都有认证媒体端点（spec v1.11）；
- **Tuwunel/continuwuity 默认禁用旧版未认证 `/_matrix/media/*`**（`allow_legacy_media = false`）：只影响 2024-05 之前的旧客户端/旧桥接；botville 自有客户端（rust-sdk）与依赖组件均走认证端点，策略上**不申请打开 legacy**；
- 远端媒体：Tuwunel/continuwuity 默认不再回源请求旧版未认证媒体（`request_legacy_media = false`），对「仅配对联邦」场景无影响。

### 3.7 数据库与运维差异（对 A1.1、A3.2）

| | Synapse | Tuwunel/continuwuity |
|---|---|---|
| 引擎 | PostgreSQL（推荐）/ SQLite | 内嵌 RocksDB，无外部数据库 |
| 备份 | `pg_dump`/快照，工具链成熟 | RocksDB Backup 引擎在线备份（两家均有 `database_backup_path` + `database_backups_to_keep`）；亦可停机冷备整个数据目录；**不能用 `pg_dump` 思路** |
| 迁移 | Synapse 自身升级即迁移 | 家族内迁移（conduwuit→Tuwunel 原地）；**Synapse ↔ Rust 家族之间无迁移通道**——用户选型后不提供跨实现搬家（写进安装向导提示） |

### 3.8 资源占用（定性，数字由 A0.2 同机实测回填）

- **Synapse + Postgres**：两容器；Python 主进程内存基线较高（数百 MB 量级），换取最全的功能覆盖与最成熟的生态工具链。
- **Tuwunel / continuwuity**：单容器、单进程 + 内嵌 RocksDB；两家默认读缓存约 `128MB + 64MB×核`、写缓存约 `48MB + 4MB×核`（`db_cache_capacity_mb`、`cache_capacity_modifier` 可调），小内存机器可整体下调。镜像体积小、启动快，低配自部署服务器与 CI runner 都友好。
- A0.2 CI 在同一 runner 上记录三家「启动时间 + 冒烟期间内存峰值」并回填本节，作为 A1.1 部署手册最低配置建议的依据。

### 3.9 其他差异与风险

1. **镜像源多样性**：continuwuity 主镜像在自建 Forgejo registry（`forgejo.ellis.link`），可用性依赖第三方自托管服务；A0.2 的 CI 与示例配置**优先用 GHCR 镜像源**（`ghcr.io/continuwuity/continuwuity`），并注明主源。Tuwunel 同理优先 `ghcr.io/matrix-construct/tuwunel`。
2. **容器细节**：continuwuity 镜像无 shell（排障用 `docker logs`/`docker attach` 控制台）；容器内命令名仍是 `/sbin/conduwuit`，Compose 里要写对。
3. **Docker DNS**：continuwuity 官方文档警告 Docker 内建 DNS 会拖慢联邦，建议挂载自定义 `/etc/resolv.conf`——CI 内网联邦冒烟若超时先查这一点（同时影响按容器名连 appservice）。
4. **well-known 服务位置**：见 §2.1 末行，统一由反代提供（含 `/.well-known/matrix/server` 委派），`.env.example` 已有 `BOTVILLE_PUBLIC_URL` 对应此用途。
5. **版本节奏**：Tuwunel `:latest` 约月更、continuwuity 日历版本月更、Synapse 双周更；A0.2 的 Compose **默认 pin tag + digest**，文档注明当前验证版本（当前：Synapse v1.164.1 / Tuwunel v1.9.4 / continuwuity v26.9.1 / Postgres 16-alpine，均为多架构 index digest，pin 表以 [`docs/ops/upgrade-images.md`](../ops/upgrade-images.md) §2 为准），升级流程与风险亦见该文档，策略细化归 A3.2。
6. **行为细节差异**：`/sync` 长轮询参数钳制（Tuwunel 有 min/default/max）、profile 全局改名的房间传播策略、push 规则计算差异（如 Tuwunel 的 MSC3664 related-event 匹配默认关闭 `msc3664_related_event_match = false`、每账号推送规则集上限 10000 条）——全部列入 CI 断言点而非假设一致。

---

## 4. 结论与对 A0.2 的约束

### 4.1 总体结论

- 「Synapse（推荐）或 Conduit 系活跃分支」的选型成立，但**把『Conduit 系活跃分支』具体化为 Tuwunel 与 continuwuity 两家**，Conduit/conduwuit 明确排除；
- botville 所需标准接口在三家文档层全部可用，未发现阻断性缺口；风险集中在部署/运维差异，均有对策（§3）；
- 本报告为桌面核实，**逐条支持状态以 A0.2 CI smoke 结果为准**，CI 结果出来后回填本表（`docs/compat/` 增补实测记录）。

### 4.2 CI 双服务器 smoke 测试的接口列表

CI 目标：同一套脚本对每个服务器实现跑一遍，断言全部通过（绿灯标准 = 接口行为一致）。

**服务器矩阵**：`synapse`、`tuwunel`、`continuwuity` 三个 profile 至少各跑一份；若资源受限，最低配置为 `synapse + tuwunel`，continuwuity 每日定时跑。

**必测清单（A0.2 验收门槛；同一套脚本对每台服务器各跑一遍，全绿才算过）**。就绪等待（`/health` 或容器探测）并入第 1 步，不单列：

| # | 步骤 | 涉及接口 | 断言 |
|---|---|---|---|
| 1 | 版本协商 | `GET /_matrix/client/versions` | 200；返回 A0.2 约定的最低规范版本（建议 ≥ v1.11 以覆盖鉴权媒体） |
| 2 | 注册主人账号（UIA） | `POST /_matrix/client/v3/register`（`m.login.registration_token` 或 `m.login.dummy`） | 200 + `user_id`/`access_token` |
| 3 | 登录 | `POST /_matrix/client/v3/login`（`m.login.password`） | 200 + 可用 `access_token` |
| 4 | 身份自检 | `GET /_matrix/client/v3/account/whoami` | 200；`user_id` 与第 2 步一致 |
| 5 | 建房（显式 `room_version`） | `POST /_matrix/client/v3/createRoom` | 200；返回 `room_id`，房间版本 = 请求值 |
| 6 | 发消息 | `PUT /_matrix/client/v3/rooms/{roomId}/send/m.room.message/{txnId}` | 200；返回 `event_id` |
| 7 | 拉消息 | `GET /_matrix/client/v3/rooms/{roomId}/messages` | 200；能读到第 6 步的 `event_id`，分页方向正确 |
| 8 | 同步 | `GET /_matrix/client/v3/sync` | 200；时间线包含该事件，`next_batch` 存在 |

**建议附加（不阻塞 A0.2 验收，A1 前补齐；全部走标准 API）**：

- 第二账号 `POST /v3/join/{roomId}` 入房，双方 sync 可见成员；`POST /v3/logout` 后旧 token 失效；
- profile 读写一致；鉴权媒体上传/下载（`/_matrix/client/v1/media/*`，无 token 被拒）；
- `POST /v3/pushers/set` + CI 内置假 push gateway 收到 `/_matrix/push/v1/notify`（payload 含 event_id）；
- `/.well-known/matrix/client`、`/server`（由反代提供，内容与 `BOTVILLE_PUBLIC_URL` 一致）；
- appservice 全链路：注册（部署层按服务器分叉）→ HS 推事务 `PUT /_matrix/app/v1/transactions/{txnId}` → 网关以虚拟用户 `?user_id=@ai_*` 代发 → namespace 外代发被拒 → AS users/rooms query 应答 → ping 往返（HS→AS `POST /_matrix/app/v1/ping` 与 AS→HS `POST /_matrix/client/v1/appservice/{id}/ping`，< 5s）→ 普通用户注册 `@ai_xxx` 被拒（exclusive 生效）。

**M0 明确不测**：配对联邦两两「互通 / 切断」（`/_matrix/federation/*`）。配对闸门是 A2.1 的产物，届时在同套 CI 上补对应冒烟：未配对服务器无法互通；配对后可互发；单方面切断后不可再发、不可再拉取。

### 4.3 对 A0.2 的硬性约束

1. **只写标准接口**：CI 脚本与后续 `server/` 代码不得调用 `/_synapse/admin` 等私有 admin 端点（含 Tuwunel 的兼容子集）或 admin room 命令；部署层（向导/Compose 初始化）的分叉逻辑单独放 `server/deploy/` 一类的目录。
2. **示例配置零真实值**：`as_token`/`hs_token`/注册令牌/Postgres 密码全部占位符（`<change-me>` 风格），CI 用脚本现场生成；与 `.env.example` 现状保持一致。
3. **镜像 pin 版本（tag + digest）**：四个服务（Synapse、Tuwunel、continuwuity、Postgres）默认一律 pin `tag@sha256:<多架构 index digest>`，且版本与本文档口径一致：**synapse v1.164.x**（`docker.io/matrixdotorg/synapse`，或官方双源之一 `ghcr.io/element-hq/synapse`）、**tuwunel v1.9.x**（`ghcr.io/matrix-construct/tuwunel`）、**continuwuity v26.9.x**（`ghcr.io/continuwuity/continuwuity`）、**postgres 16-alpine**（`docker.io/library/postgres`）；homeserver 的 tag 一律带 `v` 前缀（如 v1.164.1 / v1.9.4 / v26.9.1，Postgres 除外）；可在 `.env` 覆盖为 `:latest` 跟随上游但不推荐；不使用 Docker Hub 个人镜像作默认。当前 pin 表、备份与升级流程见 [`docs/ops/upgrade-images.md`](../ops/upgrade-images.md)。
4. **单端口模型**：三家容器统一 `8008` 对内；`6167` 不出现；反代统一 8448/443 与 well-known。
5. **注册引导分叉**：Synapse profile 预置管理员与注册策略；Rust 分支 profile 必须文档化「首个注册用户 = 管理员」并让 CI 先注册固定管理员账号。
6. **Tuwunel 容器**`address` 改 `0.0.0.0`（默认 `127.0.0.1` 在容器内不可达）；continuwuity 挂载自定义 `/etc/resolv.conf` 缓解 Docker DNS 问题。
7. **CI 断言而非假设**：§4.2 的每一步都显式断言，失败即红；结果回填本目录。

---

## 5. 参考来源（访问日期 2026-11-04）

- Tuwunel 仓库与文档：github.com/matrix-construct/tuwunel；matrix-construct.github.io/tuwunel；`docs/appservices.md`、`tuwunel-example.toml`（main 分支）
- Tuwunel 版本：GitHub Releases（v1.9.4，2026-10-10）；Waldur 部署文档（appservice 重复注册行为）
- continuwuity 官方文档：continuwuity.org（`/deploying/docker`、`/appservices`、`/authentication`、`/reference/config`（默认房间版本 `"12"`、`allow_legacy_media = false`、`[global.oauth]`、`forbidden/allowed_remote_server_names`、RocksDB 备份与缓存默认值）、`/guides/dns`）
- continuwuity 仓库与版本：forgejo.ellis.link/continuwuation/continuwuity；GitHub Releases（v26.9.1，2026-10-27；2026 年起日历版本）
- conduwuit 归档：github.com/girlbossceo/conduwuit（只读）；维护者博客（2025-05-30 放弃维护声明）
- Conduit 停更：最后常规版本 v0.7.0（This Week in Matrix 2024-04-26）；后续安全发版见 conduit.rs 更新日志（2025-12-30 修复 CVE-2025-49090）；开发现托管于 gitlab.com/famedly/conduit（`next` 重写分支）
- Tuwunel Releases（github.com/matrix-construct/tuwunel/releases）：v1.8.2+ 声明 Matrix v1.19；v1.9.2 起新房默认房间版本 v12；v1.8.3 AS ping 与事务重试联动
- conduwuit appservice ping：v0.4.6 发布说明（This Week in Matrix 2024-09-06）
- Synapse：github.com/element-hq/synapse；[安装指南](https://element-hq.github.io/synapse/latest/setup/installation.html)（官方镜像双源 Docker Hub / GHCR，旧 `ghcr.io/matrix-org/synapse` 停用见 issue #2）；matrix-docker-ansible-deploy defaults（v1.164.1）；配置文档（`app_service_config_files`）；变更日志（1.73 实验引入 MSC2659、规范 v1.7 声明后转正；1.111 认证媒体/Matrix v1.11 时代）
- Sygnal：github.com/element-hq/sygnal；Docker Hub matrixdotorg/sygnal
- Matrix 规范：spec.matrix.org（Client-Server API、Application Service API、Server-Server API、Push Gateway）
