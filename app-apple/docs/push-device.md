<!-- SPDX-License-Identifier: Apache-2.0 -->

# DEBUG 真机推送

iOS 开发包提供 APNs 注册、Matrix HTTP pusher 注册及诊断入口。PR3 接入通知路由、主 App 未读角标和 NSE 发送者显示名；#145 取证显示 `willPresent` 已回调但系统仍未展示横幅，因此前台现在改用 App 内顶部横幅。真机目标为 Mac mini 连接的 iPhone 15；Sygnal/APNs 联调留待后续，模拟器构建和单元测试不能证明真实通知送达。

## 入口与配置

在 Debug 包中打开 **我 → 设置 → 推送诊断**：

1. 保存 Sygnal notify URL。默认从 `Info-Debug.plist` 的 `BotvilleSygnalNotifyURL` 读取：`http://sygnal:5000/_matrix/push/v1/notify`。用户保存值优先，诊断页可修改覆盖，且仅此地址存入 UserDefaults。
2. 点击「申请权限并注册 / 重试」，允许系统通知权限。获准后调用 `registerForRemoteNotifications`，通过 `UIApplicationDelegateAdaptor` 接收成功/失败回调。已授权的后续启动也会重新向 APNs 注册。
3. 页面显示标准 Base64（含 `=` 填充）的 device token、APNs 注册状态和安全化错误。显示值、复制值和 pusher `pushkey` 是同一份 Base64；32 字节 APNs token 通常为 44 个字符。iOS 剪贴板只在本机可用，120 秒后过期。不要将 token 贴到日志、工单或聊天中。
4. 使用当前 Matrix 会话自动注册 pusher。点击「核对」读取 `GET /_matrix/client/v3/pushers`，检查 `app_id + pushkey` 是否同时匹配。显示匹配仅代表服务端有记录，不能替代送达验证。

