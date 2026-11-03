# server/ — 服务端与部署（A 线，ZCode）

- Matrix 服务器部署：Synapse + Postgres，或 Tuwunel / continuwuity；Docker Compose profile 按模块启用
- 只依赖标准 Client-Server、Application Service、Push Gateway 接口；CI 双服务器测试
- Sygnal 推送网关 + APNs；appservice 注册 AI 账号命名空间
- 仅配对联邦与扫码配对后端（可单方面切断）
- 动态存储与可见范围、AI 关系存储与多 AI 群
- 小镇状态服务（位置与状态同步、降频/关闭）、素材存储
- 安装向导、升级与导出脚本

## 快速开始（A0.2）

前置：Docker（含 compose v2）、`openssl`、`python3`、`make`。

```bash
make up              # core 一键启动：Synapse + Postgres，http://127.0.0.1:8008
make up HS=tuwunel   # 换 Tuwunel（continuwuity 同理）
make smoke           # 标准库冒烟测试：注册→登录→建房(v10)→发消息→回读→sync
make logs            # 跟踪日志
make ps              # 容器状态
make down            # 停止（保留数据卷；手动 down -v 可清空）
```

说明：

- 首次 `make up` 会执行 `server/scripts/init.sh`：从 `.env.example` 生成本地
  `server/compose/.env`（所有 `<change-me>` 用 `openssl rand -hex 32` 就地随机生成，
  不会提交），并把 `synapse/homeserver.yaml.tmpl` 渲染到 `generated/synapse/`（gitignore）。
- 三个 homeserver 容器内都监听 8008，宿主仅绑定 `127.0.0.1:${BOTVILLE_HTTP_PORT:-8008}`；
  需要外部访问时自行加反向代理。
- 注册策略：tuwunel / continuwuity 始终令牌门控（`BOTVILLE_REGISTRATION_TOKEN`）；
  Synapse 默认关闭注册，CI 中通过 `BOTVILLE_OPEN_REGISTRATION=true` 走 dummy 流程。
  本地想对 Synapse 跑 `make smoke` 时，把 `.env` 里该值改为 `true` 再 `make up`。
- continuwuity 首启引导：全新数据库上一次性引导令牌只打印在容器日志，配置的
  `BOTVILLE_REGISTRATION_TOKEN` 在首个账号创建前无效（401 `M_FORBIDDEN`）。
  `make up HS=continuwuity` 启动后会自动执行 `server/scripts/bootstrap-continuwuity.sh`：
  从日志提取引导令牌，以标准 UIA（`m.login.registration_token`）注册 `.env` 里的
  `BOTVILLE_ADMIN_USER` 作为首个账号（continuwuity 惯例：首用户即管理员），此后配置令牌生效。
  日志中找不到引导令牌（已引导过）时脚本提示后跳过；`M_USER_IN_USE` 视为已完成。
- 就绪探测：postgres/synapse 用容器 healthcheck；tuwunel、continuwuity 镜像为
  OS-less（无 shell/curl），由 `server/tests/smoke.py` 轮询 `/_matrix/client/versions`。
- 镜像可通过 `.env` 的 `SYNAPSE_IMAGE` / `TUWUNEL_IMAGE` / `CONTINUWUITY_IMAGE` 覆盖。

## 布局

```
server/
├── compose/
│   ├── docker-compose.yml        # profiles: synapse | tuwunel | continuwuity
│   ├── .env.example              # 仅占位符（CI 有 lint 守卫）
│   ├── synapse/homeserver.yaml.tmpl
│   └── generated/                # 渲染产物，gitignore
├── scripts/
│   ├── init.sh                   # .env 引导/补齐缺失键 + 模板渲染
│   └── bootstrap-continuwuity.sh # continuwuity 首启引导注册（标准 UIA，仅 CS API）
└── tests/smoke.py                # 仅标准库的 CS API 冒烟测试
```

约束：只用 Matrix 标准接口（禁止 `/_synapse/admin` 与 admin room 命令）；仓库不含任何真实密钥；Apache-2.0。
