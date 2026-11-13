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
失败或无头像显示名字首字母/占位。网关契约的 `bio` 映射到 `AIProfile.signature`，
在名字下方用次要色展示，支持多行；null、缺失或去空白后为空时隐藏，不用聊天正文推断。
详情成功后采用网关的名字和签名，保留 Matrix 头像。公开视图的 mood 为
calm、happy、sad、excited、tired 时，在名字下显示带 SF Symbol 的次要心情标签，
以 #1AAD8E 点缀；缺失、null 及未知值按 nil 处理并隐藏标签。

「该 AI 的主人可以查看和删除它与你的聊天记忆」仅显示在对方的 AI 资料页。
仅本服 AI 通过已确认网关读取 capabilities 判定披露，不使用动态页示例或手动网关状态。
`memory.manage.owner_can_read_dm` 必须明确为 `true`，且 AI MXID 的 `server_name`
必须与当前登录用户 MXID 的 `server_name` 完全相同（保留端口及 IPv6 部分，
不做域名后缀、网关 URL host 或 homeserver 连接 URL 的猜测）。这是当前可得的本服
规范身份域；尚无可信别名清单，因此其他身份域一律按跨服/未知处理。
缺失、`false`、读取失败、跨服或无法确定本服时均不显示；会话切换后的旧结果丢弃。
主人 `PersonaMemoryView` 固定显示「私聊内容是否可见由服务端开关决定」。
#211 已将 `owner_can_read_dm` 的 schema 默认值改为 `false`，已在 main
`a7d5a7f` 的 `gateway/openapi/gateway.v1.yaml` 核对；App 缺失字段仍默认隐藏。
manifest 的 contract_version 以网关运行时契约常量为准，manifest_version 以契约示例为准；
App 测试从这些来源读取版本，校验 OpenAPI、示例、App mock 与 MockGatewayClient 的一致性。

## 集成边界

#215、#221 已合入（普通 merge main `23f1e23`）。列表和详情按当前契约对齐：
`PersonaSummary` 为包含 nullable bio 的十字段摘要，Matrix user ID 作为 id，
列表使用平铺的 `next_cursor` / `has_more`。App mock 的 persona-page、persona-state
与 capability manifest 均逐字节采用当前契约示例；真实请求不注入示例。

GET `/v1/personas/{id}` 返回 `anyOf`：有 `owner_id` 时严格解码完整主人摘要，
否则解码 id/display_name/bio/status/mood 公开视图，不把损坏的主人数据降级为公开视图。
对方资料页使用已确认配对网关读取详情；会话切换或取消后的结果不更新页面。
404 `persona.not_found` 显示「不存在或不可见」。跨服或本服身份域未知时同样按缺失处理，
不发详情请求且不显示加载错误；server_name 比较忽略大小写，但域和端口仍须精确相同，
不猜测别名。
没有可用配对网关或其他失败显示错误和重试。

GET 集合、恰好一个非空 id 的详情路径，以及恰好一个非空 id 的 `/state` GET/PATCH
路径使用 MatrixOpenID，移除站点 Bearer，
复用 invalid_token 最多刷新一次和 GET 的 500 重试策略。id 按独立路径段编码；
正常 query 不影响选择，query 里的 personas 不触发选择。personasx、空 id、额外子路径、
编码到资源名中的分隔符及其他子路径不纳入。编码到不透明 id 内的特殊字符仍属于同一段。
「我的 AI」403（含无 problem body）只显示「没有权限查看」，保留重试；401 才提示重新登录。
首次成功前和请求进行中显示加载态，仅请求成功且列表为空时显示「暂无 AI」；
失败清除旧行并显示错误及重试，不能当作成功空列表。

主人详情的心情块先读取 `GET /v1/personas/{id}/state`，显示当前心情、五个手动选项和
「对他人显示心情」开关；每次选择或开关发送契约字段 `mood` 或 `mood_public` 的 PATCH。
界面先乐观更新，失败回滚。有心情且 `mood_public == false` 时，主人页在心情徽章旁以
次要样式显示「仅自己可见」；对方资料页不显示这个标记。
GET/PATCH 错误复用 `GatewayClientError.errorDescription`：401 提示重新登录；403（含无
problem body）显示「没有权限查看」；`404 mood.disabled` 隐藏整个心情块且不提示错误，
其他 404 走现有映射（如 `persona.not_found` 为「不存在或不可见」）；
`503 mood.unavailable` 显示「心情暂时不可用。」，无 problem code 的纯 503 保留通用网关错误。
422/429 保留心情块本地固定文案：422 为「操作失败，请稍后重试」，429 为
「操作太频繁，请稍后再试。」。非 GatewayClientError（如网络 URLError）及无共享描述的
错误兜底为「操作失败，请稍后重试」。PATCH 没有 Idempotency-Key，因此 500 不重试。

契约示例随 main 的 YAML 同步验证；不得为通过检查修改运行时行为。
`test_example_contract_version_matches_runtime_contract` 保留硬断言，merge 冲突未恢复 skipUnless。
#221 的 JSON examples 仅更新 `health.json`，App 没有 health mock，因此不新增资源。
现有 manifest/card/persona-page/persona-state 均由 `MockContractConsistencyTests` 逐字节校验。

契约的 `proactive` 仅由 `Persona`、`CreatePersonaRequest`、`UpdatePersonaRequest`
引用；`PersonaSummary`、`PersonaOwnerDetail`、`PersonaPublicView`、`PersonaState` 均不包含它。
App 共用的 `MemoryPersona` 可选解码完整 Persona 的 `ProactiveSettings`；proactive 缺失或
null 为 nil，缺 enabled 为 false。max_per_day、require_reply、quiet_hours 保留可选性，
由服务端应用省略字段的默认值；静默时段 start/end/timezone 按 SilentHours 必需，enabled
缺失按 true。未知字段忽略；既有 mood 的未知值和 null 仍为缺失，不回退默认心情。
App 尚无创建/更新人设请求模型，本轮只补配置模型、解码与测试，没有主动发言 UI 或发送接线。

AI 资料页仅将 join 计入活跃成员；invite 不作 AI 对方，也不增加活跃人数。
详情请求的跨服判断按大小写不敏感的完整 server_name 比较，端口保留且域名仍精确；
披露仍遵守现有策略，别名/多域名按跨服隐藏披露。
加密安全策略里的 join/invite 检查仍保留：它防止给已邀请的 AI 开启不兼容加密，
安全判断采用保守口径，因此与资料页活跃人数展示的口径不同。
DEBUG 未读快照和安全状态合并索引对重复房间 ID 均取数组中最后值。
全仓其余 `uniqueKeysWithValues` 仅在 GatewayTests 的固定 manifest 样本断言中，
当前键唯一；保留测试的重复键失败语义，不用于 SDK/网络运行时输入。

本轮验收范围是单元测试、示例校验、Release 隔离和无签名的 iOS 模拟器/macOS
Debug、Release 构建，不包含真机、APNs、生产网关或实际聊天数据验收。

心情 owner/对方页共用 `PersonaMoodPresentation` 的两个单一构造入口，
`PersonaMoodBadge` 仅接受 presentation。私有标记等展示决策由 view state 测试覆盖，
并有组件初始化类型守卫；渲染层未测，详见 [视图测试结构](view-testing.md)。
