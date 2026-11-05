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

生产默认只接受 HTTPS homeserver。开发时可在 DEBUG 中显式开启 HTTP，默认关闭；显式开启后仅允许 `localhost`、规范的 `.local` 名称、带字母的 Compose 单标签名称，以及私网、loopback 或 link-local 地址。Release 核心登录/恢复路径均忽略已保存的开发 HTTP 开关并拒绝非 HTTPS 会话。仅 Debug 的 `Info-Debug.plist` 声明 `NSAllowsLocalNetworking`；Release 的 `Info.plist` 没有 ATS 例外。具体策略及平台验证边界见[安全加固说明](docs/security-hardening.md)。URL 不接受账号密码、查询参数、片段或无效端口，且登录探测重定向必须保持同一 origin。

### iOS 模拟器

`Package.swift` 保留 `BotvilleCore` 库和 `BotvilleApp` 可执行目标。另提供一个很薄的 `Botville.xcodeproj`，将同一套 SwiftUI 源文件包装为可以在 iOS 模拟器运行的 `.app`，并通过本地 Swift Package 链接 `BotvilleCore`，不复制 SDK 或业务逻辑。

在 `app-apple/` 中：

```sh
xcodebuild -project Botville.xcodeproj \
  -scheme BotvilleApp-iOS -configuration Debug \
  -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath .build/xcode \
  -sdk iphonesimulator CODE_SIGN_IDENTITY="-" build
```

也可用 Xcode 打开 `Botville.xcodeproj`，选择 `BotvilleApp-iOS` 和一个已安装的 iPhone 模拟器，点击 Run。App target 的 Debug 与 Release 都使用 Automatic signing，开发团队为 `3Z8WP6U6F7`。两种配置均以 `CODE_SIGN_IDENTITY[sdk=iphonesimulator*]="-"` 保留模拟器默认 ad-hoc 签名；命令行仍可用 `CODE_SIGN_IDENTITY="-"` 覆盖。不要传 `CODE_SIGNING_ALLOWED=NO`：这会丢失 Keychain 所需的应用标识 entitlement，导致新安装后 `SecItemCopyMatching` 返回 `errSecMissingEntitlement (-34018)`。Debug 才挂 development 推送 entitlement；仓库没有加入证书、描述文件或 provisioning profile。

Keychain 诊断只记录 Security 操作名和数值 OSStatus，不记录查询、账号、令牌、密码或会话数据。用目标模拟器的 UDID 替换下方 `<UDID>`：

```sh
xcrun simctl spawn <UDID> log stream --level info --style compact \
  --predicate 'subsystem == "org.botville.app.session"'
# 启动后也可用 log show --last 5m --info 和相同 predicate 回查。
```

正常首次启动的 `SecItemCopyMatching OSStatus=-25300`（`errSecItemNotFound`）表示没有会话；删除无条目同样视为成功。签名构建会由 Xcode 生成模拟器的 `application-identifier`，无需另加共享 Keychain 分组或回退存储。卸载不保证删除 Keychain；清理测试账号前应先在 App 内退出，并确认 `SecItemDelete OSStatus=0`、重启后读取为 `-25300`，再卸载 App。

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
  -sdk iphonesimulator CODE_SIGN_IDENTITY="-" \
  'OTHER_SWIFT_FLAGS=$(inherited) -disable-sandbox' build
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
- 进程重启会重建同一 SDK 会话和存储。登出先请求服务端 logout，成功或 `M_UNKNOWN_TOKEN` 才删除 Keychain 和本地 store；其他失败保留会话供重试。恢复/同步的硬失效 `M_UNKNOWN_TOKEN` 会清理并回登录页；soft logout 保留 Keychain 与加密 store，提示在原设备 ID 上重新输入密码，详见[会话安全说明](docs/session-security.md)。损坏、不可用或持续恢复失败时可二次确认“清除本地会话并重新登录”：不依赖读取损坏项，删除此 App 的 Keychain 项与全部本地会话数据库；不会撤销服务器设备会话。清理失败保留重试出口。
- 房间列表同时展示 joined 与邀请，按最新活动排序。DM/群区分使用 SDK 的 `Room.isDirect()`（`m.direct` 语义），加入/邀请分别按 membership 展示，不以显示名猜测；未读数优先使用 SDK 给出的计数。
- 文本消息区分排队、已发送、可恢复失败的发送暂停和不可恢复失败的发送暂停，详细操作边界见下方“发送语义”。
- `/sync` 失败按有限退避重连，网络恢复和 App 回前台会触发补拉；界面显示离线或重连状态。
- DEBUG 包含 APNs token 回调、权限申请、Matrix HTTP pusher 注册和诊断页。`Info-Debug.plist` 提供 Compose 内网默认地址 `http://sygnal:5000/_matrix/push/v1/notify`，诊断页可修改覆盖；DEBUG 诊断流程允许该 HTTP 地址，Release 只接受 HTTPS。pusher 注册默认携带 `data.format=event_id_only`，推送载荷不带消息内容；development 推送 entitlement 只在 Debug 配置。真实 Sygnal/APNs 投递仍需对 Mac mini 连接的 iPhone 15 完成联调，详见 [DEBUG 真机推送](docs/push-device.md)。

