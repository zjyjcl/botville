<!-- SPDX-License-Identifier: Apache-2.0 -->

# 加密策略（C2.6）

关联 [C2.6 / #39](https://github.com/zjyjcl/botville/issues/39)、[C11-08 / C11-09](c1.1-element-findings.md#5-待办清单本任务验收产物)。

## 用户操作

- 房间列表的“新建会话”可选择私聊或群聊，输入完整 Matrix 用户 ID（MXID）；私聊限一位对方，群聊可输入多个 MXID。
- 所有新会话默认不加密。确认全部为真人后，可主动开启端到端加密；创建前必须再次确认不可关闭、AI 无法读取消息，以及设备验证和备份风险。更改建房草稿会撤销原确认。
- 已有未加密房间在成员确认全部为真人后提供“开启端到端加密”，同样需要二次确认；服务层在提交前重新读取 SDK 加密状态及全部有效成员（包括尚未接受邀请的成员）。权限不足、离线等失败不会伪装成功。
- 含 AI、成员不完整、成员加载失败或 AI 判定未配置时，不提供开启加密。用户仍可创建未加密会话。
- 已加密房间没有关闭入口。加密房邀请 AI 或无法确定身份的成员会先提示“AI 无法读取加密消息”，明确确认后才能邀请。开启加密只保护后续消息，不会把过去的明文历史重新加密。
- 列表和房间内均使用文字及图标标记“已加密”“未加密”“未加密 · 含 AI”；成员不全、加密状态未知会明确展示待确认状态。标识来自 SDK 房间状态，不根据用户勾选或房间名字推断。

## AI 判定来源与配置

统一入口为 `AIParticipantPolicy`，不以显示名、人数、`isDirect` 或“机器人”头像判断 AI。

当前仓库的 [网关 OpenAPI](../../gateway/openapi/gateway.v1.yaml) 提供 `Actor.kind = human | ai` 和 `Conversation.participants`、`Conversation.encryption = none | e2ee`。策略可接收已知 AI 的 MXID，明确 AI 身份优先。苹果端目前直接读取 Matrix SDK 成员，尚未接入网关 Actor 数据。

[A1.3](../../docs/roadmap.md) 还没有落地 appservice registration / 用户命名空间正则；`server/` 和网关契约没有可直接复用的 AI MXID 前缀。因此配置默认是**空数组占位**，不编造生产命名规则。未配置时，即使成员已加载也按可能含 AI 处理，开启加密入口禁用；部署者必须先按本部署完整 AI 命名规则配置，A1.3 完成后再替换为其权威命名空间。

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

这只是**测试占位示例**，不是 A1.3 已采用的规则。环境变量优先于同名 Info.plist；格式错误、无效正则或没有有效规则均保守处理，不静默忽略错误后认定真人。配置包含的规则被视为部署的完整 AI 命名空间，错误或不完整配置会影响判定，联邦成员也必须纳入配置。重新启动 App 后生效。没有真实凭据或密钥配置项。

成员只计当前加入和待邀请状态，已离开/被踢成员不阻止开启；SDK service member 若没有明确 AI 命中也不能当作已确认真人。离线成员缓存必须与 SDK 的有效成员数量一致，否则保持待确认。在线刷新及执行开启操作使用 SDK `members()` 完整迭代结果，并比对迭代器原始长度，防止转换丢失成员。标准 `/sync` 摘要人数可能为零，不能把这个零当作完整成员查询失败。客户端提交前的检查不能与其他客户端并发邀请形成服务端原子事务，A1.3/网关若需全局强制该策略，仍应增加服务端约束。

## SDK 行为与设备提示

固定 matrix-rust-sdk Swift 组件版本为 `26.10.27`。建房显式传 `CreateRoomParameters.isEncrypted`；已有房间调用 `Room.enableEncryption()`，由 SDK 发送 `m.room.encryption` 状态事件。状态读取使用 `RoomInfo.encryptionState` / `latestEncryptionState()`，保留 `unknown`，不会把它当作未加密。已观察到的加密状态在当前会话中不会降级。

加密房内读取 `verificationState()`、`backupState()`、`recoveryState()`：未验证会提示验证设备；恢复功能未设置/不完整会提示换设备可能看不到历史；未知或正在更新的状态分别提示，不能宣称备份不存在。可通过房间内刷新按钮重新读取。当前仅提示，不创建备份、不导出密钥，也不提供 SAS/二维码验证或恢复流程。

完整设备验证、密钥备份/恢复、跨设备历史及提醒频率由 [C11-09 / #97](https://github.com/zjyjcl/botville/issues/97) 跟进验收。加密状态回读与编译通过不代表两设备真实加密收发或历史恢复已验收。

规范参考：[Matrix m.room.encryption](https://spec.matrix.org/v1.16/client-server-api/#mroomencryption)。

## 验证范围

本次验证结果在 PR 和 `app-apple/README.md` 中记录。单测覆盖 AI 判定及配置错误、默认明文、二次确认、草稿变更、不可关闭、文字/图标映射、SDK 成员完整性与设备提示，以及服务层对确认后成员变化的处理。

仍需后续完成实际 iOS/macOS 界面、两个设备、Rust homeserver 及完整恢复流程验收；不把本地单元测试或模拟器无签名编译当作这些验收结果。
