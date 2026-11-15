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
3. **单方切断（入站保证）**：任一方 `DELETE /v1/pairing/peers/{name}` 即刻在**切断方本地**生效——对方**不能再向我方发消息、不能从我方拉取内容**（我方 fedgate 拒绝其入站推送与拉取），这部分**不需要对方配合**；对端经 best-effort 签名通知同步标记（失败不影响本端切断）。**出站**方向（我方 homeserver 仍可能主动向对方推送/拉取）在重启前由对端闸门挡住；Synapse 部署在**重启 homeserver 后**连尝试都不会再有（pairing 渲染的 `federation_domain_whitelist`，见「HS 白名单与重启」）。

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
                     │   ├─ /_matrix/pairing/relations/{request,respond,revoke}  │
                     │   │     仅 POST + X-Botville-Origin ∈ active → pairing-a   │
                     │   │     其余 /_matrix/pairing/* → 404（admin 不出公网口）  │
                     │   └─ 其余 → 404                                             │
                     │ pairing-a :8090（内网/loopback）── SQLite /data/pairing.db │
                     │        ▲ fedgate 以只读连接（mode=ro）读取、1s 缓存；       │
                     │          WAL 模式下读者需能写 -shm，故共享卷挂 rw           │
                     └────────┴── fedgate-a ─────────────────────────────────────┘
```

- **fedgate 是权威闸门**（`server/pairing/fedgate.py`，`python -m pairing.fedgate`）：标准库 `ThreadingHTTPServer` 反向代理 + 可选 TLS。它只判断"这个 origin 是否在 active 配对表中"，**事件签名验证仍由上游 homeserver 完成**（标准库无 ed25519，闸门不验签；媒体端点的残余风险见「安全说明」）。方法/端到端头/体/状态码原样转发（客户端 `Content-Length`/`Transfer-Encoding` 不透传，按已读 body 重算），响应支持流式。配对表从 pairing 的 SQLite 以只读连接（`mode=ro`）读取（同一卷；WAL 模式下读者需要写 `-shm` wal-index，所以共享卷必须挂 rw），每 1s 刷新缓存——切断即刻生效，不触碰 homeserver；**数据库读失败不等于空配对集**：最多沿用 30s 前的最后成功快照，之后 gated 请求返回 `503 M_UNAVAILABLE`（不 sink）、`/health` 返回 `503 {"ok":false,"db":false}`。
- **pairing 是配对面**（`server/pairing/service.py`，`python -m pairing.service`）：invite（一次性邀请码）/redeem/accept/sever 的 HTTP API + 审计事件，SQLite 持久化（`server/pairing/store.py`：`invites`、`peers`、`events` 三表加 A-R1 的 `meta` 小表，WAL；启动时自动迁移旧库并清理 30 天前的已完成邀请与旧事件）。
- **HS 白名单是第二道闸**（A-R2，仅 Synapse）：pairing 在配对变化时原子重渲染 `federation_domain_whitelist` include（active 对端 + 本服务器）并写「需要重启」标记，由运维/supervisor 重启 Synapse；pairing 不持有 docker socket，不自动重启。fedgate 仍是实时的第一道闸，安全上不依赖重启时机（见「HS 白名单与重启」）。

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
| `GET /v1/pairing/internal/peers/{name}` | service | 配对事实（A-R1，供网关）：`{state, changed_at}`，见下节 |
| `GET /v1/pairing/internal/events?since=&limit=` | service | 配对事实事件流（A-R1）：`peer_paired` / `peer_severed`，见下节 |
| `POST /v1/pairing/internal/relations/{request,respond,revoke}` | service | A-R3 跨服关系**出站**：本地网关交来 `{destination, request_id, payload}`，pairing 签名后只发给 active 对端，见「跨服关系传输」 |
| `POST /v1/pairing/relations/{request,respond,revoke}` | 对端（HMAC） | A-R3 跨服关系**入站**（经 fedgate `/_matrix/pairing/relations/*`）：验签 + 防重放 + active + 格式校验后交给本地网关客户端，见「跨服关系传输」 |

### 配对事实接口（A-R1，供网关只读）

网关（B 线关系存储）据此判断「对端服务器是否仍配对」，并在切断时降级跨服关系。只读，不改任何状态。

**鉴权（fail-closed）**：`Authorization: Bearer $PAIRING_SERVICE_TOKEN`，常量时间比较。
- service token 与 admin token 相互独立：admin token 调内部端点一律 401，service token 调 admin 端点也一律 401。
- 没配 `PAIRING_SERVICE_TOKEN` 时，内部端点对任何请求都返回 401（服务照常启动，启动日志 WARNING）。
- 配了但短于 32 个字符、或与 admin token 相同 → 拒绝启动，报错不回显取值。
- 鉴权在路由之前：缺 token / 错 token / 用 admin token，不论路径对不对，都是同一个 401 `M_UNKNOWN_TOKEN`，不泄露端点或对端是否存在。失败只计数（`internal_rejected`）并记 WARNING（含来源 IP，不含 token），日志每 60 秒最多一条、附带期间被压掉的条数（#243 审查第 2 条），不写 events。`/v1/pairing/internal`（无尾斜杠）同样先鉴权，通过后 404。
- 内部端点**不走** `/_matrix/pairing/` 别名（别名路径一律 404，**在鉴权之前**、带不带正确 token 都一样：别名是 fedgate 转发的公共命名空间，那里不存在内部端点，回 401 反而暗示有个可以猜 token 的认证入口），fedgate 也只转发 redeem / notify-severed / relations/{request,respond,revoke}，所以对端永远够不着。只应在内网（compose 网络或 loopback）访问。

**`GET /v1/pairing/internal/peers/{server_name}`** → `200 {"state": "active"|"severed"|"unpaired", "changed_at": ISO-8601|null}`
- `changed_at`：active 取最近一次配对时间，severed 取切断时间，unpaired（从未配对）为 null。
- server_name 按同一规则小写化并严格校验；不合法 → 400（不回显）。

**`GET /v1/pairing/internal/events?since=<cursor>&limit=<1-500>`** → `200 {"events": [...], "next_cursor": N, "has_more": bool}`
- 事件只有两类，按游标升序：
  - `{"cursor", "kind": "peer_paired", "server_name", "at", "repaired"}`：每次进入 active 都写一条——redeem 方和 accept 方都写，重新配对也写，`repaired=true` 表示写入前 `peers` 里已有这台服务器的行（以前配对过，不论当时是 severed 还是仍 active——仍 active 时再次配对同样是 true）；A-R1 之前的旧事件没有这个字段，返回 null。
  - `{"cursor", "kind": "peer_severed", "server_name", "at", "severed_by": "local"|"remote"}`：每次切断写一条；对已切断的行再收到对端通知不会重复写。
- 事件与 `peers` 行的变化在**同一个事务**里提交，读到新状态就一定读得到对应事件。事件里没有 shared_secret、peer_api_url、invite 等字段。
- **游标**：就是 `events.id`（`AUTOINCREMENT`）——严格递增、删除后也不复用；同一个游标重放得到同一页（稳定）。省略 `since` 等同 `since=0`（从头读）。`since` / `limit` 只能各出现一次，必须是非负十进制整数，其他参数一律 400。
- `has_more=false` 时 `next_cursor` 会直接跳到当前最新事件 id（包括非配对类的审计事件），所以网关只要按 5–10 秒轮询，游标就不会因为长期没有配对变化而落到保留期之外。
- **保留期 30 天的后果**：`events` 每小时清理 30 天前的行（invites 同理），清理时记下删掉的最大 id（`meta.events_pruned_through`）。
  - 游标 < 该水位：中间的事件可能已经删掉，无法保证不丢，返回 `410 {"errcode": "BV_CURSOR_EXPIRED", "reason": "cursor_pruned", "resync_cursor": N}`。
  - 游标 > 当前最新 id（比如从旧备份恢复或重建了 pairing 库）：同样返回 410，`reason` 是 `cursor_ahead_of_log`。
  - 两种情况的 `resync_cursor` 都是**清理水位**（`meta.events_pruned_through`，从未清理过时为 0），不是最新 id：水位之后的事件都还在库里，从它继续读能拿到全部仍保留的事实，一条不跳（#243 审查第 1 条修正；之前取最新 id 会跳过水位到最新之间未删的事件）。
  - 收到 410 时，网关先记下 `resync_cursor`，再对自己关心的每个服务器调 `peers/{name}` 重建状态，然后从 `resync_cursor` 继续轮询。水位之后的事件会被重放一遍（可能与刚查到的状态重复），所以网关侧必须幂等。
  - 网关停机超过 30 天、且期间有清理时一定会遇到 410；按上述步骤自愈即可。

### 跨服关系传输（A-R3，只做联邦侧）

A 线只负责「把消息安全送到对端网关、把对端网关的回答带回来」；请求 ID 的业务含义、幂等、过期、状态机都归 B 线网关。

**信封**（canonical JSON：`sort_keys`、无空格、UTF-8；签名与发送的就是这串字节）：

```json
{"destination":"hs-b","kind":"request","origin":"hs-a","payload":{...},"request_id":"01J...","v":1}
```

- 键必须恰好是 `v, kind, origin, destination, request_id, payload`；`v` 为整数 1；`kind` 必须等于 URL 里的 kind；`origin` 必须等于验签通过的发送方；`destination` 必须是本服；`request_id` 为 16–64 位 `[0-9A-Za-z_-]`（建议 ULID）；`payload` 必须是 JSON 对象（内容不校验）；整个信封 ≤16 KiB。
- 格式不符 → `400 BV_BAD_ENVELOPE`（只给原因，不回显内容）。

**签名**（复用 notify-severed 的逐对端 shared_secret，加域分隔）：`HMAC-SHA256(shared_secret, "botville-relations-v1\nrelations/<kind>\n<origin>\n<ts>\n<nonce>\n<sha256hex(body)>")`，头同为 `X-Botville-Origin/-Ts/-Nonce/-Sig`。域前缀和 kind 进签名，notify-severed 的签名、或签给 `respond` 的消息都不能拿来打 `request`。

**入站顺序（一切异常 fail-closed）**：

1. fedgate：仅 POST（否则 405）；`X-Botville-Origin` 必须恰好一个、合法小写名、且在 active 配对表 → 否则 `403 server not paired`；配对库不可读 → 503。这一步只是便宜的预筛（头还没验签）。
2. pairing：120/min/IP → 429；body ≤16 KiB → 413；未知/无 secret 的 origin、`|now-ts|>300s`、nonce 非 32–128 位小写 hex、签名不对 → 统一 `401 invalid relations authentication`（每次都计入 `relations_rejected`；WARNING 与内部 401 同样经 LogThrottle 每 60 秒最多一条、附带期间被压掉的条数；不写 events）。
3. 60/min/origin → 429；nonce 防重放（按 origin+nonce，窗口 600s，**满了不淘汰而是 503**，避免被挤掉的 nonce 在窗口内被重放）→ 重放 401。
4. 再读一次 peers：非 active → `403 BV_PEER_INACTIVE`。
5. 信封格式 → 400。
6. 交给网关客户端 `deliver(kind, envelope, meta)`（`meta = {origin, sent_at, received_at}`）：未配置 / 抛异常 / 返回不可用结果（状态码不在 `200,201,202,400,403,404,409,410,422,429`、body 非对象或 >16 KiB）→ `503 BV_RELATIONS_UNAVAILABLE`；否则**原样**把网关的状态码和 body 回给发送方。

**出站**（`POST /v1/pairing/internal/relations/<kind>`，service token）：body 恰好 `{destination, request_id, payload}`；destination 不能是本服；destination 必须 active 且有 shared_secret 和 peer_api_url，否则 `409 BV_PEER_INACTIVE`；60/min/destination → 429；签名后 POST `peer_api_url + "/relations/<kind>"`（不跟随重定向、10s 超时、不重试，`PAIRING_INSECURE_TLS` 同样适用）。

- 对端有回答 → `200 {"delivered": 2xx?, "peer_status": N, "peer_body": {...}|null}`（对端的业务拒绝也在这里，不会被误当成传输错误）；
- 超时 → `504 BV_PEER_TIMEOUT`；连不上 / 畸形响应 → `502 BV_PEER_UNREACHABLE`。重试由网关用**同一个 request_id** 发起。
- 注意：A-R1 的 service token 原本只读，现在也能触发出站发送；仍只应在内网使用。

**网关客户端**：`PAIRING_RELATIONS_GATEWAY` 选择实现。默认 `none`：所有入站消息 503（明确的「不可用」）。`test-recorder` 只给测试/CI：把 `{kind, envelope, meta}` 逐行追加到 `PAIRING_RELATIONS_TEST_RECORD`（绝对路径）并回 202——它什么都收，**不得用于真实部署**。`http`（真正调网关）保留：双方对齐网关内部端点之前不合入，配了会拒绝启动。

fedgate 路由规则：

| 路径 | 规则 |
|---|---|
| `/_matrix/key/v2/server*` | 放行（本服公钥，不含房间内容） |
| `/_matrix/federation/*`、`/_matrix/key/v2/query*`、`/_matrix/media/*` | 解析 `Authorization: X-Matrix origin=…`（引号/无引号均可）。**必须恰好一个 X-Matrix 头、参数名不得重复、origin/sig 必填、origin 必须是合法小写 DNS server_name**——与 Synapse v1.164.1 解析语义一致，但任何歧义（多头/重复参数/畸形）一律 403 fail-closed，杜绝"闸门查 A、HS 验签 B"的绕过。origin ∉ active 配对表 → `403 {"errcode":"M_FORBIDDEN","error":"server not paired"}`（未配对 `PUT /_matrix/federation/v1/send/*` 例外：200 sink，见「实测要点」）；配对库不可读 → `503 M_UNAVAILABLE` |
| `/_matrix/pairing/redeem`、`/_matrix/pairing/notify-severed` | 转发到 `PAIRING_UPSTREAM`（对端只需暴露 8448 一个端口；剥掉客户端自带的 XFF/X-Real-IP/Forwarded，写入直连客户端 IP） |
| `/_matrix/pairing/relations/{request,respond,revoke}` | A-R3：仅 POST（否则 405）；单个合法 `X-Botville-Origin` 且 ∈ active 配对表才转发到 `PAIRING_UPSTREAM`（同样重写 XFF），否则 403；配对库不可读 → 503。其他 `relations/*` 路径 404 |
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

出站方向：本端 homeserver 仍可能主动重试向已切断对端发送（Synapse 事务重试退避），会被**对端的入站闸门**拒绝——**入站**切断语义由双方各自的入站 fedgate 保证闭环，不依赖 HS 配置变更或重启；**出站**在 Synapse 部署中于重启 homeserver 后由本端白名单停发（「HS 白名单与重启」）；重启前本端 HS 的出站尝试由对端闸门拒绝。

## 安全说明

- **invite secret**：32 字节随机（urlsafe），库里只存 sha256；`hmac.compare_digest` 常量时间比对；一次性、默认 TTL 600s（`PAIRING_INVITE_TTL`）；redeem 限速每有效 IP 10/min + 每 invite 5/min；invite 校验先于任何出站请求（未认证方无法借 redeem 探活/触发 SSRF-lite）。
- **server_name 严格校验**：全链路小写（config/QR/redeem/store/fedgate origin）；仅接受 DNS 名（label ≤63、总长 ≤253、无前后连字符），拒绝 IP 字面量、`localhost`/`*.localhost`、端口、`@?#/` 等；key 校验 URL 只能由模板 + 合法 server_name 构成。
- **notify-severed 鉴权**：配对时由被核销方生成 per-peer 32 字节 shared_secret，经 redeem 响应（仅 TLS）交予对端，双方 peers 表各存一份（明文，64 hex）；通知与校验共用 `HMAC-SHA256(secret, "origin\nts\nnonce\nsha256hex(body)")`，±300s 时间窗 + nonce 防重放（内存 LRU，窗口 600s）+ 30/min/IP 限速 + `compare_digest`；未知 origin/错签/过期/重放一律 401 且不写 events。旧库自动迁移加列；迁移前的旧配对没有 secret，通知会被对端拒收——重新配对一次即恢复。
- **反代与限速**：fedgate 转发 `/_matrix/pairing/*` 时剥掉客户端自带的 `X-Forwarded-For`/`X-Real-IP`/`Forwarded`，写入直连客户端 IP；pairing 仅当直连地址 ∈ `PAIRING_TRUSTED_PROXIES`（逗号分隔 CIDR，默认空=不信任）才采信 XFF 最后一个值，否则用直连 IP——伪造 XFF 无法稀释限速桶。
- **闸门与 HS 解析一致性（B1）**：fedgate 遍历全部 Authorization 头，X-Matrix 头数量 ≠1 或任一参数名重复 → 403；参数切分/去引号/反斜杠转义与 Synapse v1.164.1 `_parse_auth_header` 一致；签名真伪由 HS 验签兜底。**HTTP 层**：Python 标准库与 Twisted 对折行（obs-fold）头的归一化不同（前者保留 `\r\n`+缩进，后者折成一个空格），任何跨解析器的归一化差异都是绕过面，所以闸门**不模拟**对方的归一化，而是在路由前拒绝任何折行头或含控制字符（HTAB 除外）的头名/头值（400），也顺带杜绝 CRLF 注入上游。**重复 Authorization**：在原始字节层面（不依赖 http.server/email 的合并结果）计数，`Authorization` 出现超过一次（不论是否 X-Matrix、不论大小写）、头名与冒号间有空白、或单个值内出现第二个凭证（第二个 `X-Matrix`/非法参数名）→ 400；转发时只转发那唯一一个已校验的值，绝不合并。不变量是「闸门解析的表示 == 转发给 HS 的表示」，任何偏离即绕过；「闸门放行的 origin == HS 认证的 origin」只在以上全部请求级检查成立时才成立（`parse_x_matrix_origin` 单独并不保证），由 raw socket 用例与差分单测（Twisted 折行语义 + 逐字照抄的 Synapse `_parse_auth_header` 参考实现，对照上游实际收到的头）守护。
- **媒体访问的硬边界**：Synapse 对 `/_matrix/media/*`（联邦媒体下载）**不验联邦签名**，闸门也不验签（容器仅标准库，无 ed25519）；因此能伪造某 active 对端 origin 的请求方，只要知道 `media_id` 就能下载。**媒体访问的唯一保护是 media_id 的保密性**（Synapse media_id 约 24 位 62 进制，≈143 bit，不可猜）。A2.1 单配对部署下接受；**多配对或生产化前必须补令牌/签名（闸门验签）**。
- **sink 的代价**：未配对 `/send` 回 `200 {"pdus":{}}` 并丢弃（读 body 上限 1 MiB，超限直接断开；日志按 origin 节流）。**切断期间的 EDU（to-device、E2EE 密钥包）永久丢失**（DAG 补不回、发送方收到 200 不再重试）；PDU 在重新配对后经 `get_missing_events`/`backfill` 补回——因此 sever 是"断流"而非"内容栅栏"，**重新配对后切断窗口内产生的事件会一并补回**；若需要"切断期内容永不再入"需后续做窗口栅栏。**产品决策（2026-11-10，B6，见 [pairing-qr.md §11](../contracts/pairing-qr.md)）**：首版**保留**这一行为——重新配对后补回切断期消息，App / 管理页在重新配对时须向用户说明「切断期间对方发出的消息会补回」；时间窗栅栏归 #118。
- **管理面**：`Authorization: Bearer $PAIRING_ADMIN_TOKEN`，常量时间比对；token 缺失时服务**拒绝启动**；pairing 服务只应绑定内网/loopback（compose 只把 admin 端口映射到 `127.0.0.1`；fedgate 只转发 redeem/notify-severed/relations（后者仅 active origin），admin 与内部端点不暴露公网口）。
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
- 白名单卷（A-R2）：`fed-hs-a-whitelist` / `fed-hs-b-whitelist`，pairing 读写、synapse 只读挂在 `/hs`；synapse 以第二个 `--config-path /hs/federation-whitelist.yaml` 加载并 `depends_on` pairing 健康。e2e 用 `docker compose exec/restart` 检查列表与标记并重启 synapse（`--compose`，Makefile 传入 `FED_COMPOSE`）。
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
| `PAIRING_SERVICE_TOKEN` | 空（内部端点全部 401） | A-R1 配对事实接口的 service token；≥32 字符且不得等于 admin token，否则拒绝启动。联邦测试栈由 `federation-setup.py` 与 admin token 分别随机生成（`PAIRING_SERVICE_TOKEN_A/B`，写入 gitignored 的 `generated/federation.env`）。它是密钥，不要贴到文档、聊天或日志。A-R3 起它也能调出站关系发送端点 |
| `PAIRING_HS_WHITELIST_PATH` | 空（关闭） | 绝对路径；设置后渲染 Synapse `federation_domain_whitelist` include（A-R2，见「HS 白名单与重启」） |
| `PAIRING_HS_RESTART_MARKER` | `<PAIRING_HS_WHITELIST_PATH>.restart-required` | 「需要重启」标记的绝对路径；须先设 `PAIRING_HS_WHITELIST_PATH`，且不能与它相同 |
| `PAIRING_RELATIONS_GATEWAY` | `none` | A-R3 入站关系消息交给谁：`none`（一律 503）、`test-recorder`（仅测试/CI）；`http` 保留，配了拒绝启动 |
| `PAIRING_RELATIONS_TEST_RECORD` | 空 | `test-recorder` 的输出文件（必须是绝对路径，否则拒绝启动） |

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

## HS 白名单与重启（A-R2，#118 第 8 项，仅 Synapse）

fedgate 是**实时的第一道闸**：配对表一变（≤1s），双方入站即按新状态放行/拒绝，不需要碰 homeserver。它挡不住的是**本端 homeserver 自己还在尝试出站**（向已切断对端推送事务、拉 key/state——请求会死在对端闸门，但本端仍在建连、排队、重试）。A-R2 让 homeserver 自己也拒绝：

1. **渲染**：pairing 每次配对状态变化（redeem 成功、accept 成功、本端 DELETE、验签通过的 notify-severed）后，按「`state='active'` 的对端 + 本服务器名」生成 Synapse include 文件（`PAIRING_HS_WHITELIST_PATH`，例 `/hs/federation-whitelist.yaml`）：

   ```yaml
   federation_domain_whitelist:
     - "hs-a"
     - "hs-b"
   ```

   列表去重、排序（同一配对状态永远渲染出相同字节）；severed 对端不在其中；名字以 JSON 字符串写出（配对本就只接受裸 DNS 名）。启动时、以及之后每 60s 也会重算一次（重试失败的写入、修复被手改/删掉的文件）。
2. **原子写入**：同目录临时文件 → fsync → `os.replace` → fsync 目录；Synapse 永远读不到半个文件；写失败时旧文件原样保留。文件权限 0644（不是密钥，homeserver 以其他 uid 读取）。
3. **重启标记**：文件内容真的变了才写 `<path>.restart-required`（可用 `PAIRING_HS_RESTART_MARKER` 改路径），内容是 `{"reason","whitelist","at"}`。**内容没变（例如重复切断、同一状态的重算）不写文件也不写标记**，不会触发无谓重启。
   - **读表→渲染→写盘在同一把锁里**（#247 审查 S1）：并发的两次同步不会出现「慢的那次用旧快照覆盖新内容」。
   - **标记写两次**（#247 审查 S2）：先写标记、再替换 include、再写一次标记。进程若在 include 与标记之间崩溃，标记已经在了（否则下次启动发现 include 已是最新、什么都不写，重启就丢了）；若 supervisor 恰好在 include 落盘前就认领了第一个标记并重启，第二个标记会让它再重启一次。最坏多重启一次，绝不漏。
   - **标记写失败**（#247 审查 N3）：include 照常更新，重启记为待办；之后每次同步（含每 60s 的后台重算）都重写并重试标记，每次失败都记一条 ERROR（`restart marker could not be written`），直到写成为止。
   - 例外：进程**首次**同步时文件根本不存在 → 只建文件不写标记（homeserver 通过 `--config-path` 加载它，文件不存在时不可能在运行，首启无需重启）。之后若文件被删再重建，照常写标记——**但这只在同一进程内成立**（#247 审查 S4）：「首次同步」是进程内状态，pairing 停机期间被删掉的文件，下次启动时按首启处理、重建但**不写标记**；运维若手动删过 include，请在 pairing 启动后自行重启一次 homeserver。
4. **不自动重启**：pairing 不持有 docker socket，也不调用任何编排 API。Synapse 只在启动时读这个 include，**由运维或外部 supervisor/systemd 在看到标记后重启 Synapse，并在重启后删除标记**。在重启之前，新的配对状态仍由 fedgate 实时执行，只是本端 HS 还会白费力气地尝试出站。
5. 写入失败不影响配对请求本身（状态已提交，fedgate 已生效）：任何异常（不只是 IO/SQLite 错误，#247 审查 S3）都只记 ERROR 日志、计入失败计数（加锁，N1），并在下一次同步重试；后台重算循环本身也包了一层异常保护，单次意外不会让循环线程退出。

**Synapse 接入**：把 include 作为第二个 `--config-path` 传给 Synapse（后加载的文件覆盖同名键，所以**不要**在 homeserver.yaml 里再写 `federation_domain_whitelist`），并让 Synapse 在 pairing 健康之后再启动（pairing 先渲染再开始服务）。CI 栈的写法：

```yaml
synapse-a:
  entrypoint: [sh, -c, "... exec python -m synapse.app.homeserver --config-path /config/homeserver.yaml --config-path /hs/federation-whitelist.yaml --keys-directory /data"]
  volumes: [fed-hs-a-whitelist:/hs:ro]
  depends_on: {pairing-a: {condition: service_healthy}}
pairing-a:
  environment: {PAIRING_HS_WHITELIST_PATH: /hs/federation-whitelist.yaml}
  volumes: [fed-hs-a-whitelist:/hs]
```

生产部署时把这个目录做成宿主机目录（bind mount），supervisor 才看得到标记。

**目录属主与权限（#247 审查 S6）**：
- pairing 需要对该目录**可写**（临时文件 + `os.replace` 发生在同一目录，只给文件写权限不够）；homeserver 只需**可读**（挂 `:ro`）；supervisor 需要在目录里 `mv` / `rm` 标记，所以也要可写。
- 官方镜像里 pairing（`python:3.12-slim`）默认以 root 运行，Synapse 镜像以 UID 991 运行。推荐：宿主机建 `/srv/botville/hs`，属主 `root:991`、模式 `0750`（或 `0755`）；include 由 pairing 以 `0644` 写出，Synapse 能读、不能改。
- 如果把 pairing 改成非 root 运行，目录属主改为该 UID（或给它组写权限 `0770`），否则每次渲染都会失败并只留下 ERROR 日志（fedgate 仍然生效）。
- 不要把这个目录放在 homeserver 的数据卷里，也不要让 homeserver 可写：include 决定它能和谁联邦。

**重启流程**（手工或 supervisor，顺序很重要）：

```sh
# 1) 认领标记（mv 是原子的；重启期间若又发生配对变化，会生成新的标记，下一轮再处理）
mv /srv/botville/hs/federation-whitelist.yaml.restart-required \
   /srv/botville/hs/federation-whitelist.yaml.restart-required.processing
# 2) 只重启 homeserver（不要 down，不要动卷）
docker compose -p <project> restart synapse
# 3) 等 Synapse 恢复服务
until curl -fsS http://127.0.0.1:8008/health >/dev/null; do sleep 2; done
# 4) 删除认领的标记
rm -f /srv/botville/hs/federation-whitelist.yaml.restart-required.processing
```

**可选 systemd path unit 示例**（宿主机上运行；路径与 compose 项目名按部署修改；pairing 本身不需要任何额外权限）：

```ini
# /etc/systemd/system/botville-hs-restart.path
[Unit]
Description=Restart Synapse when botville pairing changes the federation whitelist

[Path]
PathExists=/srv/botville/hs/federation-whitelist.yaml.restart-required
Unit=botville-hs-restart.service

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/botville-hs-restart.service
[Unit]
Description=Restart Synapse for a new botville federation whitelist

[Service]
Type=oneshot
Environment=M=/srv/botville/hs/federation-whitelist.yaml.restart-required
ExecStart=/bin/mv ${M} ${M}.processing
ExecStart=/usr/bin/docker compose -p botville -f /srv/botville/server/compose/docker-compose.yml restart synapse
ExecStart=/bin/sh -c 'for i in $(seq 1 60); do curl -fsS http://127.0.0.1:8008/health >/dev/null && exit 0; sleep 2; done; exit 1'
ExecStartPost=/bin/rm -f ${M}.processing
```

启用：`systemctl daemon-reload && systemctl enable --now botville-hs-restart.path`。

**失败不会自愈（#247 审查 S5）**：服务任何一步失败（restart 失败、60 次健康检查都没过）时，`.processing` 留在原地、不会被删除，**也不会自动重试**——标记已经被 `mv` 走，`PathExists` 条件不再成立；之后只有 pairing 再写出**新**标记（下一次配对变化）才会再次触发，而在那之前 homeserver 可能一直运行着旧白名单（fedgate 仍然实时执行配对状态，安全上不受影响）。频繁失败时 systemd 还可能让 path unit 进入 `trigger-limit-hit` 失败态，之后连新标记都不再触发。请对 `botville-hs-restart.service` 的失败配置告警（如 `OnFailure=` 发通知），人工处理：确认 Synapse 状态后，`mv ….processing ….restart-required` 让它重跑，或手动重启并删除 `.processing`；必要时 `systemctl reset-failed botville-hs-restart.path`。

**短暂停机的影响**：重启期间（CI 实测约 10–30s）本端用户的客户端请求失败/重连、推送与 appservice 事务延后；联邦方向上，对端发来的事务在本端闸门后得不到应答，对端 Synapse 会退避重试，之后补齐 PDU；**重启窗口内对端发来的 EDU（输入提示、已读回执、to-device）可能丢失**。因此重启应在配对变更后尽快、但可以择时进行——在此之前 fedgate 已经执行了新状态，安全上不依赖重启的时机。

**验收**（`make federation-e2e`，e2e 脚本自己扮演 supervisor，执行上面的认领→`docker compose restart`→删除流程）：首启双方白名单只有自己且无标记；配对后两边列表都含双方、双方都有标记，重启后互通；切断后**重启前**本端 HS 仍在出站、由对端 fedgate 拦下（对端闸门访问日志里能看到请求，作为对照）；**切断并重启后**本端 HS 向对端发出的 `/_matrix/federation|key` 请求为 0（对端闸门访问日志计数不变），向对端用户发邀请在本端直接失败；重新配对并重启后双向恢复；重复切断不写标记。

**负向断言的局限（#247 审查 N4）**：「没有出站」「消息没送到」这类断言是在一个有限窗口里观察的（`NEGATIVE_TIMEOUT` 20 秒，切断后另加闸门刷新的宽限），它们证明的是「窗口内没有发生」，**不是**「永远不会发生」。Synapse 的事务重试退避可能长于 20 秒，所以窗口之后才出现的延迟出站在 e2e 里观察不到；这一层的保证来自代码路径（白名单在 Synapse 启动时加载、fedgate 实时拦截）和单元测试，e2e 只是在合理时长内的抽样验证。

**Conduit 系不在本 PR**：Tuwunel（`allowed_remote_server_names_experimental` / `forbidden_remote_server_names`）与 continuwuity（`allowed_remote_server_names` / `forbidden_remote_server_names`）的配置面与 Synapse 不同，联邦栈也只有 Synapse；留到 A-R3 之后单独做。在此之前这两家只靠 fedgate。

仍不在本节范围：对拒不配合对端的出站拉取、切断窗口内容栅栏（按 B6 决策归 #118 后续）、App 侧扫码 UI（C2.2）、跨服 AI 关系（业务归网关 B2.8；A 线提供配对事实与经 fedgate 的跨服传输）、公网 TLS 证书的获取与续期流程、媒体端点的签名/令牌校验。

## 实测要点（federation-e2e 调试结论）

- **`/send` 事务对未配对/已切断方返回 `200 {"pdus":{}}` 并丢弃**（不转发给 HS；body 最多读 1 MiB，超限直接断开连接）。原因：Synapse 对 `/send` 的任何错误码都会把目标退避 10 分钟以上，且只在收到对方入站请求时才复位；若回 403，切断后再配对时双方会互相退避、长时间不通。丢弃后消息仍然**送不到**（满足「切断后不能发」）；代价见「安全说明」：切断期 EDU 永久丢失，PDU 在重新配对后通过 DAG（get_missing_events/backfill）补齐——切断窗口内的事件也可能随之补回。其余联邦接口（拉取 state/event/backfill/媒体、邀请、profile）对未配对方仍是 403。
- **fedgate 提供 `/.well-known/matrix/server`**（`FEDGATE_WELL_KNOWN_SERVER`，如 `hs-a:443`）：委派名带显式端口，对端就跳过 SRV 查询（Docker 内置 DNS 上 SRV 查询每次约卡 10s，会让联邦请求超时）。CI 栈中 fedgate 监听 443。
- 上游 HS 的 chunked 响应由 fedgate 以 chunked 重新分帧（以关连接分帧时 Twisted 客户端会一直等到超时）；转发请求时客户端的 `Content-Length`/`Transfer-Encoding` 一律剥掉、按已读 body 重算；TLS 握手放在每连接线程中，不阻塞 accept。

- **运维提示**：`/health` 返回 `db:false`（503）应立即告警——DB 读失败时闸门最多沿用上次快照 30s（期间刚切断的对端可能仍被放行），之后受管路径一律 503。pairing 若未设置 `PAIRING_TRUSTED_PROXIES` 启动时会打 WARN（反代后所有对端共用一个限速桶）。
- **大事务与退避**：未配对/已切断方超过 1 MiB 的 `/send` 会被直接断连而非 200，发送端 Synapse 会对本端退避 10+ 分钟；重新配对后这类大事务可能要等退避结束才送达。
- **sink 日志**：按 origin 节流 + 全局每分钟上限 + 有界 LRU（1024 个 origin），轮换新 origin 既刷不爆日志也撑不大内存。
