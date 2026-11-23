# pair_redeem

App 扫码兑换：`POST /_botville/pair/redeem`（首版仅 Synapse `registration_token`）。
与联邦 `/v1/pairing/redeem` 分开。默认绑定 `127.0.0.1:8092`。

环境变量（`BOTVILLE_PAIR_REDEEM_*`）的名字和默认值写在 `service.py` 顶部的常量注释里。

## 可选 compose 接线

默认关闭。`make up` 不会启动本服务，也不需要 Synapse admin token。显式启用：

```
make up HS=synapse PAIR_REDEEM=1
```

并在 `server/compose/.env` 填写 `BOTVILLE_PAIR_REDEEM_SYNAPSE_ADMIN_TOKEN`（`init.sh` 不会生成）。宿主机端口只绑 `127.0.0.1:8092`。这与联邦 `/v1/pairing/redeem` 分开，也不会打开配对入站或 relations 桥。HTTPS/Caddy edge 和管理签发是后续刀。说明见 `docs/deploy/pair-redeem.md`。

隔离环境端到端测试（不是真机扫码 / 不是真实 HTTPS 入口）：`make pair-redeem-e2e`。
