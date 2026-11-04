# 仅配对联邦（Paired Federation，A2.1）

| 项 | 内容 |
|---|---|
| 任务 | A2.1 —— 仅配对联邦（配对后端 + fedgate 闸门 + 双实例 CI） |
| 代码 | `server/pairing/`（纯 Python 标准库，无第三方依赖） |
| 测试 | `server/pairing/tests/`（单测，无 Docker）、`server/tests/federation_e2e.py`（compose 双实例验收）、CI job `federation-e2e` |
| 相关 | [`docs/compat/homeserver-compat.md`](../compat/homeserver-compat.md) §2.4（不依赖各 HS 白名单的决策依据） |

**语义（验收标准）**：

1. **默认拒绝**：没有配对记录的服务器之间，联邦请求（发消息、拉 state/事件/媒体）一律被拒；
2. **配对后互通**：双方互换一次性邀请码（QR 载荷）后，可互相邀请入房、互发消息；
3. **单方切断**：任一方 `DELETE /v1/pairing/peers/{name}` 即刻本地生效，双向流量断绝，**不需要对方配合**；对端经 best-effort 通知同步标记（失败不影响本端切断）。

## 架构

```
                     ┌────────────── hs-a 服务器（botville 实例 A）──────────────┐
 其他服务器 ──TLS──▶ │ fedgate-a :8448（唯一对外端口，docker 别名 = server_name） │
                     │   ├─ /_matrix/key/v2/server*  → 放行 → synapse-a :8008    │
                     │   ├─ /_matrix/{federation,key/query,media} → 配对表校验    │
                     │   │     Authorization: X-Matrix origin=… ∈ active peers？ │
                     │   │     否 → 403 M_FORBIDDEN "server not paired"          │
                     │   ├─ /_matrix/pairing/*      → pairing-a :8090            │
                     │   └─ 其余 → 404                                             │
                     │ pairing-a :8090（内网/loopback）── SQLite /data/pairing.db │
                     │        ▲ fedgate 以只读连接（mode=ro）读取、1s 缓存；       │
                     │          WAL 模式下读者需能写 -shm，故共享卷挂 rw           │
                     └────────┴── fedgate-a ──────────────────────────────────────┘
```

- **fedgate 是权威闸门**（`server/pairing/fedgate.py`，`python -m pairing.fedgate`）：标准库 `ThreadingHTTPServer` 反向代理 + 可选 TLS。它只判断"这个 origin 是否在 active 配对表中"，**事件签名验证仍由上游 homeserver 完成**；方法/头/体/状态码原样转发，响应支持流式（Content-Length 透传或 connection-close 分帧）。配对表从 pairing 的 SQLite 以只读连接（`mode=ro`）读取（同一卷；WAL 模式下读者需要写 `-shm` wal-index，所以共享卷必须挂 rw），每 1s 刷新缓存——切断即刻生效，不触碰 homeserver。
- **pairing 是配对面**（`server/pairing/service.py`，`python -m pairing.service`）：invite（一次性邀请码）/redeem/accept/sever 的 HTTP API + 审计事件，SQLite 持久化（`server/pairing/store.py`：`invites`、`peers`、`events` 三表，WAL）。
- HS 白名单（Synapse `federation_domain_whitelist` 等）只作可选纵深防御，**不是功能依赖**（compat §2.4）；本 PR 未做自动渲染/重启编排（见「出站硬化（后续）」）。

## 配对流程（扫码）

```
A（发起方）                                    B（接受方）
POST /v1/pairing/invites (admin)
  ← {invite_id, expires_at, qr_payload}        （管理员线下出示 QR/文本）
  qr_payload = botville-pair:v1:<server_name>:
               <urlsafe-b64(api_url)>:<invite_id>:<secret>
                                               POST /v1/pairing/accept {qr_payload} (admin)
                                                 ├─ 解析载荷
                                                 ├─ POST <api_url>/redeem
                                                 │    {invite_id, secret,
                                                 │     server_name: B, api_url: B}
                                                 │    （经 fedgate-a 的 /_matrix/pairing 转发）
A 侧 redeem：                                   │
  1. 校验 invite：未用、未过期、sha256 常量时间比对
  2. （默认开启）拉取 https://<server_name>/_matrix/key/v2/server
     确认对端声明的 server_name 与其发布密钥一致（防冒名）
  3. 写 peers[B]=active（Invite 置 used）        │
                                                 └─ 成功后写 peers[A]=active
双方 fedgate 的 active 集合即刻包含对方 → 联邦放行
```

QR 载荷以 `:` 分隔共 6 段，server_name 含端口（`:`）的形式不支持（`parse_qr_payload` 会拒绝）。

## API 一览

pairing 服务（错误统一 `{"errcode","error"}`；`/_matrix/pairing/<x>` 是 `/v1/pairing/<x>` 的别名，供 fedgate 原样转发）：

