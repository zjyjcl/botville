# App 扫码兑换（可选 compose 接线）

`POST /_botville/pair/redeem` 由 `server/pair_redeem` 提供，与联邦 `POST /v1/pairing/redeem` 完全分开。本页说明 **可选的官方 compose 接线**、**可选 HTTPS edge** 和 **隔离环境端到端测试**。隔离/自签 TLS 不能代替公网 HTTPS，也不是手机扫码验收。

## 为什么默认关闭

兑换进程会使用 Synapse 管理员 access token 调用 `/_synapse/admin/v1/registration_tokens/new`。默认 `make up` 不启动该服务，也不要求填写这个 token，避免无操作符确认时带上特权凭据。

## 如何显式启用

1. 在 `server/compose/.env` 设置 `BOTVILLE_PAIR_REDEEM_SYNAPSE_ADMIN_TOKEN` 为已有的 Synapse 管理员 access token。`init.sh` **不会**生成这个值（随机 hex 不是 Synapse token）。将 token 留空直到你决定启用。
2. `BOTVILLE_PAIR_REDEEM_SYNAPSE_URL` 默认 `http://synapse:8008`（compose 内网）。进程拒绝把明文 admin token 发往非 HTTPS 且非 loopback/内网的目标，并拒绝跟随重定向。
3. 启动：`make up HS=synapse PAIR_REDEEM=1`。宿主机端口只绑 `127.0.0.1:${BOTVILLE_PAIR_REDEEM_PORT:-8092}`。
4. 本服务不打开联邦配对入站，也不打开 relations 跨服桥。

首版只支持 Synapse + `registration_token`（新账号）。可选管理签发见下文，默认关闭。`login_token`、管理 Web UI、一次性管理 token、面向公网的完整 edge（homeserver / 网关 / Cloudflare）、8845 实装都还没有。可选 HTTPS edge 见下一节，默认关闭。

## 隔离环境测试

```
make pair-redeem-e2e
```

会使用独立 compose 项目 `botville-pair-redeem-e2e`（端口 `127.0.0.1:18208` / `127.0.0.1:18292`），不改 `server/compose/.env` 或默认 `botville` 项目，结束后 `down -v` 自己的项目。它会验收默认关闭 / 显式启用、真实 Synapse `registration_token` 可注册、重复兑换/过期/作废/限流/签发失败回滚，以及 pair_redeem 自身日志与审计不含明文兑换码、registration_token 或 admin token。全部 compose 日志另外检查兑换码、admin token 与栈秘密，但不承诺 Synapse 自身不会记录其签发的 registration_token。

这仍是隔离测试，不能代替真实 HTTPS 入口或真机扫码。

## 可选 HTTPS edge（默认关闭）

官方 compose 可以用 Caddy 在 `pair-redeem` 前面加一层本机 HTTPS 反代。`make up` **不会**启动它，也不要求真实证书或域名。显式启用：

1. 先按上一节启用 `PAIR_REDEEM=1`（Synapse only，并填入有效的 admin token）。
2. 把 `cert.pem` 和 `key.pem` 放到 `BOTVILLE_PAIR_REDEEM_EDGE_CERT_DIR`（默认 `server/compose/generated/pair-redeem-edge`）。`init.sh` **不会**生成证书。本地或自签证书只用于本机试验，**不能**代替公网 CA 证书，也**不能**当作真机扫码验收材料，不要把生产证书提交进仓库。
3. `make up HS=synapse PAIR_REDEEM=1 PAIR_REDEEM_EDGE=1`。必须同时打开两个 profile（Makefile 已检查；官方 compose 里 edge `depends_on` pair-redeem）。宿主机端口只绑 `127.0.0.1:${BOTVILLE_PAIR_REDEEM_EDGE_PORT:-8443}`。Caddy 按地址监听 `:8443`，不按主机名分流。

默认 `BOTVILLE_PAIR_REDEEM_TRUSTED_PROXIES` 为空：pair_redeem 不采信 `X-Forwarded-For`，限速键是直连对端。经 edge 进来时直连对端就是 edge 容器，所有客户端会共用一个 per-IP 桶，`IP_LIMIT` 实际变成全局桶。若要把限速键改成「edge 前面那一跳」，把 `TRUSTED_PROXIES` 设为 **edge 的固定 IP/CIDR**（不要填真实公网域名）。这只在 edge 是首跳（客户端直接连 edge）时，才会把真实客户端当作 XFF 的最后一跳。若前面还有公网或隧道反代，Caddy 默认会用直连对端（隧道/反代）覆盖客户端自报的 `X-Forwarded-For`；即便在 edge 上再配 `trusted_proxies` 把前置反代传来的 XFF 传给上游，pair_redeem 仍取最后一个 hop，得到的是前置反代而不是手机或真实用户，限速仍是「每隧道一个桶」。本机 loopback 试验可以继续留空。不要把空默认改成需要真实公网地址的值；也不要指望只改 `TRUSTED_PROXIES` 就能在隧道场景按真实用户限速。

