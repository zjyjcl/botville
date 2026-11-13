# C2.4 我的 AI 与记忆管理

动态页在功能清单中存在可用的 `memory.manage` 原生能力、路由为
`botville://memories` 且依赖可用时显示「我的 AI」。角色列表来自契约
`GET /v1/personas`；示例模式使用 `MockGatewayClient`，不会发送网络请求。

选择 AI 后可浏览记忆、加载更多、打开详情、确认删除、固定或取消固定。
脱敏条目显示「内容不可见」，未固定时只允许删除，已固定时允许删除和取消固定；
列表滑动、菜单、详情页与执行入口共用纯函数操作策略，均不允许新固定脱敏内容。summary 不提供固定操作，
删除确认明确说明仅重置摘要，不遗忘原始聊天。未知层级只显示元数据且禁止写操作。

`/v1/memories` 及其子路径复用 Matrix OpenID provider，不发送站点 Bearer。
`auth.invalid_token` 最多刷新并重试一次；符合现有问题响应格式的
`500 internal.error` 只对 GET 或携带非空 Idempotency-Key 的请求以 1/2/4 秒重试。
没有 key 的删除、固定、取消固定和写事实不做 500 重放；503 和 429 不自动重试。
E6 写事实只有客户端接口与测试，本轮没有编辑页面。

## AI 资料与披露

已加入的 AI 私聊可从「房间信息」进入「AI 资料」，同时保留消息免打扰设置。
读取完整 Matrix 成员列表，仅在活跃成员恰为本人和唯一对方、对方命中现有
`AIParticipantPolicy` 规则时显示入口；默认规则识别任意服务器的 `@ai_` 账号，
部署覆盖规则继续生效。群聊、真人、成员不完整或身份不明确时不猜测 AI。

名字与头像来自 Matrix 成员资料；头像通过已登录 SDK 的认证媒体通道读取，
失败或无头像显示名字首字母/占位。当前 SDK 成员资料及网关 persona 契约没有签名字段，
因此本轮不显示签名，不把房间主题、聊天正文或假 persona 当作签名。

「该 AI 的主人可以查看和删除它与你的聊天记忆」仅显示在对方的 AI 资料页。
只读取当前已确认配对网关的 capabilities，不使用动态页示例/手动网关状态。
`memory.manage.owner_can_read_dm` 必须明确为 `true`，且 AI MXID 的 `server_name`
必须与当前登录用户 MXID 的 `server_name` 完全相同（保留端口及 IPv6 部分，
不做域名后缀、网关 URL host 或 homeserver 连接 URL 的猜测）。这是当前可得的本服
规范身份域；尚无可信别名清单，因此其他身份域一律按跨服/未知处理。
缺失、`false`、读取失败、跨服或无法确定本服时均不显示；会话切换后的旧结果丢弃。
主人 `PersonaMemoryView` 固定显示「私聊内容是否可见由服务端开关决定」。
#211 已将 `owner_can_read_dm` 的 schema 默认值改为 `false`，已在 main
`a7d5a7f` 的 `gateway/openapi/gateway.v1.yaml` 核对；App 缺失字段仍默认隐藏。
新增能力使 manifest 从 1.2.0 升到 1.3.0（minor），契约版本为 0.2.0。

## 集成边界

GET `/v1/personas` 按 #215 HEAD `1d4fc06a41870420ae9cbdc2b01ad0bde421e1ab`
的 `PersonaSummary` / `PersonaPage` 和 `examples/persona-page.json` 对齐：
九字段摘要，Matrix user ID 作为 id，平铺的 `next_cursor` / `has_more`。
App Mock 资源逐字节采用该示例，Mock 记忆同步使用对应 persona id；真实请求不注入示例。

只把 GET 集合精确路径加入 MatrixOpenID，移除站点 Bearer，复用令牌刷新、
invalid_token 最多重试一次，以及 GET 或带幂等键写入的 500 重试策略。
正常分页 query 不影响选择；其他路径 query 中的 personas、personasx、编码分隔符、
`/personas/{id}` 子路径及非 GET 操作不纳入。#215 仅实现 GET 集合，其他操作保持 404。
403 `persona.not_owned` 在「我的 AI」展示中文权限错误和重试入口，成功空列表展示「暂无 AI」。
#215 尚未合入时，真实列表仍依赖网关合入并部署该实现；App 不推断或绕过所有权。

AI 资料页仅将 join 计入活跃成员；invite 不作 AI 对方，也不增加活跃人数。
跨服判断刻意按完整 server_name 精确比较，别名/多域名也按跨服隐藏披露。
加密安全策略里的 join/invite 检查仍保留：它防止给已邀请的 AI 开启不兼容加密，
不是资料页的活跃人数判定，不在本次 nit 中放宽。
DEBUG 未读快照和安全状态合并索引对重复房间 ID 均取数组中最后值。
全仓其余 `uniqueKeysWithValues` 仅在 GatewayTests 的固定 manifest 样本断言中，
当前键唯一；保留测试的重复键失败语义，不用于 SDK/网络运行时输入。

本轮验收范围是单元测试、示例校验、Release 隔离和无签名的 iOS 模拟器/macOS
Debug、Release 构建，不包含真机、APNs、生产网关或实际聊天数据验收。
