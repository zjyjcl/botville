# C2.4 我的 AI 与记忆管理

动态页在功能清单中存在可用的 `memory.manage` 原生能力、路由为
`botville://memories` 且依赖可用时显示「我的 AI」。角色列表来自契约
`GET /v1/personas`；示例模式使用 `MockGatewayClient`，不会发送网络请求。

选择 AI 后可浏览记忆、加载更多、打开详情、确认删除、固定或取消固定。
脱敏条目显示「内容不可见」，仍允许删除和取消固定。summary 不提供固定操作，
删除确认明确说明仅重置摘要，不遗忘原始聊天。未知层级只显示元数据且禁止写操作。

`/v1/memories` 及其子路径复用 Matrix OpenID provider，不发送站点 Bearer。
`auth.invalid_token` 最多刷新并重试一次；符合现有问题响应格式的
`500 internal.error` 只对 GET 或携带非空 Idempotency-Key 的请求以 1/2/4 秒重试。
没有 key 的删除、固定、取消固定和写事实不做 500 重放；503 和 429 不自动重试。
E6 写事实只有客户端接口与测试，本轮没有编辑页面。

AI 信息处的披露文案仅在 `memory.manage.owner_can_read_dm` 明确为 `true` 时显示。
缺失或 `false` 都不显示。当前 schema 的 `default: true` 与 App 的默认隐藏存在差异：
**待网关线开关落地后对齐**。本轮不修改 schema；示例和 App mock 都显式为 `false`。
新增能力使 manifest 从 1.2.0 升到 1.3.0（minor），契约版本为 0.2.0。

## 集成边界

基线 #209 已提供主人记忆端点，但当前 `http_server.py` 尚未实现契约中的
`/personas` 路由。契约规定默认仅返回自己拥有的角色，`owner_id` 仅对本服所有者生效；
客户端不传 owner_id，仍沿用普通网关凭据选路。这里保留契约客户端和错误提示；
线上 AI 列表、终端用户发现自己角色的
完整路径仍需网关后续提供。所有权最终由记忆端点校验，App 不推断或绕过权限。

本轮验收范围是单元测试、示例校验、Release 隔离和无签名的 iOS 模拟器/macOS
Debug、Release 构建，不包含真机、APNs、生产网关或实际聊天数据验收。
