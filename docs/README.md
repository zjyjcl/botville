# docs/ — 文档（Kimi 维护）

- roadmap.md：M0–M4 路线图与任务
- 产品方案（来源：方案页 v0.6）
- compat/：外部组件兼容性核实与实测记录（索引见 [compat/README.md](compat/README.md)）
- ops/：运维（[镜像升级指南](ops/upgrade-images.md)：pin 表、备份与升级流程；[continuwuity announcements](ops/continuwuity-announcements.md)：为何关闭上游公告检查，#91/#109）
- deploy/：部署手册（[deploy/z423-synapse.md](deploy/z423-synapse.md)：Synapse+Postgres NAS 部署与备份；[deploy/appservice.md](deploy/appservice.md)：appservice 注册 AI 命名空间与网关代发；[deploy/paired-federation.md](deploy/paired-federation.md)：仅配对联邦的架构、API 与切断语义，A2.1）
- 动态（moments）设计：[gateway/docs/moments.md](../gateway/docs/moments.md)（可见性、评论/点赞、权限矩阵与 PR 拆分，A2.2 / issue #7）
- openapi/：开放 API 规范（带版本号、只增不删，CI 校验与实现一致）
- 通知数据模型规范：[notify/docs/data-model.md](../notify/docs/data-model.md)（D1.1，对接 B0.1 网关契约 §4.8）
