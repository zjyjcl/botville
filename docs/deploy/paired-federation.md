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
3. **单方切断（入站保证）**：任一方 `DELETE /v1/pairing/peers/{name}` 即刻在**切断方本地**生效——对方**不能再向我方发消息、不能从我方拉取内容**（我方 fedgate 拒绝其入站推送与拉取），这部分**不需要对方配合**；对端经 best-effort 签名通知同步标记（失败不影响本端切断）。**出站**方向（我方 homeserver 仍可能主动向对方推送/拉取）依赖通知对端生效，彻底切断靠后续出站硬化（见下）。

## 架构

```
                     ┌────────────── hs-a 服务器（botville 实例 A）──────────────┐
 其他服务器 ──TLS──▶ │ fedgate-a :8448（唯一对外端口，docker 别名 = server_name） │
                     │   ├─ /_matrix/key/v2/server*  → 放行 → synapse-a :8008    │
                     │   ├─ /_matrix/{federation,key/query,media} → 配对表校验    │
                     │   │     Authorization: X-Matrix origin=… ∈ active peers？ │
                     │   │     否 → 403 M_FORBIDDEN "server not paired"          │
                     │   │     （/send 未配对 → 200 sink，见「实测要点」）        │
                     │   ├─ /_matrix/pairing/{redeem,notify-severed}             │
                     │   │     → pairing-a :8090（覆写 X-Forwarded-For）          │
                     │   │     其余 /_matrix/pairing/* → 404（admin 不出公网口）  │
                     │   └─ 其余 → 404                                             │
                     │ pairing-a :8090（内网/loopback）── SQLite /data/pairing.db │
                     │        ▲ fedgate 以只读连接（mode=ro）读取、1s 缓存；       │
                     │          WAL 模式下读者需能写 -shm，故共享卷挂 rw           │
                     └────────┴── fedgate-a ─────────────────────────────────────┘
```

- **fedgate 是权威闸门**（`server/pairing/fedgate.py`，`python -m pairing.fedgate`）：标准库 `ThreadingHTTPServer` 反向代理 + 可选 TLS。它只判断"这个 origin 是否在 active 配对表中"，**事件签名验证仍由上游 homeserver 完成**（标准库无 ed25519，闸门不验签；媒体端点的残余风险见「安全说明」）。方法/端到端头/体/状态码原样转发（客户端 `Content-Length`/`Transfer-Encoding` 不透传，按已读 body 重算），响应支持流式。配对表从 pairing 的 SQLite 以只读连接（`mode=ro`）读取（同一卷；WAL 模式下读者需要写 `-shm` wal-index，所以共享卷必须挂 rw），每 1s 刷新缓存——切断即刻生效，不触碰 homeserver；**数据库读失败不等于空配对集**：最多沿用 30s 前的最后成功快照，之后 gated 请求返回 `503 M_UNAVAILABLE`（不 sink）、`/health` 返回 `503 {"ok":false,"db":false}`。
- **pairing 是配对面**（`server/pairing/service.py`，`python -m pairing.service`）：invite（一次性邀请码）/redeem/accept/sever 的 HTTP API + 审计事件，SQLite 持久化（`server/pairing/store.py`：`invites`、`peers`、`events` 三表，WAL；启动时自动迁移旧库并清理 30 天前的已完成邀请与旧事件）。
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
  1. 校验 invite：未用、未过期、sha256 常量时间比对（只读预检，
     通过前不发起任何出站请求）
  2. （默认开启）拉取 PAIRING_PEER_KEY_URL_TEMPLATE 渲染出的
     /_matrix/key/v2/server（URL 只由模板+严格校验过的 server_name
     构成），确认对端声明的 server_name 一致（防冒名）
  3. 生成 per-peer 32 字节 shared_secret，事务内原子消费 invite
     并写 peers[B]=active（含该 secret）
  4. 响应 {ok, server_name, shared_secret}     │
     （shared_secret 仅经 TLS 返回给对端）      │
                                                 └─ 成功后写 peers[A]=active
                                                    （存下响应里的 shared_secret，
                                                     双方各持同一密钥）
