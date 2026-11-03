<!-- SPDX-License-Identifier: Apache-2.0 -->

# C2.1 安全加固

## HTTPS 与 ATS（#79 / #85）

- Release 在核心登录与恢复入口禁用本地 HTTP，旧 Keychain 中的 `allowLocalHTTP=true` 不能绕过策略；旧 HTTP 会话被拒绝并清除，显示可读错误；清理失败时保留重试出口。
- Xcode Debug 使用 `BotvilleApp/Info-Debug.plist`，仅该配置启用 `NSAllowsLocalNetworking`。Release 使用 `BotvilleApp/Info.plist`，完全不含 `NSAppTransportSecurity`。没有 `NSAllowsArbitraryLoads`。
- Debug 的应用层策略仍默认关闭 HTTP，显式开启后仅允许 `localhost` / `127.0.0.1`，并要求无凭据、有效端口及同源重定向。`NSAllowsLocalNetworking` 的系统范围比 loopback 大，因此不能代替应用层校验；Rust SDK 的请求也不能只依赖 URLSession ATS。
- [Apple 的 NSExceptionDomains 文档](https://developer.apple.com/documentation/bundleresources/information-property-list/nsapptransportsecurity/nsexceptiondomains)明确：**从 iOS 17 / macOS 14 开始支持 IP 字面量及 CIDR**，因此不能笼统认定 `127.0.0.1` 条目无效。项目最低版本正是 iOS 17 / macOS 14。本次改为 Debug 专用 `NSAllowsLocalNetworking`，统一覆盖开发时两种 loopback 写法，不保留独立 IP 域名例外；其版本行为见 [NSAllowsLocalNetworking](https://developer.apple.com/documentation/bundleresources/information-property-list/nsapptransportsecurity/nsallowslocalnetworking)。
- `swift run BotvilleApp` 是未附带 Info.plist 的 macOS 可执行文件，不能把 Xcode 的 plist 配置当作其运行证据。Release 是否接受 HTTP 始终由核心策略决定。

## 推送与错误分类（#86 / #87）

`PushPusherData` 的构造与 Codable 解码共享 HTTPS、host、无凭据、有效端口检查；不可变模型使注册边界不能携带绕过校验的 URL。测试包括合法 wire-format roundtrip、嵌套注册参数非法 URL、Noop 注册及各拒绝路径。真实 Sygnal/APNs 尚未接入。

网络分类仅依据明确的 `URLError.Code` transport 白名单、SDK `ClientBuildError.ServerUnreachable`、`ClientError.MatrixApi` 的 `.connectionFailed/.connectionTimeout`。HTTP 状态和 Matrix 错误码保留业务/限流语义；未知 SDK Generic/Sdk 字符串和解析错误不通过英文词语猜测网络状态。固定 SDK 26.10.27 的 Generic/Sdk 变体没有结构化 transport 子类，因此可能显示较保守的服务端错误，但不会因为服务器文案带有 “connection” 等词而误判网络故障。

## 本轮验证（2026-11-04）

- macOS 27.2 / Xcode 27.2：本地只读 HTTP fixture 的 URLSession 探针在 `localhost`、`127.0.0.1` 均获得 HTTP 200。分别验证无 plist 的 CLI 与嵌入 Debug plist 的程序，均通过；无账号、无 token、无 Synapse 数据改动。此证据仅覆盖当前 macOS，不代表最低版本或 iOS 实测。
- 本机 `xcrun simctl list runtimes`、`list devices available` 均为空；未下载 runtime。因此 iOS Debug HTTP **运行实测仍待完成，#85 保留 Refs**，不将无签名编译等同于连接验收。
- `swift build` 通过。Debug `swift test` 84 项、Release `swift test -c release` 85 项，各 1 项 Synapse 门控跳过、0 失败；额外显式排除真实 Keychain roundtrip，以遵守仅在 `~/AI小镇` 内写入，其余 Keychain backend 模拟测试照常执行。Release 单测实际执行其编译分支，覆盖两种 HTTP 旧会话在创建 client 前拒绝/清理及 HTTPS 兼容恢复。
- Debug / Release 的 `xcodebuild -showBuildSettings` 均确认预期 `INFOPLIST_FILE`，`CODE_SIGNING_ALLOWED=NO`、`CODE_SIGNING_REQUIRED=NO`。两个配置的 generic iOS Simulator 无签名构建均成功；`plutil` 核对源及产物：Debug 仅有 `NSAllowsLocalNetworking=true`，Release 不含 `NSAppTransportSecurity`。固定 SDK 的既有 dSYM/object 路径警告未在本 PR 扩大处理。
- soft logout 测试覆盖 `didReceiveAuthError(isSoftLogout:)` 的 true/false、同设备密码重登及 token 替换、原 store ID/key/目录内容、密码失败、身份不符、取消、candidate 恢复失败、Keychain 保存失败和重试；真实 token rotation / E2EE 历史验收未执行。
- 日志保存在本 worktree 的 `.build/security-validation/`（不提交）：`swift-build.log`、`swift-test-{debug,release}-final.log`、`{debug,release}-settings-relative-tmp.log`、`{debug,release}-build-final.log`、`local-transport.log`。
