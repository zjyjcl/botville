# 镜像升级指南（默认 pin tag + digest）

| 项 | 内容 |
|---|---|
| 任务 | issue #69 —— Compose 服务镜像统一 pin tag + digest |
| 适用范围 | `server/compose/docker-compose.yml` 与 `server/compose/.env.example` 中的全部镜像（Postgres、Synapse、Tuwunel、continuwuity，以及 push profile 的 Sygnal） |
| 信息截止 | 2026-11-04（当前 pin 表当日核实，均为多架构 index digest；Sygnal 行见下方备注） |
| 相关文档 | [compat/homeserver-compat.md](../compat/homeserver-compat.md) §1.2 / §3.9 第 5 条 / §4.3 第 3 条；Sygnal 部署见 [deploy/push-sygnal.md](../deploy/push-sygnal.md) |

## 1. 口径：默认 pin，可选跟 latest

- **默认 pin**：上述服务的镜像默认写成 `tag@sha256:<digest>`，digest 取**多架构
  index digest**（不是单平台 manifest digest），同一份配置在任何架构上拉到的都是
  同一组镜像。升级因此是显式动作：改配置 → CI 全绿 → PR，不随上游 re-tag 漂移。
- **可选跟 latest**：本地部署可在 `server/compose/.env` 把任一 `*_IMAGE` 覆盖为
  `:latest`（或其他引用）跟随上游。**不推荐**：`:latest` 会被上游随时 re-tag，
  行为不可复现，CI 也无法守护；仅适合临时试验，不要提交回仓库。
- **既有部署注意**：`server/compose/.env` 一旦存在，其中的 `*_IMAGE` 行会**覆盖**
  §2 表中的所有默认值——老部署从旧版本 `.env` 继承的 `:latest`/无 digest 值不会
  因为升级仓库而自动更新，迁移步骤见 §5；`init.sh` 检测到偏离时只打 WARNING，
  不改写。
- **CI 兜底**：`.github/workflows/ci.yml` 的 lint 有「镜像必须 pin」检查——
  `docker-compose.yml` 里每个 image 的默认值（`${VAR:-default}` 中的 default 或
  裸值）与 `.env.example` 里每个 `*_IMAGE` 的值都必须包含
  `:<tag>@sha256:<64 位 hex>`，且 tag 不是 `latest`；两处同名变量的值必须完全
  相等，否则失败。

## 2. 当前 pin 表（2026-11-04，均为多架构 index digest）

| 服务 | 镜像引用 |
|---|---|
| postgres | `docker.io/library/postgres:16-alpine@sha256:0cefaabd882e42bcb2a2f3dc56d3ce4770560ba62fd7f197f1a2e008fd5731ab` |
| synapse | `docker.io/matrixdotorg/synapse:v1.164.1@sha256:fa4aa980f279bcf26d4e7ecc2a2c9a701c73f2ebe8841da9bdcf3b2fdf693d75` |
| tuwunel | `ghcr.io/matrix-construct/tuwunel:v1.9.4@sha256:0e86c6164d6c9f5b60291938eb61ad8395021c37fe58d47a963ea7edddbedaec` |
| continuwuity | `ghcr.io/continuwuity/continuwuity:v26.9.1@sha256:c9c62bc5c0a641f3f713945c701b069c7bc30d2612b54d4a5de80a5be19d5420` |
| sygnal（push profile，A1.2） | `docker.io/matrixdotorg/sygnal:v0.17.0@sha256:141f5e72fdf99a28e29ca87f3a46c19ed1cf21d5074aa3ef9df7c067e0c1c46a` |

Sygnal 行备注（2026-11-04，A1.2 加入）：只有开 push profile（`make up PUSH=1`）
才会拉取；digest 拼错会在 CI `push-e2e` job 实际拉镜像时暴露（同 §1 的 CI 兜底
口径）。[compat](../compat/homeserver-compat.md) §2.3 有 Sygnal 的组件行（但不含
版本号），因此它的「同步处」仍是 §3 第 1/2/3/4 条共 4 处（第 5 条
homeserver-compat 的版本口径不适用）。