PR1 新增 `BotvilleNotificationService`（iOS Notification Service Extension），通过主 App 的 **Embed Foundation Extensions** 嵌入。主 App 与扩展都使用 App Group `group.com.zjyjcl.botville`，并共享 Keychain access group `$(AppIdentifierPrefix)com.zjyjcl.botville.shared`；主 App ID `com.zjyjcl.botville` 与 NSE App ID `com.zjyjcl.botville.notification` 必须在真机签名能力中同时启用该 App Group 和 Keychain 共享。两个 target 均使用 `Config/Signing.xcconfig` 的默认团队 `3Z8WP6U6F7` 和 Automatic signing；上述标识为默认前缀 `com.zjyjcl.botville` 的展开结果。换账号时按 [README](../README.md#ios-模拟器) 复制 `Config/Local.xcconfig.example` 为 `Local.xcconfig`，覆盖团队和 Bundle 前缀，并自行注册对应能力。仓库不提交证书、描述文件或真实凭据。

主 App 在登录/恢复成功后，仅向共享 Keychain 写入 homeserver URL、userId、accessToken 和 deviceId，不共享 refreshToken；退出、清除本地会话或软退出时清除。每个 target 的私有 Keychain group 保留为第一项，完整主会话和本地加密库密钥仍在私有组。共享条目使用 `AfterFirstUnlockThisDeviceOnly`；不将 token 写入 App Group 文件或 UserDefaults。共享 Keychain 不可用时记录固定诊断且不阻断主会话；本 PR 没有任何 DEBUG 或 Release 私有组/明文回退。凭据的字符串及调试描述固定脱敏；单测通过可注入的 Keychain backend 验证存取失败分支。

若共享条目删除失败，仅在 UserDefaults 持久保存待清理布尔标记，下次启动恢复流程先重试删除；成功删除或成功写入新凭据后清除标记。此标记与完整主会话的清理状态分开，软退出仍保留主会话和加密数据库。Keychain 故障期间不能保证旧共享条目已消失。

NSE 只读共享凭据，且只使用 homeserver URL 与 access token；不会记录 token、Authorization header、URL 或响应正文。PR2 的正文路径保持不变：从 `event_id_only` payload 取得一对 `room_id` / `event_id` 后，以 Foundation `URLSession` 发起一次无缓存、拒绝重定向的 GET：`/_matrix/client/v3/rooms/{roomId}/event/{eventId}`。路径分段分别正确百分号编码，并带 `Authorization: Bearer <当前会话>`；单次请求与资源超时最多 8 秒，扩展整体仍以 10 秒兜底。PR3 仅在事件成功且剩余预算允许时，用同一凭据追加至多一次 [`GET /_matrix/client/v3/profile/{userId}`](https://spec.matrix.org/latest/client-server-api/#get_matrixclientv3profileuserid)，userId 按完整路径段百分号编码，同样禁缓存、拒绝重定向，超时受剩余整体预算约束。扩展不引入 `matrix-rust-sdk`。

Sygnal 的实际标准字段是 payload 顶层的 `room_id` 与 `event_id`：其 [`_get_payload_event_id_only`](https://github.com/matrix-org/sygnal/blob/main/sygnal/apnspushkin.py) 直接从 notification 取这两个字段。NSE 也兼容两层以内的 `notification` / `data` 包装（包括交叉或同名嵌套），不在不同对象之间拼接 room/event；这些包装是兼容处理，尚无真机抓包确认。成功取得事件后，title 优先使用 profile 的非空 `displayname`，查询失败、超时、JSON 无效或没有显示名时静默回退事件发送者 MXID，再缺失则回退房间 ID，最多 120 个字符。成功取得 `m.room.message`（如 `m.text`、`m.notice`、媒体说明）或 `m.sticker` 时，body 使用 `content.body` 并按 Character 截断为最多 240 个字符；发生截断时为前 239 个字符加末尾「…」。`m.room.encrypted`、没有可显示正文或正文无法解析时显示「加密消息」。缺字段、共享凭据不可用或事件尚未成功即到期时保留占位「…」；事件 HTTP、网络或 JSON 失败显示「无法加载」。事件已成功后，profile 失败或 `serviceExtensionTimeWillExpire` 只回退显示名，保留已取得的正文。

NSE 的角标策略仍是**保留 APNs/default payload 已给出的系统值**：不读取或累加 App Group 计数，不将 payload 的 `unread_count` 转成角标。PR3 由主 App 将房间列表 `unreadCount` 的总和写入 iOS App 角标，在列表更新、App 恢复活跃、打开会话和成功标记已读时重算；打开房间后 best-effort 发送已读回执，成功才清除该房间的本地未读，失败保留原列表计数且不阻断进房，已关闭房间或更换会话后的旧回调不回写。某个房间已读后仍保留其他房间的未读总数，全部已读为零。后台 App 未运行或尚未同步时不承诺角标即时更新；本 PR 不增加后台 sync。macOS 跳过系统角标写入。

## 前台 App 内横幅与系统通知边界

- **前台候选来自 SDK 逐事件通知回调**：`MatrixService` 激活会话时通过 `registerNotificationHandler` 注册 `ForegroundSyncNotifications`，每条按 Matrix 通知规则触发的消息事件（文本、加密、贴纸）各生成一个候选，带本条真实 event ID、时间戳和裁剪后的正文。首个成功 sync 前的事件视为历史，不出卡；房间列表 / `latestEvent()` 只负责列表显示和排序，不再派生横幅，也不比较未读数或建立基线。正文、房间名、发送者显示名只取本机 SDK 已同步的事件和房间信息，不解析 APNs/NSE 正文，不新增事件或 profile 请求，不写 raw JSON。
- `ForegroundInAppBannerStore` 在主线程维护一张卡片，约 3 秒自动消失。同房连发更新为最新内容并重新计时，不堆叠；同一事件重复不会续命。候选身份固定为 room ID + event ID（`eventID` 为必填），同房同毫秒的不同事件各自出卡；时间戳不参与去重。回前台时只按系统已投递通知的 room ID + event ID 抑制。候选和去重记录只在内存保存，退出清除，不写正文或凭据日志。
- 卡片位于整个 tab 容器的最外层 top overlay（高于 `NavigationSplitView`），使用 `safeAreaPadding(.top)` 并避让顶部导航操作。显示 initials 头像占位、房间名（私聊优先本地发送者显示名）与正文。点按复用 `.room(roomID)` → `apply(route)`，切到「会话」并打开房间；其他 tab 也能显示和点按。
- 自己的消息、当前**实际可见**的房间、输入框 focused 或正在 composing 时不弹；手机已返回列表时，即使保留房间选择也允许弹。非 active 状态不显示，进入后台立即清除当前卡片；被抑制事件直接消费，不在离开输入框后补弹。未读列表和角标沿用原逻辑。
- 加密房间固定显示「加密消息」，即使本机已解密；无法解密事件同样如此。空白或暂不可展示正文显示「…」，本地消息解析失败显示「无法加载」，加密状态优先于失败提示；成员、房名等状态事件及撤回不作为新消息。标题最多 120 个 Character，正文最多 240 个 Character，超出以「…」截断。
- `UNUserNotificationCenterDelegate` 仍由进程单例安装，`willPresent` 仍**同步**完成，但只回传 `.badge`（`optionsRaw=1`），不再请求系统 banner/list/sound，避免双显。后台/被杀不走此回调，继续由系统展示 NSE 内容，仍受系统通知设置与专注模式控制。前台横幅不依赖 APNs 投递，但需要 Matrix 同步完成；断线期间不能承诺即时显示。
- **保留 #145 DEBUG 探针与诊断页**：`PushForegroundProbeLog` 和 `os.Logger` 仍记录 delegate 安装、启动及 `willPresent` 的 optionsRaw，不写正文或凭据。历史 `optionsRaw=27` 是旧系统展示策略；此版本预期为 `1`，只能证明回调命中，不能作为 App 内横幅已可见的验收证据。Release 调用路径不写探针。
- 点击通知使用 Core 中的纯函数解析 `room_id`，接受与事件引用相同的顶层及两层已知包装，不依赖 `event_id` 或 payload 正文。合法 ID 切到「会话」tab 并打开对应会话；无 room 或非法字段则清除会话选择并回房间列表。冷启动点击在根视图就绪前保留待处理路由。
- NSE 仍为 `event_id_only`，`default_payload` 仍精确为 `{"aps":{"mutable-content":1,"alert":{"body":"…"}}}`；没有改成通用「新消息」alert，没有从 payload 取正文或显示名，没有修改配对 TLS 或 #132 路径。Release 的 APNs entitlement、诊断入口、局域网 HTTP 与手动 homeserver 隔离不变。

notify URL 必须无凭据、无 query/fragment，路径严格为 `/_matrix/push/v1/notify`。默认/Release 策略仅允许 HTTPS；DEBUG 显式允许 HTTP。该地址供 **homeserver 访问 Sygnal**，因此 Compose 内部服务名可作 notify URL；iPhone 本身不必能解析该名称。App 不直接向 Sygnal 上传 token。

NSE 的事件拉取也遵从 target 的 ATS：Debug NSE 的 `Info-Debug.plist` 显式设置 `NSAllowsLocalNetworking` 及本地网络用途说明，以便受控局域网 homeserver 联调；Release NSE 只允许 HTTPS。

`pushkey` 是 APNs device token 的 Foundation 标准 Base64 编码（`Data.base64EncodedString()`，保留 `=` 填充），与 Element iOS 和 Sygnal 的默认约定一致。不得同时注册 hex 与 Base64 两种形式。NSE/default payload 变更合入后，真机必须通过 `pushers/set` 重新注册，使服务器保存 `data.default_payload={"aps":{"mutable-content":1,"alert":{"body":"…"}}}`；同时继续保持 `data.format=event_id_only`。默认 payload 仍只含 `mutable-content: 1` 与 body「…」，不放入 Matrix 正文。已有小写 hex pusher 需先删除再以 Base64 重新注册。App 升级后内存中已不知道旧 hex pushkey，不会自动删除旧 hex pusher；需由服务端/管理侧手工删除，或待 homeserver 在 Sygnal 返回 `rejected` 后自动丢弃。

DEBUG 登录页保留手动 homeserver 输入及局域网 HTTP 开关。开关仅允许本地/私有地址、`.local` 名称和 Compose 单标签主机名；含字母的单标签主机名可能经设备的 DNS search domain 解析，不能保证仍在局域网内，因此只能在 DEBUG 显式开启后使用。iPhone 连接 homeserver 时仍需使用手机可解析、可到达的地址。Release 不编译手动输入视图，初次使用扫码配对；已有账号软退出时可对固定的已保存服务器/账号重新输入密码。

## 后续真机验收步骤

1. 在有权限的开发者环境中打开 `Botville.xcodeproj`，选择 `BotvilleApp-iOS` scheme 和 Debug 配置。App target 的 Debug 与 Release 都使用 Automatic signing，开发团队和 Bundle ID 读取 `Config/Signing.xcconfig`（默认 `3Z8WP6U6F7` / `com.zjyjcl.botville`），可用不入库的 `Local.xcconfig` 覆盖；由 Xcode 在受控开发环境解析签名。仓库不提交证书、描述文件或 provisioning profile。
2. 确认对应 App ID 已开启 Push Notifications、Xcode 管理的开发描述文件允许推送，且签名后的 entitlement 包含 `aps-environment=development`。Sygnal 配置使用独立的 Matrix pusher app ID `com.zjyjcl.botville.apns`（不随 Bundle 前缀变化），APNs topic 对应展开后的 Bundle ID；换账号/前缀后需单独核对 Sygnal 与推送配置，使用开发/sandbox 环境。Release 配置不继承这个 development entitlement；归档或分发前，须在受控签名环境另配 `aps-environment=production` 与匹配的生产签名。
3. 将 Debug 包安装到 Mac mini 连接的 iPhone 15，按系统提示允许本地网络（使用局域网时）和通知。登录测试 Matrix 账号，默认 notify URL 为 Compose 内网地址 `http://sygnal:5000/_matrix/push/v1/notify`；诊断页可按实际 homeserver 可达地址修改。
4. 申请注册，确认 token 非空、pusher 注册成功，再点「核对」确认匹配。请求应包含 `Authorization: Bearer <当前会话>`，`kind=http`、`append=false`、`app_display_name=botville`、设备名称、`lang=zh-CN`，以及 `data.format=event_id_only` 与精确的 `data.default_payload={"aps":{"mutable-content":1,"alert":{"body":"…"}}}`。检查证据时不得记录真实 access token 或 device token。
5. 将 App 放到后台，从另一个已授权测试账号发送符合通知规则的消息；检查 homeserver → Sygnal → APNs 的实际投递与手机表现。此步骤需要外部服务和真实设备，不能由 mock 测试代替。
6. 对非加密房间确认横幅显示被拉取的发送者和正文；用加密房间确认 body 为「加密消息」。再分别断开网络、令事件请求返回非 2xx 或让请求超时，确认仍交付「无法加载」或到期前的「…」，且不会崩溃。
7. token 回调发生变化时，应先删除旧 pushkey，再注册新 pushkey；相同 token 的回调也可用于重试注册。APNs token 轮换无法在真机上可靠强制触发，自动化测试覆盖该顺序与失败处理。
8. 在会话页点击「退出」，先用原 access token 发送 `kind: null` 删除 pusher，再撤销会话。断网导致删除失败时应保留会话、显示错误，恢复网络后重试。需要核查服务器侧清理时，通过受控服务端工具查看结果，勿将 token 写入报告。

## 真机验收（合入后）

重新构建并安装 **Debug** App，登录/恢复两个已授权测试账号。后台推送测试仍需在「推送诊断」注册并核对 pusher，确认 `event_id_only` 和原样 `default_payload`。前台横幅不要求 APNs 回调；不得在截图、日志或工单记录真实凭据。

1. **列表前台**：先等待首次同步完成，停留房间列表，让对端向已有非加密房间发消息。顶部应出现 initials、房间名/发送者显示名和本地正文，约 3 秒消失；导航标题、返回按钮仍可用。重复同一 sync 不再弹。若从 detail 返回列表且仍保留选择，该房间的新消息仍应弹。
2. **替换与点按**：在 3 秒内向同房连发两条，只保留最新一张，计时从新消息重新开始；在卡片消失前点按，应进入对应房间。另在「动态」或「我」tab 重测，点按应切回「会话」。多房同时更新时仅显示最后一条，不堆叠。
3. **抑制**：打开该房间 detail 后收消息不弹；在另一房间输入框获得焦点，或输入草稿尚未发送时收消息不弹；自己的消息也不弹。退出输入状态后不补弹之前被抑制的消息。列表未读更新不受影响。
4. **加密与降级**：加密房间始终显示「加密消息」；本地没有可展示正文显示「…」，消息解析失败显示「无法加载」。这些前台分支不触发 NSE 的 HTTP 拉取；用单测验证无法方便构造的 SDK 解析失败。显示名缺失时回退房间名且保留正文。
5. **后台 / 被杀回归**：分别将 App 放后台、终止后让对端发消息，继续验证系统 NSE 横幅和正文（加密仍固定提示），点击合法 room 通知能进入对应房间；无 room / 非法 room 回列表，退出账号则登录后处理待路由。NSE 的 profile 回退、HTTP/网络/JSON 失败「无法加载」、到期「…」仍按原路径验收。回到前台后已消费候选不重放。
6. **角标**：准备两个有未读的房间，列表同步后角标等于总和；读完一间只扣该间，全部已读为零，恢复前台重新计算。NSE 不参与主 App 的计数累加。
7. **生命周期与隐私**：横幅显示时切后台、开始输入或退出账号，卡片应立即清除；重新登录不显示上一账号内容。DEBUG 诊断仍能查看 willPresent 探针（新值为 `optionsRaw=1`）；Release 隔离脚本仍通过，不新增正文/token 日志。

这些步骤需真机实测。单测与模拟器构建仅证明本地逻辑/编译通过，不代表前台 UI、后台 APNs、终止态或冷启动路由已经验收。

## 数据与限制

- APNs device token 和上次注册的 pushkey 仅在内存中，既不写 UserDefaults/文件/日志，也不上传到分析、网关或其他服务；请求仅发往当前 homeserver 的 pusher API。采用内存是为了减少持久化设备标识与凭据生命周期耦合。
- 因此跨进程重启不记得旧 token：若 APNs 在 App 未运行时轮换 token，无法自动定位并删除上一个进程的旧 key。重新获得的当前 token 会重新 set；旧记录需由服务端维护或后续 Keychain 方案处理。不能通过删除同 app ID 的所有条目来清理，因为可能影响其他设备。
- 服务器已使会话失效时，登录页的「清除本地会话」只删除本地数据，不能保证移除服务端 pusher；页面不再把这个操作称为「退出登录」。需要服务端清理时，应先重新认证再正常退出，或通过服务端管理完成。
- 修改 notify URL 会更新当前 pusher；注册、轮换与退出按顺序执行。失败会显示安全化状态，可通过保存/注册按钮重试。
- HTTP 仅用于开发网络，不能提供传输保密性。`NSAllowsLocalNetworking` 不绕过网络可达性、系统本地网络授权或 TLS 证书校验，也不会让手机自动解析 Compose DNS。
- NSE 请求遵从 iOS 的系统 TLS 信任。自签 homeserver 必须先由系统建立信任链；本 PR 不实现证书例外或 SPKI pin。与 #132 的配对信任/固定策略是不同边界，不能替代此处的系统 TLS 信任。
- macOS 本次不支持 APNs 注册，诊断页明确显示此限制。模拟器可编译与查看 UI；其结果不能替代开发签名真机验收。
- 主 App 前台使用本地同步驱动的自绘横幅，后台继续 NSE；点击路由和角标重算沿用 PR3。不实现后台同步重构，真机前台 UI、真实推送与已读角标仍需按上述步骤验收。
- Release 不编译诊断页、权限申请控制器或 DEBUG 手动 homeserver 视图；Release plist 无局域网 ATS/用途说明/notify 默认配置。工程只为 Debug 配置 development entitlement；Release/分发必须另配 production entitlement 与匹配的生产签名和描述文件，当前仓库不代表已具备生产 APNs 发布配置。

## 本地验证

GitHub Actions 的 [`app-apple-tests`](../../.github/workflows/ci.yml) job 在 macOS runner 上执行 `cd app-apple && swift test`，自动纳入全部 Swift 单元／集成测试，包括 Pairing、Push*、ReleaseIsolation 及现有前台通知展示策略单测；前台 banner PR 的后续测试合入后也会自动纳入。需要本机 Synapse 的集成用例仍按环境变量门控，默认跳过。CI 不替代真机 APNs 推送验收：APNs 送达、NSE 执行和前台横幅的实际展示仍须按本文步骤人工验证。

在 `app-apple/` 执行：

```sh
export TMPDIR=/tmp
export GIT_CONFIG_COUNT=0
swift test
scripts/check-release-isolation.sh
xcodebuild -project Botville.xcodeproj -scheme BotvilleApp-iOS \
  -configuration Debug -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath .build/nse-derived-data CODE_SIGN_IDENTITY="-" build
xcodebuild -project Botville.xcodeproj -scheme BotvilleApp-iOS \
  -configuration Release -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath .build/nse-derived-data CODE_SIGN_IDENTITY="-" build
```

隔离脚本检查工程 Release plist 路由、递归禁止键和源码 DEBUG guard；XCTest 读取同一仓库文件做对应检查。构建完成后还应核对 `.build/nse-derived-data/Build/Products/Release-iphonesimulator/BotvilleApp.app/Info.plist`，确认不含 `NSAppTransportSecurity`、`NSAllowsLocalNetworking`、`NSLocalNetworkUsageDescription` 或 `BotvilleSygnalNotifyURL`。

## 本次验证（2026-11-05）

| 检查 | 结果 |
| --- | --- |
| `swift test`，macOS | 共 221 项，219 通过，2 跳过，0 失败 |
| Release 隔离脚本 | 通过 |
| Debug，generic iOS Simulator，`CODE_SIGN_IDENTITY="-"` | BUILD SUCCEEDED |
| Release，同上 | BUILD SUCCEEDED |
| Release 产物 Info.plist | 四项禁止键递归检查均不存在，Bundle ID 保持不变 |

本轮两项 Synapse 集成测试因未设置 `BOTVILLE_E2E_HOMESERVER` 跳过。推送测试覆盖 token 编码、完整请求字段与 Bearer header、删除体 `kind:null`、列表解析/匹配、URL 两种策略、轮换删除顺序、注册响应未返回时退出、删除失败后的保留与重试，以及 Release 文件隔离。未执行 iPhone/APNs/Sygnal 的真实送达验收。

本轮审查修复日志为 `.build/nit-swift-test.log`、`.build/nit-debug-build.log`、`.build/nit-release-build.log`，产物检查结果为 `.build/nit-product-check.json`（构建目录不提交）。主 scheme 的 Debug/Release 产物均包含 `PlugIns/BotvilleNotificationService.appex`；验证两者 Bundle ID、`-Simulated.xcent` 中展开后的共享分组及 ad-hoc 签名。构建仍有 Matrix SDK 外部 dSYM 路径等警告；未验证最低支持系统或真机送达。本轮命令使用 `TMPDIR=/tmp`，并以 `env -u GIT_CONFIG_COUNT -u GIT_CONFIG_VALUE_0` 移除本机不完整的 Git 环境覆盖，不改动 Git 配置。

## PR2 本地验证（2026-11-06）

按上面的本地命令执行，Git 环境额外设置 `GIT_CONFIG_COUNT=0`，避免本机继承的不完整 Git 配置覆盖。结果：

| 检查 | 结果 |
| --- | --- |
| `TMPDIR=/tmp swift test` | 233 项：231 通过、2 跳过、0 失败；其中新增正文拉取测试 12 项全部通过 |
| Debug / Release，generic iOS Simulator，`xcodebuild ... build` | 两套均 BUILD SUCCEEDED |
| 构建产物 | 两套 App 均嵌入 NSE；Bundle ID、ad-hoc 签名、NSE 无 SDK 链接通过；Release App/NSE plist 均无 DEBUG 网络配置 |
| `scripts/check-release-isolation.sh` | 通过；扫描 NSE target 实际编译的三个源文件、日志白名单、SDK 依赖和唯一事件 GET |
| `shellcheck -x` | CI 的 7 个服务器脚本及 NSE 隔离脚本，共 8 个通过；使用 worktree 内临时 ShellCheck 0.11.0 |
| 仓库 Python 单测 | server 149、pairing 127、gateway 549，共 825 项全部通过 |
| 其他本地 CI 检查 | notify examples、gateway OpenAPI、12 个 Python 文件语法、秘密守卫、占位配置和镜像固定守卫全部通过 |

正文单测使用假凭据和 `URLProtocol`，涵盖顶层及嵌套字段、无效字段与禁止混配、保留字/Unicode 路径编码、Bearer GET、正文截断、加密与不可展示内容、Keychain 缺失/异常、HTTP/网络/超时/JSON 失败、拒绝重定向，以及整体预算和到期取消后的单次交付。

完整 Swift 日志及 Debug/Release 构建日志在 `app-apple/.build/nse-pr2/`；仓库其他检查日志在 `.build/nse-pr2-checks/`，均不提交。两项 Synapse 集成测试未配置 `BOTVILLE_E2E_HOMESERVER` 而跳过；未启动 Docker 集成环境，未执行 PR2 真机/APNs 验收。构建仍有现有 Matrix SDK dSYM/链接目标版本等警告；本地测试及模拟器构建不代表真机正文送达已验收。

## PR3 本地验证（2026-11-06）

| 检查 | 结果 |
| --- | --- |
| `TMPDIR=/tmp GIT_CONFIG_COUNT=0 swift test` | 269 项：267 通过、2 跳过、0 失败 |
| 通知正文 / UX / Release 隔离单测 | 17 / 6 / 5 项全部通过，包含 profile 成功与失败、自然总预算到期、手动到期单次交付、深链、badge、前台策略及已读成功/失败的列表状态 |
| `scripts/check-release-isolation.sh` / `sh -n` / `git diff --check` | 全部通过；仅允许原事件 GET 和精确的额外 profile GET，token 注册与权限申请仍要求 DEBUG guard |
| Debug / Release，generic iOS Simulator，`CODE_SIGN_IDENTITY="-"` | 两套均 BUILD SUCCEEDED |
| 构建产物 | 两套均嵌入 NSE，Bundle ID 和 ad-hoc 签名校验通过；`-Simulated.xcent` 的 App Group/APNs 配置正确；NSE 无 SDK 链接；Release App/NSE plist 均无 DEBUG 网络配置 |

日志位于 `app-apple/.build/nse-pr3/{swift-test,debug-build,release-build}.log`，产物位于同目录的 `DerivedData/`，均不提交。两项 Synapse 集成测试因未配置 `BOTVILLE_E2E_HOMESERVER` 跳过。构建有 Matrix SDK dSYM/链接等既有警告，以及 `applicationIconBadgeNumber` 在 iOS 17 的弃用警告；本次仍使用该同步 badge API。模拟器的 ad-hoc 签名和 `-Simulated.xcent` 检查不能替代真机开发签名与推送送达验收；未执行 iPhone/APNs/Sygnal 真机验收，合入后必须按 PR3 步骤重装 Debug 并重新执行 `pushers/set`。

## 前台自绘横幅本地验证（2026-11-06）

| 检查 | 结果 |
| --- | --- |
| `swift test` | 289 项：287 通过、2 项 Synapse 集成测试跳过、0 失败 |
| 本次新增测试 | 19 项：候选基线/去重/抑制/计时/路由 11 项，SDK 本地文案映射 7 项，MatrixService 实际 refresh 发布 1 项 |
| Debug，generic iOS Simulator，`CODE_SIGN_IDENTITY="-"` | BUILD SUCCEEDED（arm64 + x86_64） |
| `scripts/check-release-isolation.sh` / `git diff --check` | 通过；#145 DEBUG 探针和诊断页保留 |

日志位于本 worktree 的 `app-apple/.build/fg-banner-{swift-test,debug-build,isolation}.log`，产物位于 `.build/fg-banner-derived-data/`，不提交。运行时未配置 Synapse 集成环境；本轮未执行真机或模拟器 UI 验收。构建仍有既有 Matrix SDK 外部 dSYM 路径、badge API 弃用及 AppIntents 无依赖提示。前台展示、点击与输入抑制以及后台/被杀 NSE 送达，需要按上面的真机步骤验收。
