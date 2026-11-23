# pair_redeem

App 扫码兑换：`POST /_botville/pair/redeem`（首版仅 Synapse `registration_token`）。
与联邦 `/v1/pairing/redeem` 分开。默认绑定 `127.0.0.1:8092`。

环境变量（`BOTVILLE_PAIR_REDEEM_*`）的名字和默认值写在 `service.py` 顶部的常量注释里。