版本口径与 [compat/homeserver-compat.md](../compat/homeserver-compat.md) §1.2
一致（Synapse v1.164.1 即 matrix-docker-ansible-deploy defaults 当时停留的参考
版本；Tuwunel tag 一律带 `v` 前缀，如 v1.9.4——不带 `v` 的 tag 在 GHCR 上不存在）。

镜像源说明：Synapse 官方双源（Docker Hub / `ghcr.io/element-hq/synapse`，本仓库
默认用前者，两源同一构建 digest 相同）；Tuwunel 在 Docker Hub 另有
`docker.io/jevolk/tuwunel` 镜像；continuwuity 主源在自建 Forgejo registry，本仓库
默认用 GHCR 同步镜像（取舍见
[compat/homeserver-compat.md](../compat/homeserver-compat.md) §3.9 第 1 条）。

## 3. 升级步骤（新装 / 仓库默认值）

1. **查新版本**：先读 release notes 确认无已知回归（Synapse 侧可对照
   [compat](../compat/homeserver-compat.md) §1.2 / §5 记录的参考版本
   matrix-docker-ansible-deploy defaults）。
   - Synapse（双周更）：<https://github.com/element-hq/synapse/releases>
   - Tuwunel（约月更，tag 带 `v` 前缀）：<https://github.com/matrix-construct/tuwunel/releases>
   - continuwuity（日历版本，月更，tag 带 `v` 前缀）：其 Forgejo / GitHub releases
2. **取 index digest**（两种等价方式；注意取的是多架构 index digest，不是输出里
   各单平台 manifest 的 digest）：
   ```bash
   docker buildx imagetools inspect docker.io/matrixdotorg/synapse:v1.164.1
   # 输出第一段 "Digest: sha256:..." 即 index digest
   ```
   无 Docker 时用 registry API（匿名 pull 都要先换 token）。Docker Hub：
   ```bash
   token="$(curl -fsS "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/postgres:pull" \
     | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')"
   curl -fsSI -H "Authorization: Bearer ${token}" \
     -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json' \
     https://registry-1.docker.io/v2/library/postgres/manifests/16-alpine \
     | awk -F': ' 'tolower($1)=="docker-content-digest" {print $2}' | tr -d '\r'
   ```
   GHCR（注意 `service=` 与 auth/registry 三处都要换成 ghcr.io，token 域是 `ghcr.io`
   而不是 registry-1）：
   ```bash
   token="$(curl -fsS "https://ghcr.io/token?service=ghcr.io&scope=repository:matrix-construct/tuwunel:pull" \
     | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')"
   curl -fsSI -H "Authorization: Bearer ${token}" \
     -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json' \
     https://ghcr.io/v2/matrix-construct/tuwunel/manifests/v1.9.4 \
     | awk -F': ' 'tolower($1)=="docker-content-digest" {print $2}' | tr -d '\r'
   ```
   取回的 digest 应为 64 位小写 hex；与 tag 页面/变更公告核对后再用。
3. **同步以下全部 5 处**（同一 pin 的值必须逐字一致，缺一不可；顺序即建议的
   修改顺序）：
   1. `server/compose/docker-compose.yml`：对应服务的 `${*_IMAGE:-默认值}` 默认值；
   2. `server/compose/.env.example`：对应 `*_IMAGE=` 行；
   3. `.github/workflows/ci.yml`：`.env.example placeholders only` 检查里
      allowlist 字典的 `*_IMAGE` 值；
   4. 本文档 §2 pin 表（含核实日期）；
   5. `docs/compat/homeserver-compat.md` §3.9 第 5 条括号里的「当前验证版本」。
   CI 的 pin guard 会断言 compose 默认值与 `.env.example` 同名变量相等，但其余
   副本仍靠本清单人工同步——改漏任何一处都会造成口径漂移。
