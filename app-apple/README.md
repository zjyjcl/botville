# botville C0.1 — 苹果端 Matrix 原型

SwiftUI + matrix-rust-sdk，最低 iOS 17 / macOS 14。支持密码登录、已加入房间列表、文本时间线、加载更早消息和发送纯文本。默认 homeserver `https://matrix.example.org` 仅为占位，运行时请填写自己有权使用的 HTTPS Matrix homeserver 和账号。

客户端验证与后续待办见 [苹果端验证文档](docs/README.md)。

## 怎么跑

需要完整 Xcode、对应平台 SDK 和首次下载依赖的网络。Swift Package 使用 Swift tools 5.9，精确固定 [MatrixRustSDK 26.10.27](https://github.com/matrix-org/matrix-rust-components-swift/tree/26.10.27)，同时保留 `Package.resolved`。这是无 alpha 后缀的正式发布标签；[上游仍声明 Swift API 可能变更](https://github.com/matrix-org/matrix-rust-components-swift#usage)，升级应重新编译和联调。

### macOS

在仓库根目录运行：

```sh
cd app-apple
swift build
swift run BotvilleApp
```

填写完整 homeserver URL、用户名（或 `@user:server`）和密码。登录后选择房间即可聊天。服务器必须支持 `m.login.password`；本原型不处理 SSO/OIDC。房间需提前用其他 Matrix 客户端加入。

### iOS 模拟器

`Package.swift` 保留 `BotvilleCore` 库和 `BotvilleApp` 可执行目标。另提供一个很薄的 `Botville.xcodeproj`，将同一套 SwiftUI 源文件包装为可以在 iOS 模拟器运行的 `.app`，并通过本地 Swift Package 链接 `BotvilleCore`，不复制 SDK 或业务逻辑。

在 `app-apple/` 中：

```sh
xcodebuild -project Botville.xcodeproj \
  -scheme BotvilleApp-iOS \
  -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath .build/xcode \
  CODE_SIGNING_ALLOWED=NO build
```

也可用 Xcode 打开 `Botville.xcodeproj`，选择 `BotvilleApp-iOS` 和一个已安装的 iPhone 模拟器，点击 Run。目标默认关闭签名，模拟器路径不需要 Apple 账号。真机安装、分发和 TestFlight 不在本次范围内。

### 受限开发环境中的构建

如果工具不能写系统缓存，可把缓存全部放到本目录。以下也是本次验证使用的命令形式（`env -u GIT_CONFIG_COUNT` 仅避开宿主注入的不完整 Git 配置）：

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
  -scheme BotvilleApp-iOS -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath .build/xcode -clonedSourcePackagesDirPath .build \
  -packageCachePath .build/cache -disablePackageRepositoryCache \
  -IDEPackageSupportDisableManifestSandbox=YES \
  CODE_SIGNING_ALLOWED=NO 'OTHER_SWIFT_FLAGS=$(inherited) -disable-sandbox' build
```

`--disable-sandbox`、Xcode 的清单选项和 `OTHER_SWIFT_FLAGS` 只关闭构建工具自身的嵌套沙箱（含 SwiftUI 宏插件），不改变宿主权限或签名要求。`CFFIXED_USER_HOME` 仅对本条 Xcode 命令生效，将其缓存与配置定位到 `.build/`，无需改系统偏好。两种构建复用 `.build/` 中已校验的 SDK 依赖，请按顺序执行。普通本机开发可使用前面的简短命令。下载二进制依赖时 SwiftPM 会校验上游清单中的 SHA-256；本项目不提交二进制依赖和缓存。

## 架构

```text
Package.swift
Sources/
  BotvilleCore/
    MatrixService.swift  SDK 客户端、登录、同步、房间、时间线和发送
    Models.swift         不依赖 SwiftUI 的房间/消息模型、输入校验
    TimelineState.swift  SDK 增量事件归并与回调桥接
  BotvilleApp/
    BotvilleApp.swift    SwiftUI 入口、导航和单一会话
    LoginView.swift     登录表单
    RoomListView.swift  房间列表、同步状态、退出
    RoomView.swift      文本历史、分页和发送状态
Tests/BotvilleCoreTests/
Botville.xcodeproj/      iOS app 宿主与共享 scheme
BotvilleApp/Info.plist   iOS app 元数据（无账号配置）
```

- UI 仅依赖 `BotvilleCore`，不直接调用 SDK。`MatrixService` 在主线程发布可观察状态；网络、加解密和 Matrix 协议由 Rust SDK 处理。
- 使用同一 `Client` 的 `syncOnceV2` 连续长轮询标准 `/sync`，由 SDK 维护同步令牌，支持没有 Sliding Sync 的服务器；失败显示错误并按 1～30 秒退避重试。
- 房间列表展示 `.joined` 房间。时间线保留 SDK 的完整项目序列（包括虚拟和非文本项目），按增量更新后再筛选文本、notice 和 emote，避免索引错位。
- SDK 回调通过有序 `AsyncStream` 转到主线程。切换房间/退出时取消监听，并以代次标识隔离旧异步结果。
- 发送创建无 HTML 的 `m.text`。SDK 接受入队后清空已提交草稿，最终“等待发送 / 已发送 / 发送失败”来自时间线；入队不等于服务器已收到。
- 显式使用 SDK 内存存储。源码不包含真实密码、密钥或令牌，不写 Keychain、UserDefaults 或会话数据库。密码仅用于登录；进程结束后需要重新登录。正常“退出”调用服务端 logout；失败会保留会话并显示错误。
- macOS 使用单窗口，因为 C0.1 每个服务只维护一个活动房间时间线。

## 验证与验收

2026-11-04 本机验证：Xcode 27.2（27B52）、Swift 6.4.2。

| 检查 | 结果 |
| --- | --- |
| `swift build`，macOS arm64、部署目标 14.0 | 通过 |
| `swift test` | 4 项 XCTest 全部通过，0 失败 |
| `xcodebuild`，`BotvilleApp-iOS`，generic iOS Simulator | `BUILD SUCCEEDED`，arm64 + x86_64 |
| iOS App 元数据和签名 | 最低版本 17.0，`org.example.botville.c01`，未签名 |

最终日志位于 `.build/macos-build.log`、`.build/ios-build-final.log`，测试日志位于 `.build/tests.log`（均不纳入 Git）。单元测试覆盖 URL 验证、完整时间线增量索引和未登录操作保护。

非阻断警告：macOS 链接时，上游 Rust 预编译包的 `blake3_neon.o` 标记为 macOS 27.0，高于本项目部署目标 14.0；iOS 调试符号生成会提示上游构建机的 Rust 对象路径不存在。这些不影响本次编译成功，但最低系统运行兼容性及 Rust 源码调试仍未验收。本环境无法连接 CoreSimulator 服务，因此只完成通用模拟器目标的编译，未启动模拟器做 UI 验收。

真实服务器验收需要用户自己的测试账号：登录 → 房间出现 → 打开并加载历史 → 与另一客户端双向发送文本 → 切换房间 → 断网后恢复 → 退出。仅编译和单元测试不能代替这些端到端验收。

## 未完成项

- Keychain 会话恢复、持久缓存、多账号、SSO/OIDC、设备会话管理。直接关闭进程不会主动撤销服务端设备；反复登录可能留下设备记录，可在其他客户端管理。
- E2EE 验证、密钥备份/恢复和旧加密历史恢复界面；SDK 具备加密能力，但本原型尚未完成加密会话验收，建议先用测试用的非加密房间验证文本链路。
- 发送队列的手动重试/撤回、富媒体、搜索、创建/加入房间、邀请、未读计数、已读回执、多窗口及后台同步。不可恢复的队列失败目前只显示状态。
- APNs/Sygnal 推送、TestFlight、联系人/AI 资料、动态、关系和授权卡、小镇等后续 C 线能力。
- 真机、最低系统版本和真实 homeserver 的端到端验证；本任务不使用真实凭证，也不做 Apple 账号签名。
