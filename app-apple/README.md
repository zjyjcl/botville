# botville C2.1 — 苹果 App α：消息与推送

SwiftUI + matrix-rust-sdk，最低 iOS 17 / macOS 14。本轮在 C0.1 原型上实现密码登录、持久会话、房间与邀请、文本消息发送状态、断线恢复，以及推送注册协议的占位接口。它处于 `implemented` / `tested` 阶段；已增加本地 Synapse 的 BotvilleCore 集成验证，真实设备与 UI 验收仍未完成。

本轮对应 [issue #34](https://github.com/zjyjcl/botville/issues/34)。关联 [C1.1 待办清单](./docs/c1.1-element-findings.md#5-待办清单本任务验收产物) 的 C11-02/03/04/05/07/11；本轮实现不代表这些条目的全部验收条件已经通过，原 findings 保持不变。

## 怎么跑

需要完整 Xcode、对应平台 SDK 和首次下载依赖的网络。Swift Package 使用 Swift tools 5.9，精确固定 [MatrixRustSDK 26.10.27](https://github.com/matrix-org/matrix-rust-components-swift/tree/26.10.27)，同时保留 `Package.resolved`。这是无 alpha 后缀的正式发布标签；[上游仍声明 Swift API 可能变更](https://github.com/matrix-org/matrix-rust-components-swift#usage)，升级应重新编译和联调。

### macOS

在仓库根目录运行：

```sh
cd app-apple
swift build
swift run BotvilleApp
```

填写完整 homeserver URL、用户名（或 `@user:server`）和密码。客户端先探测 `/_matrix/client/versions` 和 `/_matrix/client/v3/login` 的密码流；可区分发现失败、网络故障、密码错误和不支持的登录方式。SSO/OIDC 不在本轮范围内。遇到 429 / `M_LIMIT_EXCEEDED` 会显示 `Retry-After`（秒或 HTTP 日期）或 `retry_after_ms` 的等待时长；未提供时长时提示稍后重试并间隔 5 秒重试。等待期间可以取消。登录和恢复共用默认 60 秒的整体超时（可通过 `MatrixService(authenticationTimeout:)` 配置），覆盖探测、请求、SDK 初始化和恢复；超时返回可读错误。

生产默认只接受 HTTPS homeserver。开发时可在 DEBUG 中显式开启本地 HTTP，且仅允许 `http://localhost` 或 `http://127.0.0.1`；默认关闭；Release 界面不提供此开关，已显式启用的开发会话会在 Keychain 保存该设置以便恢复。`Info.plist` 也只为这两个本地地址声明 ATS 例外。URL 不接受账号密码、查询参数、片段或无效端口，且登录探测重定向必须保持同一 origin。

### iOS 模拟器

`Package.swift` 保留 `BotvilleCore` 库和 `BotvilleApp` 可执行目标。另提供一个很薄的 `Botville.xcodeproj`，将同一套 SwiftUI 源文件包装为可以在 iOS 模拟器运行的 `.app`，并通过本地 Swift Package 链接 `BotvilleCore`，不复制 SDK 或业务逻辑。

在 `app-apple/` 中：

```sh
xcodebuild -project Botville.xcodeproj \
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

swift build --disable-sandbox \
  --cache-path .build/cache --config-path .build/config --security-path .build/security

swift test --disable-sandbox \
  --cache-path .build/cache --config-path .build/config --security-path .build/security

mkdir -p .build/xcode-home
CFFIXED_USER_HOME="$PWD/.build/xcode-home" \
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
    PasswordAuthentication.swift 可取消密码请求、限流与整体超时
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

- UI 只依赖 `BotvilleCore`。`MatrixService` 在主线程发布可观察状态。密码登录使用同源、无持久缓存的 URLSession 请求，以直接显示 SDK 内部重试原本隐藏的 429；返回的会话交给 Rust SDK，消息与同步仍由 SDK 处理。
- 登录后 SDK `Session` 的 access/refresh token、设备和 homeserver 数据只保存在 Keychain 项 `org.botville.app.session` / `current`。每个会话使用随机 UUID 目录和随机 32-byte key 的 SQLite store，位于 Application Support；目录强制 `0700`，iOS 采用文件保护并排除备份。源码、UserDefaults 和普通文件不保存凭据。
- 进程重启会重建同一 SDK 会话和存储。登出先请求服务端 logout，成功或 `M_UNKNOWN_TOKEN` 才删除 Keychain 和本地 store；其他失败保留会话供重试。恢复/同步的 `M_UNKNOWN_TOKEN` 会清理并回登录页。损坏、不可用或持续恢复失败时可二次确认“清除本地会话并重新登录”：不依赖读取损坏项，删除此 App 的 Keychain 项与全部本地会话数据库；不会撤销服务器设备会话。清理失败保留重试出口。
- 房间列表同时展示 joined 与邀请，按最新活动排序。DM/群区分使用 SDK 的 `Room.isDirect()`（`m.direct` 语义），加入/邀请分别按 membership 展示，不以显示名猜测；未读数优先使用 SDK 给出的计数。
- 文本消息区分排队、已发送、自动重试中和发送失败已暂停，详细操作边界见下方“发送语义”。
- `/sync` 失败按有限退避重连，网络恢复和 App 回前台会触发补拉；界面显示离线或重连状态。
- 推送只定义 `PushRegistrar`、pusher 注册参数模型和 `NoopPushRegistrar`。没有添加 Push entitlement、证书或真机注册；真实 APNs/Sygnal 接入依赖 A1.2。

## 发送语义

- `notSentYet` 显示“等待发送”，`sent` 或自己的远端回显显示“已发送”。入队成功不代表服务器已接收。
- `sendingFailed(isRecoverable: true)` 保留为“自动重试中”：提示等待/网络恢复后自动发送，不提供重试或取消，避免在途 `abortSend` 导致排队撤回。
- `sendingFailed(isRecoverable: false)` 显示“发送失败已暂停”：只有这种 parked 项可重试或取消；调用前再次检查当前状态。重试使用原 `transactionId` / `retrySend`，取消使用原 `abortSend`，不创建第二条消息；状态已变化则提示刷新结果。
- 发送队列只在登录/恢复成功，以及实际断网或同步中断后首次重连成功时启用。普通前台补拉和每次稳定 `/sync` 成功不反复启用队列；不可恢复项仍需用户重试。

登录/恢复取消后会阻止任何迟到结果激活会话。固定 SDK 的部分 Swift future 不响应 Task 取消，超时包装会及时返回，但候选客户端仍由原任务持有，结束后关闭队列、释放客户端并清理失败登录的 store；清理完成前禁用新登录/恢复/本地清除，防止同时操作同一数据库。恢复取消保留原会话。登录已取得的临时 token 会尝试用独立、5 秒超时的 logout 撤销；离线时不能保证服务器撤销成功，本地不会保存该 token。

## 验证与验收

### 可选的本地 Synapse 集成测试

`SynapseIntegrationTests` 使用真实 `MatrixService` / Rust SDK、独立的加密 SQLite 目录和另一个通过 Client-Server API 操作的测试账号。默认 `swift test` 跳过；仅在本地 Synapse 已就绪、开放 dummy UIA 注册，并明确允许停止/启动 `botville-codex-synapse-1` 时运行。在 `app-apple/` 中：

```sh
mkdir -p .build/c21-e2e-artifacts .build/c21-e2e-tmp
env \
  BOTVILLE_E2E_HOMESERVER=http://127.0.0.1:8028 \
  BOTVILLE_E2E_CONTAINER=botville-codex-synapse-1 \
  BOTVILLE_E2E_ARTIFACT_ROOT="$PWD/.build/c21-e2e-artifacts" \
  TMPDIR=.build/c21-e2e-tmp/ \
  CLANG_MODULE_CACHE_PATH="$PWD/.build/ModuleCache" \
  SWIFTPM_MODULECACHE_OVERRIDE="$PWD/.build/ModuleCache" \
  swift test --disable-sandbox --cache-path .build/cache \
  --config-path .build/config --security-path .build/security \
  --filter SynapseIntegrationTests
```

此测试只接受本地 HTTP 8028 端口；临时目录必须位于当前工作目录内，容器名必须精确匹配。使用相对 `TMPDIR` 是为了避开当前 Swift 工具链在含中文的绝对 `TMPDIR` 上崩溃的问题。

每次运行以系统安全随机数生成密码、创建两个 `c21_` 账号。凭据只保留在内存 `SessionStore` 和测试进程中，不使用真实 Keychain。会话恢复会先确认旧 service 已释放，再构建新实例，复用同一设备、会话与 SQLite 存储。断线期间只入队一条文本，不手动重新同步或重发；恢复后检查双端时间线、补拉及对端 `/messages` 的精确一次计数。

成功和异常路径都会恢复指定容器、使用密码 UIA 调用 `account/deactivate`（`erase:true`），再验证登录返回 `403 M_USER_DEACTIVATED` 或 `403 M_FORBIDDEN`，最后清空内存凭据并删除本次 SQLite 目录。`429` 只按服务端提示重试，不视为停用成功。测试本身保留容器运行以完成清理；本次任务验收后另由操作者对 **botville-codex** 执行 `docker compose ... down`，不删除卷。

### 构建与单元测试记录

2026-11-04 本地验证：Xcode 27.2（27B52）、Swift 6.4.2。

| 检查 | 结果 |
| --- | --- |
| `swift build`，macOS arm64、部署目标 14.0 | 通过 |
| `swift test`（未设置 E2E 环境变量） | 62 项 XCTest：61 项通过，1 项门控集成测试跳过，0 失败 |
| `xcodebuild`，`BotvilleApp-iOS`，generic iOS Simulator、`CODE_SIGNING_ALLOWED=NO` | `BUILD SUCCEEDED`，Debug arm64 + x86_64 |

修订验证日志位于 `.build/pr77-build.log`、`.build/pr77-tests.log` 和 `.build/pr77-ios-build.log`（均不纳入 Git）。产物核验确认 `Info.plist` 只含 localhost/127.0.0.1 ATS 例外。链接器显示 ad hoc 签名、没有 TeamIdentifier 或开发者证书，且未请求 Apple 账号。

非阻断警告：上游 Rust 包的 macOS `blake3_neon.o` 标记 macOS 27.0，高于本项目部署目标 14.0；iOS 调试符号会提示上游 Rust 对象构建路径不存在；另有无 AppIntents 依赖时的跳过提示。这些不改变本次编译结论，但最低系统运行兼容性和 Rust 源码调试仍未验收。

单元测试覆盖 URL/HTTPS 与重定向策略、密码请求与 429、整体超时/取消/迟到结果清理、Keychain backend、损坏会话清除、SQLite 目录保护、恢复和登出的 token 失效、前台与网络补拉、发送状态以及重连退避。常规单测使用占位凭据、fake 和自动删除的临时目录；可选 Synapse 集成测试会创建并停用本地真实测试账号。新增的 Keychain backend fake 测试覆盖 update/add/load/delete、损坏项和 accessibility；macOS 使用唯一测试 service 和占位凭据执行真实 Keychain round-trip，测后删除；仅环境不可用时 XCTSkip 并说明。没有运行 macOS UI 或模拟器 UI。

这些结果不代表完整 App 的 `deployed` 或 `live_accepted`。后续仍需覆盖完整进程重启、UI、登出与 token 失效清理的真实端到端路径。

### 审查修订后的 Synapse 复测（2026-11-04）

当前修订通过同一门控集成集：**1 项通过，0 失败，22.004 秒**。覆盖新的密码请求、邀请、恢复同一会话与 SQLite、双向文本、断线时单条待发、自动重连补拉及恰好一次送达。429/取消/整体超时由上述单元测试验证，本次未主动制造服务器限流。

两个新测试账号均 `deactivate` + `erase:true`，重登均返回 `403 M_FORBIDDEN`。只读数据库汇总确认同前缀累计 6 个测试账号（含历史 4 个）全部停用和擦除，剩余密码哈希与 access token 均为 0。本轮 SQLite/临时目录为空，`botville-codex` 已执行不带 `-v` 的 `down`，项目容器为空、卷保留。日志为 `.build/pr77-e2e.log`、`.build/pr77-compose-up.log`、`.build/pr77-compose-down.log`。本轮未执行 UI 或真机验收。

### 修订前的本地 Synapse 实测（2026-11-04）

环境为 Synapse **1.141.0** + Postgres，compose 项目 **botville-codex**，`server_name=localhost`，仅发布 `127.0.0.1:8028`。运行镜像的 `matrixdotorg/synapse:v1.141.0` 与要求的 `ghcr.io/element-hq/synapse:v1.141.0` 标签指向同一镜像 ID。

| 实测项 | 结果 |
| --- | --- |
| App 密码登录、开发 HTTP 开关、邀请出现在列表并接受 | 通过 |
| 销毁旧 service，从内存 SessionStore 重建，无需密码 | 通过；真实 SQLite，未测真实 Keychain/完整进程重启 |
| App→CS API 对端、对端→App 文本及时间线 | 通过 |
| 仅 stop/start 指定 Synapse，断线时发送一条 | 通过；观察到重连退避及 queued/failed 本地回显 |
| 自动重连、补齐对端两条文本、待发消息恰好一次 | 通过；不调用手动 sync/retry，对端 `/messages` 连续四次计数均为 1 |
| 账号停用、登录失败、内存/SQLite 清理 | 最终运行通过；两次运行累计 4 个账号均 deactivated、erase 标记存在，密码哈希和 access token 均为 0，测试目录为空 |

首次运行的功能项均通过，但清理测试只接受 `M_USER_DEACTIVATED`，误将本机实际的 `403 M_FORBIDDEN` 判为失败，重复登录验证又触发了限流。修正测试断言并遵循 `retry_after_ms` 后，第二轮全部通过（260.412 秒）；未修改 App 业务代码。修订前第二轮登录曾收到 `429`，上游 SDK 的[密码登录使用独立 short_retry 配置](https://github.com/matrix-org/matrix-rust-sdk/blob/e675036b27be102b05ec5c5bff843cf8d739338c/crates/matrix-sdk/src/authentication/matrix/login_builder.rs#L189-L190)，并按服务端 Retry-After 等待后成功，因此不能把 client 级 `retryLimit: 0` 解读为登录不会等待重试。

本次日志保留在 `.build/c21-synapse-build.log`、`.build/c21-synapse-unit-tests.log`、`.build/c21-synapse-ios-build.log`、`.build/c21-synapse-e2e-first.log` 和 `.build/c21-synapse-e2e.log`，均不纳入 Git。临时文件、测试 SQLite、内存凭据已清理；未创建 Keychain 项。最后执行指定项目的 `docker compose ... down`（不带 `-v`），其容器和网络已移除，卷保留；未操作其他 compose 项目。

## 未完成项

- A1.2 完成后接入 Sygnal + APNs，并在真机验证权限、前后台/终止态、深链和换 token；本轮只交付接口与 Noop 实现。
- 非本地 homeserver、macOS UI、iOS 模拟器 UI、真机和进程重启/真实 Keychain 的端到端验收。
- E2EE 设备验证、密钥备份/恢复和旧加密历史恢复；SSO/OIDC、多账号和设备会话管理。
- 媒体、搜索、创建/主动加入房间、已读回执，以及 C11 规定的互操作与性能测量。
