# 镜像升级指南（默认 pin tag + digest）

| 项 | 内容 |
|---|---|
| 任务 | issue #69 —— Compose 四个服务镜像统一 pin tag + digest |
| 适用范围 | `server/compose/docker-compose.yml` 与 `server/compose/.env.example` 中的全部镜像（Postgres、Synapse、Tuwunel、continuwuity） |
| 信息截止 | 2026-11-04（当前 pin 表当日核实，均为多架构 index digest） |
| 相关文档 | [compat/homeserver-compat.md](../compat/homeserver-compat.md) §3.9 第 5 条 / §4.3 第 3 条 |

## 1. 口径：默认 pin，可选跟 latest

- **默认 pin**：四个服务的镜像默认写成 `tag@sha256:<digest>`，digest 取**多架构
  index digest**（不是单平台 manifest digest），同一份配置在任何架构上拉到的都是
  同一组镜像。升级因此是显式动作：改配置 → CI 全绿 → PR，不随上游 re-tag 漂移。
- **可选跟 latest**：本地部署可在 `server/compose/.env` 把任一 `*_IMAGE` 覆盖为
  `:latest`（或其他引用）跟随上游。**不推荐**：`:latest` 会被上游随时 re-tag，
  行为不可复现，CI 也无法守护；仅适合临时试验，不要提交回仓库。
- **CI 兜底**：`.github/workflows/ci.yml` 的 lint 有「镜像必须 pin」检查——
  `docker-compose.yml` 里每个 image 的默认值（`${VAR:-default}` 中的 default 或
  裸值）与 `.env.example` 里每个 `*_IMAGE` 的值都必须包含
  `:<tag>@sha256:<64 位 hex>`，且 tag 不是 `latest`，否则失败。

## 2. 当前 pin 表（2026-11-04，均为多架构 index digest）

| 服务 | 镜像引用 |
|---|---|
| postgres | `docker.io/library/postgres:16-alpine@sha256:0cefaabd882e42bcb2a2f3dc56d3ce4770560ba62fd7f197f1a2e008fd5731ab` |
| synapse | `docker.io/matrixdotorg/synapse:v1.141.0@sha256:562adbfc3df12d1024c7277b9807086ed79d8d5fabe24187a31d787af050a6a4` |
| tuwunel | `ghcr.io/matrix-construct/tuwunel:v1.9.4@sha256:0e86c6164d6c9f5b60291938eb61ad8395021c37fe58d47a963ea7edddbedaec` |
| continuwuity | `ghcr.io/continuwuity/continuwuity:v26.9.1@sha256:c9c62bc5c0a641f3f713945c701b069c7bc30d2612b54d4a5de80a5be19d5420` |

镜像源说明：Synapse 官方双源（Docker Hub / `ghcr.io/element-hq/synapse`，本仓库
默认用前者）；Tuwunel 在 Docker Hub 另有 `docker.io/jevolk/tuwunel` 镜像；
continuwuity 主源在自建 Forgejo registry，本仓库默认用 GHCR 同步镜像（取舍见
[compat/homeserver-compat.md](../compat/homeserver-compat.md) §3.9 第 1 条）。

## 3. 升级步骤

1. **查新版本**：先读 release notes 确认无已知回归（Synapse 侧可参考
   matrix-docker-ansible-deploy 当时停留的版本）。
   - Synapse（双周更）：<https://github.com/element-hq/synapse/releases>
   - Tuwunel（约月更）：<https://github.com/matrix-construct/tuwunel/releases>
   - continuwuity（日历版本，月更）：其 Forgejo / GitHub releases
2. **取 index digest**（两种等价方式；注意取的是多架构 index digest，不是输出里
   各单平台 manifest 的 digest）：
   ```bash
   docker buildx imagetools inspect docker.io/matrixdotorg/synapse:v1.141.0
   # 输出第一段 "Digest: sha256:..." 即 index digest
   ```
   无 Docker 时用 registry API（匿名 pull 都要先换 token）：
   ```bash
   # Docker Hub 示例；GHCR 把 auth/registry 换成 ghcr.io 即可
   # （scope=repository:<owner>/<name>:pull，匿名 token 同样适用）
   token="$(curl -fsS "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/postgres:pull" \
     | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')"
   curl -fsSI -H "Authorization: Bearer ${token}" \
     -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json' \
     https://registry-1.docker.io/v2/library/postgres/manifests/16-alpine \
     | awk -F': ' 'tolower($1)=="docker-content-digest" {print $2}' | tr -d '\r'
   ```
   取回的 digest 应为 64 位小写 hex；与 tag 页面/变更公告核对后再用。
3. **改两处**（值必须一致，缺一不可）：
   - `server/compose/docker-compose.yml`：对应服务的 `${*_IMAGE:-默认值}` 默认值；
   - `server/compose/.env.example`：对应 `*_IMAGE=` 行。
   同时同步两处衍生引用：`.github/workflows/ci.yml` 里 `.env.example` allowlist
   的 `*_IMAGE` 值，以及本文档第 2 节 pin 表（含核实日期）。
4. **本地自检**：三个 profile 各过一遍 `docker compose -f
   server/compose/docker-compose.yml --env-file server/compose/.env --profile
   <hs> config --quiet`；有疑虑再跑 `make smoke`（见 `server/README.md`）。
5. **CI 全绿后提 PR**：lint 的「镜像必须 pin」与 `.env.example` allowlist 检查会
   兜底，smoke 会实测新镜像能起、能走完注册→发消息→回读链路。

## 4. 风险与注意

- **RocksDB 迁移不可回滚**：Tuwunel / continuwuity 升级后的首次启动会执行
  RocksDB 迁移，迁移后的数据目录通常无法再被旧版本读取——**回滚旧镜像一般不可
  行**。Compose 里两家的 `stop_grace_period: 10m` 就是为了迁移期间不被强杀；
  升级后首启不要中途停机或回退。
- **升级前备份**：先 `make down`（保留数据卷），再备份数据卷，确认备份可读后才
  升级：
  ```bash
  # 卷名前缀 botville_ 来自 Compose 项目名；postgres-data 同理
  docker run --rm -v botville_tuwunel-data:/data -v "$PWD":/backup alpine \
    tar czf /backup/tuwunel-data-$(date +%F).tar.gz -C /data .
  ```
- **Postgres 跨大版本**不能只换镜像：16 → 17 需要 `pg_upgrade` 或 dump/restore；
  同大版本内的补丁更新可直接改 pin。
- **digest 必须是 index digest**：误用单平台 manifest digest 会导致其他架构拉取
  失败；以 `docker buildx imagetools inspect` 的平台列表与第一段 Digest 为准。
