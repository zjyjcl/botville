# 在极空间 Z423 Pro 上部署 Botville（Synapse + Postgres）

任务 A1.1 部署手册 · 目标硬件：极空间 Z423 Pro（x86_64，系统自带 Docker 图形界面与 SSH）。

> **验证状态**：compose 栈本身的部署与冒烟（`make up` + `make smoke`）此前已
> 在 Mac mini + colima（x86_64 容器）验证过（A0.2）；本手册新增的
> 注册/私聊/建群 e2e 与「备份 → 清空 → 恢复」全流程由 CI 的
> `backup-restore (synapse)` job 在每次提交时完整执行，**尚未在 Mac mini +
> colima 或 NAS 实机复测**。极空间系统细节（反代界面、SSH 位置、路径）以实机
> 为准，遇到出入请以[常见问题](#常见问题)与官方文档为准并回填本手册。

手册只用 Matrix 标准接口（Client-Server API），不依赖 `/_synapse/admin`；
所有示例中域名一律用 `example.invalid` 占位，密钥一律用占位符，不会出现真实值。

---

## 目录

1. [前提条件](#前提条件)
2. [获取代码](#获取代码)
3. [初始化：make init](#初始化make-init)
4. [编辑 .env](#编辑-env)
5. [启动：make up](#启动make-up)
6. [验收：注册 → 私聊 → 建群](#验收注册--私聊--建群)
7. [反向代理与 HTTPS](#反向代理与-https)
8. [开放注册的安全建议](#开放注册的安全建议)
9. [备份与恢复](#备份与恢复)
10. [同机多实例 / 多开发线并行](#同机多实例--多开发线并行)
11. [资源与端口](#资源与端口)
12. [升级](#升级)
13. [常见问题](#常见问题)

---

## 前提条件

| 项目 | 要求 |
| --- | --- |
| 机型 | 极空间 Z423 Pro（x86_64；其他 x86_64 极空间机型同样适用） |
| Docker | 极空间系统自带 Docker（图形界面 + docker compose CLI，v2） |
| SSH | 建议开启（极空间系统设置里开启），部署/备份流程都走 SSH 命令行；极空间 Docker 图形界面用于查看容器状态、日志和重启 |
| 内存/磁盘 | ≥ 2 GB 空余内存；数据库很小，磁盘主要随媒体文件增长 |
| 网络 | 能拉取 Docker Hub 镜像（`matrixdotorg/synapse`、`library/postgres:16-alpine`）；国内网络不畅时可在 Docker 设置里配置镜像加速（见[常见问题](#常见问题)） |
| 软件 | `git`（或直接下载源码包）、`python3`（健康检查/测试脚本用）、`make`、`openssl`（init.sh 生成随机密钥用）。极空间系统一般自带；缺 `make` 时可直接照 [Makefile](../../Makefile) 里的命令手敲 |

**目录规划**：代码放在极空间的用户数据目录，示例路径（占位符）：

```
<NAS_DATA_DIR> = /data_s001/data/udata/real/<NAS_USER>/botville
```

- `<NAS_DATA_DIR>`：极空间个人空间里的项目目录，下文所有命令默认在此目录执行；
- `<NAS_USER>`：你在极空间上的账号名。

数据库和 Synapse 数据（`media_store`、`signing.key`）存在 Docker **named volume**
（`<项目名>_postgres-data`、`<项目名>_synapse-data`，默认项目名 `botville`，
见[同机多实例](#同机多实例--多开发线并行)）里，由 Docker 统一管理；
备份脚本直接从卷里导出，不依赖卷的实际落盘位置。

---

## 获取代码

```bash
ssh <NAS_USER>@<NAS_IP>
mkdir -p /data_s001/data/udata/real/<NAS_USER>
cd /data_s001/data/udata/real/<NAS_USER>

# 方式一：git（推荐，升级方便）
git clone <BOTVILLE_REPO_URL> botville
cd botville

# 方式二：下载源码包解压到 botville/ 后进入
```

仓库里与本手册相关的文件：

```
Makefile                          # make up / backup / restore / e2e 等入口
server/compose/docker-compose.yml # compose 栈定义（synapse profile）
server/compose/.env.example       # 配置模板（init.sh 会复制成 .env）
server/scripts/init.sh            # 生成 .env 与渲染 homeserver.yaml
server/scripts/backup-synapse.sh  # 备份
server/scripts/restore-synapse.sh # 恢复（需 --yes）
server/tests/smoke.py             # 冒烟测试（注册/建房/发消息）
server/tests/e2e_synapse.py       # 验收测试（seed/verify 两阶段）
```

## 初始化：make init

```bash
make init
```

`init.sh` 做两件事（都不联网）：

1. 把 `server/compose/.env.example` 复制成 `server/compose/.env`，并把模板里所有
   `<change-me>` 占位符替换成 `openssl rand -hex 32` 生成的随机值
   （`POSTGRES_PASSWORD`、`SYNAPSE_MACAROON_SECRET` 等）；
2. 用 `.env` 渲染 `server/compose/synapse/homeserver.yaml.tmpl`，生成
   `server/compose/generated/synapse/homeserver.yaml`（该目录已 gitignore）。

`.env` 和 `generated/` 都**不要提交、不要外发**。已存在时会保留当前值，重复执行安全。

## 编辑 .env

```bash
vi server/compose/.env
```

部署到 NAS 至少确认这几项：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `BOTVILLE_HOMESERVER` | `synapse` | 本手册固定 `synapse`，不改 |
| `BOTVILLE_SERVER_NAME` | `localhost` | 服务器标识，进入所有用户 ID（`@alice:<server_name>`）。**之后不能随意改**（改了等于换了一个新身份）。内网自用可保持 `localhost` 或起个名字；打算配域名反代就填域名，例如 `matrix.example.invalid` |
| `BOTVILLE_HTTP_PORT` | `8008` | Synapse 在 NAS 上监听的端口（只绑定 `127.0.0.1`，通过[反向代理](#反向代理与-https)对外） |
| `BOTVILLE_OPEN_REGISTRATION` | `false` | 是否开放注册。**保持 `false`**，需要加人时见[开放注册的安全建议](#开放注册的安全建议) |
| `COMPOSE_PROJECT_NAME` | `botville` | compose 项目名（容器/卷/网络的前缀）。单实例保持默认；同机多实例**必须**改，见[同机多实例](#同机多实例--多开发线并行) |
| `SYNAPSE_IMAGE`、`POSTGRES_IMAGE` | 随仓库 pin | 镜像版本（tag@digest）。**以 [`server/compose/.env.example`](../../server/compose/.env.example) 当前 pin 为准**，不要照抄本手册或旧 `.env` 里的版本号；[升级](#升级)时改这里 |
| `POSTGRES_*`、`SYNAPSE_*_SECRET` | 随机生成 | 不用动，保持 init.sh 生成的随机值 |

其余项见 `.env.example` 内注释。

## 启动：make up

```bash
make up          # = init.sh + docker compose --profile synapse up -d
make ps          # 查看容器状态，等 postgres/synapse 都变 healthy
                 # （始终带 push/gateway profile，与 make down 一样；不必再加
                 # PUSH=1 / GATEWAY=1 才能看到已启动的服务）
make logs        # 跟踪日志（Ctrl-C 退出；同样始终带这两个 profile）
```

首次启动会拉取镜像并：postgres 建库（UTF-8、C locale）→ Synapse 自动建表、
生成 `signing.key`。整个过程一般 1–2 分钟（视镜像下载速度）。

命令行验收（等同 Element 能做的事，走标准接口）：

```bash
# smoke/e2e 都包含「注册」步骤，而默认注册是关闭的，先临时打开（验收完关回，
# 与 CI 的做法一致；sed 两行在[开放注册的安全建议](#开放注册的安全建议)有解释）：
sed -i 's/^BOTVILLE_OPEN_REGISTRATION=.*/BOTVILLE_OPEN_REGISTRATION=true/' server/compose/.env
make up
make smoke       # 注册一个随机用户→登录→建房→发消息→读回
```

输出 `SMOKE OK: ...` 即服务正常。验收完把注册关回去：

```bash
sed -i 's/^BOTVILLE_OPEN_REGISTRATION=.*/BOTVILLE_OPEN_REGISTRATION=false/' server/compose/.env
make up
```

## 验收：注册 → 私聊 → 建群

验收分两层：脚本验收（标准 API，与 CI 同款）和 Element 实际操作。两层都过即满足
issue #3 的验收标准。

### 脚本验收（e2e）

```bash
make e2e E2E_PHASE=seed     # 注册 alice/bob/carol，建私聊+群，互发消息，写 backups/e2e-state.json
```

这一步要求临时开放注册（见下节），或者给脚本传注册 token
（`BOTVILLE_REGISTRATION_TOKEN`，在 `.env` 里）。它模拟的正是「注册 → 私聊 →
建群 → 互发消息」，全部 PASS 后输出 `E2E SEED OK`。
「备份可恢复」的验收在[备份与恢复](#备份与恢复)一节用 `E2E_PHASE=verify` 完成。

### Element 实际操作

1. **准备**：
   - 局域网 `http` 直连：用 **Element 桌面版**（浏览器版受混合内容限制，必须走 HTTPS）。
     NAS 端口只绑了 `127.0.0.1`，先用 SSH 隧道转发到本机：
     `ssh -L 8008:127.0.0.1:8008 <NAS_USER>@<NAS_IP>`，或直接上[反向代理](#反向代理与-https)。
   - HTTPS 反代后：Element 桌面版 / 浏览器版（app.element.io 或自托管 Element Web）都可以。
2. **注册**：Element 首屏 →「编辑主服务器」→「自定义」→ 填
   `http://127.0.0.1:8008`（隧道）或 `https://matrix.example.invalid`（反代）→
   「注册」→ 输入用户名密码。默认注册是**关闭**的，先看
   [开放注册的安全建议](#开放注册的安全建议)把账号建好，再把注册关回去。
3. **私聊**：登录 alice → 左侧「+」→「发起私聊」→ 输入对方完整 ID
   `@bob:<BOTVILLE_SERVER_NAME>` → 发消息；bob 登录后在「邀请」里接受即可互聊。
4. **建群**：左侧「+」→「创建房间」→ 填名称（如 `botville-test`）、选「私密房间」→
   「邀请」里添加 `@bob:…`、`@carol:…` → 建好后各自接受邀请、互发消息。

## 反向代理与 HTTPS

Synapse 只监听 `127.0.0.1:8008`，对局域网/公网提供服务需要反代。两种做法：

**A. 极空间自带反向代理**：在极空间 Docker/应用界面里把域名指向
`127.0.0.1:8008`（极空间反代会自动处理 HTTPS 证书）。适合单域名直连；
它一般**不支持自定义 `.well-known` 路径响应**，所以让 `BOTVILLE_SERVER_NAME`
直接等于反代域名（例如 `matrix.example.invalid`），就不需要 `.well-known`。

**B. Caddy 容器（推荐，配置即代码）**：把 Caddy **加到 botville 自己的 compose
文件里，或建一个独立的 `caddy/compose.yml`**；不要修改 NAS 上其它现有容器 /
compose 项目。示例 `Caddyfile`（域名占位 `example.invalid`）：

```caddyfile
# Synapse 本体：客户端 API 与 /health
matrix.example.invalid {
	reverse_proxy 127.0.0.1:8008
}

# 仅当 server_name 是裸域名、Synapse 挂在子域时才需要下面这些；
# 本部署关闭联邦，/.well-known/matrix/server 属于备用示例。
example.invalid {
	handle /.well-known/matrix/client {
		header Content-Type application/json
		header Access-Control-Allow-Origin *
		respond `{"m.homeserver":{"base_url":"https://matrix.example.invalid"}}`
	}
	handle /.well-known/matrix/server {
		header Content-Type application/json
		respond `{"m.server":"matrix.example.invalid:443"}`
	}
	respond "botville" 200
}
```

两个 `.well-known` 文件的含义与内容（部署时按需取用，全部占位）：

- `https://example.invalid/.well-known/matrix/client`
  （客户端发现 homeserver 的真实地址，**必须带 CORS 头** `Access-Control-Allow-Origin: *`）：

  ```json
  {
    "m.homeserver": { "base_url": "https://matrix.example.invalid" }
  }
  ```

- `https://example.invalid/.well-known/matrix/server`
  （联邦端口委托，指向 8448 以外的地址；本部署 `trusted_key_servers: []`、
  联邦关闭，仅作格式备用）：

  ```json
  {
    "m.server": "matrix.example.invalid:443"
  }
  ```

**最简路径**：内网自用、不打算开联邦——`BOTVILLE_SERVER_NAME` 设为反代域名，
`.well-known` 两个都可以不部署；Element 里直接填该域名即可。

公网暴露前请确认：注册保持关闭、只放行需要的端口（443）、证书自动续期正常。

## 开放注册的安全建议

默认 `BOTVILLE_OPEN_REGISTRATION=false`（注册完全关闭），推荐两种加账号的方式：

1. **临时开放（适合首批建号）**：

   ```bash
   sed -i 's/^BOTVILLE_OPEN_REGISTRATION=.*/BOTVILLE_OPEN_REGISTRATION=true/' server/compose/.env
   make up        # 重启生效
   # ……在 Element 里完成注册……
   sed -i 's/^BOTVILLE_OPEN_REGISTRATION=.*/BOTVILLE_OPEN_REGISTRATION=false/' server/compose/.env
   make up        # 立刻关回去
   ```

   开放期间任何能连到服务器的人都能注册；公网部署请缩短窗口，甚至只在内网/隧道内开放。

2. **`register_new_matrix_user`（部署层工具，不开 HTTP 注册口子）**：

   ```bash
   docker compose -f server/compose/docker-compose.yml --env-file server/compose/.env \
     exec -it synapse register_new_matrix_user http://localhost:8008 -c /config/homeserver.yaml
   ```

   它用 `.env` 里 `SYNAPSE_REGISTRATION_SHARED_SECRET` 渲染出的共享密钥在容器内
   完成注册。该密钥**只存在于部署配置（`.env`/渲染后的 homeserver.yaml）**，
   供部署层工具使用；应用与网关代码不接触它（Botville 只用标准 Client-Server API）。

不建议：长期开放注册 + 公网可达。

## 备份与恢复

### 备份

```bash
make backup
# 或自定义目录：BACKUP_DIR=/mnt/usb/backup-$(date +%F) bash server/scripts/backup-synapse.sh
```

产物在 `./backups/<UTC时间戳>/`（已 gitignore）：

| 文件 | 内容 |
| --- | --- |
| `synapse-db.dump` | `pg_dump -Fc` 数据库快照（postgres 保持运行中导出） |
| `synapse-data.tar.gz` | `media_store`、`signing.key`（短暂停 synapse 保证与数据库一致） |
| `gateway-data.tar.gz` | （可选）compose 卷 `gateway-data` 的**整卷** `tar`（卷根目录下的全部文件，不限 `.db`）。打包期间暂停正在运行的 gateway 容器；打包前仅对**卷顶层** `*.db` 做 `PRAGMA wal_checkpoint(TRUNCATE)`（sqlite3 CLI，没有则用镜像里的 python3），不递归子目录。**卷不存在则跳过，不写此文件**（没用过 `GATEWAY=1` 的栈没有该卷）。旧备份没有此文件时，恢复不碰该卷 |
| `homeserver.yaml` | 渲染后的 Synapse 配置副本 |
| `env` | `.env` 副本，**含全部密钥**（网关密钥若写在这份 `.env` 里会随它备份；`gateway-data` 卷只含业务数据，不另存密钥） |
| `manifest.txt` | 备份时间、镜像、每个文件的 sha256（有 `gateway-data.tar.gz` 时也列入） |

**整个备份目录等同密钥库**：脚本以 umask 077 运行，并强制**目录权限 700、
所有文件权限 600**（含容器内生成的 tar，结束前自检，不达标即报错）；不要明文放
公网网盘、建议至少一份离线/异地。

**网关服务与网关数据**（A2.2 / #7）：网关是可选的 compose profile `gateway`，
和 `push` 一样叠加在任一 homeserver profile 上，**默认 `make up` 不启动它**：

```bash
make up GATEWAY=1              # synapse + 网关；make up HS=tuwunel GATEWAY=1 同理
curl -s http://127.0.0.1:8020/health   # 端口 = .env 的 BOTVILLE_GATEWAY_PORT，只绑 127.0.0.1
```

镜像由 `gateway/Dockerfile` 在本机构建（固定 digest 的 `python:3.12-slim` + 网关源码 +
`gateway/migrations/moments/`，以非 root 的 uid 10001 运行，不含任何密钥）；
`GATEWAY_AUTH_TOKEN` / `GATEWAY_HMAC_SECRET` 由 init.sh 生成到 `.env` 的
`BOTVILLE_GATEWAY_AUTH_TOKEN` / `BOTVILLE_GATEWAY_HMAC_SECRET`。

`make up GATEWAY=1` 会按 homeserver profile 给网关接线（详见 [appservice.md](appservice.md)）：

- **synapse / tuwunel**：`GATEWAY_MATRIX_TRANSPORT=appservice`，
  `GATEWAY_HOMESERVER_URL=http://<服务名>:8008`（`BOTVILLE_HOMESERVER` 就是 compose
  服务名，容器里不要写 `127.0.0.1`），`GATEWAY_MATRIX_SERVER_NAME=${BOTVILLE_SERVER_NAME}`，
  `GATEWAY_AS_TOKEN` / `GATEWAY_HS_TOKEN` 注入现有的 `APPSERVICE_AS_TOKEN` /
  `APPSERVICE_HS_TOKEN`（各自同一份值，不另造；init.sh 生成的两者互不相同），
  `GATEWAY_HOMESERVER_FEDERATION_URL` 留空（OpenID userinfo 回落到上面的内网地址，
  不走 fedgate 或公网）。注册文件的 `url` 是 `http://gateway:8080`（无路径前缀），
  HS 把 AS 事务推给网关，网关按 txnId 幂等存进
  `GATEWAY_AS_TXN_DB_PATH=/var/lib/botville-gateway/as_transactions.db`。
- **没开 `GATEWAY=1`，或 continuwuity**：注册 `url` 为 `null`。continuwuity 仍用
  `memory` 传输：它还没有文件方式的 appservice 注册，切到 appservice 后网关启动
  whoami 会失败并反复退出。
- 注册 url 由 `make up` 决定并交给 init.sh 渲染；url 变了 `make up` 会先重启 HS
  （它只在启动时读注册文件）。在 `make up GATEWAY=1` 与 `make up` 之间切换都会
  自动处理；`make down` 不改 url。
- 只用 `make up` 重建网关：`docker compose up` 直接重建会落回 compose 兜底值
  （memory 传输、`http://synapse:8008`，兜底表见 [appservice.md](appservice.md)），
  `make gateway-hs-smoke` 能查出这种情况。
- 切到 appservice 后，网关启动会请求 HS 的 whoami，失败就退出；compose 里
  `restart: unless-stopped`，并对当前 HS 设了 `depends_on`（synapse 等 healthy，
  tuwunel 等 started——后者镜像没有 healthcheck）。HS 未就绪时会自动重启几次。
- 改 `BOTVILLE_SERVER_NAME` 时，注册正则（init.sh 渲染）和
  `GATEWAY_MATRIX_SERVER_NAME` 要一起改，并重启 homeserver 与网关。
- 轮换 `APPSERVICE_AS_TOKEN` / `APPSERVICE_HS_TOKEN` 时必须让 HS（重新渲染注册
  文件并重启）和网关（同一变量注入为 `GATEWAY_AS_TOKEN` / `GATEWAY_HS_TOKEN`）
  同时切换。两者不能相同，网关会拒绝启动。
- 只能跑一个网关（单进程，SQLite 库加进程内锁）：不要 `--scale gateway=2`，也
  不要让第二个网关挂同一个卷或用同一份注册文件。
- 明文 `http://` 时，网关只允许 loopback 或单标签服务名（`http://synapse:8008` /
  `http://tuwunel:8008` 可以；不要开 `GATEWAY_ALLOW_INSECURE_HS`）。

角色引擎（B 线 PR2）：`GATEWAY_MODEL_BASE_URL` / `GATEWAY_MODEL_NAME` /
`GATEWAY_MODEL_API_KEY` / `GATEWAY_MODEL_TIMEOUT_SECONDS` / `GATEWAY_PERSONAS_FILE` /
`GATEWAY_CONSUMER_ENABLED` 和记忆变量 `GATEWAY_MEMORY_DB_PATH`（空 = 关闭；启用写
`/var/lib/botville-gateway/memory.db`，0600，在 gateway-data 卷里随备份走）/
`GATEWAY_MEMORY_RETENTION_DAYS` / `GATEWAY_MEMORY_PURGE_INTERVAL_SECONDS` 从 `.env`
透传给网关（init.sh 不生成，开关默认关）；
人设目录 `server/compose/gateway-personas/` 只读挂到 `/etc/botville-gateway`（真实人设
gitignore）。只有开关打开、配了模型并且配了人设，AI 才会开口。API key 只放本地
`.env`，不要贴 `docker compose config` 的输出（用 `--quiet`）。详见
[appservice.md](appservice.md#角色引擎模型与人设)。

本地或 CI 冒烟：`make gateway-hs-smoke`——检查容器里的传输与 HS 一致；`/health` 的
matrix 检查 = 启动 whoami；用 OpenID token 打 `GET /v1/moments` 校验内网 userinfo；
HS→网关 ping；`@ai_*` 所在房间的消息被推送并落进 `as_transactions.db`；重启网关后
重放同一 txnId 只存一份。脚本不打印任何 token，失败只报 HTTP 状态和完整 URL。

网关挂载 compose 卷 `gateway-data`（实际名 `<项目名>_gateway-data`）到
`/var/lib/botville-gateway`，`GATEWAY_MOMENTS_DB_PATH=/var/lib/botville-gateway/moments.db`，
启动时自动执行镜像内的 moments 迁移；AS 事务库 `as_transactions.db` 也在卷顶层，
随卷一起备份（备份前对顶层 `*.db` 做 WAL checkpoint）和恢复。删除语义：

- `make down` **不传** `-v`，不删除任何卷（包括 `gateway-data`）。
- `docker compose ... --profile gateway down -v`（只要带上 gateway profile，与网关
  容器是否正在运行无关）会连同其他卷一起删掉 `gateway-data`。**不带** `--profile gateway`
  时 compose 不认为该卷在用，`down -v` 删不掉它，`docker compose config --volumes` 也不列它。
- 任何时候都可以单独删除：

```bash
docker volume rm -f botville_gateway-data   # 项目名按 .env 的 COMPOSE_PROJECT_NAME
```

在 #188 时期手工 `docker volume create` 过、里面文件属主是 root 的旧卷，非 root 网关可能
写不进去：先 `make backup-gateway`，再 `make restore-gateway RESTORE_DIR=…`（恢复时会把卷内
文件交给 uid 10001），或直接删掉旧卷让网关重建。

备份：synapse 用 `make backup`（上表，含 `gateway-data.tar.gz`）；tuwunel / continuwuity
没有 homeserver 备份脚本，用只备份网关数据的

```bash
make backup-gateway                                   # 卷不存在则跳过、不写任何文件
make restore-gateway RESTORE_DIR=backups/<时间戳>      # 备份里没有 gateway-data.tar.gz 时不碰现有卷
```

产物同样落在 `./backups/<UTC时间戳>/`（`gateway-data.tar.gz` + `manifest.txt`，目录 700、
文件 600），**不含 `.env`**——网关密钥只在 `.env` 里，请另行保管 `.env`；卷内只有业务
数据。两套脚本共用 `server/scripts/lib.sh` 的同一份归档/恢复逻辑（暂停网关容器、顶层
`*.db` 的 WAL checkpoint、整卷 tar、manifest sha256 校验）。联邦栈（botville-fed）没有
网关服务、也没有 `gateway-data` 卷，不涉及。
保留天数沿用现有备份目录清理惯例（下文 crontab 的 14 天，同一个 `backups/` 目录），
本卷不另设保留期。

**备份介质必须支持 Unix 权限位**：备份目录 700、文件 600 这套权限要原样落在
备份介质上。目标盘/挂载点须是 ext4、btrfs、ZFS、APFS 等保留权限的文件系统
（极空间 NAS 自身的数据卷一般没问题）；**不要把 exFAT / FAT32 格式的 U 盘或移动
硬盘直接当作 `BACKUP_DIR`**——这类文件系统没有 Unix 权限位，权限无法保持，备份
脚本的权限自检会失败，或密钥文件在世界可读状态下留在盘上。需要用这类介质搬运时，
先把备份目录 `tar` 打包再拷贝，在目标端解开并 `chmod` 恢复权限。

脚本最后会把原本运行中的 synapse 重新拉起（备份中途失败也会自动拉回；备份期间
服务中断约几秒到几十秒，取决于媒体量）。成功后会把本次备份目录路径记录到
`backups/.last-backup`，供脚本/CI 读取。

定时备份示例（crontab，每天 04:00，保留 14 天）：

```cron
0 4 * * * cd <NAS_DATA_DIR>/botville && mkdir -p backups && make backup >> backups/cron.log 2>&1
30 4 * * * find <NAS_DATA_DIR>/botville/backups -maxdepth 1 -mindepth 1 -type d -mtime +14 -exec rm -rf {} +
```

（极空间也可用系统的计划任务/定时脚本功能执行同样两条命令。）

### 恢复

**恢复是破坏性操作**：会用备份覆盖当前数据库、`synapse-data` 数据卷、`.env` 和渲染配置。
若备份含 `gateway-data.tar.gz`，也会重建并覆盖 `gateway-data` 卷；若没有该文件（旧备份
或当时卷不存在），**不碰**现有 `gateway-data` 卷。脚本需要
`--yes` 才执行（`make restore` 目标已自动带上）。

在**原机器**上恢复：

```bash
make down
make restore RESTORE_DIR=backups/<时间戳>
```

在**新机器**上恢复（换 NAS/重建）：

```bash
git clone <BOTVILLE_REPO_URL> botville && cd botville
make init                       # 生成临时随机 .env（稍后被备份覆盖）
# 把备份目录传到 ./backups/<时间戳>/（含 5 个文件），然后：
make restore RESTORE_DIR=backups/<时间戳>
```

`make init` 生成的 `.env` 默认项目名/端口（`botville` / `8008`）与默认配置的备份
一致，可直接恢复；若备份来自改过名/换过端口的实例，先把这两项改成与备份一致再
恢复（或临时 `BOTVILLE_RESTORE_ALLOW_CROSS_INSTANCE=yes make restore ...`）。

脚本流程：先核对备份与当前实例的 `COMPOSE_PROJECT_NAME`、`BOTVILLE_HTTP_PORT`
一致（缺省分别 `botville` / `8008`；不一致会报错拒绝，防止把别的实例的备份写进
当前卷，确属跨实例/改名恢复时用 `BOTVILLE_RESTORE_ALLOW_CROSS_INSTANCE=yes`
显式跳过）→ 校验 manifest 里**全部 4 个文件**（dump、tar、`homeserver.yaml`、
`env`）的 sha256 → 停 synapse → 装回 `.env`/`homeserver.yaml`（之后的 compose
命令按装回的 `.env` 重新计算项目名）→ 等 postgres 健康 →
drop/recreate 数据库（UTF8、C locale）→ `pg_restore` → 恢复数据卷 → 启动 synapse
并等待 `GET /health` 返回 200。备份缺任一文件（含 `env`）、manifest 存在但缺任一
文件的 sha256 行、或校验不过，都会直接中止；没有 `manifest.txt` 时打告警并跳过
校验。

### 备份可恢复验收（对应 issue #3）

```bash
# 前提：临时开放注册（seed 要注册三个用户，见上文 sed 两行），并 make up
make e2e E2E_PHASE=seed                  # ① 造数据：三用户、私聊、群、消息
make backup                             # ② 备份（记下输出的目录）
make down                               # ③ 停栈（make down 不删卷）
# 带上 --profile gateway：down -v 就会删掉 gateway-data（与容器是否在跑无关）
docker compose -f server/compose/docker-compose.yml \
  --env-file server/compose/.env --profile synapse --profile gateway down -v --remove-orphans
# 没带 --profile gateway 时它不会被删，需要：docker volume rm -f <项目名>_gateway-data
make up                                 # ④ 起一个全新空栈（用网关就 make up GATEWAY=1）
make restore RESTORE_DIR=backups/<时间戳>  # ⑤ 恢复
make e2e E2E_PHASE=verify               # ⑥ 用 state 文件重新登录，确认私聊/群消息都在
```

第 ⑥ 步输出 `E2E VERIFY OK` 即「备份可恢复」验收通过（之后可把注册关回去）。
CI 的 `backup-restore (synapse)` job 每次提交都跑同样流程。



> **实例一致性检查的口径（#110）**：恢复前比对的是当前 `server/compose/.env`
> 与备份里 `env` 的 `COMPOSE_PROJECT_NAME` / `BOTVILLE_HTTP_PORT`（同机
> project/port 维度；两台主机都用默认 `botville/8008` 时无法区分）。
> 装回备份 `.env` 后会按新值重算 compose 项目名——**调用方已显式
> `export COMPOSE=...` 时除外**（脚本会 WARNING，仍使用该 COMPOSE）。

## 同机多实例 / 多开发线并行

生产、测试或多条开发线要在同一台机器（NAS 或开发机）上各跑一套栈时，**每个实例
必须同时满足两点**，缺一不可：

1. **独立的 compose 项目名**：编辑该 checkout 的 `server/compose/.env`，把
   `COMPOSE_PROJECT_NAME` 改成唯一值（生产实例保持 `botville`）；
2. **独立的宿主端口**：`BOTVILLE_HTTP_PORT` 也要各不相同（虽然都只绑定
   `127.0.0.1`，端口相同仍会冲突起不来）。

约定示例（端口随意，不冲突即可）：

| 实例 | `COMPOSE_PROJECT_NAME` | `BOTVILLE_HTTP_PORT` |
| --- | --- | --- |
| 生产 | `botville` | `8008` |
| zcode 开发线 | `botville-zcode` | `8018` |
| codex 开发线 | `botville-codex` | `8028` |

`Makefile` 的所有 compose 命令都自动带 `-p <项目名>`（可用 `make ps
PROJECT=<名字>` 临时覆盖）；备份/恢复脚本同样从 `.env` 读取项目名，在哪个
checkout 里跑就操作哪个实例。

**为什么必须**：compose 用「项目名 + compose 文件」识别一套栈。两个 checkout
项目名相同时，compose 会把它们当成**同一套栈**——容器与卷互相复用/覆盖，任何
一边的 `down` 会停掉对方正在跑的容器，`down -v` 更会把共享的
`<项目名>_postgres-data`、`<项目名>_synapse-data` 连同另一边的数据一起删掉。
用 `docker compose ls` / `docker volume ls` 可以核对当前机器上实际的项目与卷名。

注意两点：

- **卷名带项目名前缀**：named volume 实际名字是 `<项目名>_synapse-data`、
  `<项目名>_postgres-data`（生产默认即 `botville_*`）；
- **直接敲 `docker compose`（不经 make）时**，务必带
  `--env-file server/compose/.env`（它提供 `COMPOSE_PROJECT_NAME`），或显式
  `-p <项目名>`——否则可能落到 compose 文件内置的默认项目名上，正好撞上生产
  实例。恢复备份时，脚本会**强制核对**备份与当前 checkout 的项目名/端口一致，
  不一致直接拒绝（防止跨实例误恢复；确需跨实例见上文
  `BOTVILLE_RESTORE_ALLOW_CROSS_INSTANCE`）。

## 资源与端口

**端口**（默认）：

| 端口 | 服务 | 绑定 | 说明 |
| --- | --- | --- | --- |
| 8008 | synapse | `127.0.0.1`（host） | Client API + `/health`；对外走反代 |
| 5432 | postgres | 仅 compose 内部 | 未发布到主机，不要打开 |
| 443/80 | 反代（Caddy/极空间） | `0.0.0.0` | HTTPS 入口（可选） |

**资源参考**（空载小家庭规模，实测于 colima 验证环境）：

- synapse：约 150–300 MB 内存，空闲 CPU 接近 0；
- postgres 16-alpine：约 50–150 MB 内存；
- 磁盘：纯文本消息的数据库只有几 MB～几十 MB，媒体文件按实际使用增长
  （可在 homeserver.yaml 模板中后续加 `max_upload_size` 等限制）。

Z423 Pro 的 CPU/内存余量非常大，瓶颈只会来自磁盘休眠或网络。

## 升级

```bash
make backup                                          # 1. 先备份
make down                                            # 2. 停栈
vi server/compose/.env                               # 3. 改 SYNAPSE_IMAGE 为新版本，如
                                                     #    docker.io/matrixdotorg/synapse:<新tag>@sha256:<digest>
make up                                              # 4. 启动
make smoke                                           # 5. 冒烟
make e2e E2E_PHASE=verify                            #    （如做过 e2e seed，可进一步验证历史消息）
```

注意：

- **当前基线版本以 [`server/compose/.env.example`](../../server/compose/.env.example)
  的 pin（tag@digest）为准**，不要照抄本手册或旧 `.env` 里的版本号；完整的镜像
  升级流程（怎么取 digest、哪些文件要同步改）见
  [docs/ops/upgrade-images.md](../ops/upgrade-images.md)；
- 只改 `.env` 里的 `SYNAPSE_IMAGE`，不要动 compose 文件里的默认 pin（跟随上游）；
- Synapse 数据库会在启动时自动做 schema 迁移，版本跨度别太大，逐个稳定版升；
- **postgres 保持 16.x**（compose 与 `.env.example` 已按 tag@digest pin，可用
  `POSTGRES_IMAGE` 覆盖成其它 16.x 小版本）；备份/恢复的 `pg_dump`/`pg_restore`
  行为依赖它。跨大版本升级（如 16→17）不在本手册范围：需 `pg_dumpall` 导出
  重建，确有必要时单独评估。

## 常见问题

**`make up` 后容器不健康 / smoke 一直等待**
`make ps` 看哪个容器 unhealthy，`make logs` 看报错。最常见是 8008 端口被占用
（换 `BOTVILLE_HTTP_PORT` 后 `make up`）或首次拉镜像太慢（见下条）。

**拉不动 Docker Hub 镜像**
极空间 Docker 设置里配置国内镜像加速器（registry mirror），或先在其他机器
`docker pull` 后 `docker save | docker load`。

**Element 点「注册」报错 / 注册不了**
默认注册关闭。按[开放注册的安全建议](#开放注册的安全建议)临时开放，或用
`register_new_matrix_user` 建号。

**忘记密码**
当前部署没有配置邮件服务，标准 API 的密码重置走不通；也不要用 `/_synapse/admin`。
用 `register_new_matrix_user` 注册新账号即可（数据在房间里，不在账号上）。
后续版本如配置 SMTP 再启用自助重置。

**浏览器版 Element（app.element.io）连不上 `http://<NAS_IP>:8008`**
浏览器禁止 HTTPS 页面调用 HTTP 接口（混合内容），且 compose 默认只绑定
`127.0.0.1`。用 Element 桌面版 + SSH 隧道，或上 HTTPS 反代。

**想换 `BOTVILLE_SERVER_NAME`**
用户 ID 里带 server_name，改了等于全新服务器，没有平滑迁移。真要换：
备份留档 → `make down` → `docker compose ... --profile <hs> --profile gateway down -v` 清卷（带 gateway profile 才会删 `gateway-data`；否则再 `docker volume rm -f <项目名>_gateway-data`）→ 改名重来。

**恢复后 synapse 起不来 / 数据库连接失败**
确认恢复流程完整（`make restore` 日志里 `/health OK`）；若是在旧环境上恢复更早的
备份，`.env` 已被备份副本覆盖，密码会自动对齐（脚本里有 ALTER USER 兜底），
仍异常时 `make logs` 看 postgres/synapse 报错。

**如何完全卸载**
**先把 `backups/` 目录迁出到安全位置**（里面有全部备份和 `e2e-state.json`，
目录一删就全没了），再 `make down`，执行 `docker compose -f
server/compose/docker-compose.yml --env-file server/compose/.env --profile synapse
--profile gateway down -v --remove-orphans`（删除数据卷；带 `--profile gateway` 才会
连 `gateway-data` 一起删），再用 `docker volume ls` 确认没有 `<项目名>_gateway-data`
残留（有就 `docker volume rm -f <项目名>_gateway-data`，否则 moments 数据库会留在盘上），最后删掉
`<NAS_DATA_DIR>/botville` 目录。

**备份目录能直接删吗**
能，但先确认不再需要（删掉前建议先做一次恢复演练）。`backups/e2e-state.json`
里含 e2e 测试账号密码，同样按敏感文件对待。
