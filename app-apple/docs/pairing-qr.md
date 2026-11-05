<!-- SPDX-License-Identifier: Apache-2.0 -->

# 扫码配对

规格来源将是 **A 线 PR 中的 `docs/contracts/pairing-qr.md`**。本文按本任务提供的 v1 草案实现；A 线契约合并后须核对字段、错误状态和指纹作用域。这里不宣称已经与真实配对服务联调。

## URI 与确认

```text
botville://pair?v=1&hs=<https>&gw=<https，可选>&fp=sha256:<base64url，可选>&code=<base64url>&exp=<unix秒>
```

- scheme 为 `botville`，host 为 `pair`。同名参数重复时整体拒绝，未知参数忽略；未知参数重复也拒绝。
- 必须包含 `v`、`hs`、`code`、`exp`；非 v1 显示“请升级 App”。`hs`、`gw` 只接受严格校验的 HTTPS 地址，拒绝凭据、查询、片段、非法端口和混淆组件。
- `code` 为 32 字节的无填充 base64url；`fp` 是 `sha256:` 加 32 字节的无填充 base64url。指纹取叶证书 DER `SubjectPublicKeyInfo` 的 SHA-256，不能用完整证书摘要或裸公钥摘要代替。
- `exp` 为 Unix 秒，本地到期即拒绝；最终有效性以服务器为准。
- 登录页提供“扫码配对”。iOS 用 AVFoundation 摄像扫码并说明相机用途；macOS 用 Vision 读取所选图片中的二维码，也可粘贴链接。
- 解析成功后显示 homeserver、gateway、分组指纹和过期时间。只有用户点击确认才发送 redeem；解析、截图模式和确认页展示不发起 redeem。

## 兑换与认证

发送 `POST <gw 或 hs>/_botville/pair/redeem`，JSON body 为 `{"code":"…","device_display_name":"…"}`。不记录二维码、兑换码、密码或令牌；兑换码仅保留在配对流程内存中，不进入 UserDefaults、文件或 Keychain。

成功响应：

```json
{
  "v": 1,
  "homeserver": "https://matrix.example.invalid",
  "gateway": "https://gateway.example.invalid",
  "auth": {
    "type": "registration_token",
    "token": "fixture-only-not-a-real-token",
    "expires_in_ms": 300000
  },
  "features_url": "https://gateway.example.invalid/_botville/capabilities"
}
```

`gateway` 可为 null。返回 homeserver 必须与二维码中的 hs 一致，不能由兑换服务将登录凭证导向其他服务器。认证类型仅接受 `registration_token` 和 `login_token`。