| 端点 | 鉴权 | 说明 |
|---|---|---|
| `POST /v1/pairing/invites` | admin | 创建一次性邀请，返回 `{invite_id, expires_at, qr_payload}`；secret 只此一次出现 |
| `POST /v1/pairing/redeem` | 对端（限速） | `{invite_id, secret, server_name, api_url}`：消费邀请并写 `peers[server_name]=active`；每 IP 每分钟 10 次 |
| `POST /v1/pairing/accept` | admin | `{qr_payload}`：本端调用对端 redeem，成功后本地也写 active；对端错误原样透传 |
| `GET /v1/pairing/peers` | admin | 配对状态列表（含 `severed_by`、`peer_api_url`） |
| `DELETE /v1/pairing/peers/{name}` | admin | 单方切断：本地立即 severed（`severed_by=local`），随后 best-effort POST 对端 `/notify-severed`，失败忽略（响应 `notified` 标明） |
| `POST /v1/pairing/notify-severed` | 对端 | `{server_name}`：把该对端标 `severed_by=remote` |
| `GET /v1/pairing/allowed` | admin | active 名单（调试用；fedgate 直接读 SQLite） |
| `GET /health` | 无 | 存活探针 |

fedgate 路由规则：

| 路径 | 规则 |
|---|---|
| `/_matrix/key/v2/server*` | 放行（本服公钥，不含房间内容） |
| `/_matrix/federation/*`、`/_matrix/key/v2/query*`、`/_matrix/media/*` | 解析 `Authorization: X-Matrix origin=…`（引号/无引号均可，多个 Authorization 头取第一个 X-Matrix）；origin ∉ active 配对表（含缺失/畸形）→ `403 {"errcode":"M_FORBIDDEN","error":"server not paired"}` |
| `/_matrix/pairing/*` | 转发到 `PAIRING_UPSTREAM`（对端只需暴露 8448 一个端口） |
| 其余 | 404 |
| `/health` | 本地应答（存活探针） |

## 切断语义与状态机

`peers(server_name, state, paired_at, severed_at, severed_by, peer_api_url)`：

- `active` ⇄（redeem/accept/upsert）→ 初始态，fedgate 放行；
- `severed`（`severed_by=local`）：本端管理员 DELETE，即刻生效——本端 fedgate 拒绝该 origin 的联邦请求（对方发不进来、也拉不走）；
- `severed`（`severed_by=remote`）：收到对端 `/notify-severed`；
- **重新配对**：任一方再走一遍 invite→accept 即恢复 `active`（`severed_*` 字段清空）；
- 无记录 = 拒绝（默认拒绝不需要显式黑名单）。

出站方向：本端 homeserver 仍可能主动重试向已切断对端发送（Synapse 事务重试退避），会被**对端的入站闸门**拒绝——切断语义由双方各自的入站 fedgate 保证闭环，不依赖 HS 配置变更或重启。

## 安全说明

- **invite secret**：32 字节随机（urlsafe），库里只存 sha256；`hmac.compare_digest` 常量时间比对；一次性、默认 TTL 600s（`PAIRING_INVITE_TTL`）；redeem 每 IP 每分钟 10 次。
- **管理面**：`Authorization: Bearer $PAIRING_ADMIN_TOKEN`，常量时间比对；token 缺失时服务**拒绝启动**；pairing 服务只应绑定内网/loopback（compose 只把 admin 端口映射到 `127.0.0.1`）。
- **日志**：不打印 secret/token（qr_payload 只出现在请求体，永不落日志）。
- **对端身份校验**（`PAIRING_VERIFY_PEER_KEY`，默认开）：redeem 时拉取对端 `/_matrix/key/v2/server` 并要求 server_name 一致，防止冒名配对。`PAIRING_PEER_KEY_URL_TEMPLATE`（默认 `https://{server_name}/_matrix/key/v2/server`）供测试/特殊拓扑改写；`PAIRING_INSECURE_TLS=true` 关闭证书校验，**仅限 CI/一次性测试栈**（自签证书场景）。
- 已知边界（后续硬化）：`/notify-severed` 无鉴权，任何知道双方 server_name 的攻击者可冒发（影响是把已有配对置为 severed，DoS 而非数据泄露）；生产化前计划加配对期共享密钥签名。`server_name` 以 DNS 别名/私网地址部署时依赖网络层隔离。
- 审计：`events` 表记录 invite 创建/兑换/拒绝原因、配对、切断、通知成败、限速与对端校验失败。

## 运行（本地 / CI）

```sh
make federation-e2e   # 生成自签证书与配置 → 起 botville-fed 项目 → 跑验收
make federation-down  # 只 down -v 该项目
```

