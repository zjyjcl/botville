# server/ — 服务端与部署（A 线，ZCode）

- Matrix 服务器部署：Synapse + Postgres，或 Tuwunel / continuwuity；Docker Compose profile 按模块启用
- 只依赖标准 Client-Server、Application Service、Push Gateway 接口；CI 双服务器测试
- Sygnal 推送网关 + APNs；appservice 注册 AI 账号命名空间
- 仅配对联邦与扫码配对后端（可单方面切断；入站闸门本地即刻生效，出站停止依赖通知对端）
- 动态存储与可见范围、AI 关系存储与多 AI 群
- 小镇状态服务（位置与状态同步、降频/关闭）、素材存储
- 安装向导、升级与导出脚本

## 快速开始（A0.2）

前置：Docker（含 compose v2）、`openssl`、`python3`、`make`。

```bash
make up              # core 一键启动：Synapse + Postgres，http://127.0.0.1:8008
make up HS=tuwunel   # 换 Tuwunel（continuwuity 同理）
make up PUSH=1       # 叠加 sygnal 推送网关（push profile，A1.2；可与任一 HS 组合）
make up GATEWAY=1    # 叠加 botville 网关（gateway profile，A2.2；本机构建 gateway/Dockerfile，
                     # 挂 gateway-data 卷，http://127.0.0.1:8020；可与任一 HS 组合。
                     # synapse/tuwunel 用 appservice 传输、注册 url=http://gateway:8080 收 AS 事务
                     # （url 变了会重启 HS）；continuwuity 仍用 memory、url=null）
make gateway-hs-smoke # 对真实 HS 冒烟：whoami + OpenID userinfo + AS 事务 ping/落库/重启幂等
                     # （需 GATEWAY=1 的 synapse/tuwunel 栈；网关只能跑一个，只用 make up 重建；
                     # 测试用户/房间跨次复用，记在 generated/gateway-hs-smoke.json）
make config          # 只校验、不写任何文件：网关预检 + compose config --quiet（不跑 init，不打印插值后的值；没有 .env 时报错）
                     # 注意：自己手敲 docker compose config 时一定带 --quiet，不带会把 .env 里的密钥全部展开打印
make smoke           # 标准库冒烟测试：注册→登录→建房(v10)→发消息→回读→sync
make backup          # 备份到 ./backups/<UTC时间戳>/（目录 700、文件 600，A1.1）
make restore RESTORE_DIR=backups/<时间戳>   # 破坏性恢复（自动 --yes）
make backup-gateway  # 只备份 gateway-data 卷（任一 HS；卷不存在则跳过，A2.2）
make restore-gateway RESTORE_DIR=backups/<时间戳>   # 只恢复 gateway-data（无该产物则不碰卷）
make e2e             # 验收：注册→私聊→建群；E2E_PHASE=verify 复核备份恢复
make push-e2e        # 推送验收：HS→sygnal 时延 <5s（mock pushkin，需 PUSH=1 起栈）
make appservice-e2e  # 验收：网关以 @ai_* 身份代发（A1.3；synapse/tuwunel）
make federation-e2e  # 配对联邦验收（A2.1；独立 botville-fed 项目，双 Synapse + fedgate）
make federation-down # 只销毁 botville-fed 项目（down -v）
make logs            # 跟踪日志（与 make down 一样始终带 push/gateway profile，
                     # 不必再加 PUSH=1 / GATEWAY=1 才能看到已启动的服务）
make ps              # 容器状态（同上）
make down            # 停止（不传 -v，不删任何卷）。清卷：docker compose ... --profile <hs> --profile gateway down -v
                     # （带 gateway profile 才删 gateway-data）；单删：docker volume rm -f <项目名>_gateway-data
```