双方 fedgate 的 active 集合即刻包含对方 → 联邦放行
```

QR 载荷以 `:` 分隔共 6 段；server_name 统一为小写 DNS 名（含端口、IP 字面量、localhost、`_`/`@`/`/` 等字符一律拒绝，见 `server/pairing/validate.py`）。

## API 一览

pairing 服务（错误统一 `{"errcode","error"}`；`/_matrix/pairing/<x>` 是 `/v1/pairing/<x>` 的别名，供 fedgate 原样转发；限速以**有效来源 IP** 计——仅当直连地址属于 `PAIRING_TRUSTED_PROXIES` 时才采信 fedgate 覆写的 `X-Forwarded-For`）：

| 端点 | 鉴权 | 说明 |
|---|---|---|
| `POST /v1/pairing/invites` | admin | 创建一次性邀请，返回 `{invite_id, expires_at, qr_payload}`；secret 只此一次出现 |
| `POST /v1/pairing/redeem` | 对端（限速） | `{invite_id, secret, server_name, api_url}`：先只读校验 invite（通过前不出站），再（可选）校验对端 key，事务内原子消费并写 `peers[server_name]=active`；响应含 per-peer `shared_secret`（仅 TLS 传输）。限速：每有效 IP 10/min + 每 invite 5/min |
| `POST /v1/pairing/accept` | admin | `{qr_payload}`：本端调用对端 redeem，成功后本地也写 active（存下 `shared_secret`）；对端错误原样透传 |
| `GET /v1/pairing/peers` | admin | 配对状态列表（含 `severed_by`、`peer_api_url`；不含 shared_secret） |
| `DELETE /v1/pairing/peers/{name}` | admin | 单方切断：本地立即 severed（`severed_by=local`）；此前签发的 invite 永远不能再用于该对端（`invite predates the sever`；按 `last_local_sever_at` 水位判断，重新配对后也不清除；在 redeem 的 `BEGIN IMMEDIATE` 事务内复核，无 TOCTOU；其他待配对方的 invite 不受影响），随后 best-effort 以 shared_secret 签名 POST 对端 `/notify-severed`，失败忽略（响应 `notified` 标明） |
| `POST /v1/pairing/notify-severed` | 对端（HMAC） | `{server_name}` + `X-Botville-Origin/-Ts/-Nonce/-Sig`：把该对端标 `severed_by=remote`（仅 active→severed，不覆盖 `severed_by=local`）。签名 = `HMAC-SHA256(shared_secret, "origin\nts\nnonce\nsha256hex(body)")`；`\|now-ts\|≤300s`、nonce ≥16 字节 hex 且防重放、30/min/IP；一切失败 → 401 且不写 events（仅日志计数） |
| `GET /v1/pairing/allowed` | admin | active 名单（调试用；fedgate 直接读 SQLite） |
| `GET /health` | 无 | 存活探针 |

fedgate 路由规则：

| 路径 | 规则 |
|---|---|
| `/_matrix/key/v2/server*` | 放行（本服公钥，不含房间内容） |
| `/_matrix/federation/*`、`/_matrix/key/v2/query*`、`/_matrix/media/*` | 解析 `Authorization: X-Matrix origin=…`（引号/无引号均可）。**必须恰好一个 X-Matrix 头、参数名不得重复、origin/sig 必填、origin 必须是合法小写 DNS server_name**——与 Synapse v1.164.1 解析语义一致，但任何歧义（多头/重复参数/畸形）一律 403 fail-closed，杜绝"闸门查 A、HS 验签 B"的绕过。origin ∉ active 配对表 → `403 {"errcode":"M_FORBIDDEN","error":"server not paired"}`（未配对 `PUT /_matrix/federation/v1/send/*` 例外：200 sink，见「实测要点」）；配对库不可读 → `503 M_UNAVAILABLE` |
| `/_matrix/pairing/redeem`、`/_matrix/pairing/notify-severed` | 转发到 `PAIRING_UPSTREAM`（对端只需暴露 8448 一个端口；剥掉客户端自带的 XFF/X-Real-IP/Forwarded，写入直连客户端 IP） |
| 其余 `/_matrix/pairing/*` | 404（admin 端点不出公网口） |
| 其余 | 404 |
| `/health`、`/.well-known/matrix/server` | 本地应答（前者含配对库可读性，后者为委派名） |

所有进入 fedgate/pairing 的路径先做归一化检查：含 `..` 段、`//`、`%2e`/`%2f`/`%5c`（大小写不敏感）直接 404。

## 切断语义与状态机

`peers(server_name, state, paired_at, severed_at, severed_by, peer_api_url, shared_secret)`：

- `active` ⇄（redeem/accept/upsert）→ 初始态，fedgate 放行；
- `severed`（`severed_by=local`）：本端管理员 DELETE，即刻生效——本端 fedgate 拒绝该 origin 的联邦请求（对方发不进来、也拉不走）；在最近一次本端切断之前签发的 invite 对该对端永久失效（水位 `last_local_sever_at` 不随重新配对清除），因此重新配对只能用该次切断之后新签发的 invite；其他待配对方不受影响；
- `severed`（`severed_by=remote`）：收到对端**验签通过**的 `/notify-severed`（只作用于 active 态，绝不覆盖 `severed_by=local`）；
- **重新配对**：任一方再走一遍 invite→accept 即恢复 `active`（`severed_*` 清空、shared_secret 换新；即使此前是 `severed_by=local` 也允许，需新 invite）；
- 无记录 = 拒绝（默认拒绝不需要显式黑名单）。

出站方向：本端 homeserver 仍可能主动重试向已切断对端发送（Synapse 事务重试退避），会被**对端的入站闸门**拒绝——**入站**切断语义由双方各自的入站 fedgate 保证闭环，不依赖 HS 配置变更或重启；**出站**是否停发依赖对端收到通知（或后续出站硬化），对拒不配合的对端我方出站拉取会一直失败但停止推送需出站硬化兜底。

## 安全说明

- **invite secret**：32 字节随机（urlsafe），库里只存 sha256；`hmac.compare_digest` 常量时间比对；一次性、默认 TTL 600s（`PAIRING_INVITE_TTL`）；redeem 限速每有效 IP 10/min + 每 invite 5/min；invite 校验先于任何出站请求（未认证方无法借 redeem 探活/触发 SSRF-lite）。
- **server_name 严格校验**：全链路小写（config/QR/redeem/store/fedgate origin）；仅接受 DNS 名（label ≤63、总长 ≤253、无前后连字符），拒绝 IP 字面量、`localhost`/`*.localhost`、端口、`@?#/` 等；key 校验 URL 只能由模板 + 合法 server_name 构成。
- **notify-severed 鉴权**：配对时由被核销方生成 per-peer 32 字节 shared_secret，经 redeem 响应（仅 TLS）交予对端，双方 peers 表各存一份（明文，64 hex）；通知与校验共用 `HMAC-SHA256(secret, "origin\nts\nnonce\nsha256hex(body)")`，±300s 时间窗 + nonce 防重放（内存 LRU，窗口 600s）+ 30/min/IP 限速 + `compare_digest`；未知 origin/错签/过期/重放一律 401 且不写 events。旧库自动迁移加列；迁移前的旧配对没有 secret，通知会被对端拒收——重新配对一次即恢复。
- **反代与限速**：fedgate 转发 `/_matrix/pairing/*` 时剥掉客户端自带的 `X-Forwarded-For`/`X-Real-IP`/`Forwarded`，写入直连客户端 IP；pairing 仅当直连地址 ∈ `PAIRING_TRUSTED_PROXIES`（逗号分隔 CIDR，默认空=不信任）才采信 XFF 最后一个值，否则用直连 IP——伪造 XFF 无法稀释限速桶。
- **闸门与 HS 解析一致性（B1）**：fedgate 遍历全部 Authorization 头，X-Matrix 头数量 ≠1 或任一参数名重复 → 403；参数切分/去引号/反斜杠转义与 Synapse v1.164.1 `_parse_auth_header` 一致；签名真伪由 HS 验签兜底。**HTTP 层**：Python 标准库与 Twisted 对折行（obs-fold）头的归一化不同（前者保留 `\r\n`+缩进，后者折成一个空格），任何跨解析器的归一化差异都是绕过面，所以闸门**不模拟**对方的归一化，而是在路由前拒绝任何折行头或含控制字符（HTAB 除外）的头名/头值（400），也顺带杜绝 CRLF 注入上游。**重复 Authorization**：在原始字节层面（不依赖 http.server/email 的合并结果）计数，`Authorization` 出现超过一次（不论是否 X-Matrix、不论大小写）、头名与冒号间有空白、或单个值内出现第二个凭证（第二个 `X-Matrix`/非法参数名）→ 400；转发时只转发那唯一一个已校验的值，绝不合并。不变量是「闸门解析的表示 == 转发给 HS 的表示」，任何偏离即绕过；「闸门放行的 origin == HS 认证的 origin」只在以上全部请求级检查成立时才成立（`parse_x_matrix_origin` 单独并不保证），由 raw socket 用例与差分单测（Twisted 折行语义 + 逐字照抄的 Synapse `_parse_auth_header` 参考实现，对照上游实际收到的头）守护。
- **媒体访问的硬边界**：Synapse 对 `/_matrix/media/*`（联邦媒体下载）**不验联邦签名**，闸门也不验签（容器仅标准库，无 ed25519）；因此能伪造某 active 对端 origin 的请求方，只要知道 `media_id` 就能下载。**媒体访问的唯一保护是 media_id 的保密性**（Synapse media_id 约 24 位 62 进制，≈143 bit，不可猜）。A2.1 单配对部署下接受；**多配对或生产化前必须补令牌/签名（闸门验签）**。
- **sink 的代价**：未配对 `/send` 回 `200 {"pdus":{}}` 并丢弃（读 body 上限 1 MiB，超限直接断开；日志按 origin 节流）。**切断期间的 EDU（to-device、E2EE 密钥包）永久丢失**（DAG 补不回、发送方收到 200 不再重试）；PDU 在重新配对后经 `get_missing_events`/`backfill` 补回——因此 sever 是"断流"而非"内容栅栏"，**重新配对后切断窗口内产生的事件会一并补回**；若需要"切断期内容永不再入"需后续做窗口栅栏。
- **管理面**：`Authorization: Bearer $PAIRING_ADMIN_TOKEN`，常量时间比对；token 缺失时服务**拒绝启动**；pairing 服务只应绑定内网/loopback（compose 只把 admin 端口映射到 `127.0.0.1`；fedgate 只转发 redeem/notify-severed，admin 端点不暴露公网口）。
- **日志**：不打印 secret/token（qr_payload 只出现在请求体，永不落日志；shared_secret 同样只存 DB/响应体）。
- **对端身份校验**（`PAIRING_VERIFY_PEER_KEY`，默认开）：redeem（invite 校验通过后）拉取对端 `/_matrix/key/v2/server` 并要求 server_name 一致，防止冒名配对；该校验比对的是对端声明字段，签名仍由联邦层验签保证。`PAIRING_PEER_KEY_URL_TEMPLATE`（默认 `https://{server_name}/_matrix/key/v2/server`）供测试/特殊拓扑改写；`PAIRING_INSECURE_TLS=true` 关闭证书校验，**仅限 CI/一次性测试栈**（自签证书场景）。
- 审计与保留：`events` 表记录 invite 创建/兑换/拒绝原因、配对、切断、通知成败、限速与对端校验失败；`store.prune()`（启动时+每小时）删除 30 天前已消费/过期 invite 与旧事件。
- `server_name` 以 DNS 别名/私网地址部署时依赖网络层隔离。

## 运行（本地 / CI）

```sh
make federation-e2e   # 生成自签证书与配置 → 起 botville-fed 项目 → 跑验收
make federation-down  # 只 down -v 该项目
```

- compose：`server/compose/federation/docker-compose.federation.yml`（独立项目 `botville-fed`，子网 `172.30.81.0/24`，与主栈完全隔离）；hs-a/hs-b 两套 synapse（镜像同主 compose 的 pin）+ pairing + fedgate（`python:3.12-slim` 挂载 `server/pairing` 源码直接 `python -m` 运行）。静态地址：fedgate-a/b = `172.30.81.10/.11`，pairing-a/b = `172.30.81.20/.21`（pairing 的 `PAIRING_TRUSTED_PROXIES` 各指向自己的 fedgate）。
- fedgate 容器持有网络别名 `hs-a`/`hs-b`，对端 Synapse 对无端口 server_name 走 well-known→SRV→8448 回落后直达闸门。
- 宿主只映射测试端口且绑定 `127.0.0.1`：`18101/18102`（两台 Synapse client API）、`18111/18112`（pairing admin）。**test-only**：这些 loopback 端口绕过 fedgate，仅供验收脚本用，生产部署不得映射。
- 运行时生成（全部 gitignored）：`server/compose/federation/generated/certs/`（`server/scripts/federation-certs.sh` 的自签证书）、`hs-a|hs-b/homeserver.yaml` 与 `federation.env`（`server/scripts/federation-setup.py`，token 全随机）。
- CI-only 配置叠加（写在生成的 homeserver.yaml 里，勿抄进生产）：`federation_verify_certificates: false`、`federation_ip_range_blacklist: []`、`enable_registration: true`、`trusted_key_servers: []`；pairing/fedgate 的 `PAIRING_INSECURE_TLS: "true"`。
- 单测（无需 Docker）：`python3 -m unittest discover -s server/pairing/tests`。

### 环境变量

pairing 服务：

| 变量 | 默认 | 说明 |
|---|---|---|
| `PAIRING_DB` | `pairing.db` | SQLite 路径（与 fedgate 共享同一卷） |
| `PAIRING_ADMIN_TOKEN` | 必填 | 缺失拒绝启动 |
| `PAIRING_SERVER_NAME` | 必填 | 本端 Matrix server_name（进入 qr_payload；启动时小写化并严格校验为 DNS 名） |
| `PAIRING_API_URL` | 必填 | 对端看到的本端 pairing 基址（如 `https://hs-a.example:8448/_matrix/pairing`） |
| `PAIRING_HOST` / `PAIRING_PORT` | `127.0.0.1` / `8090` | 监听地址（容器内改 `0.0.0.0`） |
| `PAIRING_INVITE_TTL` | `600` | 邀请有效期（秒） |
| `PAIRING_VERIFY_PEER_KEY` | `true` | redeem 时校验对端 server key |
| `PAIRING_PEER_KEY_URL_TEMPLATE` | `https://{server_name}/_matrix/key/v2/server` | key 拉取模板（CI 经 fedgate 地址 + 端口） |
| `PAIRING_INSECURE_TLS` | `false` | 关闭出站 TLS 校验（仅 CI） |
| `PAIRING_TRUSTED_PROXIES` | 空 | 逗号分隔 IP/CIDR；仅当直连地址命中时才采信 `X-Forwarded-For` 最后一个值作为限速键（空=一律用直连 IP） |

fedgate：

| 变量 | 默认 | 说明 |
|---|---|---|
| `FEDGATE_HOST` / `FEDGATE_PORT` | `0.0.0.0` / `8448` | 监听 |
| `FEDGATE_TLS_CERT` / `FEDGATE_TLS_KEY` | 无 | 成对出现则启用 TLS；公网部署必须配置（CA 签发，流程另文档化） |
| `FEDGATE_UPSTREAM` | `http://127.0.0.1:8008` | 内网 homeserver |
| `FEDGATE_PAIRING_DB` | `pairing.db` | 配对表 SQLite（只读） |
| `PAIRING_UPSTREAM` | 无 | pairing 服务基址；未设置时 `/_matrix/pairing/*` 返回 404 |

## 主机有代理时（NO_PROXY 同样固化，issue #134）

背景与完整说明见 [`docs/deploy/push-sygnal.md` §11 主机有代理时](push-sygnal.md#主机有代理时no_proxy-固化issue-134)：docker 客户端与 Docker Desktop 会把主机的 `HTTP(S)_PROXY` 注入每个新建容器，而容器内 `127.0.0.1` 是容器自己——主机上的环回代理在容器内不可达。本联邦栈的 `docker-compose.federation.yml` 顶部有**同一个 `x-noproxy-env` 锚点**，六个服务（synapse-a/b、pairing-a/b、fedgate-a/b）全部合并：固定默认名单与主 compose **逐字节相同**（单一名单服务两套栈，`server/tests/test_compose_noproxy.py` 保证不漂移），**不可覆盖**，`BOTVILLE_NO_PROXY_EXTRA` 只做追加，同样不设置/不清空 `HTTP(S)_PROXY`。

- **EXTRA 怎么传进来**：本栈 compose 插值只用 `server/compose/federation/generated/federation.env`（Makefile `FED_COMPOSE` 的 `--env-file`），不读主栈的 `server/compose/.env`。`server/scripts/federation-setup.py` 因此把 `BOTVILLE_NO_PROXY_EXTRA`（shell 环境变量优先，其次 `server/compose/.env` 里的同名项）写进 `federation.env`；`make federation-e2e` 每次先跑 setup，一处配置两套栈共用。
- **`hs-a,hs-b` 已在默认名单内**：联邦对端按 server_name 解析到 fedgate 的网络别名 `hs-a`/`hs-b`（是别名不是服务名），而 pairing 的对端 key 校验用标准库 `urllib`（遵循 `*_PROXY`），所以这两个别名已固化进默认 NO_PROXY（两份 compose 一致），无需再设 `BOTVILLE_NO_PROXY_EXTRA`。fedgate 自身转发走 `http.client`（不读代理环境变量），不受影响。

主机有代理时的自检（宿主机执行；先 `make federation-e2e` 让 `federation.env` 存在）：

```bash
# 1) 容器内 NO_PROXY 生效：synapse-a 与 fedgate-a 大小写两个变量都在、含六个服务名；
#    若同时看到 HTTP_PROXY=...127.0.0.1...，说明 docker 注入了环回代理（处理见 push-sygnal.md §11）。
docker compose -p botville-fed -f server/compose/federation/docker-compose.federation.yml \
    --env-file server/compose/federation/generated/federation.env \
    exec -T synapse-a env | grep -i proxy
docker compose -p botville-fed -f server/compose/federation/docker-compose.federation.yml \
    --env-file server/compose/federation/generated/federation.env \
    exec -T fedgate-a env | grep -i proxy

# 2) fedgate -> synapse 直连可达（FEDGATE_UPSTREAM；NO_PROXY 含 synapse-a 时 urllib 不经代理）
docker compose -p botville-fed -f server/compose/federation/docker-compose.federation.yml \
    --env-file server/compose/federation/generated/federation.env \
    exec -T fedgate-a python -c \
    "import urllib.request; print(urllib.request.urlopen('http://synapse-a:8008/health', timeout=5).status)"
#    期望 200
```

## 出站硬化（后续，不在本 PR）

切断的**入站**验收语义（对方不能再向我方发消息、不能从我方拉取内容）已由双方入站闸门闭环；「我方 homeserver 主动出站仍会尝试」的硬化（配对变更时重渲染 HS 白名单并编排重启，Tuwunel/continuwuity 用 allowed/forbidden 列表；含对拒不配合对端的出站停推与切断窗口内容栅栏）列为后续任务。同样不在本 PR：App 侧扫码 UI（C2.2）、跨服 AI 关系（A2.3）、公网 TLS 证书的获取与续期流程、媒体端点的签名/令牌校验。

## 实测要点（federation-e2e 调试结论）

- **`/send` 事务对未配对/已切断方返回 `200 {"pdus":{}}` 并丢弃**（不转发给 HS；body 最多读 1 MiB，超限直接断开连接）。原因：Synapse 对 `/send` 的任何错误码都会把目标退避 10 分钟以上，且只在收到对方入站请求时才复位；若回 403，切断后再配对时双方会互相退避、长时间不通。丢弃后消息仍然**送不到**（满足「切断后不能发」）；代价见「安全说明」：切断期 EDU 永久丢失，PDU 在重新配对后通过 DAG（get_missing_events/backfill）补齐——切断窗口内的事件也可能随之补回。其余联邦接口（拉取 state/event/backfill/媒体、邀请、profile）对未配对方仍是 403。
- **fedgate 提供 `/.well-known/matrix/server`**（`FEDGATE_WELL_KNOWN_SERVER`，如 `hs-a:443`）：委派名带显式端口，对端就跳过 SRV 查询（Docker 内置 DNS 上 SRV 查询每次约卡 10s，会让联邦请求超时）。CI 栈中 fedgate 监听 443。
- 上游 HS 的 chunked 响应由 fedgate 以 chunked 重新分帧（以关连接分帧时 Twisted 客户端会一直等到超时）；转发请求时客户端的 `Content-Length`/`Transfer-Encoding` 一律剥掉、按已读 body 重算；TLS 握手放在每连接线程中，不阻塞 accept。

- **运维提示**：`/health` 返回 `db:false`（503）应立即告警——DB 读失败时闸门最多沿用上次快照 30s（期间刚切断的对端可能仍被放行），之后受管路径一律 503。pairing 若未设置 `PAIRING_TRUSTED_PROXIES` 启动时会打 WARN（反代后所有对端共用一个限速桶）。
- **大事务与退避**：未配对/已切断方超过 1 MiB 的 `/send` 会被直接断连而非 200，发送端 Synapse 会对本端退避 10+ 分钟；重新配对后这类大事务可能要等退避结束才送达。
- **sink 日志**：按 origin 节流 + 全局每分钟上限 + 有界 LRU（1024 个 origin），轮换新 origin 既刷不爆日志也撑不大内存。