4. **本地自检**：先 `make init`（生成 `server/compose/.env` 与
   `generated/` 下的渲染物——`push-denylist.env` 等 compose 依赖的 env 文件；
   未跑过 init 的 checkout 上 `--env-file` 会直接报错），再三个 profile 各过一遍
   `docker compose -f server/compose/docker-compose.yml --env-file
   server/compose/.env --profile <hs> config --quiet`；有疑虑再跑
   `make smoke`（见 `server/README.md`）。
5. **CI 全绿后提 PR**：lint 的「镜像必须 pin」「compose 默认值 = `.env.example`」
   与 allowlist 检查会兜底；smoke 会实际 pull 每个 profile 的全部镜像并走完
   注册→发消息→回读链路（digest 拼错会在 smoke 暴露；三个 profile 均为
   必过）。

## 4. 升级前备份（所有 profile 通用）

先 `make down HS=<hs>`（`synapse` / `tuwunel` / `continuwuity`；不带 `-v`，
数据卷保留），再按本节备份，**确认备份可读后才升级**。

**备份落点**：统一写到专用目录 `BACKUP_ROOT`，默认仓库内 `./backups/`
（`.gitignore` 已含 `backups/` 与 `*.tar.gz`，不会误提交——数据卷里是全部
私有消息/媒体，绝不能进 git）。

- **不要把 `BACKUP_ROOT` 指向 NAS 既有目录**（或任何有其他数据的目录）：备份
  只应新增自己的文件，写入他人目录可能污染既有数据；确需归档到 NAS 时，先落
  本地并校验，再把整个 `.tar.gz` / `.dump` 作为新文件复制过去。
- 备份目录与产物按私有数据处理：

```bash
BACKUP_ROOT="${BACKUP_ROOT:-./backups}"
STAMP="$(date +%F)"
mkdir -p "${BACKUP_ROOT}"
chmod 700 "${BACKUP_ROOT}"
umask 077
```

**helper 镜像**：打包卷统一复用 `.env` 里**已 pin 的 postgres 镜像**做 `tar`
（alpine 基底、自带 tar，且是本地已有镜像）——不再引入 `alpine:latest` 这类
浮动 tag，与「no :latest」口径一致。Tuwunel / continuwuity 自身镜像 FROM
scratch（无 shell），不能用来打包自己的卷。

```bash
# 复用当前 pin 的 postgres 镜像作为 tar helper（不要换成浮动 tag）
PG_IMAGE="$(sed -n 's/^POSTGRES_IMAGE=//p' server/compose/.env)"
```

**Synapse（profile = synapse）**：数据库用 `pg_dump -Fc`，媒体/密钥卷用 tar：

```bash
# 1) 数据库（-Fc 自定义格式，可用 pg_restore 校验/恢复）
docker compose -f server/compose/docker-compose.yml --env-file server/compose/.env \
  --profile synapse exec -T postgres \
  pg_dump -U botville -Fc synapse > "${BACKUP_ROOT}/synapse-db-${STAMP}.dump"

# 2) synapse-data 卷（媒体、签名密钥等）
docker run --rm --entrypoint tar \
  -v botville_synapse-data:/data:ro -v "${BACKUP_ROOT}":/backup \
  "${PG_IMAGE}" czf "/backup/synapse-data-${STAMP}.tar.gz" -C /data .
```

**Tuwunel / continuwuity（RocksDB 数据目录整卷冷备）**：

```bash
docker run --rm --entrypoint tar \
  -v botville_tuwunel-data:/data:ro -v "${BACKUP_ROOT}":/backup \
  "${PG_IMAGE}" czf "/backup/tuwunel-data-${STAMP}.tar.gz" -C /data .

docker run --rm --entrypoint tar \
  -v botville_continuwuity-data:/data:ro -v "${BACKUP_ROOT}":/backup \
  "${PG_IMAGE}" czf "/backup/continuwuity-data-${STAMP}.tar.gz" -C /data .
```

（`postgres-data` 卷同理可整卷 tar，但 Synapse 的库请优先用上面的 `pg_dump`，
恢复更灵活。）

**校验备份可读**（不校验的备份不算备份）：