角色引擎（B 线 PR2）：把 `GATEWAY_MODEL_*` / `GATEWAY_PERSONAS_FILE` / `GATEWAY_CONSUMER_ENABLED`
写进 `server/compose/.env` 后 `make up GATEWAY=1`；人设放 `server/compose/gateway-personas/`
（只读挂载，真实文件不进仓库；自定义目录用 `BOTVILLE_GATEWAY_PERSONAS_DIR`，不存在时
`make up GATEWAY=1` / `make config` 预检就报错）。详见
[docs/deploy/appservice.md](../docs/deploy/appservice.md#角色引擎模型与人设)。
群聊回复上限 `GATEWAY_GROUP_MAX_RESPONDERS`（B 线 B2.8a，默认 `3`，整数 1–20，私聊不受影响）
同样原样透传，init.sh 不生成；超出范围网关拒绝启动。
群邀请（B 线 B2.8b）：`GATEWAY_GROUPS_ENABLED`（默认 `false`，严格布尔）、
`GATEWAY_GROUP_MAX_ROOMS_PER_PERSONA`（20，1–200）、`GATEWAY_INVITE_LEDGER_TTL_SECONDS`
（86400，60–2592000）原样透传，init.sh 不生成。⚠️ **开启群功能必须有持久的 HMAC 密钥**：
compose 部署里就是 `.env` 的 `BOTVILLE_GATEWAY_HMAC_SECRET`，**不能清空，也不能重新生成；
轮换它会让已接受的群失效**（它是密钥，别贴出来）。还需记忆库和 persona 的 `groups.enabled`。
群节流（B 线 B2.8c）：`GATEWAY_GROUP_COOLDOWN_SECONDS`（30，0–3600）、`GATEWAY_GROUP_MAX_REPLIES_PER_HOUR`
（20，1–200）、`GATEWAY_GROUP_AI_LOOP_MAX_PER_HOUR`（30，1–1000）、`GATEWAY_GROUP_MAX_CONSECUTIVE_AI_MESSAGES`
（3，1–50）原样透传；**节流计数重启后清零**。`GATEWAY_GROUP_FORGET_STALE_KEYS`（默认 `false`）是
**轮换密钥后的一次性清理开关**：打开后每次启动都会永久删除旧 key_id 的群台账（解除轮换隔离，
旧 left / encrypted_disabled 记录丢失），**重启一次后改回 `false`**。

记忆（B 线 PR3a）：`GATEWAY_MEMORY_DB_PATH` 默认空 = 关闭；启用时写
`/var/lib/botville-gateway/memory.db`（gateway-data 卷顶层，`make backup` /
`make restore` 自动覆盖）。网关以 0600 创建这个文件，里面有聊天内容，备份同样要私密。
`GATEWAY_MEMORY_RETENTION_DAYS`（默认 90）、`GATEWAY_MEMORY_PURGE_INTERVAL_SECONDS`
（默认 86400）同样透传。PR3b 的 `GATEWAY_MEMORY_SUMMARY_MODEL` / `GATEWAY_EMBEDDING_*` /
`GATEWAY_MEMORY_VECTOR_BACKEND` 也透传（embedding 默认关）。PR3d 的
`GATEWAY_MEMORY_OWNER_CAN_READ_DM`（默认 `false`，严格布尔）打开后**主人能看到 AI 的私聊记忆，
App 会向聊天对方显示披露**。**设了 `GATEWAY_EMBEDDING_MODEL`，私聊正文就会
发给 embedding 服务商**（地址留空 = 聊天模型同一家）；`GATEWAY_EMBEDDING_API_KEY` 是密钥，只放本地 `.env`。

情绪与主动发言（B 线 PR4b / PR4c）：`GATEWAY_MOOD_ENABLED`（默认 `false`）、
`GATEWAY_MOOD_HALF_LIFE_MINUTES`（240）、`GATEWAY_PROACTIVE_ENABLED`（`false`）、
`GATEWAY_PROACTIVE_RECENT_CONTACTS_ENABLED`（`false`）、`GATEWAY_PROACTIVE_TICK_SECONDS`（300）、
`GATEWAY_PROACTIVE_MIN_IDLE_MINUTES`（180）、`GATEWAY_PROACTIVE_MIN_INTERVAL_MINUTES`（240）、
`GATEWAY_PROACTIVE_PER_TARGET_PER_DAY`（1）原样透传，init.sh 不生成。**开启
`GATEWAY_PROACTIVE_ENABLED` 前必须先启用记忆库（`GATEWAY_MEMORY_DB_PATH`），并且要有持久的
`GATEWAY_HMAC_SECRET`**（compose 里即 `.env` 的 `BOTVILLE_GATEWAY_HMAC_SECRET`，init.sh 生成后
保留；它是密钥，别清空、别重新生成，也别贴出来）。情绪同样依赖记忆库。详见
[docs/deploy/appservice.md](../docs/deploy/appservice.md#角色引擎模型与人设)「情绪与主动发言」。


continuwuity 默认关闭上游 announcements 检查（避免 SIGTERM 卡到 SIGKILL）：见 [docs/ops/continuwuity-announcements.md](../docs/ops/continuwuity-announcements.md)。


说明：

- 首次 `make up` 会执行 `server/scripts/init.sh`：从 `.env.example` 生成本地
  `server/compose/.env`（所有 `<change-me>` 用 `openssl rand -hex 32` 就地随机生成，
  不会提交），并把 `synapse/homeserver.yaml.tmpl` 渲染到 `generated/synapse/`（gitignore）、
  把 `appservice/botville-gateway.yaml.tmpl` 渲染到 `generated/appservice/`
  （gitignore，**0600**——携带 as_token；A1.3，Synapse 经
  `app_service_config_files`、Tuwunel 经 `TUWUNEL_APPSERVICE_DIR` 读取，详见
  [docs/deploy/appservice.md](../docs/deploy/appservice.md)）。
  `.env` 已存在时保留原值；其中的 `*_IMAGE` 与当前默认不同（如 `:latest`、无 digest）
  只会打 WARNING，不会自动改写——迁移见
  [docs/ops/upgrade-images.md](../docs/ops/upgrade-images.md) §5。
- 三个 homeserver 容器内都监听 8008，宿主仅绑定 `127.0.0.1:${BOTVILLE_HTTP_PORT:-8008}`；
  需要外部访问时自行加反向代理。
- **compose 项目名**：make 与备份/恢复脚本都带 `-p ${COMPOSE_PROJECT_NAME}`（`.env`
  提供，默认 `botville`，卷名 `<project>_*`）。同机多实例必须每套用独立项目名 +
  独立 `BOTVILLE_HTTP_PORT`，否则 `down -v` 会误删彼此的卷；详见
  [docs/deploy/z423-synapse.md](../docs/deploy/z423-synapse.md) 的多实例一节。
- **备份介质须支持 Unix 权限**：备份目录按密钥库对待（目录 700/文件 600），目标
  盘须为 ext4/btrfs/ZFS/APFS 等保留权限位的文件系统，不要用 exFAT/FAT32 盘；详见
  [docs/deploy/z423-synapse.md](../docs/deploy/z423-synapse.md) 的备份与恢复一节。
- 注册策略：tuwunel / continuwuity 始终令牌门控（`BOTVILLE_REGISTRATION_TOKEN`）；
  Synapse 默认关闭注册，CI 中通过 `BOTVILLE_OPEN_REGISTRATION=true` 走 dummy 流程。
  本地想对 Synapse 跑 `make smoke` 时，把 `.env` 里该值改为 `true` 再 `make up`。
  唯一例外是 continuwuity 的首启窗口：首个账号创建之前配置令牌尚未生效（见下条），
  首个账号创建后注册回到「配置令牌门控」。
- continuwuity 首启引导：全新数据库上一次性引导令牌只打印在容器日志，配置的
  `BOTVILLE_REGISTRATION_TOKEN` 在首个账号创建前无效（401 `M_FORBIDDEN`）。
  **每次** `make up HS=continuwuity` 启动后都会自动执行
  `server/scripts/bootstrap-continuwuity.sh`（幂等，可重复执行）：
  - 从日志提取引导令牌，以标准 UIA（`m.login.registration_token`）注册 `.env` 里的
    `BOTVILLE_ADMIN_USER` 作为首个账号（continuwuity 惯例：首用户即管理员），此后配置令牌生效；
  - 管理员已存在（`M_USER_IN_USE`）→ 视为已完成，成功退出；
  - 一次性令牌被拒（`M_FORBIDDEN`）→ 判定库里已有别的首个用户（例如旧部署在升级前
    手动注册过管理员）：打印指引后**成功退出**，`make up` 不会失败。想让脚本识别现有
    管理员，把 `.env` 里 `BOTVILLE_ADMIN_USER` 改成该用户名即可；不改也无妨（服务器已可用）；
  - 日志里没有引导令牌 → 不静默当作已引导：用配置令牌查标准接口
    `GET /_matrix/client/v1/register/m.login.registration_token/validity`（spec v1.2），
    有效 → 已引导完成；无效 → 打印诊断并以非零退出（令牌行可能已被日志轮转冲掉）；
  - 密码占位符检查只在确实要注册时进行：已引导的服务器不会因
    `BOTVILLE_ADMIN_PASSWORD=<change-me>` 而失败。
  脚本变量（环境变量覆盖）：`BOTVILLE_BASE_URL`（默认
  `http://127.0.0.1:${BOTVILLE_HTTP_PORT}`；指向其他地址时覆盖，尾斜杠自动去除）、
  `BOTVILLE_BOOTSTRAP_TIMEOUT`（等待客户端 API 就绪的秒数，默认 180）、
  `COMPOSE`（docker compose 命令前缀）。密码与令牌经环境变量传给 python，不进 argv、不回显。
- 重置（仅手动，⚠️ 不可恢复：会删除该实例全部账号与消息；生产/NAS 上执行前务必先备份并二次确认，勿在已有数据的部署上执行）：bootstrap 脚本不会删除或重置任何数据。确需丢弃 continuwuity 全部数据
  从零开始时，手动执行（`make down` 不传 `-v`，不会删任何卷）：
  `docker compose -f server/compose/docker-compose.yml --env-file server/compose/.env --profile continuwuity --profile gateway down -v && make up HS=continuwuity`。
  带 `--profile gateway` 的 `down -v` 就会删掉 `gateway-data`（与网关容器是否正在运行无关）；
  未启用该 profile 或需单独删除时用 `docker volume rm -f <项目名>_gateway-data`
  （项目名取 `.env` 的 `COMPOSE_PROJECT_NAME`，默认 `botville`）。要用网关再加
  `make up HS=continuwuity GATEWAY=1`。
  带 `BOTVILLE_BOOTSTRAP_RESET_CONFIRM=yes` 运行 bootstrap 脚本可以让它打印这组命令
  （同样只打印、绝不自动执行）。
- 环境变量读取：`init.sh`、bootstrap 脚本与 Makefile 直接读 `server/compose/.env`，
  重复键以**最后一条**为准（与 `docker compose --env-file` 一致）；注意 compose 插值时
  shell 环境变量优先级高于 `.env`。
- 就绪探测：postgres/synapse/sygnal 用容器 healthcheck（sygnal 用镜像内 python
  探测 `/health`——运行镜像无 curl，自带的 curl HEALTHCHECK 会误报 unhealthy）；
  tuwunel、continuwuity 镜像为 OS-less（无 shell/curl），由 `server/tests/smoke.py`
  轮询 `/_matrix/client/versions`。
- 推送网关（A1.2，[docs/deploy/push-sygnal.md](../docs/deploy/push-sygnal.md)）：
  `make up PUSH=1` 叠加 `push` profile 启动 sygnal（宿主 `127.0.0.1:8019` → 容器
  5000；homeserver 走内网 `http://sygnal:5000`）。配置由 `init.sh` 从
  `sygnal/sygnal.yaml.tmpl` 渲染，默认带 `org.botville.mock` pushkin（写 JSONL，
  供 `make push-e2e` 测时延；`BOTVILLE_PUSH_MOCK=false` 可移除，生产只跑真实
  APNs）；真实 APNs（issue #4，单 app 方案）仅当 `.env` 里
  `BOTVILLE_APNS_ENABLED=true` 且前置齐备时渲染：`BOTVILLE_APNS_BUNDLE_ID`（反向
  域名，`APNS_TOPIC` 默认取它、`APNS_APP_ID` 默认推导为 `<bundle-id>.apns`）、
  `APNS_KEY_ID`/`APNS_TEAM_ID`（10 位大写字母数字）、`APNS_PLATFORM`
  （sandbox|production）与 `server/compose/secrets/apns.p8`（普通文件、非空、
  mode 600/400，只 stat 不读内容）；**缺项/占位 → WARNING 跳过（mock 照常），
  已填但格式/权限非法 → init.sh exit 1**。key 以**单文件**只读挂载进容器
  （无真实 key 时 init.sh 维护 0600 空占位文件，mock-only 部署不受影响），
  `BOTVILLE_PUSH_DOMAIN` 记录反代用的推送公网域名（运维参考）。**Apple 开发者
  账号、Team ID 与 APNs Auth Key（.p8）已就绪（密钥只在部署侧、不进仓库；仓库内 APNs 值一律占位），
  真实 APNs 送达待真机验证（issue #4）**（8845 mini PC 部署 runbook 见 push-sygnal.md §12）。
  homeserver 的内网推送放行只有 sygnal 容器的固定 IP `BOTVILLE_SYGNAL_IP`（一个
  /32，留空则完全不放行；详见 push-sygnal.md 的「内网推送放行」）。CI 的
  `push-e2e` job 在 synapse、tuwunel、continuwuity 三家上各跑一遍；lint 另有
  `server/scripts/check_no_secrets.sh` 守卫（任何被跟踪的 .p8 / 私钥 PEM 头即红）。
- 镜像默认全部 pin 为 `tag@sha256:<digest>`（含 Postgres，digest 取多架构 index）；
  可在 `.env` 用 `POSTGRES_IMAGE` / `SYNAPSE_IMAGE` / `TUWUNEL_IMAGE` /
  `CONTINUWUITY_IMAGE` / `SYGNAL_IMAGE` 覆盖（如临时跟 `:latest`，不推荐）。
  升级流程与风险见
  [docs/ops/upgrade-images.md](../docs/ops/upgrade-images.md)；
  continuwuity 的实测版本与 digest 另见 `docs/compat/homeserver-compat.md` §3.3。

## 布局

```
server/
├── compose/
│   ├── docker-compose.yml        # profiles: synapse | tuwunel | continuwuity (+ push)
│   ├── .env.example              # 仅占位符（CI 有 lint 守卫）
│   ├── synapse/
│   │   ├── homeserver.yaml.tmpl  # homeserver 配置模板（whitelist 由 part 追加）
│   │   └── ip-range-whitelist.part  # sygnal /32 放行（BOTVILLE_SYGNAL_IP 非空时追加）
│   ├── appservice/botville-gateway.yaml.tmpl  # AI 网关 appservice 注册模板（A1.3）
│   ├── sygnal/
│   │   ├── sygnal.yaml.tmpl      # sygnal 配置模板（apps 由 part 追加，A1.2）
│   │   ├── mock-app.yaml.part    # org.botville.mock（BOTVILLE_PUSH_MOCK，默认开）
│   │   ├── apns-app.yaml.part    # APNs app 片段（APNs 启用且密钥就绪时追加）
│   │   └── botville_mock_pushkin.py  # 自定义 pushkin：收通知写 JSONL
│   ├── federation/               # 配对联邦测试栈（A2.1，独立项目 botville-fed）
│   │   ├── docker-compose.federation.yml  # hs-a/hs-b 双 Synapse + pairing + fedgate
│   │   └── generated/            # 运行时证书/配置/token，gitignore
│   ├── secrets/                  # 真实 apns.p8 放这里（init.sh 建 700，gitignore，绝不入库；单文件只读挂载）
│   └── generated/                # 渲染产物 + mock 推送数据，gitignore（appservice/ 下 0600）
├── pairing/                      # 配对联邦组件（A2.1，纯标准库；python -m pairing.service|fedgate）
│   ├── store.py                  # SQLite：invites（一次性，仅存 sha256）/ peers / events 审计
│   ├── service.py                # 配对 HTTP API（invite/redeem/accept/sever，限速，对端 key 校验）
│   ├── fedgate.py                # 8448 反代闸门：X-Matrix origin ∈ active 配对表，否则 403
│   └── tests/                    # 单测（无 Docker；python3 -m unittest discover -s server/pairing/tests）
├── scripts/
│   ├── init.sh                   # .env 引导/补齐缺失键 + 模板渲染（含 sygnal）
│   ├── render_push_denylist.py   # push denylist 渲染 + subnet/sygnal IP 校验（A1.2）
│   ├── check_no_secrets.sh       # 密钥入库守卫：被跟踪 .p8 / 私钥 PEM 头即失败（#4）
│   ├── federation-certs.sh       # 联邦测试栈自签证书（运行时生成，gitignore，A2.1）
│   ├── federation-setup.py       # 联邦测试栈 Synapse 配置 + admin token 渲染（A2.1）
│   ├── bootstrap-continuwuity.sh # continuwuity 首启引导注册（标准 UIA，仅 CS API）
│   ├── lib.sh                    # backup/restore 共享辅助函数
│   ├── backup-synapse.sh         # DB dump + synapse-data / 可选 gateway-data 归档 + manifest（A1.1 / A2.2）
│   ├── restore-synapse.sh        # 校验 sha256 后破坏性恢复（需 --yes，A1.1；可选恢复 gateway-data）
│   ├── backup-gateway-data.sh    # 只备份 gateway-data（任一 HS profile，A2.2；与上面共用 lib.sh 逻辑）
│   └── restore-gateway-data.sh   # 只恢复 gateway-data（需 --yes；无产物则不碰卷）
└── tests/
    ├── smoke.py                  # 仅标准库的 CS API 冒烟测试
    ├── e2e_synapse.py            # seed/verify 两阶段验收测试（A1.1）
    ├── push_e2e.py               # 推送时延验收（HS→sygnal <5s，A1.2）
    ├── appservice_e2e.py         # appservice 代发验收（A1.3，复用 gateway/src）
    ├── federation_e2e.py         # 配对联邦验收：未配对拒绝/配对互发/双向切断（A2.1）
    ├── test_bootstrap_continuwuity.py  # bootstrap/init 脚本 mock 单测（CI lint 跑）
    ├── test_push_sygnal.py       # mock pushkin + init.sh sygnal/APNs 渲染与校验单测（A1.2/#4）
    ├── test_push_denylist.py     # push denylist 渲染/校验单测（A1.2 review B1）
    └── test_check_no_secrets.py  # 密钥入库守卫脚本单测（#4）
```

约束：只用 Matrix 标准接口（禁止 `/_synapse/admin` 与 admin room 命令）；仓库不含任何真实密钥；Apache-2.0。
