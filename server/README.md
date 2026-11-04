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
make smoke           # 标准库冒烟测试：注册→登录→建房(v10)→发消息→回读→sync
make backup          # 备份到 ./backups/<UTC时间戳>/（目录 700、文件 600，A1.1）
make restore RESTORE_DIR=backups/<时间戳>   # 破坏性恢复（自动 --yes）
make e2e             # 验收：注册→私聊→建群；E2E_PHASE=verify 复核备份恢复
make push-e2e        # 推送验收：HS→sygnal 时延 <5s（mock pushkin，需 PUSH=1 起栈）
make appservice-e2e  # 验收：网关以 @ai_* 身份代发（A1.3；synapse/tuwunel）
make federation-e2e  # 配对联邦验收（A2.1；独立 botville-fed 项目，双 Synapse + fedgate）
make federation-down # 只销毁 botville-fed 项目（down -v）
make logs            # 跟踪日志
make ps              # 容器状态
make down            # 停止（保留数据卷；手动 down -v 可清空）
```

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
  从零开始时，手动执行：
  `docker compose -f server/compose/docker-compose.yml --env-file server/compose/.env --profile continuwuity down && docker volume rm botville_continuwuity-data && make up HS=continuwuity`。
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
  APNs）；真实 APNs 仅当 `.env` 里 `BOTVILLE_APNS_ENABLED=true` 且 APNS_* 为真实值
  且 `server/compose/secrets/apns.p8` 存在时渲染（**当前无 Apple 账号，真实 APNs
  送达未验证**）。homeserver 的内网推送放行只有 sygnal 容器的固定 IP
  `BOTVILLE_SYGNAL_IP`（一个 /32，留空则完全不放行；详见 push-sygnal.md 的
  「内网推送放行」）。CI 的 `push-e2e` job 在 synapse、tuwunel、continuwuity
  三家上各跑一遍。
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
│   ├── secrets/                  # 真实 apns.p8 放这里（init.sh 建 700，gitignore，绝不入库）
│   └── generated/                # 渲染产物 + mock 推送数据，gitignore（appservice/ 下 0600）
├── pairing/                      # 配对联邦组件（A2.1，纯标准库；python -m pairing.service|fedgate）
│   ├── store.py                  # SQLite：invites（一次性，仅存 sha256）/ peers / events 审计
│   ├── service.py                # 配对 HTTP API（invite/redeem/accept/sever，限速，对端 key 校验）
│   ├── fedgate.py                # 8448 反代闸门：X-Matrix origin ∈ active 配对表，否则 403
│   └── tests/                    # 单测（无 Docker；python3 -m unittest discover -s server/pairing/tests）
├── scripts/
│   ├── init.sh                   # .env 引导/补齐缺失键 + 模板渲染（含 sygnal）
│   ├── render_push_denylist.py   # push denylist 渲染 + subnet/sygnal IP 校验（A1.2）
│   ├── federation-certs.sh       # 联邦测试栈自签证书（运行时生成，gitignore，A2.1）
│   ├── federation-setup.py       # 联邦测试栈 Synapse 配置 + admin token 渲染（A2.1）
│   ├── bootstrap-continuwuity.sh # continuwuity 首启引导注册（标准 UIA，仅 CS API）
│   ├── lib.sh                    # backup/restore 共享辅助函数
│   ├── backup-synapse.sh         # DB dump + 数据卷归档 + manifest（A1.1）
│   └── restore-synapse.sh        # 校验 sha256 后破坏性恢复（需 --yes，A1.1）
└── tests/
    ├── smoke.py                  # 仅标准库的 CS API 冒烟测试
    ├── e2e_synapse.py            # seed/verify 两阶段验收测试（A1.1）
    ├── push_e2e.py               # 推送时延验收（HS→sygnal <5s，A1.2）
    ├── appservice_e2e.py         # appservice 代发验收（A1.3，复用 gateway/src）
    ├── federation_e2e.py         # 配对联邦验收：未配对拒绝/配对互发/双向切断（A2.1）
    ├── test_bootstrap_continuwuity.py  # bootstrap/init 脚本 mock 单测（CI lint 跑）
    ├── test_push_sygnal.py       # mock pushkin + init.sh sygnal 渲染单测（A1.2）
    └── test_push_denylist.py     # push denylist 渲染/校验单测（A1.2 review B1）
```

约束：只用 Matrix 标准接口（禁止 `/_synapse/admin` 与 admin room 命令）；仓库不含任何真实密钥；Apache-2.0。
