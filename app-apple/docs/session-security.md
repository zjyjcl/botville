<!-- SPDX-License-Identifier: Apache-2.0 -->

# 会话安全边界

## Release 恢复

`allowLocalHTTP` 是旧 Debug 会话中保存的开发标记。恢复时由正在运行的构建重新判定：只有 Debug 且该标记为真时，才允许 `localhost` 或 `127.0.0.1` 的 HTTP；Release 一律按 HTTPS 策略验证。公开的 `MatrixService.login` 入口也使用同一构建门禁，不能通过绕过 UI 传入 `allowLocalHTTP` 放宽 Release。

## 加密存储丢失时强制重新登录（#81，关联 #245）

**适用范围**：这套机制只**防止再次发生**，不修复已经发生的情况。旧版本已经在空库上以旧设备 ID 重建过加密身份的设备（库文件如今非空、检测判为「在」），不会被强制换成新设备；这类设备走「清除本地会话」再重新登录。iPhone 15 的设备验收走的就是这条路径。旧设备在服务端留下的 device keys 会继续让对方向一个本机已无私钥的设备加密，表现为解密失败等加密噪音；既成设备和服务端旧设备的清理见 #254。

**场景**：Keychain 里的会话记录还在，但本机 SDK 加密存储已丢失——iOS 卸载重装（Keychain 保留、App 容器清空）、Application Support 下的存储目录被清空或数据库被删、首次建库中断留下空文件等。此前恢复流程会先 `create(for: storeID)` 补建空目录，再让 SDK 在新库里以**旧设备 ID** 生成一套新的 Olm 账号；服务端看到同一设备 ID 换了身份密钥，导致 #245 中的 OTK 签名失败和对方无法加密给本机。

**检测信号**：该 storeID 目录下 SDK 加密数据库 `matrix-sdk-crypto.sqlite3` 的状态（`SessionStoreDirectory.cryptoStoreState(for:)`），分三态：

- **在**：非空普通文件。
- **丢失**：只认确定的结论——目录或文件不存在（ENOENT / ENOTDIR / Cocoa NoSuchFile），或文件为 0 字节；只接受普通文件，符号链接和目录也按丢失处理（有意为之：SDK 只会写普通文件，链接指向的库不可信）。
- **未定**：读元数据报权限错误或其他错误、读不到文件大小。未定**不算丢失**。

- 选它的理由：Olm 账号（设备身份密钥）只存在这个库里，它的有无就是「旧设备 ID 还能不能安全复用」的直接判据。检查只读，在构建 SDK client、`create(for:)` 和任何网络请求之前执行，不会顺手建库。已安装用户的库本来就有这个文件，不需要迁移或额外写 sentinel。
- 没选的方案：只看目录——覆盖不了「目录在、库没了」；自写 sentinel 文件——老用户没有 sentinel，需要迁移逻辑，且 sentinel 在而库被删时会漏判；比对 SDK 加密身份——必须先用旧设备 ID 构建 client 并 restore，SDK 会在空库里先生成新账号，正是要避免的动作，还要额外在 Keychain 里存指纹。
- 文件名来自固定 SDK 26.10.27（在其二进制中确认 `matrix-sdk-crypto.sqlite3` / `matrix-sdk-state.sqlite3` 位于 data path，event-cache / media 位于 cache path）。`CryptoStoreDetectionTests` 用生产 `clientFactory` 和真 SDK 离线建库，断言文件正落在检测位置；升级 SDK 若改名或改位置，这条测试会先失败。

**受保护数据未就绪时不判定**：iOS 首次解锁前（`UIApplication.isProtectedDataAvailable == false`），或元数据读取得到「未定」时，不清任何东西：保留 Keychain 会话和本地存储，提示稍后继续，可重试，也保留「清除本地会话」出口。收到 `protectedDataDidBecomeAvailableNotification` 或回到前台时再判定一次。

**误判风险**：

- 假阳性（库在却判丢）：SDK 升级改了文件名/位置（由上面的真 SDK 用例拦截）。暂时不可读不再导致误判（见上一条）。
- 假阴性（库坏却判在）：数据库内容损坏但文件非空、或 store key 与库不匹配——检测不覆盖，仍走既有路径：SDK 构建失败按存储错误提示，用户可用「清除本地会话」退出。不对这类情况自动清除，避免把暂时性错误当成丢失。

**行为**：判定丢失后绝不构建 client、不调用 `restoreSession`、不向服务端发送旧设备 ID。顺序是：

1. 用 Keychain 里旧会话自己的 access token 尽力调用一次 `POST /_matrix/client/v3/logout`（只登出这个 token 对应的设备，服务端随之删除它的 device keys 和 pusher）。不调用 `/logout/all`，也不调用 `delete_devices`，不走 UIA，不重试；总时长上限 5 秒，网络卡住也不会拖住流程。成功与否都只在 DEBUG 日志里记录结果类别，不记 token 或任何标识。homeserver 地址按 Release 恢复规则校验，不会经 HTTP 发出 token。
2. 删除 Keychain 会话、扩展可读的共享推送凭据和本地存储目录（含残留目录），回到登录页并提示加密数据已丢失、需要重新登录（具体文案以代码为准）。若 Keychain 删除失败，则不删本地目录，改为存储错误提示，并保留「清除本地会话」出口。

前台事件不会再次触发恢复或 `/logout`。soft logout 重输密码时同样先检查，库已丢失就不带旧设备 ID 去登录，按同样方式处理。重新登录不传 device ID，由服务端分配新设备、新建 store ID 与 store key。正常启动、正常登出再登录、首次安装不受影响，也不会触发这次 `/logout`（均有单测）。