## 加密策略（C2.6）

新建会话支持输入 MXID 开私聊或建群，均默认不加密。成员通过部署 AI 规则检查后可二次确认开启；含 AI 或身份未知时禁用开启，编辑成员导致不再符合策略时会复位开关，仍可创建明文会话。botville 的 AI 接入目前不支持加密会话；已加密或状态未知房间拒绝 AI／身份未确认的邀请，没有确认后放行的接口。本客户端不提供关闭入口，也不保证 AI 或未验证设备无法解密。

AI 判定集中在 `AIParticipantPolicy`，使用部署配置的 MXID 正则/前缀；未命中规则不等于已验证真人身份。修订时已核对 [#99](https://github.com/zjyjcl/botville/pull/99) 尚未合入，默认规则保持空占位；未配置或配置无效时保守拒绝开启。加密房显示 SDK 的设备验证/备份风险，完整恢复流程由 [C11-09 / #97](https://github.com/zjyjcl/botville/issues/97) 跟进。配置方法、数据来源和限制见[加密策略文档](docs/encryption-policy.md)。

## 发送语义

- `notSentYet` 显示“等待发送”，`sent` 或自己的远端回显显示“已发送”。入队成功不代表服务器已接收。
- 固定 SDK 26.10.27 的 `sendingFailed(isRecoverable: true)` 表示其有限重试已耗尽，并已禁用该房间的发送队列；SDK 的[队列实现](https://github.com/matrix-org/matrix-rust-sdk/blob/e675036b27be102b05ec5c5bff843cf8d739338c/crates/matrix-sdk/src/send_queue/mod.rs#L1134-L1188)会先禁用队列再通知失败。客户端立即显示“发送失败，已暂停”，不再假定网络恢复后会自动发送。用户“重试”会启用该房间原队列并复用原 transaction；不提供取消，避免把在途 `abortSend` 变成服务端 redaction。
- `sendingFailed(isRecoverable: false)` 同样显示“发送失败，已暂停”，但使用原 `transactionId` 的 `retrySend` / `abortSend`；只有这种不可恢复项可取消。所有操作前会再次检查当前状态，不创建第二条消息；状态已变化则提示刷新结果。
- 发送队列只在登录或本地恢复会话成功时全量启用。`/sync` 成功、网络恢复和前台补拉不会重新启用已暂停的房间队列；暂停仅限当前 Client 会话，会阻塞该房间后续消息，直到用户重试原消息恢复队列。SDK 对新消息只入队、不自行启用 disabled 队列；重新启动或恢复会话时会重新启用原队列，持久的未发送事务可能继续发送，并依原 transaction 去重。

登录/恢复取消后会阻止任何迟到结果激活会话。固定 SDK 的部分 Swift future 不响应 Task 取消，超时包装会及时返回，但候选客户端仍由原任务持有，结束后关闭队列、释放客户端并清理失败登录的 store；清理完成前禁用新登录/恢复/本地清除，防止同时操作同一数据库。恢复取消保留原会话。普通新登录已取得的临时 token 会尝试用独立、5 秒超时的 logout 撤销；离线时不能保证服务器撤销成功，本地不会保存该 token。

## 验证与验收

### C2.6 验证范围

[PR #98](https://github.com/zjyjcl/botville/pull/98) 记录本次修订的构建、测试数量、回归复现及无签名模拟器编译结果。单测覆盖新建私聊／群聊邀请列表、加密房后续邀请的服务层硬拒绝、已观测加密不降级、成员完整性、配置提示和修改成员后的加密开关复位。普通房间同步只读成员缓存，安全操作按需完整读取。

按本次任务边界，测试使用占位凭据和 fake SDK，排除真实 Keychain 往返，并保持需要真实账号的 Synapse 集成门控关闭。缓存和临时数据留在本 worktree 的 `.build/`。操作日志和机器环境细节不作为产品文档维护，具体证据见 PR 评论。

无签名编译和单元测试不代表双设备加密收发、UI 或历史恢复已经验收；完整验证／恢复由 [#97](https://github.com/zjyjcl/botville/issues/97) 跟进。上游 Rust 对象的最低系统与调试符号警告仍需独立兼容性验收。

### 可选的本地 Synapse 集成测试

`SynapseIntegrationTests` 使用真实 `MatrixService` / Rust SDK、独立的加密 SQLite 目录和另一个通过 Client-Server API 操作的测试账号。默认 `swift test` 跳过；仅在本地 Synapse 已就绪、开放 dummy UIA 注册，并明确允许停止/启动指定的测试容器时运行。在 `app-apple/` 中：

```sh
mkdir -p .build/c21-e2e-artifacts .build/c21-e2e-tmp
env \
  BOTVILLE_E2E_HOMESERVER="http://127.0.0.1:<port>" \
  BOTVILLE_E2E_CONTAINER="<your-synapse-container>" \
  BOTVILLE_E2E_ARTIFACT_ROOT="$PWD/.build/c21-e2e-artifacts" \
  TMPDIR=.build/c21-e2e-tmp/ \
  CLANG_MODULE_CACHE_PATH="$PWD/.build/ModuleCache" \
  SWIFTPM_MODULECACHE_OVERRIDE="$PWD/.build/ModuleCache" \
  swift test --disable-sandbox --cache-path .build/cache \
  --config-path .build/config --security-path .build/security \
  --filter SynapseIntegrationTests
```

`BOTVILLE_E2E_HOMESERVER`、`BOTVILLE_E2E_CONTAINER` 和 `BOTVILLE_E2E_ARTIFACT_ROOT` 均为必填；缺少时测试会跳过。homeserver 必须是本地 HTTP 地址；临时目录必须位于当前工作目录内。容器名称由环境变量显式提供，测试只会 stop/start 这一容器。相对 `TMPDIR` 有助于让构建缓存留在工作目录。

每次运行以系统安全随机数生成密码、创建两个 `c21_` 账号。凭据只保留在内存 `SessionStore` 和测试进程中，不使用真实 Keychain。会话恢复会先确认旧 service 已释放，再构建新实例，复用同一设备、会话与 SQLite 存储。断线期间只入队一条文本；恢复后确认可恢复失败已暂停，再通过 `retryMessage` 恢复原队列，并检查双端时间线、补拉及对端 `/messages` 的精确一次计数。

成功和异常路径都会恢复指定容器、使用密码 UIA 调用 `account/deactivate`（`erase:true`），再验证登录返回 `403 M_USER_DEACTIVATED` 或 `403 M_FORBIDDEN`，最后清空内存凭据并删除本次 SQLite 目录。`429` 只按服务端提示重试，不视为停用成功。测试本身保留容器运行以完成清理；验收后可由操作者对自己的 compose 项目执行 `docker compose ... down`，不删除卷。

### 构建与单元测试记录

2026-11-04 第二轮复审验证：使用项目所需的 Xcode、Swift 和平台 SDK 完成。

| 检查 | 结果 |
| --- | --- |
| `swift build`，macOS arm64、部署目标 14.0 | 通过 |
| `swift test`（未设置 E2E 环境变量） | 66 项 XCTest：65 项通过，1 项门控集成测试跳过，0 失败 |
| `xcodebuild`，`BotvilleApp-iOS`，generic iOS Simulator、`CODE_SIGNING_ALLOWED=NO` | `BUILD SUCCEEDED`，Debug arm64 + x86_64 |

修订验证的临时日志位于本地构建目录，均不纳入 Git。产物核验确认 `Info.plist` 只含 localhost/127.0.0.1 ATS 例外。链接器显示 ad hoc 签名、没有 TeamIdentifier 或开发者证书，且未请求 Apple 账号。

非阻断警告：上游 Rust 包的 macOS `blake3_neon.o` 标记 macOS 27.0，高于本项目部署目标 14.0；iOS 调试符号会提示上游 Rust 对象构建路径不存在；另有无 AppIntents 依赖时的跳过提示。这些不改变本次编译结论，但最低系统运行兼容性和 Rust 源码调试仍未验收。

本轮新增离线恢复在首次成功同步前显示本地房间、激活中取消后的回滚、同步正常时可恢复发送失败立即暂停、前台/网络恢复不重启队列、原房间重试与取消边界测试。单元测试覆盖 URL/HTTPS 与重定向策略、密码请求与 429、整体超时/取消/迟到结果清理、Keychain backend、损坏会话清除、SQLite 目录保护、恢复和登出的 token 失效、前台与网络补拉、发送状态以及重连退避。常规单测使用占位凭据、fake 和自动删除的临时目录；可选 Synapse 集成测试会创建并停用本地真实测试账号。新增的 Keychain backend fake 测试覆盖 update/add/load/delete、损坏项和 accessibility；macOS 使用唯一测试 service 和占位凭据执行真实 Keychain round-trip，测后删除；仅环境不可用时 XCTSkip 并说明。没有运行 macOS UI 或模拟器 UI。

这些结果不代表完整 App 的 `deployed` 或 `live_accepted`。后续仍需覆盖完整进程重启、UI、登出与 token 失效清理的真实端到端路径。

### 第二轮复审的 Synapse 实测（2026-11-04）

当前门控集成测试 **1 项通过，0 失败，22.239 秒**。覆盖密码登录、邀请、同会话与 SQLite 恢复、双向文本、真实断线与补拉。发送测试先等待 SDK 有限重试耗尽，再恢复服务；确认 `/sync` 已恢复但该消息仍暂停、对端计数为 0，显式重试原房间队列后，对端连续检查均只收到 1 条。

两个新测试账号均完成 `deactivate` + `erase:true`，重登均返回 `403 M_FORBIDDEN`。只读数据库汇总确认测试前缀账号全部停用和擦除，剩余密码哈希与 access token 均为 0。本轮 SQLite 目录为空；测试 compose 项目执行了不带 `-v` 的 `down`，容器已移除、卷保留。日志仅保留于本地构建目录。

429、认证取消与整体超时由单元测试覆盖，本轮没有主动制造服务器限流。上述结果只代表本地 Core 集成通过，未执行 UI 或真机验收。

## 未完成项

- Sygnal + APNs 的真实送达验收：当前已实现 APNs token 与 Matrix pusher 注册，仍需在真机验证权限、前后台/终止态、深链和换 token。
- 非本地 homeserver、macOS UI、iOS 模拟器 UI、真机和进程重启/真实 Keychain 的端到端验收。
- E2EE 设备验证、密钥备份/恢复和旧加密历史恢复；SSO/OIDC、多账号和设备会话管理。
- 媒体、搜索、按房间 ID/别名主动加入、已读回执，以及 C11 规定的互操作与性能测量；C2.6 仅新增 MXID 建房和邀请入口。
