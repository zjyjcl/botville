<!-- SPDX-License-Identifier: Apache-2.0 -->

# C2.7 拉群与群管理

依据 [网关契约 §2.4](../../gateway/docs/contract.md)、[OpenAPI](../../gateway/openapi/gateway.v1.yaml) 与 [#232 同步清单](https://github.com/zjyjcl/botville/pull/232)。本次仅覆盖 #40 的多 AI 拉群及群管理，不涵盖 AI 关系审批、撤销等其余验收。

## 使用路径

- 「我的 AI → 某个 AI」读取 owner 详情，仅当响应包含 `groups` 时显示群设置与群聊入口；对方资料页不显示。
- 「允许被拉进群」和「谁可以拉它进群」保存完整的 `groups` 对象，失败恢复上次服务端值；不发送 revision、If-Match 或其他 persona 字段。
- 群列表默认活跃群，切换「显示已退出的群」重置分页。下拉刷新重新请求第一页，加载更多原样传递 `next_cursor`，按 `room_ref` 去重。PATCH 成功后若还需加载更多，先刷新第一页以重新取得更新后排序的游标；写入期间暂停筛选、刷新和其他提交。
- 每行用「群聊 1、2…」及可选加入时间展示；序号仅为本次列表位置。绝不向用户显示不透明引用或把它当作 Matrix room ID。
- 安静模式仅在 accepted/encrypted_disabled 且模式已知时可改。开启后被 @ 也不回复，仍会记住群里的内容。
- AI 退群需红色 destructive 二次确认，明确不可撤销、不会继续发言或自动加入。成功使用响应原地更新，活跃视图暂保留「已退出」，下次刷新再移除。
- `left` 始终显示「已退出」；`leave_pending=true` 只加弱提示「正在同步」。该字段不是持久状态，后续列表不承诺提供。未知 state/mode 显示未知提示并禁止操作。
- 「新建会话 → 群聊」可多选已有 joined 私聊中的对方及我的 AI，同时保留手填 MXID。对私聊读取完整成员快照，仅恰好自己与另一位已加入成员时列为候选。按完整 server_name（包括端口）过滤跨服 AI；联系人按 MXID 去重，至少邀请一位，支持一次邀请多个 AI。
- 选择 AI 后提示邀请开关要求，复用 EncryptionPolicy 禁止开启加密；owner 列表已知的 AI 即使未命中部署命名规则也不能开加密。创建成功不代表 AI 已接受邀请，仍依赖网关与 Matrix 同步结果。
- 群房间「聊天信息」展示真实 Matrix 已加入成员及 AI 标记，复用邀请成员和消息免打扰设置，并提供用户本人退出群的二次确认。私聊继续使用既有房间信息。

## 门控及错误

没有已发布的群/rooms capability ID；不新增 ID，不改变 mock manifest。owner `groups` 字段控制入口与设置是否出现；列表响应 `groups_enabled=false` 时隐藏列表，显示「部署未开启群聊功能」。部署开关与 persona 邀请开关是两层独立条件。

所有页面复用 `GatewayClientError.errorDescription` 的脱敏错误映射，不显示服务端原始 detail：

| 返回 | 行为 |
| --- | --- |
| 401 | 既有 OpenID 换令牌一次的策略；会话失效提示重新登录。重复 invalid-token 沿用服务器认证异常提示。其他状态不触发重新登录。 |
| 403（含无 problem body） | 显示「没有权限查看」，提供重试；不切到登录页。 |
| 404 persona.not_found | 「不存在或不可见」。 |
| 404 room.not_found / 409 room.state_conflict | 清理旧引用及分页，从第一页刷新；成功提示群状态已变化，失败显示刷新错误。 |
| 503 room.unavailable / persona.unavailable | 服务暂时不可用，可手动重试。 |
| 409 persona.state_conflict | 回滚设置，提示刷新重试。 |
| 400 / 其他错误 | 共享通用失败提示。 |
| 403 conversation.e2ee | 「加密群不支持 AI 发言」，不重试。 |

GET 保持现有有限退避重试；三个新 PATCH 均不携带 Idempotency-Key，遇 500 只发送一次。OpenID 路由严格按方法、组件数及非空组件匹配，不接受形似名称或额外层级。persona ID 和 room_ref 分别编码为一个 path 组件；cursor 仅原样传回，不解析或构造。

## 需要总管拿主意

1. 功能清单没有群 capability，当前按响应字段门控；如需部署前隐藏独立入口，需先补正式契约。
2. room_ref 不能映射 Matrix 房间，且没有群名/成员信息：owner 列表仅序号；Matrix 聊天信息不能显示或修改 AI 群模式。
3. leave_pending 仅属于本次 leave 响应，刷新后不保留确认提示；joined_rooms 查询失败可能无法入清理队列，不能保证后台恢复。因此 UI 不承诺一定自动退出 Matrix 成员资格，也不对 left 提供自动重复提交。
4. PATCH 没有幂等键/版本条件，500 后结果可能不确定；不自动重试，用户可刷新核对，设置失败按要求回滚本地显示。
5. 分页无快照，更新会移动记录；客户端去重并允许刷新，不声称一次遍历取得原子快照。
6. #232 清单 1 的既有版本断言调整由独立 #234 负责，本分支不改那些行，也不依赖其改动。新测试不硬编码契约版本。
7. #232 清单 7：App 当前通过 Matrix 发文本，尚无 Gateway 发言预检/发送入口；共享错误支持 conversation.e2ee。group.replies_per_hour.exceeded / min_interval.too_soon 是 HTTP 200 blocked 策略原因，当前没有对应 Gateway policy 响应 UI，本次不伪造发送成功/失败或另建链路，待接入该链路时沿用通用 blocked 展示。

## 验证边界

单元测试覆盖模型、分页、错误、鉴权路径、PATCH 编码/重试、设置回滚、mock 契约样例一致性与选择器。Release 隔离检查和 iOS 模拟器/macOS Debug、Release 构建验证编译及隔离。本任务不操作手机、真实群或生产网关；真人邀请、AI 实际接受及真实退群仍待部署后的端到端验收。
