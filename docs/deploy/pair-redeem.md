# App 扫码兑换（可选 compose 接线）

`POST /_botville/pair/redeem` 由 `server/pair_redeem` 提供，与联邦 `POST /v1/pairing/redeem` 完全分开。本页只说明 **可选的官方 compose 接线** 和 **隔离环境端到端测试**。这不是真实 HTTPS 入口，也不是手机扫码验收。

## 为什么默认关闭

兑换进程会使用 Synapse 管理员 access token 调用 `/_synapse/admin/v1/registration_tokens/new`。默认 `make up` 不启动该服务，也不要求填写这个 token，避免无操作符确认时带上特权凭据。

## 如何显式启用

1. 在 `server/compose/.env` 设置 `BOTVILLE_PAIR_REDEEM_SYNAPSE_ADMIN_TOKEN` 为已有的 Synapse 管理员 access token。`init.sh` **不会**生成这个值（随机 hex 不是 Synapse token）。将 token 留空直到你决定启用。
2. `BOTVILLE_PAIR_REDEEM_SYNAPSE_URL` 默认 `http://synapse:8008`（compose 内网）。进程拒绝把明文 admin token 发往非 HTTPS 且非 loopback/内网的目标，并拒绝跟随重定向。
3. 启动：`make up HS=synapse PAIR_REDEEM=1`。宿主机端口只绑 `127.0.0.1:${BOTVILLE_PAIR_REDEEM_PORT:-8092}`。
4. 本服务不打开联邦配对入站，也不打开 relations 跨服桥。

首版只支持 Synapse + `registration_token`（新账号）。`login_token`、管理签发 API/UI/CLI、Caddy edge / 公网 HTTPS、8845 实装都还没有。

## 隔离环境测试

```
make pair-redeem-e2e
```

会使用独立 compose 项目 `botville-pair-redeem-e2e`（端口 `127.0.0.1:18208` / `127.0.0.1:18292`），不改 `server/compose/.env` 或默认 `botville` 项目，结束后 `down -v` 自己的项目。它会验收默认关闭 / 显式启用、真实 Synapse `registration_token` 可注册、重复兑换/过期/作废/限流/签发失败回滚，以及 pair_redeem 自身日志与审计不含明文兑换码、registration_token 或 admin token。全部 compose 日志另外检查兑换码、admin token 与栈秘密，但不承诺 Synapse 自身不会记录其签发的 registration_token。

这仍是隔离测试，不能代替真实 HTTPS 入口或真机扫码。

### 处理时限与启用前检查

显式 `PAIR_REDEEM=1` 前必须先填入非空、有效的 Synapse admin token；`docker compose up -d` 成功不代表服务健康，空 token 会被进程拒绝，应检查 `/health`，不要将 crash-loop 当作启动成功。

请求 deadline 同时限制 store 锁等待与 issuer 总时限。取得事务锁后、更新 used 前、调用 issuer 前以及提交前均检查 deadline，超时回滚，不再启动签发。无法原子保证数据库提交与客户端收到响应：提交后网络断开仍可能丢失响应；已经发往 Synapse 的 HTTP 调用无法撤销，超时回滚可能留下未交付的短期 registration_token。本刀不引入 token 恢复/撤销或新契约。
