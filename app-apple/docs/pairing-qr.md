<!-- SPDX-License-Identifier: Apache-2.0 -->

# 扫码配对

规格来源是本任务提供的 Pairing QR v1 契约及 OpenAPI。这里记录 App 当前实现的边界；不宣称已经与真实配对服务联调。

## URI 与确认

```text
botville://pair?v=1&hs=<https>&gw=<https，可选>&fp=sha256:<base64url，可选>&code=<base64url>&exp=<unix秒>
```

- 解析先去除 URI 首尾空白；其余原始字符串完全按字节式文本处理，含 `%` 即拒绝，绝不通过 `URLComponents.queryItems` 解码查询。它必须以 `botville://pair?` 开头，以 `&` 分段并在每段的第一个 `=` 处分开；空段、没有 `=`、fragment、内部空白或控制字符均整体拒绝。
- 合法且大小写敏感的键只有 `v`、`hs`、`gw`、`fp`、`code`、`exp`。未知键（包括空值和 `V`）及重复键一律拒绝。
- 必须包含 `v`、`hs`、`code`、`exp`，且 `v` 只能是字符串 `"1"`；非 v1 显示“请升级 App”。`hs`、`gw` 只接受以小写 `https://` 开头的严格 HTTPS 地址，拒绝凭据、查询、片段、非法端口和混淆组件。
- **当前负责人决定 `gw` 在 App 中可选**，与 A 线契约 §3.1 的必填定义不一致，待双方对齐。无 `gw` 时隐藏 AI / 网关功能入口，只保留聊天；功能清单视为缺省，不尝试启用网关功能。
- `code` 为 32 字节的无填充 base64url；`fp` 是 `sha256:` 加 32 字节的无填充 base64url。指纹取叶证书 DER `SubjectPublicKeyInfo` 的 SHA-256，不能用完整证书摘要或裸公钥摘要代替。
- `exp` 为 Unix 秒，本地到期即拒绝；最终有效性以服务器为准。
- 登录页提供“扫码配对”。iOS 用 AVFoundation 摄像扫码并说明相机用途；macOS 用 Vision 读取所选图片中的二维码，也可粘贴链接。
- 解析成功后显示 homeserver、gateway、分组指纹和过期时间。只有用户点击确认才发送 redeem；解析、截图模式和确认页展示不发起 redeem。

## 兑换与认证

发送 `POST <hs>/_botville/pair/redeem`，JSON body 为 `{"code":"…","device_display_name":"…"}`。兑换地址永远由二维码的 `hs` 拼出，不能改由 `gw` 承担。设备名在发送前截断为 1–128 个 Unicode 字符。不记录二维码、兑换码、密码或令牌；兑换码仅保留在配对流程内存中，不进入 UserDefaults、文件或 Keychain。

成功响应：