**`/logout` 失败时的残留**：旧设备的 pusher 和 device keys 仍留在服务端，重新登录后可能出现重复推送、旧设备推送无法解密的通知，以及上面说的加密噪音；扩展的共享凭据已随旧会话删除。这些不在客户端自动重试，由 #254 跟踪。

**测试数**（以代码为准）：#251 在 `MatrixServiceLifecycleTests` 新增 6 个测试方法（先失败提交里 4 个失败、2 个正常路径通过），共 9 种情形：存储目录缺失；空目录、0 字节库、只剩 state 库和 cache 三种；丢失后重新登录；soft logout 期间丢失；正常启动；首次安装；登出再登录再恢复。另有 `CryptoStoreDetectionTests` 2 个方法。#251 描述中的「新增 7 项」「3 项正常路径」计数有误，以此为准。本次（尽力 `/logout` 与延后判定）新增 9 个测试方法：`MatrixServiceLifecycleTests` 7 个、`CryptoStoreDetectionTests` 1 个、`PasswordAuthenticationTests` 1 个。

## Soft logout 与 refresh token

固定的 `matrix-rust-components-swift` **26.10.27**（commit `989ea1cc1e0c38cb2dd63970b8ab03d7c1e192eb`，对应 Rust SDK `e675036b27be102b05ec5c5bff843cf8d739338c`）具备自动 refresh 与 token 轮换持久化接口，不能简单认定 SDK 不支持：

- Swift `ClientBuilder.disableAutomaticTokenRefresh()` 控制自动 refresh，`setSessionDelegate()` 可注册 `ClientSessionDelegate`。其 `retrieveSessionFromKeychain(userId:)` / `saveSessionInKeychain(session:)` 承担 token 读取和保存。固定 [Rust builder 源码](https://github.com/matrix-org/matrix-rust-sdk/blob/e675036b27be102b05ec5c5bff843cf8d739338c/bindings/matrix-sdk-ffi/src/client_builder.rs#L607-L609) 在未 disable 时启用 `handle_refresh_tokens()`。
- 固定 [Client 源码](https://github.com/matrix-org/matrix-rust-sdk/blob/e675036b27be102b05ec5c5bff843cf8d739338c/bindings/matrix-sdk-ffi/src/client.rs#L485-L502) 将 token retrieve/save 接到 delegate；[保存回调](https://github.com/matrix-org/matrix-rust-sdk/blob/e675036b27be102b05ec5c5bff843cf8d739338c/bindings/matrix-sdk-ffi/src/client.rs#L3025-L3047) 读取轮换后的 Session 并调用 `saveSessionInKeychain`。普通 `ClientDelegate` 的 [`TokensRefreshed` 分支为空](https://github.com/matrix-org/matrix-rust-sdk/blob/e675036b27be102b05ec5c5bff843cf8d739338c/bindings/matrix-sdk-ffi/src/client.rs#L3008-L3017)，不能靠当前认证错误回调保存新 token。
- 当前 app 没有接入 session delegate，仅移除 disable 会留下轮换与 Keychain 不一致的风险。Swift `Client` 没有直接的 access-token refresh 方法；`HomeserverCapabilities.refresh()` 只刷新服务端能力。固定 FFI 密码登录参数不暴露请求 refresh token 的选项；app 自有 REST 密码请求本轮也未设置 `refresh_token: true`，单测固定此默认行为。收到的可选 refresh token 仍兼容保存在 Keychain。

**本次继续关闭自动 refresh，不使用 refresh token。** 后续需先实现 Keychain session delegate，覆盖保存失败、轮换后重启、多请求并发及旧 token 失效路径，再启用自动 refresh；因此本 PR 对 #90 使用 Refs。

SDK 的 `M_UNKNOWN_TOKEN` 带 `softLogout: true` 时，服务端要求重新认证但明确保留该会话。客户端会停用当前 SDK client，保留 Keychain 记录和加密 SQLite store，并提示用户重输密码；请求会带原 device ID，成功后复用原 store ID 和 store key。重新认证期间不再自动恢复旧 token。密码错误、取消或新会话激活失败不会删除本地数据；登录页始终提供经二次确认的退出登录和清除本地会话入口，清除后可修改账号与服务器。两者均复用 `clearLocalSession()`：删除本机 Keychain 会话及本地消息数据库（包括待发送消息），不撤销服务器设备会话、不删除服务器消息。`softLogout: false` 仍按失效会话清理。

失败的 soft logout 重登不会对候选 token 调用 `/logout`。[Matrix 规范](https://spec.matrix.org/latest/client-server-api/#post_matrixclientv3logout)规定该接口还会删除关联 device 及其 device keys；同设备重登后调用它，会破坏原设备加密状态。代价是激活或持久化失败后的候选 token 可能仍在服务器有效，当前不能保证立即撤销它；后续需服务端支持安全的单 token 撤销或明确的设备管理流程。普通新登录失败仍执行凭据撤销。正常登录在激活前保存新 store key；soft logout 保留旧记录到替换会话激活成功，原子保存失败仍保留旧记录与本地清除出口。若用户清除本地会话时发生部分失败，必须先完成清除，不能继续用旧记录重建已删除的 store。


本轮使用模拟 client/password transport 验证设备 ID、store ID/key、目录内容保留与失败路径；不代表真实服务端 token rotation 或旧加密历史端到端验收。
