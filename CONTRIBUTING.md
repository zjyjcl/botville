# 贡献指南 / Contributing

## 分工 / Owners

| 负责者 | 范围 | 目录 |
|---|---|---|
| Grok Bot（AI微信总管） | 总协调、拆任务、排期、最终审核与合并 | 全仓库 |
| ZCode | A 线：服务端与部署 | `server/` |
| Claude Code | B 线：AI 网关与角色引擎 | `gateway/` |
| Codex | C 线：苹果端 App UI（SwiftUI、SpriteKit） | `app-apple/` |
| Kimi | D 线：通知与迁移、开放 API 文档、小镇网页版 | `notify/`、`docs/` |
| DeepSeek | 测试与代码审查、CI、密钥扫描 | 全仓库（测试） |

小镇（`town/`）按任务 ID 前缀归属：A4.x→ZCode，B4.x→Claude Code，C4.x→Codex，D4.x→Kimi，T4.x→DeepSeek。

## 分支与 PR / Branches & PRs

1. `main` 受保护，禁止直接推送。
2. 分支命名：`<owner>/<任务ID>-<简述>`，例如 `zcode/A1.1-synapse-deploy`、`claude/B2.1-quota`、`codex/C2.1-chat`、`kimi/D1.1-notify-schema`、`deepseek/T4.1-town-tests`。
3. 一个 PR 对应一个任务 ID（issue），标题格式 `[A1.1] 简述`，描述里写 `Closes #<issue>`、验收标准自查结果。
4. 合并流程：CI 全绿（含 Synapse 与 Conduit 系双服务器测试）→ DeepSeek 代码审查通过 → 总管审核后 squash 合并。
5. 跨线改动（改别人的目录或 OpenAPI 契约）需在 PR 中 @ 对应负责者并取得同意。
6. 开放 API 只增不删；破坏性变更需新版本号。

## 禁止提交密钥 / No secrets, ever

- 任何 token、API key、密码、证书、APNs `.p8`、签名密钥 **一律不得进入仓库、PR、issue、日志或截图**。
- 配置只用环境变量；示例写在 `.env.example`，只放 `<change-me>` 等占位符。本地 `.env` 与 `secrets/` 已被 `.gitignore` 忽略。
- CI 运行密钥扫描（如 gitleaks），命中即失败；误提交须立即轮换该密钥并清理历史，不能只删文件。
- 私有人设（个人助手角色等）与个人部署配置（如 iMessage 通知）不进本仓库。

## 许可 / License

提交即表示同意以 Apache-2.0 授权你的贡献。第三方素材必须兼容 Apache-2.0，并在 `NOTICE` 中写明来源与许可。
