<!-- SPDX-License-Identifier: Apache-2.0 -->

# C2.7 拉群与群管理

依据 [网关契约 §2.4](../../gateway/docs/contract.md)、[OpenAPI](../../gateway/openapi/gateway.v1.yaml)、[#232 同步清单](https://github.com/zjyjcl/botville/pull/232) 与 [#235（B2.8d2）](https://github.com/zjyjcl/botville/pull/235)。本次仅覆盖 #40 的多 AI 拉群及群管理，不涵盖 AI 关系审批、撤销等其余验收。

#232 清单 1 的既有版本断言调整已由上游 #234 处理；第二轮补充合成夹具假版本说明、OpenAPI info.version 的可变缩进解析及严格唯一性测试，不新增硬编码的真实契约版本断言。

## 使用路径

- 「我的 AI → 某个 AI」读取 owner 详情，仅当响应包含 `groups` 时显示群设置与群聊入口；对方资料页不显示。
- 「允许被拉进群」和「谁可以拉它进群」保存完整的 `groups` 对象，失败恢复上次服务端值；不发送 revision、If-Match 或其他 persona 字段。
- 群列表默认活跃群，切换「显示已退出的群」重置分页。下拉刷新重新请求第一页，加载更多原样传递 `next_cursor`，按 `room_ref` 去重。PATCH 成功后若还需加载更多，先刷新第一页以重新取得更新后排序的游标；写入期间暂停筛选、刷新和其他提交。
- 每行默认用「群聊 1、2…」及可选加入时间展示；序号仅为本次列表位置。网关声明 `room_lookup` 时，能通过 lookup 映射到本机已知房间的行改显示本地群名（见下文「群名映射」），映射不到的保持序号。绝不向用户显示不透明引用或把它当作 Matrix room ID。
- 安静模式仅在 accepted/encrypted_disabled 且模式已知时可改。群功能开启时可以切换 muted / mention_only；关闭时只有单向「设为安静」，已 muted 仅显示状态，同时保留「退出群聊」。控制器在关闭或 persona 设置尚未确认时拒绝 mention_only，不发请求。开启安静模式后被 @ 也不回复，仍会记住群里的内容。
- AI 退群需红色 destructive 二次确认，明确不可撤销、不会继续发言或自动加入。成功使用响应原地更新，活跃视图暂保留「已退出」，下次刷新再移除。
- `left` 始终显示「已退出」；`leave_pending=true` 只加弱提示「正在同步」。该字段不是持久状态，后续列表不承诺提供。未知 state/mode 显示未知提示并禁止操作。
- 「新建会话 → 群聊」可多选已有 joined 私聊中的对方及我的 AI，同时保留手填 MXID。对私聊读取完整成员快照，仅恰好自己与另一位已加入成员时列为候选。按完整 server_name（包括端口）过滤跨服 AI；联系人按 MXID 去重，至少邀请一位，支持一次邀请多个 AI。
- 选择 AI 后提示邀请开关要求，复用 EncryptionPolicy 禁止开启加密；owner 列表已知的 AI 即使未命中部署命名规则也不能开加密。创建成功不代表 AI 已接受邀请，仍依赖网关与 Matrix 同步结果。
- 群房间「聊天信息」展示真实 Matrix 已加入成员及 AI 标记，复用邀请成员和消息免打扰设置，并提供用户本人退出群的二次确认。私聊继续使用既有房间信息。

## 门控及错误

契约 0.7.0（#244）起部署级能力 `groups.manage` 决定入口形态（见下文）；能力缺失（旧网关）时保持原行为，由 owner `groups` 字段控制入口与设置是否出现。部署 `groups_enabled=false` **或** owner `groups.enabled=false` 即视为群功能关闭：仍显示现有列表与分页，顶部显示普通提示「群聊功能已关闭，AI 不会在群里发言」，不当作故障。关闭时内置 AI 即使被 @ 或回复也不发言，不自动退群。设置页与群列表共享 owner 设置控制器，关闭邀请开关后立即联动，保存失败会回滚；重载列表同时重取 owner 详情，详情失败时保留可读列表但禁止恢复 mention_only。

所有页面复用 `GatewayClientError.errorDescription` 的脱敏错误映射，只根据 problem `code` 与 HTTP status 分支；解析器不读取或保留 `title`，页面也不比较 title，不显示服务端原始 detail：

| 返回 | 行为 |
| --- | --- |
| 401 | 既有 OpenID 换令牌一次的策略；会话失效提示重新登录。重复 invalid-token 沿用服务器认证异常提示。其他状态不触发重新登录。 |
| 403（含无 problem body） | 显示「没有权限查看」，提供重试；不切到登录页。 |
| 404 persona.not_found | 「不存在或不可见」。 |
| 404 room.not_found | 可能是密钥轮换使旧引用失效：先清理旧引用及分页，从第一页刷新，并重新读取 persona 详情以更新群开关；群列表刷新成功时提示「群状态已变化，已刷新」，失败显示刷新错误（列表为空）。 |
| 409 room.state_conflict | 从第一页刷新并重新读取 persona 详情；新页到达前保留旧列表。群列表刷新成功时提示「群状态已变化，已刷新」；刷新失败时继续显示旧列表并给出刷新错误，下一次加载更多改为从第一页重新加载，不沿用冲突前的游标。 |
| 503 room.unavailable / persona.unavailable | 服务暂时不可用，可手动重试。 |
| 纯 503 / 无 code 或空 code 的 503 problem | 「网关请求失败，请稍后重试。」；不映射为 room.unavailable。 |
| 409 persona.state_conflict | 回滚设置，提示刷新重试。 |
| 400 / 405 request.invalid（含 Allow）/ 其他错误 | 共享通用失败提示；正常 rooms 调用不会触发 405。 |
| 403 conversation.e2ee | 「加密群不支持 AI 发言」，不重试。 |

404/409 触发的刷新里，群列表和 persona 群设置分别判断成败：群列表刷新成功、群设置刷新失败时，提示「群状态已变化，已刷新；群设置未能刷新：<共享错误文案>」，列表照常更新，群设置重新加载成功前不显示「安静模式」开关，只保留单向「设为安静」（已安静时显示「已设为安静」），不会发出切回 mention_only 的请求；群列表刷新失败时只显示群列表的刷新错误。

GET 保持现有有限退避重试；三个新 PATCH 均不携带 Idempotency-Key，遇 500 只发送一次。OpenID 路由严格按方法、组件数及非空组件匹配，不接受形似名称或额外层级。persona ID 和 room_ref 分别编码为一个 path 组件；cursor 仅原样传回，不解析或构造。

## 群名映射与 groups.manage（#244，契约 0.7.0）

依据契约 §2.4.1 与「groups.manage（B2.8f）」。#244 的示例 manifest 与 App mock 逐字节一致（1.4.0 / 0.7.0），本 PR 未改动两者，版本断言继续从契约读取。

### 群名映射

- 进入某个 AI 的群列表并且首页加载成功后，若 `groups.manage.room_lookup == true`，从本机 Matrix 房间列表取非私聊房间，按网关同样的规则预过滤：严格 `!<localpart>:<server_name>`、localpart 仅 `[A-Za-z0-9._~=-]`、总长 ≤255、无空白/控制字符、服务器名与登录 MXID 的完整 server_name（含端口）忽略大小写相等；按「localpart 原样 + 服务器名小写」去重，保留首个原始字符串。任一不合规 ID 都会让整批 400，所以不发。
- 每批最多 50 个，超过即分批顺序 POST `/personas/{id}/rooms/lookup`（OpenID 鉴权，room_id 只在请求体，不进 URL）。回复必须与请求按下标对齐，否则整批丢弃；`null` 表示「没有这个群」，不生成映射。
- 映射为 `room_ref → 本地房间名`；名字为空或等于 room_id 时不用。行标题有群名就显示群名，否则保持「群聊 N」。
- 群名只是装饰：首页一到即可操作，lookup 期间不锁行；任一批失败（含 429 与 memory 共用的每分钟读配额、503、未对齐）静默停止后续批次，保留已得到的映射，不显示错误。
- 每次整页重载（含 404 room.not_found 触发的轮换刷新）先清空旧映射再用本地已知 room_id 重查；409 保留旧列表的刷新同样重查。加载更多不重查（映射与分页无关）。
- 不提供反查或发现房间：只查本机已知房间；owner 不在的群继续显示序号。

### groups.manage 显示规则

判断在 Core 的 `GroupsManageEntryState`，有单测覆盖。

| 能力状态 | 入口 | mention_only | 群聊设置 | lookup |
| --- | --- | --- | --- | --- |
| enabled | 正常显示 | 与 persona 开关、房间 state 共同决定 | 可进入 | 按 room_lookup |
| degraded，或 unavailable_reason=groups_disabled | 置灰显示并附说明「群聊功能已关闭，只能设为安静或退出群聊」，仍可进入 | 不提供 | 置灰不可进入 | 按 room_lookup |
| disabled（module_disabled 等） | 不显示 | — | — | 不调用 |
| 其他未知 status | 不显示（同未知能力默认 hidden） | — | — | 不调用 |
| 能力缺失（网关 < 0.7.0）或清单未加载 | 保持旧行为 | 旧规则 | 旧规则 | 不调用 |

degraded 选「置灰」而非隐藏：参照微信对暂不可用功能保留灰色条目并说明原因的做法，让用户知道群聊被关闭而不是消失；同时契约要求 degraded 仍可安静/退出，隐藏会让已在群里的 AI 无法被设为安静或退出。群聊设置置灰是因为部署关闭期间改 persona 开关不生效，避免误导。`groups.manage` 不出现在动态功能卡片列表（与 memory.manage 一样只在「我的 AI」里）。

## 依赖 #235（B2.8d2）

依赖 #235（B2.8d2）的网关行为，#235 已合入 main。未升级到 #235 的旧网关在关闭时 GET 返回空页、PATCH mention_only 可能成功，App 行为仍安全：本地关闭判定后不提供也不发送 mention_only，仍可管理网关实际返回的房间。部署升级后关闭时会恢复显示现有房间，无需修改 App。开启时仍按 #232 允许 accepted/encrypted_disabled 在两种模式间切换。

#235 的 C 线同步清单逐项对应：

1. `POST /policy/evaluate` 的 404 conversation.not_found / 400 request.invalid：当前 App 没有该调用入口；共享传输只对 GET/带幂等键写的 500 internal.error 做有限重试，不把这两个 4xx 当作服务端故障重试。未来接入仍需按不可重试会话错误处理。
2. rooms 不支持的方法返回 owner 鉴权后的 405 request.invalid + Allow：正常 GET/PATCH 不触发，沿用通用文案。
3. rooms title 可为 Room request rejected 或任意其他值：只按 code 判断；测试同时验证看似匹配的 title 配其他 code 不会触发刷新。
4. 关闭时内置群 AI 完全不发言、不自动退群：顶部使用非错误提示；DM 不受影响。
5. 关闭时 GET 仍列出：列表、筛选、分页保留，合并部署开关与共享 persona 开关决定可用操作。
6. 关闭时 PATCH 仅 muted / leave：隐藏双向切换，控制器也拒绝 mention_only；409 冲突刷新列表及 persona。
7. 外部运行时 POST /messages 不受群开关限制：App 未引入该调用或额外禁令。

#234 审查 nit：a 合成 manifest 使用注明用途的假版本；b 心情不可用文案统一句号；c info.version 解析及测试改为块内直接子级、可变缩进、引号可选且严格唯一；d 群管理、新建会话和心情的展示决策现由 view state 测试覆盖，并有展示组件初始化类型守卫；渲染层未测（见 [视图测试结构](view-testing.md)）；e mood.unavailable 的 OpenAPI 枚举归 B 线，本轮不修改 gateway/。

## 需要总管拿主意

1. ~~功能清单没有群 capability~~：0.7.0 已发布 `groups.manage`，见上文显示规则。
2. room_ref 仍不能反查 Matrix 房间；0.7.0 lookup 只能把本机已知房间映射成名字，owner 不在的群仍是序号；Matrix 聊天信息仍不能显示或修改 AI 群模式。
3. leave_pending 仅属于本次 leave 响应，刷新后不保留确认提示；joined_rooms 查询失败可能无法入清理队列，不能保证后台恢复。因此 UI 不承诺一定自动退出 Matrix 成员资格，也不对 left 提供自动重复提交。
4. PATCH 没有幂等键/版本条件，500 后结果可能不确定；不自动重试，用户可刷新核对，设置失败按要求回滚本地显示。
5. 分页无快照，更新会移动记录；客户端去重并允许刷新，不声称一次遍历取得原子快照。
6. #232 清单 7：App 当前通过 Matrix 发文本，尚无 Gateway 发言预检/发送入口；共享错误支持 conversation.e2ee。group.replies_per_hour.exceeded / min_interval.too_soon 是 HTTP 200 blocked 策略原因，当前没有对应 Gateway policy 响应 UI，本次不伪造发送成功/失败或另建链路，待接入该链路时沿用通用 blocked 展示。

## 验证边界

单元测试覆盖模型、分页、错误、鉴权路径、PATCH 编码/重试、设置回滚、mock 契约样例一致性与选择器。Release 隔离检查和 iOS 模拟器/macOS Debug、Release 构建验证编译及隔离。本任务不操作手机、真实群或生产网关；真人邀请、AI 实际接受及真实退群仍待部署后的端到端验收。
