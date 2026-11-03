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

    private var canLogIn: Bool {
        !homeserver.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && !username.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && !password.isEmpty
            && !isLoggingIn
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
            }
            .frame(maxWidth: 480)

            if let errorMessage {
                Text(errorMessage)
                    .font(.footnote)
                    .foregroundStyle(.red)
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: 480)
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
        .padding(32)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func logIn() {
        guard canLogIn else { return }

        isLoggingIn = true
        errorMessage = nil
        Task {
            defer { isLoggingIn = false }
            do {
                try await service.login(
                    homeserver: homeserver.trimmingCharacters(in: .whitespacesAndNewlines),
                    username: username.trimmingCharacters(in: .whitespacesAndNewlines),
                    password: password
                )
                password = ""
            } catch {
                errorMessage = error.localizedDescription
            }
        }
    }
}
