// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import BotvilleCore
import SwiftUI

@MainActor
struct LoginView: View {
    @ObservedObject var service: MatrixService

    @State private var homeserver = "https://matrix.example.org"
    @State private var username = ""
    @State private var password = ""
    @State private var isLoggingIn = false
    @State private var errorMessage: String?
    @State private var confirmLocalClear = false
    @State private var isClearing = false
#if DEBUG
    @State private var allowLocalHTTP = false
#endif

    private var canLogIn: Bool {
        !homeserver.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && !username.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && !password.isEmpty
            && !isLoggingIn && !service.isAuthenticating && !service.isRestoring && !isClearing
    }

    var body: some View {
        VStack(spacing: 20) {
            Image(systemName: "bubble.left.and.bubble.right.fill")
                .font(.system(size: 48))
                .foregroundStyle(.tint)

            VStack(spacing: 6) {
                Text("botville")
                    .font(.largeTitle.bold())
                Text("登录你的 Matrix 服务器")
                    .foregroundStyle(.secondary)
            }

            Form {
                TextField("Homeserver", text: $homeserver)
                    .autocorrectionDisabled()
#if os(iOS)
                    .textInputAutocapitalization(.never)
                    .keyboardType(.URL)
#endif
                TextField("用户名或用户 ID", text: $username)
                    .autocorrectionDisabled()
#if os(iOS)
                    .textInputAutocapitalization(.never)
#endif
                SecureField("密码", text: $password)
#if DEBUG
                Toggle("允许本机 HTTP（仅 localhost/127.0.0.1）", isOn: $allowLocalHTTP)
#endif
            }
            .frame(maxWidth: 480)

            if let errorMessage = errorMessage ?? service.sessionError {
                Text(errorMessage)
                    .font(.footnote)
                    .foregroundStyle(.red)
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: 480)
            }

            if let status = service.authenticationStatus {
                Text(status).font(.footnote).multilineTextAlignment(.center)
            }

            if service.isAuthenticating || service.isRestoring {
                Button("取消登录或恢复") { service.cancelAuthentication() }
                Text("操作可取消；取消后如仍有请求结束中，会先清理再允许重试。")
                    .font(.caption).foregroundStyle(.secondary)
            }

            if service.canRetryRestore {
                Button("重试恢复会话") { Task { await service.restoreSession() } }
                    .disabled(isLoggingIn || service.isAuthenticating || service.isRestoring || isClearing)
            }

            if service.canClearLocalSession {
                Button("清除本地会话并重新登录", role: .destructive) { confirmLocalClear = true }
                    .disabled(isLoggingIn || service.isAuthenticating || service.isRestoring || isClearing)
            }

            Button(action: logIn) {
                if isLoggingIn {
                    ProgressView()
                        .controlSize(.small)
                } else {
                    Text("登录")
                }
            }
            .buttonStyle(.borderedProminent)
            .disabled(!canLogIn)
        }
        .confirmationDialog("清除本地会话并重新登录？", isPresented: $confirmLocalClear, titleVisibility: .visible) {
            Button("清除本地会话", role: .destructive) {
                isClearing = true
                Task {
                    defer { isClearing = false }
                    do { try await service.clearLocalSession(); errorMessage = nil; password = "" }
                    catch { errorMessage = error.localizedDescription }
                }
            }
            Button("取消", role: .cancel) {}
        } message: {
            Text("将删除此 App 的 Keychain 会话和本地消息数据库，包括尚未发送的消息。服务器上的消息不会删除；此操作不会撤销服务器上的设备会话。")
        }
        .padding(32)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func logIn() {
        guard canLogIn else { return }

        isLoggingIn = true
        errorMessage = nil
        Task {
            defer { isLoggingIn = false; password = "" }
            do {
                try await service.login(
                    homeserver: homeserver.trimmingCharacters(in: .whitespacesAndNewlines),
                    username: username.trimmingCharacters(in: .whitespacesAndNewlines),
                    password: password,
                    allowLocalHTTP: {
#if DEBUG
                        allowLocalHTTP
#else
                        false
#endif
                    }()
                )
                password = ""
            } catch is CancellationError {
                errorMessage = "已取消登录。"
            } catch {
                errorMessage = error.localizedDescription
            }
        }
    }
}