`registration_token` 路径让用户选择用户名与密码，通过 Matrix `/register` 的 `m.login.registration_token` UIA（含服务端 session）完成注册。`login_token` 路径通过 `/login` 的 `m.login.token` 登录。实现遵循 [Matrix 客户端认证规范](https://spec.matrix.org/latest/client-server-api/#token-authenticated-registration)。获得的 Matrix 会话使用既有 Keychain 与 SDK 会话生命周期。

认证凭据按收到兑换响应的时间计算 `expires_in_ms`，在 SDK 初始化后及每次凭据请求前重新检查。支持 token-only 与 token + `m.login.dummy` UIA；需要 CAPTCHA、邮件或其他 UIA stage 的服务器暂不支持。Matrix 429 只重试收到限流的当前请求，保留 UIA session / stage，不重新消耗已完成的 token；等待受凭据有效期和既有认证总超时约束。兑换 429 在确认页显示倒计时，等待结束后可由用户重试。网络失败不自动重放一次性兑换。

| HTTP | errcode | 用户提示 |
| --- | --- | --- |
| 400 | `BV_PAIR_INVALID` | 配对码无效 |
| 410 | `BV_PAIR_EXPIRED` | 配对码已过期 |
| 410 | `BV_PAIR_REVOKED` | 配对码已撤销 |
| 409 | `BV_PAIR_USED` | 配对码已使用 |
| 429 | `BV_PAIR_RATE_LIMITED` | 请求过于频繁，并显示 `retry_after_ms` 换算的等待时间 |
| 任意 | 未知 errcode | 通用失败提示，不显示服务端 error 原文 |

## TLS 固定与边界

有 `fp` 时，兑换端点（`gw ?? hs`）使用 URLSession delegate 检查该 host 的叶证书 SPKI SHA-256；系统证书链和 hostname 验证仍须通过。指纹不匹配立即拒绝。禁止跨 origin 重定向，不把兑换码或凭证发送到未确认的服务器。网关客户端使用相同的固定配置。

确认页将摘要转换为每 4 个十六进制字符一组显示，并标明 SHA-256。含指纹的兑换响应若把 gateway 改到另一 host，直接拒绝。网关 URL 和指纹作为非秘密配置随 Matrix 会话保存在 Keychain，恢复及 soft logout 重新认证时保留；配对码和兑换所得临时 token 不保存。动态页启用已配对网关时复用固定传输，设置页不能将其静默改建为无指纹客户端。这里的网关固定范围为 `HTTPGatewayClient` 的 API 请求；WebKit 模块网页仍遵循现有网页 origin 与系统 TLS 策略。

**Matrix Rust SDK 内部请求的 SPKI 固定：待定。** 当前精确固定依赖为 `matrix-rust-components-swift` **26.10.27**。其生成的 `Sources/MatrixRustSDK/matrix_sdk_ffi.swift` 中 `ClientBuilderProtocol`（约 4421–4634 行）暴露 `addRootCertificates`、`disableBuiltInRootCertificates`、`disableSslVerification` 等接口，但没有叶证书 SPKI pin 或自定义 TLS verifier / URLSession 注入接口。自定义根证书不等于叶证书 SPKI 固定，不能用关闭 TLS 校验代替。SDK 的同步、消息及其内部请求继续使用系统 TLS；本次不宣称它们已固定指纹。后续须由上游绑定增加能力或另行评估 Rust 层实现。

## Mock 与本地验证

协议 `PairingService` 隔离兑换传输，DEBUG 提供 `MockPairingService` 与 JSON fixture；Release 不包含 mock 配对开关。示例域名保留为 `.invalid`，示例 token / code 均是假数据。

DEBUG 启动参数：

- `-pair-entry`：直接展示登录页，跳过会话恢复，用于入口截图。
- `-pair-confirm`：直接展示假二维码的确认页，使用 mock。
- `-pairing-mock`：配对兑换使用 mock，不连接真实服务。

构建与测试命令（在 `app-apple/`）：

```sh
swift build --disable-sandbox --cache-path .build/cache --config-path .build/config --security-path .build/security
swift test --disable-sandbox --cache-path .build/cache --config-path .build/config --security-path .build/security
xcodebuild -project Botville.xcodeproj -scheme BotvilleApp-iOS \
  -configuration Debug -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath .build/xcode -sdk iphonesimulator \
  CODE_SIGN_IDENTITY="-" CODE_SIGNING_ALLOWED=YES build
```

测试涵盖 URI 必填/重复/未知参数/版本/过期、URL 组件、base64url、兑换响应及错误映射、homeserver 一致性、Matrix token 登录与注册 UIA，以及运行时生成测试密钥/证书后比对 SPKI hash。测试证书与私钥不进入源码；mock 截图也不证明真实摄像头、真实兑换服务或 SDK 内部 pin 已验收。

## 本次验证（2026-11-05，Asia/Shanghai）

- macOS Debug `swift build`：通过。
- iOS Simulator Debug：通用模拟器目标（arm64 / x86_64）构建通过；`codesign` 确认为 ad-hoc，bundle id 仍为 `org.example.botville.c01`。
- `swift test`：169 项，167 通过、2 跳过、0 失败。新增配对测试 16 项全部通过；跳过的是原有两项需要显式配置本地 Synapse 的集成测试，不能计作联调通过。
- 运行时生成的测试 RSA 密钥和证书位于 `app-apple/.build/` 下的临时目录，测试后清理。SPKI 与 OpenSSL 导出的 SubjectPublicKeyInfo 比对一致，错误指纹被拒绝。
- 截图实际来自 iPhone 18 Pro / iOS 27.2 模拟器，经过目视检查：`~/AI小镇/tmp/screenshots/pair-entry.png`、`~/AI小镇/tmp/screenshots/pair-confirm.png`。确认页直接启动已避免触发相机权限提示。
- 构建/测试日志在本地忽略目录 `.build/verification/`。SDK 预编译二进制仍有 macOS 最低版本及缺失上游调试对象的链接警告；本机构建成功不代表已验证 macOS 14 真机兼容性。
- 尚未验证：真实摄像头、真实 A 线 redeem 服务、真实注册/登录联调；SDK 内部 SPKI 固定仍为“待定”。

改动文件（均在 `app-apple/`，不含忽略的构建产物）：

| 范围 | 文件 |
| --- | --- |
| 新增配对核心 | `Sources/BotvilleCore/Pairing.swift`、`PairingTLS.swift`、`PairingAuthentication.swift` |
| 认证及网关接入 | `Sources/BotvilleCore/MatrixService.swift`、`GatewayClient.swift`、`SessionStore.swift` |
| 界面 | `Sources/BotvilleApp/PairingView.swift`（新增）、`LoginView.swift`、`BotvilleApp.swift`、`DynamicUI.swift` |
| Mock 与测试 | `Sources/BotvilleCore/Resources/pairing-mock.json`；`Tests/BotvilleCoreTests/PairingProtocolTests.swift`、`PairingPinningTests.swift`、`PairingAuthenticationTests.swift` |
| 工程配置 | `Botville.xcodeproj/project.pbxproj`、`BotvilleApp/Info.plist`、`BotvilleApp/Info-Debug.plist` |
| 文档 | `docs/pairing-qr.md`、`docs/README.md` |