```bash
tar tzf "${BACKUP_ROOT}/synapse-data-${STAMP}.tar.gz" | head
tar tzf "${BACKUP_ROOT}/tuwunel-data-${STAMP}.tar.gz" | head
docker run --rm --entrypoint pg_restore -v "${BACKUP_ROOT}":/backup:ro \
  "${PG_IMAGE}" -l "/backup/synapse-db-${STAMP}.dump" | head
```

说明：Linux 上容器内打包/校验产生的文件属主可能是 root，属正常；`BACKUP_ROOT`
已 `chmod 700`，目录本身不可被其他用户进入。macOS（Docker Desktop）无此问题。

## 5. 既有部署：`.env` 里的 `*_IMAGE` 覆盖

问题：`init.sh` 对已存在的 `.env` 明确「keeping current values」，`Makefile`
又显式 `--env-file server/compose/.env`。因此旧部署升级到新 pin 后，**`.env`
里的旧 `*_IMAGE` 值继续生效**——例如从旧版本 `.env.example` 继承的
`TUWUNEL_IMAGE=…:latest`、无 digest 的 `SYNAPSE_IMAGE=…:v1.141.0` 会原样沿用，
仓库新默认值对这些实例不生效（CI 每次从干净 `.env` 起步，测不到这一点）。
`server/scripts/init.sh` 现会在 `.env` 的 `*_IMAGE` 与 `.env.example` 默认不同
（尤其含 `:latest` 或无 `@sha256:`）时打印 WARNING，但**不会自动改写**——按
下面步骤手动迁移：

```bash
# 0) 停服（数据卷保留）；按 §4 备份并校验通过后，再继续下面的步骤：
make down HS=<hs>            # <hs> = synapse | tuwunel | continuwuity

# 1) 处理 .env 里的 *_IMAGE 覆盖（二选一）
#    a) 推荐：删除全部 *_IMAGE 行，回落到 compose 默认（即 §2 pin 表）
sed -i.bak -E '/^[A-Za-z_][A-Za-z0-9_]*_IMAGE=/d' server/compose/.env
#    b) 或只改写某一项为 §2 的新 pin（| 作分隔符，避免转义镜像名里的 / 与 :）
sed -i.bak 's|^SYNAPSE_IMAGE=.*|SYNAPSE_IMAGE=docker.io/matrixdotorg/synapse:v1.164.1@sha256:fa4aa980f279bcf26d4e7ecc2a2c9a701c73f2ebe8841da9bdcf3b2fdf693d75|' server/compose/.env
#    sed -i.bak 在旁边留 server/compose/.env.bak（已被 .gitignore 的 .env.* 覆盖，
#    含密钥，确认无误后可自行删除）。

# 2) 拉新镜像并起服
docker compose -f server/compose/docker-compose.yml --env-file server/compose/.env \
  --profile <hs> pull
make up HS=<hs>
```

**切换风险（务必先备份）**：从 `:latest`（或旧 tag）切到 pin 可能跨过多个上游
版本，Tuwunel / continuwuity 首次启动会执行 **RocksDB 迁移且不可回滚**（见 §6），
Synapse 大跨度升级也可能跑 schema 迁移。没有 §4 的备份并通过校验之前，不要执行
第 1 步。

## 6. 风险与注意

- **RocksDB 迁移不可回滚**：Tuwunel / continuwuity 升级后的首次启动会执行
  RocksDB 迁移，迁移后的数据目录通常无法再被旧版本读取——**回滚旧镜像一般不可
  行**。Compose 里两家的 `stop_grace_period: 10m` 就是为了迁移期间不被强杀；
  升级后首启不要中途停机或回退。
- **Postgres 跨大版本**不能只换镜像：16 → 17 需要 `pg_upgrade` 或 dump/restore；
  同大版本内的补丁更新可直接改 pin。
- **digest 必须是 index digest**：误用单平台 manifest digest 会导致其他架构拉取
  失败；以 `docker buildx imagetools inspect` 的平台列表与第一段 Digest 为准。
