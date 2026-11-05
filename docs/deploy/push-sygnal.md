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
10. [内网推送放行](#内网推送放行botville_docker_subnet-与-botville_sygnal_ip)
11. [主机有代理时](#主机有代理时no_proxy-固化issue-134)
12. [8845 mini PC（Ubuntu Server 24.04）部署](#8845-mini-pcubuntu-server-2404-部署)

---

## 架构

```
iOS App (Botville)
   │  ① 注册 pusher：POST /_matrix/client/v3/pushers/set
   ▼
homeserver（synapse / tuwunel / continuwuity，pusher 表）
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
make up PUSH=1          # synapse + postgres + sygnal（当前 HS 按 .env）；
                        # PUSH=1 时每次都会 force-recreate 仅 sygnal，保证
                        # init.sh 刚渲染的 sygnal.yaml 被运行中的网关重新加载
make up HS=tuwunel PUSH=1
make push-e2e           # 时延验收（见 §6）
make logs PUSH=1        # 跟踪含 sygnal 的日志
make down               # 停止（总是连带 sygnal 一起停；数据卷保留）
```

为什么 `PUSH=1` 的 `up` 要 force-recreate sygnal：`APNS_*` /
`BOTVILLE_APNS_ENABLED` 等键只被 init.sh 用来**渲染**
`generated/sygnal/sygnal.yaml`（bind mount 进容器），不参与 compose 插值——
改值不会改变服务配置哈希，`up -d` 不会重建容器；而 sygnal 只在进程启动时读
一次配置（无热加载）。所以 `make up PUSH=1` 在 `up -d` 之后用
`up -d --force-recreate --no-deps sygnal` **只重建 sygnal**（不动
homeserver/postgres），保证首起、反复 up、改配置后再 up 三种情况网关都用的是
新配置。不带 `PUSH` 的 `make up` 完全不碰 sygnal。

`server/compose/.env` 中的推送相关键（`server/compose/.env.example` 只有占位符，
由 `server/scripts/init.sh` 生成/补齐）：

| 键 | 默认 | 说明 |
|---|---|---|
| `BOTVILLE_PUSH_PORT` | `8019` | sygnal 5000 端口映射到宿主 `127.0.0.1` 的端口，仅供本机手工探测/`push_e2e.py`；homeserver 走 compose 内网 `http://sygnal:5000`，不需要它对外暴露 |
| `BOTVILLE_SYGNAL_IP` | `172.31.250.2` | sygnal 容器在 compose 网络里的固定 IP（`BOTVILLE_DOCKER_SUBNET` 的 `.2`，须在动态池 `BOTVILLE_DOCKER_IP_RANGE` 之外——池外的固定地址才不会被动态分配抢走）。**整个部署唯一放行的私网地址**：Synapse `ip_range_whitelist` 与 tuwunel/continuwuity 的 denylist 例外都只有这个 /32。必须在子网内且不是网络/网关(`.1`)/广播地址，非法时 init.sh 友好报错退出；**留空 = 完全不放行内网**（denylist 用完整上游默认、Synapse 不写 whitelist），见 §10 |
| `BOTVILLE_PUSH_MOCK` | `true` | 是否在生成的 sygnal.yaml 里带 `org.botville.mock` 测试 pushkin（`make push-e2e` / CI 依赖）。生产只跑真实 APNs 时设 `false`；它与 APNs app 都关时 sygnal 无任何 app，init.sh 会 WARNING——**且 sygnal v0.17 会因 "No app IDs are configured" 拒绝启动并反复重启**（实测），两个开关不要同时关 |
| `BOTVILLE_APNS_ENABLED` | `false` | 真值（`true/1/yes`，大小写不敏感；其他值打 WARNING 并按真处理）时 init.sh 尝试渲染 APNs app（前置条件见 §3）。**缺项/占位 → WARNING 跳过（mock 照常）；已填但格式非法或 .p8 权限不对 → init.sh 直接报错退出（exit 1）**，报错只写字段名和规则、不回显值 |
| `BOTVILLE_APNS_BUNDLE_ID` | `<bundle-id>` | iOS Bundle ID（反向域名，即 Xcode 的 `PRODUCT_BUNDLE_IDENTIFIER`）。`APNS_TOPIC` 默认取它，`APNS_APP_ID` 默认推导为 `<bundle-id>.apns`；格式非法时 exit 1 |
| `APNS_APP_ID` | 由 bundle 推导 | Sygnal 侧（及 App 端 pusher 的）`app_id`。单 app 约定（issue #4 方案 A）：**必须等于 `<bundle-id>.apns`**；留空/占位时 init.sh 推导并写回 `.env`，显式填了但不符则 exit 1 |
| `APNS_TOPIC` | 由 bundle 推导 | APNs topic = App 的 Bundle ID。留空/占位时 init.sh 取 `BOTVILLE_APNS_BUNDLE_ID` 并写回 `.env`；显式填了但不是反向域名格式则 exit 1 |
| `APNS_KEY_ID` | `<key-id>` | Apple 后台 Keys 页的 10 位 Key ID（`[A-Z0-9]{10}`，填了但格式不对 exit 1） |
| `APNS_TEAM_ID` | `<team-id>` | Apple 开发者 Team ID（10 位，同上格式校验） |
| `APNS_PLATFORM` | `sandbox` | `sandbox`（开发构建）或 `production`（TestFlight/App Store），见 §4；切环境只改这一项后重启（方案 A，单 app） |
| `BOTVILLE_PUSH_DOMAIN` | `<push-domain>` | 推送公网域名（反代 `push.<domain>` 用）。**仅在 `BOTVILLE_PUSH_PUBLIC=true` 时需要**：App 注册 pusher 的 `data.url` 即 `https://<BOTVILLE_PUSH_DOMAIN>/_matrix/push/v1/notify`；运维参考值，compose 栈本身不消费它（见 §12 反代示例） |
| `BOTVILLE_PUSH_PUBLIC` | `false` | #128 决策：`push.<domain>` **默认不对公网开放**。`false`（默认）= homeserver → sygnal 走 compose 内网 `http://sygnal:5000`（放行面只有 `BOTVILLE_SYGNAL_IP` 那一个 /32，见 §10），不部署公网反代，App 的 `data.url` 用内网地址（只需 homeserver 容器可达，App 自身不访问它）；`true` 才按 §12 的反代示例把网关暴露到 `BOTVILLE_PUSH_DOMAIN`（仅当 homeserver 无法经内网到达 sygnal 时才需要）。运维开关，compose 栈本身不消费（同 `BOTVILLE_PUSH_DOMAIN`） |
| `SYGNAL_IMAGE` | pin 值 | 镜像覆盖，规则同其他服务（tag@sha256 digest） |

配置渲染：`server/compose/sygnal/sygnal.yaml.tmpl` →
`server/compose/generated/sygnal/sygnal.yaml`（gitignored，容器内
`SYGNAL_CONF=/config/sygnal.yaml` 指向它）。模板本身不带 app；init.sh 按配置
把 app 片段追加到 `apps:` 下，默认结果只带一个 app：

```yaml
apps:
  "org.botville.mock":
    type: botville_mock_pushkin.MockPushkin   # server/compose/sygnal/ 下的自定义 pushkin
    pushes_path: /data/mock-pushes.jsonl
```

`org.botville.mock` 是**默认渲染**的测试 pushkin（`BOTVILLE_PUSH_MOCK=true`，
生产可设 `false` 移除；sygnal 0.17 的自定义 pushkin 机制：
`type` 写成可 import 的全限定类名，compose 把 `server/compose/sygnal/` 挂到容器
`/custom` 并设 `PYTHONPATH=/custom`）。它实现 Pushkin 接口
（`async dispatch_notification(n, device, context) -> rejected pushkeys 列表`），
把收到的每条通知以 JSONL 追加写入 `/data/mock-pushes.jsonl`
（宿主路径 `server/compose/generated/sygnal-data/mock-pushes.jsonl`）并打日志，
用来在无 Apple 账号的环境里测前半链路时延。JSONL 只记元数据外加
`has_content`（通知 content 的**键名**列表，不含值）：`event_id_only` 下必须为空，
`push_e2e.py` 对此有断言（§6/§8）。

## 启用真实 APNs（.p8 放置）

前提：Apple Developer 账号下创建过 APNs Auth Key（.p8）。**当前项目没有账号，
以下步骤属于「拿到账号后」的操作指引；在拿到之前不要把任何占位值改成看似真实的值。**

1. 把 .p8 文件放到 `server/compose/secrets/apns.p8`。目录由 `init.sh` 自动创建
   （`0700`，Linux 上首次 `up` 不会再让 Docker 建出更宽的 root 目录）：

   ```bash
   cp /path/to/AuthKey_XXXXXXXXXX.p8 server/compose/secrets/apns.p8
   chmod 600 server/compose/secrets/apns.p8   # 或 400；644 等会被 init.sh 拒绝
   ```

   `.gitignore` 已覆盖 `secrets/` 与 `*.p8`，CI 另有守卫（`server/scripts/
   check_no_secrets.sh`：任何被跟踪的 `*.p8` 或私钥 PEM 头直接红）；**任何情况下
   不要把 .p8 或其内容提交进仓库、贴进 issue/日志**。compose 把 **单个文件**
   `./secrets/apns.p8` 只读挂载到容器 `/secrets/apns.p8`（`create_host_path:
   false`，不挂整个目录；init.sh 会在没有真实 key 时维护一个 0600 的**空占位
   文件**，保证不启用 APNs 的部署 `up` 也不受影响——空文件永远不会被当成真 key）。
   init.sh 对该文件只用 `stat` 检查（存在、普通文件、mode 600/400、非空），
   **绝不读取/回显其内容**。
2. 编辑 `server/compose/.env`：

   ```bash
   BOTVILLE_APNS_ENABLED=true
   BOTVILLE_APNS_BUNDLE_ID=<你的 iOS Bundle ID>   # 反向域名，如 org.botville.app.ios
   APNS_KEY_ID=<10 位大写字母数字 Key ID>
   APNS_TEAM_ID=<10 位大写字母数字 Team ID>
   APNS_PLATFORM=sandbox                           # 或 production，见 §4
   # 可留占位（推荐），由 init.sh 从 Bundle ID 推导并写回 .env：
   #   APNS_TOPIC=<你的 Bundle ID>
   #   APNS_APP_ID=<你的 Bundle ID>.apns           # 必须恰好是 <bundle-id>.apns
   ```
3. `make up PUSH=1`（会先跑 init.sh），校验分两类：
   - **缺项/占位**（bundle/Key ID/Team ID/platform 为空或 `<...>`、没有真实
     .p8）：打印 WARNING 并**跳过 APNs app**（mock pushkin 不受影响）。这是有意
     设计：占位值渲染出去只会让 sygnal 起不来，还会连累 mock 链路。
   - **已填但非法**（Key/Team ID 不是 10 位大写字母数字、bundle/topic 不是反向
     域名、`APNS_APP_ID` 不等于 `<bundle-id>.apns`、platform 不是
     sandbox/production、.p8 权限不是 600/400 或不是普通文件）：**init.sh 直接
     exit 1**，报错只写字段名和规则、不回显值。成功日志同理不含任何 APNs 值。
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
- 方案 A（issue #4，已确认）：**单个 APNs app**，切环境只改 `.env` 里
  `APNS_PLATFORM=production`（或改回 `sandbox`）后 `make up PUSH=1`——它会
  重跑 init.sh 重新渲染 sygnal.yaml，并 **force-recreate 仅 sygnal**（不动
  homeserver/postgres），新 platform 随即生效；不做双 app。完整切换/回滚
  步骤与原因见 §12.8/§12.9。

## App 端需要的参数

App（iOS 客户端）向 homeserver 注册 pusher 时需要的四个关键值：

| 参数 | 值 | 说明 |
|---|---|---|
| `app_id` | `org.botville.mock`（联调）或 `<APNS_APP_ID>`（真实） | 必须与 sygnal.yaml `apps:` 下的键一致；不一致时 sygnal 按规范把 pushkey 放进 `rejected`，homeserver 会丢弃该 pusher |
| `pushkey` | APNs device token 的标准 Base64 字符串（保留 `=` 填充） | 系统注册远程通知回调里拿到的 `deviceToken` 用 `Data.base64EncodedString()` 编码；一个设备一个 token，可能失效（App 重装/系统重置），失效后由 `rejected` 机制回收 |
| `data.url` | `http://sygnal:5000/_matrix/push/v1/notify` | homeserver 回调网关的地址，唯一要求是**homeserver 容器可达**（App 自身不访问它）。本仓库默认（`BOTVILLE_PUSH_PUBLIC=false`，#128 决策）用 compose 内网地址 `http://sygnal:5000/_matrix/push/v1/notify`（即 CI 里的值，放行面见 §10）；仅当 `BOTVILLE_PUSH_PUBLIC=true` 开了公网反代才用 `https://<push-domain>/_matrix/push/v1/notify` |
| `data.format` | `event_id_only`（**硬前置**） | 本仓库推送链路的隐私前提，不是可选建议：注册 pusher **必须**带 `data.format=event_id_only`，否则通知正文会经网关明文流转（§8）。`PushPusherData`（`app-apple/Sources/BotvilleCore/PushRegistrar.swift`）已包含 `format` 字段，类型为 `PushPusherDataFormat`，默认 `.eventIDOnly`（编码为 `event_id_only`）；DEBUG iOS App 已支持实际注册 pusher，入口与限制见 `app-apple/docs/push-device.md` |

注册示例（标准 `POST /_matrix/client/v3/pushers/set`，即
`server/tests/push_e2e.py` 里的做法）：

```json
{
  "app_id": "<bundle-id>.apns",
  "pushkey": "<APNs device token standard Base64 with = padding>",
  "kind": "http",
  "app_display_name": "Botville",
  "device_display_name": "iPhone",
  "lang": "zh-Hans",
  "data": {
    "url": "http://sygnal:5000/_matrix/push/v1/notify",
    "format": "event_id_only",
    "default_payload": {
      "aps": {
        "mutable-content": 1,
        "alert": { "body": "…" }
      }
    }
  }
}
```

（真实模式的 `app_id` 即 `<bundle-id>.apns`——init.sh 推导的 `APNS_APP_ID`，
单 app 约定见 §2；联调用 `org.botville.mock`。`data.url` 上表两种取值按部署
模式选。）`default_payload` 只用于 APNs 的 NSE 唤醒和内容为空时的占位提示；它不改变
`event_id_only`，也不应带 Matrix 正文。iOS PR1 的 NSE 原样返回 `…`；PR2 才会用
`event_id`/`room_id` 和共享凭据拉正文，并把加密房间显示为「加密消息」。部署真实设备前
须再次执行 `pushers/set`，让 homeserver 保存新 payload，详情见
`app-apple/docs/push-device.md`。

反向代理是**可选**的（#128 决策，`BOTVILLE_PUSH_PUBLIC` 默认 `false`）：默认
不开公网 push 域名，homeserver 走 compose 内网 `http://sygnal:5000`。仅当
`BOTVILLE_PUSH_PUBLIC=true` 时才把 `BOTVILLE_PUSH_DOMAIN`（如
`push.example.invalid`，示例占位域名）TLS 终止后转发到
`127.0.0.1:${BOTVILLE_PUSH_PORT}`（默认 8019）。App 到 APNs 的长连接与本仓库
无关；这里只需要保证 homeserver → `data.url` 可达。配置示例见 §12（Caddy/
nginx，均标注「仅当开关打开时」）。

## 时延验收：mock 链路（已自动化）

`server/tests/push_e2e.py`（仅标准库）在 `make push-e2e` 里的完整流程：

1. 等 homeserver 就绪；同时用「未知 app_id 直接 POST sygnal
   `/_matrix/push/v1/notify`，断言 200 且 `rejected` 含该 pushkey」验证网关
   在线与拒绝语义（sygnal 的 compose healthcheck 只证明 `/health` 存活——
   镜像无 curl，healthcheck 用镜像内 python 探测——这一步才是 notify 路径的
   功能级就绪探测）；
2. 注册 alice/bob（通用 UIA）；alice 建私聊邀请 bob，bob join；
3. bob 设置 `app_id=org.botville.mock` 的 http pusher
   （`data.url=http://sygnal:5000/...`，`format=event_id_only`）；
4. alice 发消息，记录发送时刻（墙钟 ms；容器与宿主共享时钟）；
5. 轮询 `docker compose ... exec -T sygnal cat /data/mock-pushes.jsonl`
   （`--pushes-file` 可直接读宿主路径 `server/compose/generated/
   sygnal-data/mock-pushes.jsonl`），直到出现该 `event_id`，断言记录的
   `has_content` 为空（event_id_only 下通知不得带正文键，§8），再计算
   `received_at_ms - 发送时刻`，断言 `0 <= 时延 < --max-latency`（默认 5s，
   Makefile 里 `E2E_MAX_PUSH_LATENCY` 可调；负值视为时钟偏差直接失败）并打印。

CI（`.github/workflows/ci.yml`）在 **synapse、tuwunel、continuwuity** 三台
homeserver 上各跑一遍（matrix job `push-e2e`；continuwuity 的 `make up` 会自动
执行首启 bootstrap），失败自动 dump 全部容器日志并 `down -v` 清场。
本地：

```bash
make up HS=synapse PUSH=1 && make push-e2e
make up HS=tuwunel PUSH=1 && make push-e2e
make up HS=continuwuity PUSH=1 && make push-e2e
```

注意：该验收覆盖 **homeserver → sygnal**（含 homeserver 内部 pusher 调度）；
`sygnal → APNs → 真机` 不在内（mock pushkin 在 ③ 的位置截断）。

## 时延验收：真实 APNs 端到端（手工，待账号）

前置：Apple 开发者账号、按 §3 放好 .p8 并启用、真机装了带推送 entitlement
的 debug 构建、`APNS_PLATFORM` 与构建类型匹配。

**App 端现状**：DEBUG iOS App 已支持 APNs 注册、Matrix HTTP pusher 注册及
推送诊断，操作步骤与限制见 `app-apple/docs/push-device.md`。
`PushPusherData`（`app-apple/Sources/BotvilleCore/PushRegistrar.swift`）已包含
`format` 字段，默认编码为 `event_id_only`。也可用脚本/其他客户端按 §5
的示例注册（务必带 `format=event_id_only`）；真实送达与时延仍待真机验证。

1. `make up PUSH=1`，确认 `grep 'type: apns' server/compose/generated/sygnal/sygnal.yaml`
   有输出，`docker compose ... --profile push ps` 里 sygnal 为 running；
2. 真机 App 登录任一账号（bob），允许通知，注册 pusher（`app_id=<APNS_APP_ID>`，
   `pushkey=deviceToken.base64EncodedString()`（标准 Base64，保留 `=` 填充），
   `data.url` 按 §5 表取值（默认内网
   `http://sygnal:5000/...`，公网模式用 `https://<push-domain>/...`），
   `format=event_id_only`）；
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

- `data.format=event_id_only` 是本仓库推送链路的**硬前置**，不是可选建议：
  App 注册 pusher 时必须带（§5 表）。它不是网关侧的强制项，而是 homeserver
  按每个 pusher 的注册数据决定——漏带的 pusher 默认就是全量正文推送。
- 带 `event_id_only` 时：homeserver 发给网关的通知**只含 event_id/room_id/
  计数等元数据，不含消息内容**；sygnal 转给 APNs 的 payload 同样不含明文
  内容，**消息正文不会离开自己的部署**、不会发给 Apple。App 收到推送后自行
  通过 Matrix CS API 拉取事件正文解密/展示（端到端加密房间中正文本来就只有
  客户端能解）。
- 自动化兜底：mock pushkin 把通知 content 的**键名**记进 `has_content`，
  `push_e2e.py` 断言其为空——homeserver 或注册侧退回全量正文时 CI 会红，
  而不是静默泄内容。
- 不用 `event_id_only` 时（不允许出现在本仓库的注册里）：通知正文会经网关
  明文流转（APNs 通道本身是 TLS 的，但内容会出你的部署边界），且 sygnal
  日志可能携带更多字段。
- mock pushkin 的 JSONL 只含 event_id/room_id/pushkey/时间戳等元数据外加
  `has_content` 键名列表（不含值）；该目录（`generated/sygnal-data/`）已
  gitignore，属临时测试数据，可随时删除。

## 运维与排障

- **sygnal 不启动/反复重启**：`docker compose ... --profile push logs sygnal`；
  最常见是渲染出的配置带占位值（本仓库的 init.sh 已拦截）或 YAML 缩进被改坏。
- **pusher 被静默删除**：sygnal 对无效 pushkey 返回 `rejected`，homeserver 会
  删除对应 pusher；App 端需要在 token 变化时重新 `pushers/set`。
- **升级 sygnal 镜像**：与其他服务同一套 pin 流程，
  见 [docs/ops/upgrade-images.md](../ops/upgrade-images.md)（同一 pin 需同步
  **4 处**：compose 默认值、`.env.example`、CI allowlist、ops pin 表，口径与
  upgrade-images.md §3 一致）。
- **mock JSONL 增长**：只在跑 push-e2e 时增长，量级极小；要清理直接
  `rm server/compose/generated/sygnal-data/mock-pushes.jsonl`。生产可把
  `BOTVILLE_PUSH_MOCK=false`，init.sh 就不再渲染该 app。
- 相关文件索引：`server/compose/sygnal/`（sygnal.yaml.tmpl、mock-app.yaml.part、
  apns-app.yaml.part、botville_mock_pushkin.py）、
  `server/scripts/render_push_denylist.py`（§10 的 denylist 渲染）、
  `server/scripts/check_no_secrets.sh`（§3 的密钥入库守卫，CI lint 跑）、
  `server/tests/push_e2e.py`、`server/tests/test_push_sygnal.py`、
  `server/tests/test_push_denylist.py`、`server/tests/test_check_no_secrets.py`、
  Makefile（`PUSH=1` / `push-e2e`）、CI job `push-e2e` / lint。

## 内网推送放行（BOTVILLE_DOCKER_SUBNET 与 BOTVILLE_SYGNAL_IP）

三家 homeserver 默认拒绝向私有地址发出 HTTP 请求（SSRF 防护），而 Sygnal 在
同一 compose 网络（私有网段）里。本仓库的做法：compose 网络固定为
`BOTVILLE_DOCKER_SUBNET`（默认 `172.31.250.0/24`），sygnal 容器通过
`ipv4_address` 固定拿到 `BOTVILLE_SYGNAL_IP`（默认网段的 `.2`），**放行面只有
这一个 /32**——不是整个网段（`.1` 网关与同网段里的 postgres 等其他容器都保持
不可达）：

- Synapse：init.sh 把 `ip_range_whitelist`（只含 `BOTVILLE_SYGNAL_IP/32`）追加进
  `homeserver.yaml`。注意作用域：`ip_range_whitelist` 对 Synapse **所有走黑名单
  的出站客户端**生效（推送、URL 预览、远程媒体、联邦），不止推送——这正是只放
  /32、不放网段的原因。
- Tuwunel / continuwuity：init.sh 经 `server/scripts/render_push_denylist.py` 生成
  `generated/push-denylist.env`，`*_IP_RANGE_DENYLIST` = **完整上游默认私网黑名单
  （tuwunel v1.9.4，含 `0.0.0.0/8` 与 `::/128`）仅挖去该 /32**；清单由
  `server/tests/test_push_denylist.py` 锁定（含「其他私网地址仍被拒」断言）。

配置校验（`init.sh` 渲染时执行，非法值友好报错退出、无 traceback）：
`BOTVILLE_DOCKER_SUBNET` 必须是 RFC1918 IPv4、前缀不短于 `/24`、无主机位；
`BOTVILLE_DOCKER_IP_RANGE`（compose 的动态地址池 `ipam.ip_range`，默认
`172.31.250.64/26`）必须在子网内——Docker 的动态分配从网段 `.2` 顺序起，池子
把动态地址限制在 `.64–.127`，sygnal 的固定 `.2` 才不会被先启动的容器抢走；
`BOTVILLE_SYGNAL_IP` 必须在子网内、在动态池外，且不是网络/网关(`.1`)/广播地址。

同机多实例时每个实例要用**不同的网段**（如 `botville` 172.31.250.0/24、
`botville-zcode` 172.31.251.0/24、`botville-codex` 172.31.252.0/24），
`BOTVILLE_DOCKER_IP_RANGE` 与 `BOTVILLE_SYGNAL_IP` 相应取各自网段的
`.64/26` 与 `.2`，否则 compose 建网络时会冲突（init.sh 会指出该改哪个值）。

生产上若选择了公网网关模式（`BOTVILLE_PUSH_PUBLIC=true`，见 §2/§12.3——**非默认**，
#128 决策默认内网直连）：把 `BOTVILLE_SYGNAL_IP` **留空**即可
关闭内网放行——denylist 用完整上游默认、Synapse 不写 `ip_range_whitelist`，
pusher 的 `data.url` 用公网地址。（留空时 compose 仍按其 `ipv4_address` 的回退
默认 `.250.2` 赋地址，故仅默认网段下可直接起 push profile；自定义网段又留空的
组合，init.sh 会 WARNING 提示。）

## 主机有代理时（NO_PROXY 固化，issue #134）

Docker 客户端（`~/.docker/config.json` 的 `proxies` 段）与 Docker Desktop 会把主机的
`HTTP(S)_PROXY` 注入**每个新建容器**。容器里的 `127.0.0.1` 是容器自己，主机上
`http://127.0.0.1:7890` 这类本地代理在容器内并不存在——于是遵循 `*_PROXY` 的出站
HTTP（典型：Synapse pusher → `http://sygnal:5000`）被送进一个不可达的代理，推送
静默失败。

仓库的处理（`server/compose/docker-compose.yml` 顶部的 `x-noproxy-env` 锚点，合并进
synapse / tuwunel / continuwuity / sygnal / postgres 五个服务）：

- 固定默认 `NO_PROXY` **和**小写 `no_proxy`（curl / Python / Go 各认不同大小写）：
  全部 compose 服务名 + 预留名（`gateway`/`admin`）+ 联邦栈服务名
  （`synapse-a/b`、`pairing-a/b`、`fedgate-a/b`）+ `localhost,127.0.0.1,::1` +
  `.local,.internal` + RFC1918 网段。**默认名单不可覆盖**；`server/compose/.env` 里
  `BOTVILLE_NO_PROXY_EXTRA` 只做**追加**（留空 = 只有默认项，且不会出现尾部逗号）。
- Synapse 的 Twisted 匹配 `NO_PROXY` 只认主机名/后缀、**不认 CIDR 网段**，所以服务
  名必须逐个列出，不能只靠网段兜底（Python/requests/Go 认 CIDR，Twisted 不认）。
- **不清空也不设置 `HTTP_PROXY`/`HTTPS_PROXY`**：有部署故意让 homeserver/sygnal
  经代理出外网（联邦、APNs）。容器确需走代理时，地址必须换成容器可达的
  `http://host.docker.internal:<port>`（改 `~/.docker/config.json` 的 proxies 或
  Docker Desktop 的代理设置；`127.0.0.1`/`localhost` 在容器内永远指容器自己）。
- `server/scripts/init.sh` 启动时若发现主机代理环境变量（`HTTP_PROXY`/
  `HTTPS_PROXY` 及小写）或 `~/.docker/config.json` 的代理指向 `127.0.0.1`/
  `localhost`，只打 WARNING（仅回显 host:port，凭据一律剥离），**不会**因此失败。

主机有代理时的自检 4 步（宿主机上执行；`-p botville` 与 `--profile synapse` 按实际
部署替换）：

```bash
# 1) 主机代理从哪来：docker info 的代理行 + ~/.docker/config.json 的 proxies 段。
#    只看 proxies 段——config.json 里可能还有 auths 等敏感段，不要整文件 cat。
docker info | grep -i proxy
python3 -c 'import json, os; print(json.load(open(os.path.expanduser("~/.docker/config.json"))).get("proxies", "<no proxies section>"))'

# 2) 容器内 NO_PROXY 生效：大小写两个变量都在，且包含 sygnal 与各服务名。
#    若同时看到 HTTP_PROXY=...127.0.0.1...，说明 docker 注入了环回代理——
#    栈内流量靠 NO_PROXY 兜底，容器出外网仍需把代理换成 host.docker.internal。
docker compose -f server/compose/docker-compose.yml --env-file server/compose/.env \
    -p botville --profile synapse exec -T synapse env | grep -i proxy

# 3) 容器内到 sygnal 真连通（NO_PROXY 生效时直连 compose 内网 DNS，不经代理）
docker compose -f server/compose/docker-compose.yml --env-file server/compose/.env \
    -p botville --profile synapse exec -T synapse python -c \
    "import urllib.request; print(urllib.request.urlopen('http://sygnal:5000/health').status)"
#    期望 200

# 4) Synapse 日志没有代理连接错误（ProxyConnectionError / 连 7890 被拒之类）
docker compose -f server/compose/docker-compose.yml --env-file server/compose/.env \
    -p botville --profile synapse logs --no-color synapse 2>&1 \
    | grep -iE 'proxy.*(error|refused|failed)|7890' || echo clean
```

（tuwunel / continuwuity 栈把 `--profile synapse` 换成对应 profile 即可，它们同样
吃 `x-noproxy-env`。）

QQ/NapCat 不走境外代理的原则不受本方案影响：NapCat 不在 botville 的 compose 内，
本节只约束本栈容器；若将来 NapCat 容器化，需确保它不继承主机的 7890 代理（在其
`NO_PROXY` 加 QQ 相关域名，或不注入 `HTTP_PROXY`）。

---

## 8845 mini PC（Ubuntu Server 24.04）部署

Botville 的参考生产宿主是一台 8845 mini PC（Ubuntu Server 24.04）。本节是该机器上
启用真实 APNs 推送的完整 runbook（通用原理见前文各节；尚未在真机执行过，执行后回填）。

### 1. 首次初始化，然后放置 APNs 密钥（.p8）

顺序很重要：**先初始化、后放密钥**。全新 checkout 上 `server/compose/.env`、
`server/compose/secrets/` 都还不存在，先跑一次 `make up`（或 `make init`）让
init.sh 生成 `.env`（`<change-me>` 自动随机化）、创建 `secrets/`（0700）与其中
0600 的**空占位** `apns.p8`（compose 的单文件挂载依赖它存在；空文件永远不会被
当成真 key），**然后**再放入真实密钥：

```bash
cd /path/to/botville
make up                                   # 首次初始化（不带 PUSH 也可以）
cp /path/to/AuthKey_XXXXXXXXXX.p8 server/compose/secrets/apns.p8
chmod 600 server/compose/secrets/apns.p8  # 或 400；644 等会被 init.sh exit 1 拒绝
```

（目录已是 0700；若手动重建，`chmod 700 server/compose/secrets`。）

init.sh 只用 `stat` 检查该文件（普通文件、mode 600/400、非空），**绝不读取或回显
内容**。`.gitignore` 忽略 `*.p8` 与 `secrets/`，CI 另有
`server/scripts/check_no_secrets.sh` 守卫（任何被跟踪的 .p8 / 私钥 PEM 头直接红）。

### 2. 填写 server/compose/.env（占位说明）

`.env` 已在步骤 1 生成。APNs 相关键按 §3 填：

```bash
BOTVILLE_APNS_ENABLED=true
BOTVILLE_APNS_BUNDLE_ID=<你的 iOS Bundle ID>     # 反向域名；唯一必须手填的 APNs 标识
APNS_KEY_ID=<10 位大写字母数字>                   # Apple 后台 Keys 页
APNS_TEAM_ID=<10 位大写字母数字>                  # Apple 开发者 Team ID
APNS_PLATFORM=sandbox                            # 开发构建先 sandbox，见下
# push 域名默认不对公网开放（#128 决策）：homeserver 走 compose 内网
# http://sygnal:5000，以下两项保持默认即可；确需公网网关才改
# BOTVILLE_PUSH_PUBLIC=true 并填真实域名（见「3. 反向代理」）。
BOTVILLE_PUSH_PUBLIC=false
BOTVILLE_PUSH_DOMAIN=<push-domain>               # 仅 BOTVILLE_PUSH_PUBLIC=true 时需要
# APNS_TOPIC / APNS_APP_ID 留占位即可：init.sh 推导为 <bundle-id> / <bundle-id>.apns
# 并写回 .env；显式填写的值必须符合该规则，否则 exit 1。
```

注意 `.env` 里**只应有占位符和你的真实部署值**；任何示例文件（.env.example）都不
允许出现真实 Key ID / Team ID / Bundle ID。

### 3. 反向代理 push.<domain>（可选：仅当 BOTVILLE_PUSH_PUBLIC=true）

**默认不开公网**（#128 决策，`BOTVILLE_PUSH_PUBLIC=false`）：`push.<domain>` 不对
外暴露，homeserver → sygnal 走 compose 内网 `http://sygnal:5000`（sygnal 容器的
固定 IP `BOTVILLE_SYGNAL_IP` 是部署唯一放行的私网 /32，见 §10）；App 注册 pusher
的 `data.url` 也用该内网地址（只需要 homeserver 容器可达，App 自身不访问它）。

仅当 homeserver 无法经 compose 内网到达 sygnal（如两者不同机）时，才把 `.env` 里
`BOTVILLE_PUSH_PUBLIC=true`，并按下例反代（sygnal 的宿主端口本就只绑
`127.0.0.1`，默认 `BOTVILLE_PUSH_PORT=8019`，由反代对外提供
`https://push.<domain>`；此时 App 的 `data.url` 用
`https://<BOTVILLE_PUSH_DOMAIN>/_matrix/push/v1/notify`）。sygnal 无 WebSocket
需求，普通 HTTP 反代即可，TLS 由反代终止。

Caddy（自动 HTTPS，推荐；仅当开关打开时）：

```caddyfile
push.example.invalid {
    reverse_proxy 127.0.0.1:8019
}
```

nginx（证书自备，`certbot` 或手动放置；仅当开关打开时）：

```nginx
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name push.example.invalid;
    ssl_certificate     /etc/letsencrypt/live/push.example.invalid/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/push.example.invalid/privkey.pem;
    location / {
        proxy_pass http://127.0.0.1:8019;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

（两例中的 `8019` 即 `${BOTVILLE_PUSH_PORT}` 默认值，改过端口的话同步。）

### 4. 出站网络（到 Apple）

放行出站 TCP 443 到 `api.push.apple.com` 与 `api.sandbox.push.apple.com`（DNS 解析
多个 IP，按域名放行而非按 IP）。ufw 示例（默认 deny outgoing 的部署才需要显式
放行；默认 allow 的无需配置）：

```bash
sudo ufw allow out 443/tcp
```

### 5. NTP 时间同步（JWT 依赖时钟）

sygnal 用 .p8 对每个 APNs 请求签发短期 JWT（provider token）；时钟偏差过大（约
>50s）会被 Apple 以 `JWTExpired` / 403 拒绝。确认时间同步已开启：

```bash
timedatectl          # 期望 "NTP service: active" 且 "synchronized: yes"
# 未开启时：
sudo timedatectl set-ntp true
```

### 6. 启动

```bash
make up PUSH=1     # 先跑 init.sh（校验 APNs 配置）再起 synapse + postgres + sygnal；
                   # PUSH=1 会 force-recreate 仅 sygnal（--no-deps），网关加载的
                   # 一定是刚渲染的 sygnal.yaml（PR #127 B1）
```

init.sh 校验语义：缺项/占位 → WARNING 跳过 APNs app（mock 照常）；已填但非法
（ID 格式、bundle 反向域名、`APNS_APP_ID != <bundle-id>.apns`、platform、.p8
权限）→ **exit 1**，`make up` 直接失败，改好 `.env` 重跑。

### 7. 验证

```bash
# a) 渲染与启动：APNs app 在配置里、sygnal 健康
grep -A6 'type: apns' server/compose/generated/sygnal/sygnal.yaml
docker compose -f server/compose/docker-compose.yml --env-file server/compose/.env \
    -p botville --profile push ps     # sygnal 应为 running/healthy

# b) notify 路径功能探测（未知 app_id 必须 200 且 rejected 含 pushkey）
BOTVILLE_PUSH_PORT="$(sed -n 's/^BOTVILLE_PUSH_PORT=//p' server/compose/.env | tail -n 1)"
curl -s -X POST "http://127.0.0.1:${BOTVILLE_PUSH_PORT}/_matrix/push/v1/notify" \
  -H 'Content-Type: application/json' \
  -d '{"notification":{"event_id":"$probe","room_id":"!r:x","counts":{"unread":1},"devices":[{"app_id":"org.botville.unknown","pushkey":"probe","data":{},"tweaks":{}}]}}'
# 期望 {"rejected": ["probe"]}

# c) 时延验收（mock 链路，自动化）
make push-e2e

# d) 日志（最后看：前台跟随输出，Ctrl-C 退出，别放在上面的命令之前）
make logs PUSH=1
```

**真机验收**（sygnal → APNs → 真机横幅 < 5s）可通过 DEBUG iOS App 的推送诊断
注册 pusher（见 `app-apple/docs/push-device.md`），也可按 §7 手工注册
（务必使用标准 Base64 APNs token，保留 `=` 填充，并设置
`data.format=event_id_only`、`app_id=<bundle-id>.apns`、
`data.default_payload={"aps":{"mutable-content":1,"alert":{"body":"…"}}}`，
`data.url` 按 §5 表取值——默认内网 `http://sygnal:5000/_matrix/push/v1/notify`，
公网模式 `https://<BOTVILLE_PUSH_DOMAIN>/_matrix/push/v1/notify`）掐表验收并回填。

### 8. sandbox ↔ production 切换（方案 A：单 app，改环境重启）

```bash
sed -i 's/^APNS_PLATFORM=.*/APNS_PLATFORM=production/' server/compose/.env
make up PUSH=1     # init.sh 重渲染 sygnal.yaml + force-recreate 仅 sygnal；切回 sandbox 同理
```

**为什么需要重启**：`APNS_PLATFORM` 只被 init.sh 用来渲染
`generated/sygnal/sygnal.yaml`（bind mount 进容器），不参与 compose 插值——改值
不会改变服务配置哈希，`up -d` 不会重建容器，而 sygnal 只在进程启动时读一次配置
（无热加载）。所以 `make up PUSH=1` 在 `up -d` 之后会 force-recreate **仅
sygnal**（`--no-deps`，synapse/postgres 原容器不动），运行中的网关随即用上新
platform。不重启的话磁盘 yaml 已更新、运行中的 sygnal 仍是旧环境（PR #127 B1）。

不需要动 .p8 / Key ID / Team ID / Bundle ID；两个环境互不相通（§4），切完用对
应构建类型的真机验证。

### 9. 回滚（关闭真实 APNs）

```bash
sed -i 's/^BOTVILLE_APNS_ENABLED=.*/BOTVILLE_APNS_ENABLED=false/' server/compose/.env
make up PUSH=1     # init.sh 重渲染（APNs app 移除，只剩 mock，若 BOTVILLE_PUSH_MOCK=true）
                   # + force-recreate 仅 sygnal
```

与 §8 同理：`BOTVILLE_APNS_ENABLED=false` 也不参与 compose 插值，仅靠 `up -d`
不会让运行中的 sygnal 丢掉已加载的 APNs app——`make up PUSH=1` 的 force-recreate
才用无 APNs app 的新配置重建容器。

回滚后**不要只看磁盘 yaml**，要确认**运行中的 sygnal** 已无 APNs app——用曾经
启用过的 app_id（`<bundle-id>.apns`）打 notify，应被 `rejected`：

```bash
BOTVILLE_PUSH_PORT="$(sed -n 's/^BOTVILLE_PUSH_PORT=//p' server/compose/.env | tail -n 1)"
curl -s -X POST "http://127.0.0.1:${BOTVILLE_PUSH_PORT}/_matrix/push/v1/notify" \
  -H 'Content-Type: application/json' \
  -d '{"notification":{"event_id":"$probe","room_id":"!r:x","counts":{"unread":1},"devices":[{"app_id":"<bundle-id>.apns","pushkey":"probe","data":{},"tweaks":{}}]}}'
# 期望 {"rejected": ["probe"]}：运行中的 sygnal 已不认识该 app（回滚前它不会被拒）。
# 也可以进容器看运行进程实际加载的配置（bind mount，即 init.sh 刚渲染的版本）：
#   docker compose -f server/compose/docker-compose.yml --env-file server/compose/.env \
#       -p botville --profile push exec -T sygnal cat /config/sygnal.yaml
```

homeserver 侧已注册的真实 pusher 会因 sygnal 不再认识该 app_id 走 `rejected`
机制被回收。不需要删 `.p8`（留在 `secrets/` 里无妨；删除的话 init.sh 会维护空
占位文件以满足 compose 的单文件挂载，空文件永远不会被当成真 key）。