Edge **只**把以下精确路径反代到 compose 内网的 `pair-redeem:8092`（pair_redeem 在内网仍是 HTTP）：

- `POST /_botville/pair/redeem`（公开兑换，契约不变）
- `GET /health`（进程健康检查，不含密钥）

其它路径由 edge 直接返回 404，包括但不限于：`/_synapse/admin/*`、联邦 `POST /v1/pairing/redeem`、网关 `/_botville/gateway/*`、签发/管理面（含 `POST /_botville/pair/issue`）、`/_botville/pair/redeem/` 的子路径。不要把 pair_redeem 的宿主机 8092 端口、Synapse、网关、数据库或密钥挂到公共 edge。管理签发**永远不得**暴露到公共 edge，也不可无鉴权签发。

面向 App / 公网的 homeserver 与 gateway URL 必须是 `https://`，文档和示例只用占位域名 `example.org`（例如 `hs=https://hs.example.org`、`gw=https://example.org/_botville/gateway`）。不要把可被当成生产可用的明文 `http://` 写进公网 URL。内网里的 `http://synapse:8008` 和 `http://pair-redeem:8092` 只走 compose 网络，不对外发布。

本 edge 不是 #128 完整公网入口（不含 homeserver 客户端 API、网关路径、`.well-known`、Cloudflare Tunnel / frp）。真实域名和 Cloudflare 配置只写在部署侧，不进仓库。

隔离测试：

```
make pair-redeem-edge-e2e
```

使用独立 compose 项目 `botville-pair-redeem-edge-e2e`（端口 `127.0.0.1:18408` / `127.0.0.1:18492` / `127.0.0.1:18443`），测试时生成一次性自签证书，结束后 `down -v` 自己的项目并删除证书和秘密环境文件。它会验收默认关闭、开启后的合法路由、以及内部路径不能从 edge 到达。这仍是隔离/mock TLS，不能代替公网 HTTPS 或真机扫码。

## 可选管理签发（默认关闭）

给 App 扫码准备 `registration_token` 配对 URI。本刀只提供**鉴权后的签发能力**：

- HTTP：`POST /_botville/pair/issue`（JSON：必填 `hs`，可选 `gw` / `fp` / `auth_type`）
- 本机 CLI：`python -m pair_redeem issue --hs https://hs.example.org`（可选 `--gw`、`--fp`；token 只从环境变量读取，不进 argv）

默认关闭。`BOTVILLE_PAIR_REDEEM_ISSUE_TOKEN` 留空（`init.sh` **不会**生成）时，该路由返回 404，与未提供的路径相同。显式启用：先按上文启用 `PAIR_REDEEM=1`，再把该变量设为至少 32 个字符、且**必须不同于** `BOTVILLE_PAIR_REDEEM_SYNAPSE_ADMIN_TOKEN` 的值。请求头为 `Authorization: Bearer <token>`。错误或缺 token 时不要把 token、兑换码或完整 URI 写进日志。

本切片用的是**长期环境变量 token**，不是 `docs/contracts/pairing-qr.md` §4.1 所写的「一次性、短 TTL 管理 token」。一次性管理 token 留给后续，本刀尚未实现。

`hs` / `gw` 必须是 `https://`（示例域名只用 `example.org`）。本机签发 API 走 loopback 上的 HTTP（宿主机端口仍只绑 `127.0.0.1`）；这不是给手机用的公网入口。手机扫码兑换走二维码里的 `hs` + `POST /_botville/pair/redeem`，需要公网 HTTPS（或已单独启用的可选 edge）。本地或自签证书、以及本 CLI 打到 loopback **不能**代替公网 HTTPS，也**不能**当作真机扫码验收。

成功响应只返回一次配对 URI（`botville://pair?v=1&hs=…&code=…&exp=…`，可选 `gw` / `fp`）和 `exp` / `auth_type`。明文 `code` 只出现在这条 URI 里，库中只存 `sha256(code)`。不要把 URI 或 token 提交进仓库，也不要重定向到会长期保留的日志文件。

本刀**不**做：管理 Web UI、管理员 Matrix 登录、一次性短 TTL 管理 token、签发记录列表/筛选/批量作废、`login_token`（包括「本人从已登录设备发起」）。作废仍可通过现有 store 接口在测试里使用，没有管理面 API。公共 edge 的允许列表不得加入 `/_botville/pair/issue`。

### 处理时限与启用前检查

显式 `PAIR_REDEEM=1` 前必须先填入非空、有效的 Synapse admin token；`docker compose up -d` 成功不代表服务健康，空 token 会被进程拒绝，应检查 `/health`，不要将 crash-loop 当作启动成功。

请求 deadline 同时限制 store 锁等待与 issuer 总时限。取得事务锁后、更新 used 前、调用 issuer 前以及提交前均检查 deadline，超时回滚，不再启动签发。无法原子保证数据库提交与客户端收到响应：提交后网络断开仍可能丢失响应；已经发往 Synapse 的 HTTP 调用无法撤销，超时回滚可能留下未交付的短期 registration_token。本刀不引入 token 恢复/撤销或新契约。
