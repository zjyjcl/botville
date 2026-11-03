<!-- SPDX-License-Identifier: Apache-2.0 -->

# 加密策略（C2.6）

关联 [C2.6 / #39](https://github.com/zjyjcl/botville/issues/39)、[C11-08 / C11-09](c1.1-element-findings.md#5-待办清单本任务验收产物)。

## 用户操作

- 房间列表的“新建会话”可选择私聊或群聊，输入完整 Matrix 用户 ID（MXID）；私聊限一位对方，群聊可输入多个 MXID。
- 所有新会话默认不加密。成员通过部署 AI 规则检查后，可主动开启端到端加密；创建前必须再次确认本客户端不提供关闭入口，以及设备验证和备份风险。更改建房草稿会撤销原确认；开启后把成员改为 AI 或无法确认的身份，会复位为不加密并提示，用户仍可创建明文会话。
- 已有未加密房间在成员通过策略检查后提供“开启端到端加密”，同样需要二次确认；服务层在提交前重新读取 SDK 加密状态及全部有效成员（包括尚未接受邀请的成员）。权限不足、离线等失败不会伪装成功。
- 含 AI、成员不完整、成员加载失败或 AI 判定未配置时，不提供开启加密。用户仍可创建未加密会话。
- botville 的 AI 接入目前不支持加密会话。已加密或加密状态未知的房间直接拒绝 AI／身份未确认的邀请，没有确认后放行的接口。邀请前也回读房内完整成员；不能确认房内成员符合策略时拒绝继续邀请。本客户端不提供关闭加密入口；开启加密只作用于后续消息，不会把过去的明文历史重新加密。
- 列表和房间内均使用文字及图标标记“已加密”“未加密”“未加密 · 含 AI”；成员不全、加密状态未知会明确展示待确认状态。标识来自 SDK 房间状态，不根据用户勾选或房间名字推断。

## AI 判定来源与配置

统一入口为 `AIParticipantPolicy`，不以显示名、人数、`isDirect` 或“机器人”头像判断 AI。

当前仓库的 [网关 OpenAPI](../../gateway/openapi/gateway.v1.yaml) 提供 `Actor.kind = human | ai` 和 `Conversation.participants`、`Conversation.encryption = none | e2ee`。苹果端目前直接读取 Matrix SDK 成员，尚未接入网关 Actor 数据；已移除未接线的 `knownAIUserIDs` 参数，不暗示有额外身份来源。

2026-11-04 修订时已用 `gh pr view 99` 核对：[A1.3 / #99](https://github.com/zjyjcl/botville/pull/99) 的 `@ai_*` 命名空间仍未合入 `main`。配置继续默认**空数组占位**，不提前采用待合入规则。未配置时，即使成员已加载也按可能含 AI 处理，开启加密入口禁用；部署者必须先按本部署完整 AI 命名规则配置，#99 合入后再更新默认规则和测试。

可通过环境变量（JSON 字符串数组）或 App `Info.plist` 的同名 String 数组配置：

| 配置键 | 作用 |
| --- | --- |
| `BOTVILLE_AI_USER_ID_PATTERNS` | 正则表达式列表；建议完整锚定 MXID 并限定服务器 |
| `BOTVILLE_AI_USER_ID_PREFIXES` | MXID 前缀列表；必须覆盖部署内全部 AI 账号 |

例如本地演示部署约定 AI 名称以 `demo_ai_` 开头时，可在 Xcode Scheme 的环境变量中设置：

```text
BOTVILLE_AI_USER_ID_PATTERNS=["^@demo_ai_[^:]+:example\\.test$"]
BOTVILLE_AI_USER_ID_PREFIXES=[]
```

这只是**测试占位示例**，不是 A1.3 已采用的规则。环境变量优先于同名 Info.plist；格式错误、无效正则或没有有效规则均保守处理，并在界面展示配置提示。配置包含的规则被视为部署的完整 AI 命名空间，错误或不完整配置会影响判定，联邦成员也必须纳入配置。`humanOnly` 只表示完整成员列表未命中部署 AI 规则，**不是已证明账号属于真人**；未覆盖的 bot 账号可能被误判，没有身份认证保证。新建界面明确提示这一前提。重新启动 App 后生效。没有真实凭据或密钥配置项。

成员只计当前加入和待邀请状态，已离开/被踢成员不阻止开启；SDK service member 若没有明确 AI 命中也不能当作已确认真人。房间列表读取缓存，缓存必须与 SDK 的有效成员数量一致，否则保持待确认；不在每轮同步串行全量拉取所有房间成员。用户查看安全信息、开启或邀请时按需使用 SDK `members()` 完整迭代结果，并比对迭代器原始长度，防止转换丢失成员。`nil` 和空分块都表示遍历结束，但不跳过长度校验。标准 `/sync` 摘要人数可能为零，不能把这个零当作完整成员查询失败。

## 服务层与契约边界

私聊、群聊及建房 `invite` 列表共用 `MatrixService.createRoom`，开启和后续邀请分别通过 `enableEncryption`、`inviteUser`；UI 无法绕过这些检查，SDK Client 不公开暴露。创建时校验的是发起者和本次邀请列表；它不是创建后的永久成员证明。已有房间操作重新读取状态与成员，并串行保护本客户端同一房间的安全操作。

[网关契约](../../gateway/docs/contract.md#会话加密与-ai-参与)要求 `Conversation.encryption=e2ee` 时 `ai_participation=disabled`，网关不得代 AI 发言。它并不表示 AI 已被 Matrix 踢出房间或已获密钥被撤销。当前网关参考实现尚未接入 Conversation 登记和房间加密状态校验，本 PR 强制的是苹果端 `MatrixService` 入口。

其他客户端、直接 Matrix REST 调用或跨客户端并发邀请无法由 App 阻止；创建／开启前的快照校验不是 homeserver 原子约束。若外部客户端已造出“加密且含 AI”房间，本客户端保持已加密标识，拒绝继续邀请，不自动降级加密或声称 AI 已被移除。全局服务端执行和网关接入仍需后续实现。

## SDK 行为与设备提示

固定 matrix-rust-sdk Swift 组件版本为 `26.10.27`。建房显式传 `CreateRoomParameters.isEncrypted`；已有房间调用 `Room.enableEncryption()`，由 SDK 发送 `m.room.encryption` 状态事件。状态读取使用 `RoomInfo.encryptionState` / `latestEncryptionState()`，保留 `unknown`，不会把它当作未加密。已观察到的加密状态在当前会话中不会降级。

加密房内读取 `verificationState()`、`backupState()`、`recoveryState()`：未验证会提示验证设备；恢复功能未设置/不完整会提示换设备可能看不到历史；未知或正在更新的状态分别提示，不能宣称备份不存在。可通过房间内刷新按钮重新读取。当前仅提示，不创建备份、不导出密钥，也不提供 SAS/二维码验证或恢复流程。

当前未配置仅向可信设备分享密钥的策略，也未验证 AI 设备身份。房间内的未验证设备仍可能得到密钥；本客户端不保证 AI 无法读取消息，也不能用“未被信任”推断设备不能解密。拒绝 AI 参与是产品与服务入口策略，不是密码学隔离证明。

完整设备验证、密钥备份/恢复、跨设备历史及提醒频率由 [C11-09 / #97](https://github.com/zjyjcl/botville/issues/97) 跟进验收。加密状态回读与编译通过不代表两设备真实加密收发或历史恢复已验收。

规范参考：[Matrix m.room.encryption](https://spec.matrix.org/v1.16/client-server-api/#mroomencryption)。

## 验证范围

本次验证命令与结果见 [PR #98](https://github.com/zjyjcl/botville/pull/98)。单测覆盖 AI 判定及配置错误、默认明文、二次确认、草稿变更后加密选择复位、没有关闭入口、文字/图标映射、SDK 成员完整性与设备提示，以及直接服务调用的建房／邀请／开启限制。

本轮仅使用占位凭据与 fake SDK，排除真实 Keychain 往返和需要账号的 Synapse 集成。构建缓存及测试临时数据按任务边界留在本 worktree 的 `app-apple/.build/`，测试后清理临时数据；不改用工作树外的系统临时目录。

仍需后续完成实际 iOS/macOS 界面、两个设备、Rust homeserver 及完整恢复流程验收；不把本地单元测试或模拟器无签名编译当作这些验收结果。