- compose：`server/compose/federation/docker-compose.federation.yml`（独立项目 `botville-fed`，子网 `172.30.81.0/24`，与主栈完全隔离）；hs-a/hs-b 两套 synapse（镜像同主 compose 的 pin）+ pairing + fedgate（`python:3.12-slim` 挂载 `server/pairing` 源码直接 `python -m` 运行）。
- fedgate 容器持有网络别名 `hs-a`/`hs-b`，对端 Synapse 对无端口 server_name 走 well-known→SRV→8448 回落后直达闸门。
- 宿主只映射测试端口且绑定 `127.0.0.1`：`18101/18102`（两台 Synapse client API）、`18111/18112`（pairing admin）。
- 运行时生成（全部 gitignored）：`server/compose/federation/generated/certs/`（`server/scripts/federation-certs.sh` 的自签证书）、`hs-a|hs-b/homeserver.yaml` 与 `federation.env`（`server/scripts/federation-setup.py`，token 全随机）。
- CI-only 配置叠加（写在生成的 homeserver.yaml 里，勿抄进生产）：`federation_verify_certificates: false`、`federation_ip_range_blacklist: []`、`enable_registration: true`、`trusted_key_servers: []`；pairing/fedgate 的 `PAIRING_INSECURE_TLS: "true"`。
- 单测（无需 Docker）：`python3 -m unittest discover -s server/pairing/tests`。

### 环境变量

pairing 服务：

| 变量 | 默认 | 说明 |
|---|---|---|
| `PAIRING_DB` | `pairing.db` | SQLite 路径（与 fedgate 共享同一卷） |
| `PAIRING_ADMIN_TOKEN` | 必填 | 缺失拒绝启动 |
| `PAIRING_SERVER_NAME` | 必填 | 本端 Matrix server_name（进入 qr_payload） |
| `PAIRING_API_URL` | 必填 | 对端看到的本端 pairing 基址（如 `https://hs-a.example:8448/_matrix/pairing`） |
| `PAIRING_HOST` / `PAIRING_PORT` | `127.0.0.1` / `8090` | 监听地址（容器内改 `0.0.0.0`） |
| `PAIRING_INVITE_TTL` | `600` | 邀请有效期（秒） |
| `PAIRING_VERIFY_PEER_KEY` | `true` | redeem 时校验对端 server key |
| `PAIRING_PEER_KEY_URL_TEMPLATE` | `https://{server_name}/_matrix/key/v2/server` | key 拉取模板（CI 经 fedgate 地址 + 端口） |
| `PAIRING_INSECURE_TLS` | `false` | 关闭出站 TLS 校验（仅 CI） |

fedgate：

| 变量 | 默认 | 说明 |
|---|---|---|
| `FEDGATE_HOST` / `FEDGATE_PORT` | `0.0.0.0` / `8448` | 监听 |
| `FEDGATE_TLS_CERT` / `FEDGATE_TLS_KEY` | 无 | 成对出现则启用 TLS；公网部署必须配置（CA 签发，流程另文档化） |
| `FEDGATE_UPSTREAM` | `http://127.0.0.1:8008` | 内网 homeserver |
| `FEDGATE_PAIRING_DB` | `pairing.db` | 配对表 SQLite（只读） |
| `PAIRING_UPSTREAM` | 无 | pairing 服务基址；未设置时 `/_matrix/pairing/*` 返回 404 |

## 出站硬化（后续，不在本 PR）

切断的验收语义已由双方入站闸门闭环；「我方 homeserver 主动出站仍会尝试」的硬化（配对变更时重渲染 HS 白名单并编排重启，Tuwunel/continuwuity 用 allowed/forbidden 列表）列为后续任务。同样不在本 PR：App 侧扫码 UI（C2.2）、跨服 AI 关系（A2.3）、公网 TLS 证书的获取与续期流程。

## 实测要点（federation-e2e 调试结论）

- **`/send` 事务对未配对/已切断方返回 `200 {"pdus":{}}` 并丢弃**（不转发给 HS）。原因：Synapse 对 `/send` 的任何错误码都会把目标退避 10 分钟以上，且只在收到对方入站请求时才复位；若回 403，切断后再配对时双方会互相退避、长时间不通。丢弃后消息仍然**送不到**（满足「切断后不能发」），重新配对后缺失事件通过 DAG（get_missing_events/backfill）补齐。其余联邦接口（拉取 state/event/backfill/媒体、邀请、profile）对未配对方仍是 403。
- **fedgate 提供 `/.well-known/matrix/server`**（`FEDGATE_WELL_KNOWN_SERVER`，如 `hs-a:443`）：委派名带显式端口，对端就跳过 SRV 查询（Docker 内置 DNS 上 SRV 查询每次约卡 10s，会让联邦请求超时）。CI 栈中 fedgate 监听 443。
- 上游 HS 的 chunked 响应由 fedgate 以 chunked 重新分帧（以关连接分帧时 Twisted 客户端会一直等到超时）；TLS 握手放在每连接线程中，不阻塞 accept。
