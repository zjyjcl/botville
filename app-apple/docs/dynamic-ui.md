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
| `POST /capabilities/{id}/session-tokens` | 请求短时网页会话，校验 audience、有效期、一次性 code 与同源 URL，并收窄导航白名单。 |
| `POST /capabilities/{id}/session-cookies` | HTTP 客户端已实现免 Bearer 兑换并要求 204；只返回状态，不向调用方返回 cookie 值。生产网页应自行兑换。 |

模拟实现加载与 OpenAPI examples 逐字节一致的 capability manifest 和 card，运行时生成合成的一次性 code，并刷新卡片时间。动态页可在未登录 Matrix 时浏览 mock；Debug 启动参数 `-dynamic-ui-mock` 可直接进入动态页。设置页可切换 mock 或 HTTP 来源，并配置 HTTP base URL（不提供凭据输入）。HTTP 客户端已实现上述列表、action 和 session 请求及响应校验；动态界面服务端尚未实现，不能据此把 HTTP 路径视为端到端可用。请求版本从 `Bundle` 读取，语言使用系统首选语言。

“查看日志”显式标为只读低风险，避免继承审批卡片的 high。`notify.approvals` 保留 `card_list` 首页入口，同时在 `entry.web_url` 声明同源日志网页并启用 `session_token`；点击日志链接可建立该能力的短时会话并显示离线部署日志。App mock 与 `gateway/openapi/examples/{capability-manifest,card}.json` 同步修改，由一致性测试防止漂移。

## 前向兼容与降级

响应中的枚举按开放枚举处理，单个未知字段或值不得使整份清单、整张卡片解码失败。

- 未知 capability ID 的 `web` 和 `card_list` 入口照常显示；`unknown_capability` 规则只适用于无法路由的 `native` 入口。未知 `entry.type` 按 `none`，不显示入口。
- 未知 capability 状态按 disabled，未知模块归入“其他”，未知图标不显示图标。
- capability 数组逐项解码，畸形条目跳过并保留其他有效能力；时间字段兼容带小数秒的 ISO 8601 UTC 值。
- 卡片结构 major 高于客户端支持版本时仅显示 `fallback_text`；minor 更高时继续渲染已知部分，未知 block 跳过并附带 `fallback_text`。未知 card type 同样仅显示 `fallback_text`。
- 未知 action kind 隐藏；未知 style 用 secondary；未知 priority 按 normal；未知 status 禁用操作但仍显示卡片；未知 risk 一律按 high。

高风险操作的 Secure Enclave 签名、challenge 获取、Face ID 与二次确认由 C3.1 完成。本轮只有 `HighRiskAuthorizer` 预留边界，因此高风险或 risk 未知时不会调用网关操作，而会提示“需在后续版本授权”。

提交 approval 卡片操作必须有 `revision`；approval 或有效风险为 high 的卡片必须有 `expires_at`，否则保留内容并禁用所有操作（包括低风险 deny）。这是有意的 fail-closed，符合最新 [`gateway/docs/contract.md` §4.6 第 9 条](../../gateway/docs/contract.md#46-校验规则服务端必须做)；通用字段表中的 optional 只表示所有卡片不统一必填。

## 模块网页会话

网页会话遵循一次性 code 换 HttpOnly cookie 的契约：App 请求 session token，使用网关返回且已含 code 的 URL 载入网页；网页向 `/capabilities/{id}/session-cookies` 兑换 cookie。cookie 必须带 `HttpOnly`、`Secure` 与 `SameSite` 属性，且 code 与 cookie 值不得写日志、持久化到 localStorage，或暴露给脚本。

内置浏览器只放行网关 grant 与本地白名单的交集。当前本地边界固定为请求 URL 的 origin；调用方白名单与网关 grant 均只能收窄，不能增加其他 origin。未提供白名单或传空数组时仍使用这个单一 origin。比较按规范化 origin 执行，包含 scheme、主机和有效端口，避免大小写、子域、端口或编码形式绕过。非白名单的合法 HTTPS 链接提示改在外部浏览器打开；HTTP、mailto、自定义 scheme、含 userinfo 或编码主机等无效 URL 直接阻止。网页通过可信主 frame 的 `botville.session.renew` bridge 申请续期，App 重新走 session token 流程。HTTP 默认只接受 HTTPS；仅 Debug 可显式放行 loopback HTTP 以支持本地开发。

mock 先在 `MockGatewayClient` 消耗一次性 code，再用 `MockModuleWebCookieExchange` 生成合成 cookie，写入 non-persistent `WKHTTPCookieStore` 并加载离线 HTML。这验证了 code 单次使用与 WebKit cookie 存储边界，**不等于真实网关 `Set-Cookie → WebKit` 链路验证**，也没有真实 cookie 或生产认证。

## 未完成项

- 以本次 rebase 的 main `ccbc507` 为准，[网关 HTTP 路由](../../gateway/src/botville_gateway/http_server.py) 已实现 `GET /health`（别名 `GET /v1/health`，也接受 HEAD）、`POST /v1/policy/evaluate`、`POST /v1/conversations/{conversation_id}/messages`。capabilities、cards、action 和 session token/cookie 尚无服务端路由，生产认证与动态界面端到端联调待后续完成。
- HTTP 客户端尚未实现单卡 `GET /cards/{id}`，也未提供 `card_schema_max` 覆盖协商。
- capability manifest 的上次成功结果只保留在当前 App 进程；ETag 协商、磁盘缓存和离线刷新策略尚未实现。
- C3.1 负责真实的 Secure Enclave 签名、challenge、Face ID、二次确认、重放防护与授权回执；本轮没有实现签名或保存密钥。
- mock 数据用于开发验证，并非真实账号、令牌或 cookie。

## 验证

2026-11-04，在上述 main 基线上复验；构建缓存和本轮日志位于本 worktree 的 `app-apple/.build/`。

- `swift build`：通过。
- `swift test`（macOS）：153 项，151 项通过、2 项 Synapse 环境门控集成测试跳过、0 失败。覆盖真实 HTTP 请求的 grant 白名单收窄、高风险/biometric 不发请求、UI 共用动作与导航策略、capability 逐项容错、小数秒时间以及 mock 与契约示例的一致性。
- “查看日志”集成测试通过：从打包 mock 选择 low-risk 动作，建立会话，验证 code 单次使用，把合成 HttpOnly/Secure cookie 写入 non-persistent `WKHTTPCookieStore`，再由实际 `WKWebView` 加载离线日志页并读取标题。
- iOS Simulator Debug 无签名构建：`BUILD SUCCEEDED`，产物含 arm64 + x86_64，`CODE_SIGNING_ALLOWED=NO`、`CODE_SIGNING_REQUIRED=NO`。本轮未重跑模拟器 UI 点按、真机或最低支持系统验收。
- 共享契约示例校验：`python3 gateway/scripts/validate_openapi.py` 的 36 项检查通过。Matrix Rust SDK 的既有 macOS 部署版本链接警告、iOS 缺失上游调试对象警告仍存在，未阻断构建。
