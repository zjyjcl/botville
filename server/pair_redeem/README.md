# pair_redeem

App 扫码兑换：`POST /_botville/pair/redeem`（首版仅 Synapse `registration_token`）。
与联邦 `/v1/pairing/redeem` 分开。默认绑定 `127.0.0.1:8092`。

环境变量（`BOTVILLE_PAIR_REDEEM_*`）的名字和默认值写在 `service.py` 顶部的常量注释里。

## 可选 compose 接线

默认关闭。`make up` 不会启动本服务，也不需要 Synapse admin token。显式启用：

```
make up HS=synapse PAIR_REDEEM=1
```

并在 `server/compose/.env` 填写 `BOTVILLE_PAIR_REDEEM_SYNAPSE_ADMIN_TOKEN`（`init.sh` 不会生成）。宿主机端口只绑 `127.0.0.1:8092`。这与联邦 `/v1/pairing/redeem` 分开，也不会打开配对入站或 relations 桥。

可选 HTTPS edge 默认关闭：`make up HS=synapse PAIR_REDEEM=1 PAIR_REDEEM_EDGE=1`，并把 `cert.pem` / `key.pem` 放到 `BOTVILLE_PAIR_REDEEM_EDGE_CERT_DIR`（`init.sh` 不会生成证书）。宿主机端口只绑 `127.0.0.1:8443`。Edge 只反代 `POST /_botville/pair/redeem` 与 `GET /health`。本地或自签证书不能代替公网 HTTPS 或真机扫码。

可选管理签发（`POST /_botville/pair/issue` 与 `python -m pair_redeem issue`）同样默认关闭：把 `BOTVILLE_PAIR_REDEEM_ISSUE_TOKEN` 设为至少 32 字符、且不同于 Synapse admin token 的值后才会提供该路由（`init.sh` 不会生成）。签发必须鉴权，**永远不得**把 `/_botville/pair/issue` 加到公共 edge 的允许列表。本刀没有管理 Web UI，也不签发 `login_token`。说明见 `docs/deploy/pair-redeem.md`。

隔离环境端到端测试（不是真机扫码 / 不是真实 HTTPS 入口）：`make pair-redeem-e2e`。可选 HTTPS edge 的隔离测试（mock TLS，同样不能代替公网 HTTPS 或真机扫码）：`make pair-redeem-edge-e2e`。
