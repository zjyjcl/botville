<!-- SPDX-License-Identifier: Apache-2.0 -->

# DEBUG 真机推送

本次提供 iOS 开发包的 APNs 注册、Matrix HTTP pusher 注册及诊断入口。真机目标为 Mac mini 连接的 iPhone 15；Sygnal/APNs 联调留待后续，模拟器构建和单元测试不能证明真实通知送达。

## 入口与配置

在 Debug 包中打开 **我 → 设置 → 推送诊断**：

1. 保存 Sygnal notify URL。默认从 `Info-Debug.plist` 的 `BotvilleSygnalNotifyURL` 读取：`http://sygnal:5000/_matrix/push/v1/notify`。用户保存值优先，诊断页可修改覆盖，且仅此地址存入 UserDefaults。
2. 点击「申请权限并注册 / 重试」，允许系统通知权限。获准后调用 `registerForRemoteNotifications`，通过 `UIApplicationDelegateAdaptor` 接收成功/失败回调。已授权的后续启动也会重新向 APNs 注册。
3. 页面显示小写十六进制 device token、APNs 注册状态和安全化错误。可主动复制 token；iOS 剪贴板只在本机可用，120 秒后过期。不要将 token 贴到日志、工单或聊天中。
4. 使用当前 Matrix 会话自动注册 pusher。点击「核对」读取 `GET /_matrix/client/v3/pushers`，检查 `app_id + pushkey` 是否同时匹配。显示匹配仅代表服务端有记录，不能替代送达验证。

notify URL 必须无凭据、无 query/fragment，路径严格为 `/_matrix/push/v1/notify`。默认/Release 策略仅允许 HTTPS；DEBUG 显式允许 HTTP。该地址供 **homeserver 访问 Sygnal**，因此 Compose 内部服务名可作 notify URL；iPhone 本身不必能解析该名称。App 不直接向 Sygnal 上传 token。

DEBUG 登录页保留手动 homeserver 输入及局域网 HTTP 开关。开关仅允许本地/私有地址、`.local` 名称和 Compose 单标签主机名；含字母的单标签主机名可能经设备的 DNS search domain 解析，不能保证仍在局域网内，因此只能在 DEBUG 显式开启后使用。iPhone 连接 homeserver 时仍需使用手机可解析、可到达的地址。Release 不编译手动输入视图，初次使用扫码配对；已有账号软退出时可对固定的已保存服务器/账号重新输入密码。

## 后续真机验收步骤

1. 在有权限的开发者环境中打开 `Botville.xcodeproj`，选择 `BotvilleApp-iOS` scheme 和 Debug 配置。App target 的 Debug 与 Release 都使用 Automatic signing，开发团队为 `3Z8WP6U6F7`；由 Xcode 为 Mac mini 连接的 iPhone 15 解析受控的开发签名。Bundle ID 保持 `com.zjyjcl.botville`。仓库不提交证书、描述文件或 provisioning profile。
2. 确认对应 App ID 已开启 Push Notifications、Xcode 管理的开发描述文件允许推送，且签名后的 entitlement 包含 `aps-environment=development`。Sygnal 配置使用 app ID `com.zjyjcl.botville.apns`，APNs topic 对应 Bundle ID，使用开发/sandbox 环境。Release 配置不继承这个 development entitlement；归档或分发前，须在受控签名环境另配 `aps-environment=production` 与匹配的生产签名。
3. 将 Debug 包安装到 Mac mini 连接的 iPhone 15，按系统提示允许本地网络（使用局域网时）和通知。登录测试 Matrix 账号，默认 notify URL 为 Compose 内网地址 `http://sygnal:5000/_matrix/push/v1/notify`；诊断页可按实际 homeserver 可达地址修改。
4. 申请注册，确认 token 非空、pusher 注册成功，再点「核对」确认匹配。请求应包含 `Authorization: Bearer <当前会话>`，`kind=http`、`append=false`、`app_display_name=botville`、设备名称、`lang=zh-CN`，及 `data.format=event_id_only`。检查证据时不得记录真实 access token 或 device token。
5. 将 App 放到后台，从另一个已授权测试账号发送符合通知规则的消息；检查 homeserver → Sygnal → APNs 的实际投递与手机表现。此步骤需要外部服务和真实设备，不能由 mock 测试代替。
6. token 回调发生变化时，应先删除旧 pushkey，再注册新 pushkey；相同 token 的回调也可用于重试注册。APNs token 轮换无法在真机上可靠强制触发，自动化测试覆盖该顺序与失败处理。
7. 在会话页点击「退出」，先用原 access token 发送 `kind: null` 删除 pusher，再撤销会话。断网导致删除失败时应保留会话、显示错误，恢复网络后重试。需要核查服务器侧清理时，通过受控服务端工具查看结果，勿将 token 写入报告。

