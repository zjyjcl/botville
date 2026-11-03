<!-- SPDX-License-Identifier: Apache-2.0 -->

# C2.1 安全加固

## HTTPS 与 ATS（#79 / #85）

- Release 在核心登录与恢复入口禁用本地 HTTP，旧 Keychain 中的 `allowLocalHTTP=true` 不能绕过策略；旧 HTTP 会话被拒绝并清除，显示可读错误；清理失败时保留重试出口。
- Xcode Debug 使用 `BotvilleApp/Info-Debug.plist`，仅该配置启用 `NSAllowsLocalNetworking`。Release 使用 `BotvilleApp/Info.plist`，完全不含 `NSAppTransportSecurity`。没有 `NSAllowsArbitraryLoads`。
- Debug 的应用层策略仍默认关闭 HTTP，显式开启后仅允许 `localhost` / `127.0.0.1`，并要求无凭据、有效端口及同源重定向。`NSAllowsLocalNetworking` 的系统范围比 loopback 大，因此不能代替应用层校验；Rust SDK 的请求也不能只依赖 URLSession ATS。
- [Apple 的 NSExceptionDomains 文档](https://developer.apple.com/documentation/bundleresources/information-property-list/nsapptransportsecurity/nsexceptiondomains)明确：**从 iOS 17 / macOS 14 开始支持 IP 字面量及 CIDR**，因此不能笼统认定 `127.0.0.1` 条目无效。项目最低版本正是 iOS 17 / macOS 14。本次改为 Debug 专用 `NSAllowsLocalNetworking`，统一覆盖开发时两种 loopback 写法，不保留独立 IP 域名例外；其版本行为见 [NSAllowsLocalNetworking](https://developer.apple.com/documentation/bundleresources/information-property-list/nsapptransportsecurity/nsallowslocalnetworking)。
- `swift run BotvilleApp` 是未附带 Info.plist 的 macOS 可执行文件，不能把 Xcode 的 plist 配置当作其运行证据。Release 是否接受 HTTP 始终由核心策略决定。

## 推送与错误分类（#86 / #87）

`PushPusherData` 的构造与 Codable 解码共用 `URLComponents` 校验：只接受 HTTPS；要求非空 host，`percentEncodedHost` 不含 `%`；拒绝 userinfo、fragment、空白、控制字符和 IPv6 zone。原始 authority 的端口必须是 1–65535 的十进制整数，不能依赖 `components.port == nil` 判定“未指定端口”，从而拒绝空端口和整数溢出。测试覆盖 `%40/%00/%20/%09/%5c/%2f`、超长端口、构造/Codable/嵌套解码，以及合法 wire-format roundtrip 与 Noop 注册。Homeserver 复用相同 authority 校验；旧 HTTP 记录先去除首尾空白再按当前构建策略判定。

该校验保证 URL 语法和传输协议，不是 SSRF 防火墙：合法私网/loopback IP 仍可作为部署配置，DNS 解析、重绑定和出站网络隔离由 homeserver/网关负责，不能把 URL 模型当作任意用户可选的代理目的地。PR #96 的 `docs/deploy/push-sygnal.md` 仅作接口参考：`app_id` 对应 Sygnal app、`pushkey` 为 APNs device token、`data.url` 为 HTTPS 网关；服务端 CI 的内网 HTTP 示例不放宽 App 的 HTTPS 策略。当前仍使用 Noop 注册，未接入 Sygnal/APNs、entitlement 或密钥。

网络分类仅依据明确的 `URLError.Code` transport 白名单、SDK `ClientBuildError.ServerUnreachable`、`ClientError.MatrixApi` 的 `.connectionFailed/.connectionTimeout`。HTTP 状态和 Matrix 错误码保留业务/限流语义；未知 SDK Generic/Sdk 字符串和解析错误不通过英文词语猜测网络状态。固定 SDK 26.10.27 的 Generic/Sdk 变体没有结构化 transport 子类，因此可能显示较保守的服务端错误，但不会因为服务器文案带有 “connection” 等词而误判网络故障。

## 审查修复验证（2026-11-04）

- `swift build` 通过；最终 Debug `swift test` **88 项、0 失败**，Release `swift test -c release` **90 项、0 失败**。两个配置均显式排除 `KeychainSessionStoreTests.testRealKeychainRoundTripUsesUniqueServiceAndDeletesIt` 与 `SynapseIntegrationTests`；Keychain 模拟 backend 测试照常执行。测试文件夹全部位于当前 package 的 `.build`，不使用真实账号或系统 Keychain。
- soft logout 测试覆盖退出/清除可用状态、密码错误后保持出口、restore 抛 soft logout、前台不再次恢复（调用次数不增）、同设备重登保留数据、清除后解除账号锁定，以及部分清除失败后禁止重写旧 Keychain 记录或重建 store。另测普通登录在 activation 前已保存记录、activation 失败会清除并撤销新 token；soft logout 失败不调用会删除设备的 logout。
- Release 的真实编译分支验证公开 `login(allowLocalHTTP: true)` 对 localhost/127.0.0.1 均拒绝，且 discovery、clientFactory、password transport 都未调用；旧 HTTP 恢复（含前后空白）在创建 client 前被拒绝/清理，HTTPS 旧记录仍可恢复。
- URL 测试覆盖构造、Codable 与嵌套解码，编码 host、空/超大端口、IPv6 zone、fragment、路径/查询的原始及编码空白/控制字符，以及 HTTPS 大小写归一和合法最大端口。`DiscoveryURLProtocol.handler` 与请求记录在 `tearDown` 重置。
- `xcodebuild -showBuildSettings` 实测 Debug 的 `SWIFT_ACTIVE_COMPILATION_CONDITIONS = DEBUG`，原工程已经正确，无需追加；Release 不定义 DEBUG。Debug 使用 `Info-Debug.plist`，Release 使用 `Info.plist`。
- iOS Simulator generic destination 的 Debug / Release 无签名构建均成功。`CODE_SIGNING_ALLOWED=NO`、`CODE_SIGNING_REQUIRED=NO`、`AD_HOC_CODE_SIGNING_ALLOWED=NO`；两个构建日志均无 CodeSign action。产物 plist 核对：Debug 仅启用 `NSAllowsLocalNetworking=true`，Release 不含 `NSAppTransportSecurity`。
- SwiftPM scratch/cache/config/security、Xcode DerivedData 与 package cache 均放在当前 worktree 的 `app-apple/.build`。日志位于 `app-apple/.build/security-validation/`（不提交）：`swift-build.log`、`swift-test-{debug,release}-final.log`、`{debug,release}-settings.log`、`xcode-{debug,release}-build.log`。
- 当前没有可用 iOS Simulator runtime；以上是编译和模拟单元测试证据，未执行 iOS UI/HTTP 运行验收、真实 Synapse token rotation、旧加密历史或 APNs/Sygnal 验收。#85 / #90 保留 Refs。
- 固定 SDK 仍产生预编译 dSYM/object 路径警告，以及 macOS 二进制对象最低版本高于 package 声明的警告；本轮未验证最低支持系统。TLS/证书错误独立文案、`dataNotAllowed`/`cancelled` 分类及 Apple CI job 仍为后续建议项。