```json
{
  "v": "1",
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

`gateway` 可为 null 或 HTTPS 字符串。响应 `v` 必须解码为字符串且严格等于 `"1"`；数字 `1` 不接受。响应 `homeserver` 必须逐字符等于二维码中未经规范化的 `hs`，不能由兑换服务将登录凭证导向其他服务器。`expires_in_ms` 必须至少为 1。认证类型仅接受 `registration_token` 和 `login_token`。

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

> **语义待对齐（#132）**：下列「系统证书链和 hostname 验证仍须通过」以及按 host
> （而非 `(scheme, host, port)`）套用 pin 的描述，是 #130 合入时的**旧语义**。
> 契约真源已改为 `docs/contracts/pairing-qr.md` §3.3.2（有 `fp`：SPKI pin **替代**
> 系统根 + 叶证自锚的 SAN/有效期/serverAuth EKU；作用域 `(scheme, host, port)`；
> `gw` 仅同源复用 pin）。App / `PairingTLS` 实现与本文对齐工作见 C 线 #132-PR2；
> 在此之前以契约文档为准，本文不得覆盖契约。


有 `fp` 时，URLSession delegate 对 **hs 主机**的叶证书 SPKI SHA-256 做固定：覆盖兑换及本 App URLSession 发起的同 host 凭证交换和网关请求。系统证书链和 hostname 验证仍须通过；固定失败归类为 `certificateMismatch`，不作为一般网络失败。禁止跨 origin 重定向，不把兑换码或凭证发送到未确认的服务器。

确认页将摘要转换为每 4 个十六进制字符一组显示，并标明 SHA-256。`gw` 与 `hs` 不是同一 host 时不得因指纹而拒绝：网关请求仅在 `gw` host 与 `hs` host 相同才套用该 pin，否则使用系统 TLS。网关 URL 和指纹作为非秘密配置随 Matrix 会话保存在 Keychain，恢复及 soft logout 重新认证时保留；配对码和兑换所得临时 token 不保存。动态页启用已配对网关时复用固定传输，设置页不能将其静默改建为无指纹客户端。这里的网关固定范围为 `HTTPGatewayClient` 的 API 请求；WebKit 模块网页仍遵循现有网页 origin 与系统 TLS 策略。

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

测试逐个加载契约的 `uri-valid*`、`uri-invalid*` 与 `redeem-*.json` fixture，并注入 `now` 避免示例 `exp` 自然过期；另覆盖未知参数、原始 `%`、可选 `gw`、固定兑换地址、字符串版响应 `v`、逐字符 homeserver、大小写 HTTPS 和设备名边界。测试证书与私钥不进入源码；mock 截图也不证明真实摄像头、真实兑换服务或 SDK 内部 pin 已验收。

## 本次验证（2026-11-05，Asia/Shanghai）

- `swift test`：186 项，184 项通过，0 失败；**环境相关 skip**：本机 2 项 Synapse 集成测试未设置 E2E 环境变量，未计为配对联调通过。配对相关 33 项全部通过，覆盖全部 17 个契约 fixture；临时本地 TLS 服务也验证了公开 redeem 路径将证书拒绝报告为 `certificateMismatch`。
- macOS Debug `swift build`：通过。iOS Simulator ad-hoc Debug：`BUILD SUCCEEDED`，arm64 + x86_64，`codesign` 确认 `Signature=adhoc`，bundle id 保持 `org.example.botville.c01`。
- 本地验证日志位于 `app-apple/.build/pr130-verification/`，不纳入 Git。构建或单元测试成功不代表已验证真实摄像头、真实 A 线 redeem 服务、真实注册/登录联调或 macOS 14 真机兼容性。
- SDK 内部 SPKI 固定仍为“待定”；此处所述 pin 仅覆盖本 App 的 URLSession 请求范围。

配对相关文件（含既有 PR 实现，均在 `app-apple/`，不含忽略的构建产物）：

| 范围 | 文件 |
| --- | --- |
| 新增配对核心 | `Sources/BotvilleCore/Pairing.swift`、`PairingTLS.swift`、`PairingAuthentication.swift` |
| 认证及网关接入 | `Sources/BotvilleCore/MatrixService.swift`、`GatewayClient.swift`、`SessionStore.swift` |
| 界面 | `Sources/BotvilleApp/PairingView.swift`（新增）、`LoginView.swift`、`BotvilleApp.swift`、`DynamicUI.swift` |
| Mock 与测试 | `Sources/BotvilleCore/Resources/pairing-mock.json`；`Tests/BotvilleCoreTests/PairingProtocolTests.swift`、`PairingPinningTests.swift`、`PairingAuthenticationTests.swift`、`PairingTrustPolicyTests.swift`；`Fixtures/pairing-qr-contract/` |
| 工程配置 | `Package.swift`（测试资源）；既有 `Botville.xcodeproj/project.pbxproj`、`BotvilleApp/Info.plist`、`BotvilleApp/Info-Debug.plist` |
| 文档 | `docs/pairing-qr.md`、`docs/README.md` |
