# 推送网关（Sygnal + APNs）部署与验收

任务 A1.2（issue #4）· iOS 推送到达目标 **< 5s**。

> **验证状态与当前卡点**：homeserver → Sygnal → mock pushkin 链路的时延验收
> （< 5s）已由 CI 的 `push-e2e (synapse)` / `push-e2e (tuwunel)` job 在每次提交时
> 自动执行（`make push-e2e`，本地同样可跑）。**真实 APNs 送达尚未验证**：
> 本项目当前没有 Apple 开发者账号，也没有 APNs 证书（.p8）。因此仓库内一切
> APNs 相关配置均为占位符（`<key-id>` / `<team-id>` / `<bundle-id>`）或默认关闭
> （`BOTVILLE_APNS_ENABLED=false`），不存在任何真实 Apple 值；Sygnal 侧 APNs
> app 只有在放入真实 .p8 并填好真实值后才会被渲染。真实端到端（Sygnal →
> APNs → 真机横幅）需要账号/证书/真机就绪后按 §7 手工验证并回填本页。

本手册（及整条推送链路）只用 Matrix 标准接口（Client-Server `pushers/*` +
Push Gateway `/_matrix/push/v1/notify`）；不引入任何私有 homeserver API。

---

## 目录

1. [架构](#架构)
2. [启动与配置项](#启动与配置项)
3. [启用真实 APNs（.p8 放置）](#启用真实-apnsp8-放置)
4. [sandbox / production](#sandbox--production)
5. [App 端需要的参数](#app-端需要的参数)
6. [时延验收：mock 链路（已自动化）](#时延验收mock-链路已自动化)
7. [时延验收：真实 APNs 端到端（手工，待账号）](#时延验收真实-apns-端到端手工待账号)
8. [隐私](#隐私)
9. [运维与排障](#运维与排障)

---

## 架构

```
iOS App (Botville)
   │  ① 注册 pusher：POST /_matrix/client/v3/pushers/set
   ▼
homeserver（synapse / tuwunel，pusher 表）
   │  ② 事件触发通知：POST http://sygnal:5000/_matrix/push/v1/notify
   │     （compose 内网；data.url 由 App 注册 pusher 时提供）
   ▼
sygnal（`push` profile，本仓库 compose 服务）
   │  ③a app_id = org.botville.mock  → MockPushkin：写 JSONL（测试/CI）
   │  ③b app_id = <APNS_APP_ID>      → APNs pushkin：token 连接 APNs 推送
   ▼
APNs ──► iOS 真机横幅
```

- ②③ 的时延（homeserver 收到消息 → sygnal 侧 pushkin 收到通知）由
  `make push-e2e` 自动断言 < 5s；③b 之后到真机横幅的一段需要真实证书，
  见 §7。
- Sygnal 是 Matrix 参考推送网关（element-hq/sygnal），镜像 pin 见
  [docs/ops/upgrade-images.md](../ops/upgrade-images.md) §2 pin 表。
- `push` 是**叠加型 profile**：与任一 homeserver profile 组合
  （`make up HS=tuwunel PUSH=1`）。不开 `PUSH=1` 时 sygnal 完全不启动，
  homeserver 行为与 A0.2/A1.1 完全一致。

## 启动与配置项

```bash
make up PUSH=1          # synapse + postgres + sygnal（当前 HS 按 .env）
make up HS=tuwunel PUSH=1
make push-e2e           # 时延验收（见 §6）
make logs PUSH=1        # 跟踪含 sygnal 的日志
make down PUSH=1        # 停止（含 sygnal；数据卷保留）
```

`server/compose/.env` 中的推送相关键（`server/compose/.env.example` 只有占位符，
由 `server/scripts/init.sh` 生成/补齐）：

| 键 | 默认 | 说明 |
|---|---|---|
| `BOTVILLE_PUSH_PORT` | `8019` | sygnal 5000 端口映射到宿主 `127.0.0.1` 的端口，仅供本机手工探测/`push_e2e.py`；homeserver 走 compose 内网 `http://sygnal:5000`，不需要它对外暴露 |
| `BOTVILLE_APNS_ENABLED` | `false` | `true` 时 init.sh 尝试渲染 APNs app（前置条件见 §3） |
| `APNS_APP_ID` | `org.botville.app.ios` | Sygnal 侧（及 App 端 pusher 的）`app_id`，与 Apple 后台无必然关系，但两端必须一致 |
| `APNS_TOPIC` | `<bundle-id>` | APNs topic，填 App 的 Bundle ID（如 `org.botville.app.ios`） |
| `APNS_KEY_ID` | `<key-id>` | Apple 后台 Keys 页的 10 位 Key ID |
| `APNS_TEAM_ID` | `<team-id>` | Apple 开发者 Team ID（10 位） |
| `APNS_PLATFORM` | `sandbox` | `sandbox`（开发构建）或 `production`（TestFlight/App Store），见 §4 |
| `SYGNAL_IMAGE` | pin 值 | 镜像覆盖，规则同其他服务（tag@sha256 digest） |

配置渲染：`server/compose/sygnal/sygnal.yaml.tmpl` →
`server/compose/generated/sygnal/sygnal.yaml`（gitignored，容器内
`SYGNAL_CONF=/config/sygnal.yaml` 指向它）。模板默认只带一个 app：

```yaml
apps:
  "org.botville.mock":
    type: botville_mock_pushkin.MockPushkin   # server/compose/sygnal/ 下的自定义 pushkin
    pushes_path: /data/mock-pushes.jsonl
```

`org.botville.mock` 是**常驻**的测试 pushkin（sygnal 0.17 的自定义 pushkin 机制：
`type` 写成可 import 的全限定类名，compose 把 `server/compose/sygnal/` 挂到容器
`/custom` 并设 `PYTHONPATH=/custom`）。它实现 Pushkin 接口
（`async dispatch_notification(n, device, context) -> rejected pushkeys 列表`），
把收到的每条通知以 JSONL 追加写入 `/data/mock-pushes.jsonl`
（宿主路径 `server/compose/generated/sygnal-data/mock-pushes.jsonl`）并打日志，
用来在无 Apple 账号的环境里测前半链路时延。

## 启用真实 APNs（.p8 放置）

前提：Apple Developer 账号下创建过 APNs Auth Key（.p8）。**当前项目没有账号，
以下步骤属于「拿到账号后」的操作指引；在拿到之前不要把任何占位值改成看似真实的值。**

1. 把 .p8 文件放到 `server/compose/secrets/apns.p8`（目录不存在就创建）：

   ```bash
   mkdir -p server/compose/secrets
   cp /path/to/AuthKey_XXXXXXXXXX.p8 server/compose/secrets/apns.p8
   chmod 600 server/compose/secrets/apns.p8
   ```

   `.gitignore` 已覆盖 `secrets/` 与 `*.p8`；**任何情况下不要把 .p8 或其内容
   提交进仓库、贴进 issue/日志**。compose 把 `./secrets` 只读挂载到容器
   `/secrets`。
2. 编辑 `server/compose/.env`：

   ```bash
   BOTVILLE_APNS_ENABLED=true
   APNS_APP_ID=org.botville.app.ios      # 与 App 端 pusher app_id 一致
   APNS_TOPIC=<你的 Bundle ID>
   APNS_KEY_ID=<10 位 Key ID>
   APNS_TEAM_ID=<10 位 Team ID>
   APNS_PLATFORM=sandbox                 # 或 production，见 §4
   ```
3. `make up PUSH=1`（会先跑 init.sh）：init.sh 只有在
   **`APNS_*` 全为真实值（没有 `<placeholder>`）且 `secrets/apns.p8` 存在**时，
   才把 APNs app 追加渲染进 `generated/sygnal/sygnal.yaml`（keyfile 固定指向
   `/secrets/apns.p8`）；否则打印 WARNING 并**跳过 APNs app**（mock pushkin 不受
   影响）。这是有意设计：占位值渲染出去只会让 sygnal 起不来，还会连累 mock 链路。
4. 验证渲染结果：

   ```bash
   grep -A6 'type: apns' server/compose/generated/sygnal/sygnal.yaml
   ```

多实例（`COMPOSE_PROJECT_NAME` 不同）注意：`.env` 是每份 checkout 一套，
`secrets/` 同理；同机多实例各自放各自的 key（或共用同一 bundle 的 key，但
`BOTVILLE_PUSH_PORT` 仍需错开）。

## sandbox / production

- `APNS_PLATFORM=sandbox`：开发构建（Xcode 直装、`aps-environment =
  development` 的 provisioning profile）收推送走 APNs sandbox 环境。
- `APNS_PLATFORM=production`：TestFlight / App Store 构建。
- 两者**互不相通**：sandbox 证书发的推送 production 构建收不到，反之亦然。
  真机联调常见「发了但收不到」就是环境配错。
- 升级到 production 只需改 `.env` 里 `APNS_PLATFORM=production` 后
  `make up PUSH=1` 重新渲染。

## App 端需要的参数

App（iOS 客户端）向 homeserver 注册 pusher 时需要的四个关键值：

| 参数 | 值 | 说明 |
|---|---|---|
| `app_id` | `org.botville.mock`（联调）或 `<APNS_APP_ID>`（真实） | 必须与 sygnal.yaml `apps:` 下的键一致；不一致时 sygnal 按规范把 pushkey 放进 `rejected`，homeserver 会丢弃该 pusher |
| `pushkey` | APNs device token（hex 字符串） | 系统注册远程通知回调里拿到的 `deviceToken` 转 hex；一个设备一个 token，可能失效（App 重装/系统重置），失效后由 `rejected` 机制回收 |
| `data.url` | `https://<push-domain>/_matrix/push/v1/notify` | homeserver 回调网关的地址，必须是**homeserver 容器可达**的公网 https 地址（反代到 sygnal 的 5000；内网部署联调可用 `http://sygnal:5000/_matrix/push/v1/notify`，即 CI 里的值） |
| `data.format` | `event_id_only`（建议） | 见 §8 隐私；同时是本仓库假定的推送格式 |

注册示例（标准 `POST /_matrix/client/v3/pushers/set`，即
`server/tests/push_e2e.py` 里的做法）：

```json
{
  "app_id": "org.botville.app.ios",
  "pushkey": "<APNs device token hex>",
  "kind": "http",
  "app_display_name": "Botville",
  "device_display_name": "iPhone",
  "lang": "zh-Hans",
  "data": {
    "url": "https://push.example.invalid/_matrix/push/v1/notify",
    "format": "event_id_only"
  }
}
```

反向代理：把 `push.example.invalid`（示例占位域名）TLS 终止后转发到
`127.0.0.1:8019`（或直接转发到 compose 内网 `sygnal:5000`）。App 到 APNs 的
长连接与本仓库无关；这里只需要保证 homeserver → `data.url` 可达。

## 时延验收：mock 链路（已自动化）

`server/tests/push_e2e.py`（仅标准库）在 `make push-e2e` 里的完整流程：

1. 等 homeserver 就绪；同时用「未知 app_id 直接 POST sygnal
   `/_matrix/push/v1/notify`，断言 200 且 `rejected` 含该 pushkey」验证网关
   在线与拒绝语义（sygnal 容器无 healthcheck，这一步就是就绪探测）；
2. 注册 alice/bob（通用 UIA）；alice 建私聊邀请 bob，bob join；
3. bob 设置 `app_id=org.botville.mock` 的 http pusher
   （`data.url=http://sygnal:5000/...`，`format=event_id_only`）；
4. alice 发消息，记录发送时刻（墙钟 ms；容器与宿主共享时钟）；
5. 轮询 `docker compose ... exec -T sygnal cat /data/mock-pushes.jsonl`
   （`--pushes-file` 可直接读宿主路径 `server/compose/generated/
   sygnal-data/mock-pushes.jsonl`），直到出现该 `event_id`，计算
   `received_at_ms - 发送时刻`，断言 `< --max-latency`（默认 5s，Makefile 里
   `E2E_MAX_PUSH_LATENCY` 可调）并打印。

CI（`.github/workflows/ci.yml`）在 **synapse 与 tuwunel** 两台 homeserver 上各跑
一遍（matrix job `push-e2e`），失败自动 dump 全部容器日志并 `down -v` 清场。
本地：

```bash
make up HS=synapse PUSH=1 && make push-e2e
make up HS=tuwunel PUSH=1 && make push-e2e
```

注意：该验收覆盖 **homeserver → sygnal**（含 homeserver 内部 pusher 调度）；
`sygnal → APNs → 真机` 不在内（mock pushkin 在 ③ 的位置截断）。

## 时延验收：真实 APNs 端到端（手工，待账号）

前置：Apple 开发者账号、按 §3 放好 .p8 并启用、真机装了带推送 entitlement
的 debug 构建、`APNS_PLATFORM` 与构建类型匹配。

1. `make up PUSH=1`，确认 `grep 'type: apns' server/compose/generated/sygnal/sygnal.yaml`
   有输出，`docker compose ... --profile push ps` 里 sygnal 为 running；
2. 真机 App 登录任一账号（bob），允许通知，注册 pusher（`app_id=<APNS_APP_ID>`，
   `pushkey=deviceToken hex`，`data.url` 指向公网网关地址，`format=event_id_only`）；
3. 保持 App **在后台/锁屏**（前台时 iOS 默认不展示横幅，容易误判「收不到」）；
4. 另一账号（alice）给 bob 发一条消息，掐表记录从发送到真机横幅出现的时间；
   目标 < 5s（覆盖 homeserver→sygnal 实测约几十至几百 ms，剩余预算在
   sygnal→APNs→设备，正常网络下 1–2s 内）；
5. 边界项：App 冷启动（未运行）时是否收到；badge 数字是否随 unread 变化；
   App 在前台时不弹横幅但 unread/badge 正常；
6. 失败排查：`make logs PUSH=1` 看 sygnal 对该 pushkey 的错误（`BadDeviceToken`
   ⇒ sandbox/production 配错或 token 失效；`InvalidProviderToken` ⇒ key_id/
   team_id/topic 填错）；homeserver 侧看是否真的 POST 了网关
   （`data.url` 不可达是最常见问题）；
7. 把实测时延、机型/iOS 版本、网络环境回填本节（当前状态：**未验证**）。

## 隐私

- `data.format=event_id_only`（本仓库默认且建议）：homeserver 发给网关的通知
  **只含 event_id/room_id/计数等元数据，不含消息内容**；sygnal 转给 APNs 的
  payload 同样不含明文内容，**消息正文不会离开自己的部署**、不会发给 Apple。
- App 收到推送后自行通过 Matrix CS API 拉取事件正文解密/展示（端到端加密房间
  中正文本来就只有客户端能解）。
- 不用 `event_id_only` 时（不推荐）：通知正文会经网关明文流转（APNs 通道本身
  是 TLS 的，但内容会出你的部署边界），且 sygnal 日志可能携带更多字段。
- mock pushkin 的 JSONL 只含 event_id/room_id/pushkey/时间戳等元数据，不含
  消息内容；该目录（`generated/sygnal-data/`）已 gitignore，属临时测试数据，
  可随时删除。

## 运维与排障

- **sygnal 不启动/反复重启**：`docker compose ... --profile push logs sygnal`；
  最常见是渲染出的配置带占位值（本仓库的 init.sh 已拦截）或 YAML 缩进被改坏。
- **pusher 被静默删除**：sygnal 对无效 pushkey 返回 `rejected`，homeserver 会
  删除对应 pusher；App 端需要在 token 变化时重新 `pushers/set`。
- **升级 sygnal 镜像**：与其他服务同一套 pin 流程，
  见 [docs/ops/upgrade-images.md](../ops/upgrade-images.md)（compose 默认值、
  `.env.example`、CI allowlist 三处同步）。
- **mock JSONL 增长**：只在跑 push-e2e 时增长，量级极小；要清理直接
  `rm server/compose/generated/sygnal-data/mock-pushes.jsonl`。
- 相关文件索引：`server/compose/sygnal/`（sygnal.yaml.tmpl、apns-app.yaml.part、
  botville_mock_pushkin.py）、`server/tests/push_e2e.py`、
  `server/tests/test_push_sygnal.py`、Makefile（`PUSH=1` / `push-e2e`）、
  CI job `push-e2e`。
