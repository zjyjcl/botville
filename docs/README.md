# docs/ — 文档（Kimi 维护）

- roadmap.md：M0–M4 路线图与任务
- 产品方案：外部方案页 v0.6 不在仓库内；仓库内的落地拆解见 [roadmap.md](roadmap.md)（其来源标注为产品方案 v0.6）
- compat/：外部组件兼容性核实与实测记录（索引见 [compat/README.md](compat/README.md)）
- ops/：运维（[镜像升级指南](ops/upgrade-images.md)：pin 表、备份与升级流程；[continuwuity announcements](ops/continuwuity-announcements.md)：为何关闭上游公告检查，#91/#109）
- deploy/：部署手册（[deploy/z423-synapse.md](deploy/z423-synapse.md)：Synapse+Postgres NAS 部署与备份；[deploy/appservice.md](deploy/appservice.md)：appservice 注册 AI 命名空间与网关代发；[deploy/paired-federation.md](deploy/paired-federation.md)：仅配对联邦的架构、API 与切断语义，A2.1；[deploy/push-sygnal.md](deploy/push-sygnal.md)：Sygnal 推送网关部署、APNs 配置与密钥入库守卫，A1.2/#4）
- 动态（moments）设计：[gateway/docs/moments.md](../gateway/docs/moments.md)（可见性、评论/点赞、权限矩阵与 PR 拆分，A2.2 / issue #7）
- contracts/：跨端契约——[pairing-qr.md](contracts/pairing-qr.md)：App 扫码配对 v1（URI、兑换与注册/登录，含 #132 `fp` SPKI pin；无 `gw` 配对为 chat-only、`features_url` 可选，#128/#132 Option A）；[pairing-redeem.openapi.yaml](contracts/pairing-redeem.openapi.yaml)：兑换接口 OpenAPI；示例见 [contracts/examples/pairing-qr/](contracts/examples/pairing-qr/)——该目录是 App fixture（`app-apple/Tests/BotvilleCoreTests/Fixtures/pairing-qr-contract/`）的单一来源，由 `scripts/sync-pairing-qr-fixtures.sh` 同步（除 README.md 外逐字节一致，README 不同步以免相对链接在 App 侧失效）并经 CI 守卫
- design/：手机端低保真草图与交互流程（静态 HTML/PNG 与设计规范，索引见 [design/README.md](design/README.md)）
- migration/：中枢功能盘点（[D0.1-hub-inventory.md](migration/D0.1-hub-inventory.md)）与搁置归档（[migration/deferred.md](migration/deferred.md)）
- OpenAPI 规范按域放在各模块目录：[gateway/openapi/gateway.v1.yaml](../gateway/openapi/gateway.v1.yaml)（网关契约，OpenAPI 3.1，示例在 [gateway/openapi/examples/](../gateway/openapi/examples/)，版本规则见 [gateway/docs/contract.md](../gateway/docs/contract.md)）、[notify/openapi/notify.v1.yaml](../notify/openapi/notify.v1.yaml)（通知域 schema，引用网关 `Card`，示例在 [notify/openapi/examples/](../notify/openapi/examples/)）。CI lint job（[.github/workflows/ci.yml](../.github/workflows/ci.yml)）运行 `gateway/scripts/validate_openapi.py` 与 `notify/scripts/validate_examples.py` 校验规范文档结构与示例；不含规范与实现一致性的检查
- 通知数据模型规范：[notify/docs/data-model.md](../notify/docs/data-model.md)（D1.1，对接 B0.1 网关契约 §4.8）
