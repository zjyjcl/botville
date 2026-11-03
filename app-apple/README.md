# botville C2.1 — 苹果 App α：消息与推送

SwiftUI + matrix-rust-sdk，最低 iOS 17 / macOS 14。本轮在 C0.1 原型上实现密码登录、持久会话、房间与邀请、文本消息发送状态、断线恢复，以及推送注册协议的占位接口。它处于 `implemented` / `tested` 阶段；尚不是已完成真实设备与真实 homeserver 验收的日常客户端。

本轮对应 [issue #34](https://github.com/zjyjcl/botville/issues/34)。关联 [C1.1 待办清单](docs/c1.1-element-findings.md#5-待办清单本任务验收产物) 的 C11-02/03/04/05/07/11；本轮实现不代表这些条目的全部验收条件已经通过，原 findings 保持不变。

## 怎么跑

需要完整 Xcode、对应平台 SDK 和首次下载依赖的网络。Swift Package 使用 Swift tools 5.9，精确固定 [MatrixRustSDK 26.10.27](https://github.com/matrix-org/matrix-rust-components-swift/tree/26.10.27)，同时保留 `Package.resolved`。这是无 alpha 后缀的正式发布标签；[上游仍声明 Swift API 可能变更](https://github.com/matrix-org/matrix-rust-components-swift#usage)，升级应重新编译和联调。

所有 Git/GitHub 外网访问都使用本机代理；回环服务不走代理：

```sh
export HTTP_PROXY=http://127.0.0.1:7890
export HTTPS_PROXY=http://127.0.0.1:7890
export NO_PROXY=localhost,127.0.0.1
```

Git 命令前使用 `env -u GIT_CONFIG_COUNT`，避免宿主注入的不完整 Git 配置。

### macOS

在仓库根目录运行：

```sh
cd app-apple
env -u GIT_CONFIG_COUNT swift build
env -u GIT_CONFIG_COUNT swift run BotvilleApp
```

填写完整 homeserver URL、用户名（或 `@user:server`）和密码。客户端先探测 `/_matrix/client/versions` 和 `/_matrix/client/v3/login` 的密码流；可区分发现失败、网络故障、密码错误和不支持的登录方式。SSO/OIDC 不在本轮范围内。

生产默认只接受 HTTPS homeserver。开发时可在 DEBUG 中显式开启本地 HTTP，且仅允许 `http://localhost` 或 `http://127.0.0.1`；默认关闭；Release 界面不提供此开关，已显式启用的开发会话会在 Keychain 保存该设置以便恢复。`Info.plist` 也只为这两个本地地址声明 ATS 例外。URL 不接受账号密码、查询参数、片段或无效端口，且登录探测重定向必须保持同一 origin。

### iOS 模拟器

`Package.swift` 保留 `BotvilleCore` 库和 `BotvilleApp` 可执行目标。另提供一个很薄的 `Botville.xcodeproj`，将同一套 SwiftUI 源文件包装为可以在 iOS 模拟器运行的 `.app`，并通过本地 Swift Package 链接 `BotvilleCore`，不复制 SDK 或业务逻辑。

在 `app-apple/` 中：

```sh
env -u GIT_CONFIG_COUNT xcodebuild -project Botville.xcodeproj \
  -scheme BotvilleApp-iOS -configuration Debug \
  -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath .build/xcode \
  CODE_SIGNING_ALLOWED=NO build
```

也可用 Xcode 打开 `Botville.xcodeproj`，选择 `BotvilleApp-iOS` 和一个已安装的 iPhone 模拟器，点击 Run。目标默认关闭签名，模拟器路径不需要 Apple 账号。真机安装、分发和 TestFlight 不在本次范围内。

### 受限开发环境中的构建

如果工具不能写系统缓存，可把缓存全部放到本目录：

```sh
export CLANG_MODULE_CACHE_PATH="$PWD/.build/ModuleCache"
export SWIFTPM_MODULECACHE_OVERRIDE="$PWD/.build/ModuleCache"

env -u GIT_CONFIG_COUNT swift build --disable-sandbox \
  --cache-path .build/cache --config-path .build/config --security-path .build/security

env -u GIT_CONFIG_COUNT swift test --disable-sandbox \
  --cache-path .build/cache --config-path .build/config --security-path .build/security

mkdir -p .build/xcode-home
env -u GIT_CONFIG_COUNT CFFIXED_USER_HOME="$PWD/.build/xcode-home" \
  xcodebuild -project Botville.xcodeproj \
  -scheme BotvilleApp-iOS -configuration Debug -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath .build/xcode -clonedSourcePackagesDirPath .build \
  -packageCachePath .build/cache -disablePackageRepositoryCache \
  -IDEPackageSupportDisableManifestSandbox=YES \
  CODE_SIGNING_ALLOWED=NO 'OTHER_SWIFT_FLAGS=$(inherited) -disable-sandbox' build
```

`--disable-sandbox`、Xcode 的清单选项和 `OTHER_SWIFT_FLAGS` 只关闭构建工具自身的嵌套沙箱（含 SwiftUI 宏插件），不改变宿主权限或签名要求。`CFFIXED_USER_HOME` 仅对本条 Xcode 命令生效，将其缓存与配置定位到 `.build/`，无需改系统偏好。

## 架构与当前行为

```text
Sources/
  BotvilleCore/
    Authentication.swift       URL 策略、能力探测和安全错误分类
    SessionStore.swift         Keychain 凭据与每会话 SQLite 路径
    MatrixService.swift        SDK 生命周期、同步、房间、邀请和登出
    DeliveryState.swift        文本发送状态投影
    SDKTimelineSendActions.swift 原事务重试和取消适配
    ReconnectPolicy.swift      退避和网络恢复后的补拉策略
    PushRegistrar.swift        推送协议、注册参数和 Noop 实现
    TimelineState.swift        SDK 增量事件归并与回调桥接
  BotvilleApp/                 SwiftUI 登录、房间列表和时间线界面
Tests/BotvilleCoreTests/       认证、会话、发送和重连的单元测试
```

- UI 只依赖 `BotvilleCore`。`MatrixService` 在主线程发布可观察状态，网络和 Matrix 协议由 Rust SDK 处理。
- 登录后 SDK `Session` 的 access/refresh token、设备和 homeserver 数据只保存在 Keychain 项 `org.botville.app.session` / `current`。每个会话使用随机 UUID 目录和随机 32-byte key 的 SQLite store，位于 Application Support；目录强制 `0700`，iOS 采用文件保护并排除备份。源码、UserDefaults 和普通文件不保存凭据。
- 进程重启会重建同一 SDK 会话和存储。登出先请求服务端 logout，成功才删除 Keychain 和本地 store；失败保留会话供重试。显式 `M_UNKNOWN_TOKEN` 会清理本地状态并回登录页，网络失败不会误删凭据或串到其他账号。
- 房间列表同时展示 joined 与邀请，按最新活动排序。DM/群区分使用 SDK 的 `Room.isDirect()`（`m.direct` 语义），加入/邀请分别按 membership 展示，不以显示名猜测；未读数优先使用 SDK 给出的计数。
- 文本消息显示排队、已发或失败。重试重用 SDK 原事务/发送队列语义，取消不创建第二条消息。取消和自动重连同时发生时，SDK 可能撤回仍在途的消息；UI 保留可见状态，不把它标成已送达。
- `/sync` 失败按有限退避重连，网络恢复和 App 回前台会触发补拉；界面显示离线或重连状态。
- 推送只定义 `PushRegistrar`、pusher 注册参数模型和 `NoopPushRegistrar`。没有添加 Push entitlement、证书或真机注册；真实 APNs/Sygnal 接入依赖 A1.2。

## 验证与验收

2026-11-04 本地验证：Xcode 27.2（27B52）、Swift 6.4.2。

| 检查 | 结果 |
| --- | --- |
| `swift build`，macOS arm64、部署目标 14.0 | 通过 |
| `swift test` | 32 项 XCTest 全部通过，0 失败 |
| `xcodebuild`，`BotvilleApp-iOS`，generic iOS Simulator、`CODE_SIGNING_ALLOWED=NO` | `BUILD SUCCEEDED`，Debug arm64 + x86_64 |

日志位于 `.build/c21-macos-build.log`、`.build/c21-tests.log` 和 `.build/c21-ios-build.log`（均不纳入 Git）。产物核验确认 `Info.plist` 只含 localhost/127.0.0.1 ATS 例外。链接器显示 ad hoc 签名、没有 TeamIdentifier 或开发者证书，且未请求 Apple 账号。

非阻断警告：上游 Rust 包的 macOS `blake3_neon.o` 标记 macOS 27.0，高于本项目部署目标 14.0；iOS 调试符号会提示上游 Rust 对象构建路径不存在；另有无 AppIntents 依赖时的跳过提示。这些不改变本次编译结论，但最低系统运行兼容性和 Rust 源码调试仍未验收。

单元测试覆盖 URL/HTTPS 与重定向策略、登录能力探测及错误分类、内存假 Keychain 会话存储、SQLite 目录保护、发送状态机，以及重连退避。测试只用内存 fake 和自动删除的临时目录：没有创建真实 Keychain 条目、测试账号或 Docker 数据，也没有运行本地 Continuwuity、macOS UI 或模拟器 UI 的端到端路径。

因此这不是 `deployed` 或 `live_accepted` 结果。后续端到端验收仍需覆盖：重启恢复、真实 Keychain、双端消息/邀请、网络中断恢复、登出与 token 失效清理。

## 未完成项

- A1.2 完成后接入 Sygnal + APNs，并在真机验证权限、前后台/终止态、深链和换 token；本轮只交付接口与 Noop 实现。
- 真实 homeserver、macOS UI、iOS 模拟器 UI、真机和进程重启/真实 Keychain 的端到端验收。
- E2EE 设备验证、密钥备份/恢复和旧加密历史恢复；SSO/OIDC、多账号和设备会话管理。
- 媒体、搜索、创建/主动加入房间、已读回执，以及 C11 规定的互操作与性能测量。
