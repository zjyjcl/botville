# docs/compat/ — 服务器兼容性文档索引

botville 与外部组件（Matrix 服务器、推送网关等）的兼容性核实与实测记录。结论先行：botville 运行时**只依赖标准 Client-Server / Application Service / Push Gateway 接口**，任何一家服务器的私有 API 都不进入产品代码路径。

| 文档 | 内容 | 状态 |
|---|---|---|
| [homeserver-compat.md](homeserver-compat.md) | A0.1《Synapse 与 Tuwunel/continuwuity 兼容性验证报告》：分支现状（Conduit 停更、conduwuit 2025-05 归档、两个活跃继任分支）、标准接口清单与三家支持状态、差异与风险、对 A0.2 双服务器 CI 的约束与 smoke 接口列表 | 桌面核实完成（2026-11-04）；接口行为待 A0.2 CI 实测回填 |

## 规划中的后续文档

- `ci-smoke-results.md`（A0.2）：双服务器 CI smoke 的实测结果，逐条回填 homeserver-compat.md §2/§4.2 的状态列。
- 组件兼容性文档按需增加（Sygnal/APNs 的部署与验收状态见 [deploy/push-sygnal.md](../deploy/push-sygnal.md)：mock 链路 CI 已实测，真实 APNs 送达待真机 device token 验证（issue #4）；配对联邦实测归 A2.1）。

## 维护约定

- 本目录只写核实结论与实测记录，**不写任何真实密钥、令牌或个人部署信息**（示例一律 `<change-me>` 占位符）。
- 每份文档标注信息截止日期与来源；分支状态变化（归档、换仓库、改版本策略）时更新对应表格，不改历史结论而是加注日期。
