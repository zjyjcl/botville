<!-- SPDX-License-Identifier: Apache-2.0 -->

# C2.5 动态界面（mock）

本页说明苹果端对网关 v1 动态界面契约的实现边界。C2.5 的目标是：网关增加网页模块或卡片内容时，App 无需为每个模块新增页面即可提供入口和可读的卡片；本轮数据来自随 App 打包的 mock 示例，尚未连接可用的生产网关。

## 契约映射

| 网关资源 | App 行为 |
| --- | --- |
| `GET /capabilities` | 读取 capability manifest，只显示 `enabled` 或 `degraded` 的可用入口，并检查 capability 依赖。`disabled`、`none` 入口和缺少依赖的能力不产生入口。 |
| `entry.native` | 仅在 App 能路由对应深链时显示；不能路由时按 manifest 的 `defaults.unknown_capability` 隐藏或以不可用状态显示。 |
| `entry.web` | 申请一次性 session token 后，在内置浏览器打开网关提供的 URL。App 不自行向 URL 拼接 code。 |
| `entry.card_list` | 显示契约卡片列表；页面按 `card_types` 限制展示类型。 |
| `GET /cards` | 声明客户端支持的 `X-Card-Schema-Version`，按 capability 和 card type 筛选卡片。 |
| `POST /cards/{card_id}/actions/{action_id}` | 普通操作交给客户端实现；有效风险为高的操作转交 C3.1 授权预留接口。 |

模拟实现加载与 OpenAPI examples 等价的 capability manifest、card 和 session token 数据。动态页可在未登录 Matrix 时浏览 mock；Debug 启动参数 `-dynamic-ui-mock` 可直接进入动态页。设置页可切换 mock 或 HTTP 来源，并配置 HTTP base URL（不提供凭据输入）。客户端也保留 HTTP 请求构造与 JSON 解码骨架；将客户端来源切换为 HTTP 并配置网关 base URL 后，才会向真实网关发起请求。真实网关尚未就绪，不能据此把 HTTP 路径视为端到端可用。

## 前向兼容与降级

响应中的枚举按开放枚举处理，单个未知字段或值不得使整份清单、整张卡片解码失败。

- 未知 capability ID 的 `web` 和 `card_list` 入口照常显示；`unknown_capability` 规则只适用于无法路由的 `native` 入口。未知 `entry.type` 按 `none`，不显示入口。
- 未知 capability 状态按 disabled，未知模块归入“其他”，未知图标不显示图标。
- 卡片结构 major 高于客户端支持版本时仅显示 `fallback_text`；minor 更高时继续渲染已知部分，未知 block 跳过并附带 `fallback_text`。未知 card type 同样仅显示 `fallback_text`。
- 未知 action kind 隐藏；未知 style 用 secondary；未知 priority 按 normal；未知 status 禁用操作但仍显示卡片；未知 risk 一律按 high。

高风险操作的 Secure Enclave 签名、challenge 获取、Face ID 与二次确认由 C3.1 完成。本轮只有 `HighRiskAuthorizer` 预留边界，因此高风险或 risk 未知时不会调用网关操作，而会提示“需在后续版本授权”。

## 模块网页会话

网页会话遵循一次性 code 换 HttpOnly cookie 的契约：App 请求 session token，使用网关返回且已含 code 的 URL 载入网页；网页向 `/capabilities/{id}/session-cookies` 兑换 cookie。cookie 必须带 `HttpOnly`、`Secure` 与 `SameSite` 属性，且 code 与 cookie 值不得写日志、持久化到 localStorage，或暴露给脚本。

内置浏览器仅放行 session token 的 `allowed_origins`。比较按规范化 origin 执行，包含 scheme、主机和有效端口，避免大小写、子域、端口或编码形式绕过；非白名单导航提示改在外部浏览器打开。网页通过 `botville.session.renew` bridge 申请续期，App 重新走 session token 流程。mock 流程使用 cookie store 写入接口演练兑换与写入，不保存敏感 cookie 值。HTTP 默认只接受 HTTPS；仅 Debug 可显式放行 loopback HTTP 以支持本地开发。

## 未完成项

- HTTP 客户端当前只实现 capability 与卡片列表请求；尚未实现单卡 `GET /cards/{id}`，也未提供 `card_schema_max` 覆盖协商。真实网关当前仅提供 `/health`，capabilities、cards、session token/cookie 和 action 的生产端实现及端到端验证待网关具备后完成。
- capability manifest 的上次成功结果只保留在当前 App 进程；ETag 协商、磁盘缓存和离线刷新策略尚未实现。
- C3.1 负责真实的 Secure Enclave 签名、challenge、Face ID、二次确认、重放防护与授权回执；本轮没有实现签名或保存密钥。
- mock 数据用于开发验证，并非真实账号、令牌或 cookie。

## 验证

- `swift build`：通过。
- `swift test`：82 项完成，81 项通过、0 项失败；1 项既有 Synapse 集成测试因环境门控跳过。动态界面覆盖包括 manifest 与卡片未知值、风险映射、HTTP 请求构造、一次性 code、会话过期、origin 白名单，以及 non-persistent `WKHTTPCookieStore` 的 cookie 写入与 `HttpOnly` 属性。
- iOS Simulator Debug 无签名构建：通过。模拟器已启动 mock 动态首页和 mock 模块网页，并完成截图确认：首页展示功能清单与卡片；模块网页在 `WKWebView` 中显示离线 mock 内容、续期按钮与站外链接提示入口。续期和站外链接的策略行为由单元测试覆盖。
