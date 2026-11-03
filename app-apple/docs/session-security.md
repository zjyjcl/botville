<!-- SPDX-License-Identifier: Apache-2.0 -->

# 会话安全边界

## Release 恢复

`allowLocalHTTP` 是旧 Debug 会话中保存的开发标记。恢复时由正在运行的构建重新判定：只有 Debug 且该标记为真时，才允许 `localhost` 或 `127.0.0.1` 的 HTTP；Release 一律按 HTTPS 策略验证。公开的 `MatrixService.login` 入口也使用同一构建门禁，不能通过绕过 UI 传入 `allowLocalHTTP` 放宽 Release。

## Soft logout 与 refresh token

固定的 `matrix-rust-components-swift` **26.10.27**（commit `989ea1cc1e0c38cb2dd63970b8ab03d7c1e192eb`，对应 Rust SDK `e675036b27be102b05ec5c5bff843cf8d739338c`）具备自动 refresh 与 token 轮换持久化接口，不能简单认定 SDK 不支持：

- Swift `ClientBuilder.disableAutomaticTokenRefresh()` 控制自动 refresh，`setSessionDelegate()` 可注册 `ClientSessionDelegate`。其 `retrieveSessionFromKeychain(userId:)` / `saveSessionInKeychain(session:)` 承担 token 读取和保存。固定 [Rust builder 源码](https://github.com/matrix-org/matrix-rust-sdk/blob/e675036b27be102b05ec5c5bff843cf8d739338c/bindings/matrix-sdk-ffi/src/client_builder.rs#L607-L609) 在未 disable 时启用 `handle_refresh_tokens()`。
- 固定 [Client 源码](https://github.com/matrix-org/matrix-rust-sdk/blob/e675036b27be102b05ec5c5bff843cf8d739338c/bindings/matrix-sdk-ffi/src/client.rs#L485-L502) 将 token retrieve/save 接到 delegate；[保存回调](https://github.com/matrix-org/matrix-rust-sdk/blob/e675036b27be102b05ec5c5bff843cf8d739338c/bindings/matrix-sdk-ffi/src/client.rs#L3025-L3047) 读取轮换后的 Session 并调用 `saveSessionInKeychain`。普通 `ClientDelegate` 的 [`TokensRefreshed` 分支为空](https://github.com/matrix-org/matrix-rust-sdk/blob/e675036b27be102b05ec5c5bff843cf8d739338c/bindings/matrix-sdk-ffi/src/client.rs#L3008-L3017)，不能靠当前认证错误回调保存新 token。
- 当前 app 没有接入 session delegate，仅移除 disable 会留下轮换与 Keychain 不一致的风险。Swift `Client` 没有直接的 access-token refresh 方法；`HomeserverCapabilities.refresh()` 只刷新服务端能力。固定 FFI 密码登录参数不暴露请求 refresh token 的选项；app 自有 REST 密码请求本轮也未设置 `refresh_token: true`，单测固定此默认行为。收到的可选 refresh token 仍兼容保存在 Keychain。

**本次继续关闭自动 refresh，不使用 refresh token。** 后续需先实现 Keychain session delegate，覆盖保存失败、轮换后重启、多请求并发及旧 token 失效路径，再启用自动 refresh；因此本 PR 对 #90 使用 Refs。

SDK 的 `M_UNKNOWN_TOKEN` 带 `softLogout: true` 时，服务端要求重新认证但明确保留该会话。客户端会停用当前 SDK client，保留 Keychain 记录和加密 SQLite store，并提示用户重输密码；请求会带原 device ID，成功后复用原 store ID 和 store key。重新认证期间不再自动恢复旧 token。密码错误、取消或新会话激活失败不会删除本地数据；登录页始终提供经二次确认的退出登录和清除本地会话入口，清除后可修改账号与服务器。两者均复用 `clearLocalSession()`：删除本机 Keychain 会话及本地消息数据库（包括待发送消息），不撤销服务器设备会话、不删除服务器消息。`softLogout: false` 仍按失效会话清理。

失败的 soft logout 重登不会对候选 token 调用 `/logout`。[Matrix 规范](https://spec.matrix.org/latest/client-server-api/#post_matrixclientv3logout)规定该接口还会删除关联 device 及其 device keys；同设备重登后调用它，会破坏原设备加密状态。代价是激活或持久化失败后的候选 token 可能仍在服务器有效，当前不能保证立即撤销它；后续需服务端支持安全的单 token 撤销或明确的设备管理流程。普通新登录失败仍执行凭据撤销。正常登录在激活前保存新 store key；soft logout 保留旧记录到替换会话激活成功，原子保存失败仍保留旧记录与本地清除出口。若用户清除本地会话时发生部分失败，必须先完成清除，不能继续用旧记录重建已删除的 store。


本轮使用模拟 client/password transport 验证设备 ID、store ID/key、目录内容保留与失败路径；不代表真实服务端 token rotation 或旧加密历史端到端验收。