## 数据与限制

- token 和上次注册的 pushkey 仅在内存中，既不写 UserDefaults/文件/日志，也不上传到分析、网关或其他服务；请求仅发往当前 homeserver 的 pusher API。采用内存是为了减少持久化设备标识与凭据生命周期耦合。
- 因此跨进程重启不记得旧 token：若 APNs 在 App 未运行时轮换 token，无法自动定位并删除上一个进程的旧 key。重新获得的当前 token 会重新 set；旧记录需由服务端维护或后续 Keychain 方案处理。不能通过删除同 app ID 的所有条目来清理，因为可能影响其他设备。
- 服务器已使会话失效时，登录页的「清除本地会话」只删除本地数据，不能保证移除服务端 pusher；页面不再把这个操作称为「退出登录」。需要服务端清理时，应先重新认证再正常退出，或通过服务端管理完成。
- 修改 notify URL 会更新当前 pusher；注册、轮换与退出按顺序执行。失败会显示安全化状态，可通过保存/注册按钮重试。
- HTTP 仅用于开发网络，不能提供传输保密性。`NSAllowsLocalNetworking` 不绕过网络可达性、系统本地网络授权或 TLS 证书校验，也不会让手机自动解析 Compose DNS。
- macOS 本次不支持 APNs 注册，诊断页明确显示此限制。模拟器可编译与查看 UI；其结果不能替代开发签名真机验收。
- 这是开发推送链路准备工作，尚未实现通知服务扩展、从 `event_id_only` 获取通知正文、后台事件处理或点击通知跳转。完整可见通知取决于后续服务端 payload 与客户端通知展示实现。
- Release 不编译诊断页、权限申请控制器或 DEBUG 手动 homeserver 视图；Release plist 无局域网 ATS/用途说明/notify 默认配置。工程只为 Debug 配置 development entitlement；Release/分发必须另配 production entitlement 与匹配的生产签名和描述文件，当前仓库不代表已具备生产 APNs 发布配置。

## 本地验证

在 `app-apple/` 执行：

```sh
swift test
scripts/check-release-isolation.sh
xcodebuild -project Botville.xcodeproj -scheme BotvilleApp-iOS \
  -configuration Debug -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath DerivedData CODE_SIGN_IDENTITY="-" build
xcodebuild -project Botville.xcodeproj -scheme BotvilleApp-iOS \
  -configuration Release -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath DerivedData CODE_SIGN_IDENTITY="-" build
```

隔离脚本检查工程 Release plist 路由、递归禁止键和源码 DEBUG guard；XCTest 读取同一仓库文件做对应检查。构建完成后还应核对 `DerivedData/Build/Products/Release-iphonesimulator/BotvilleApp.app/Info.plist`，确认不含 `NSAppTransportSecurity`、`NSAllowsLocalNetworking`、`NSLocalNetworkUsageDescription` 或 `BotvilleSygnalNotifyURL`。

## 本次验证（2026-11-05）

| 检查 | 结果 |
| --- | --- |
| `swift test`，macOS | 0 失败；跳过项随本机环境而定 |
| Release 隔离脚本 | 通过 |
| Debug，generic iOS Simulator，`CODE_SIGN_IDENTITY="-"` | BUILD SUCCEEDED |
| Release，同上 | BUILD SUCCEEDED |
| Release 产物 Info.plist | 四项禁止键递归检查均不存在，Bundle ID 保持不变 |

跳过项取决于本机的 Keychain 与本地 Synapse 配置。推送测试覆盖 token 编码、完整请求字段与 Bearer header、删除体 `kind:null`、列表解析/匹配、URL 两种策略、轮换删除顺序、注册响应未返回时退出、删除失败后的保留与重试，以及 Release 文件隔离。未执行 iPhone/APNs/Sygnal 的真实送达验收。

本地日志在 `.build/validation/swift-test.log`、`xcode-debug.log`、`xcode-release.log`，产物检查结果在 `product-plist-check.json`（构建目录不提交）。构建保留固定 Matrix SDK 的外部 dSYM 路径及最低 macOS 版本警告，本次未验证最低支持系统。Xcode 本轮使用 `-derivedDataPath DerivedData` 和 `-clonedSourcePackagesDirPath .build/xcode-packages`；未使用会导致本机解析出空依赖图的 `-packageCachePath` 参数。
