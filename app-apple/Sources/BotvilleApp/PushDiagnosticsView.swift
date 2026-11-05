// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

#if DEBUG
import BotvilleCore
import SwiftUI
import UserNotifications
#if os(iOS)
import UIKit
#elseif os(macOS)
import AppKit
#endif

/// Development APNs state stays in memory. Only the non-secret notify URL is
/// persisted; the Matrix service owns token rotation and authenticated requests.
@MainActor
final class PushDeviceController: ObservableObject {
    static let shared = PushDeviceController()
    private static let notifyURLKey = "BotvilleSygnalNotifyURL"

    @Published private(set) var authorizationStatus = "尚未申请通知权限"
    @Published private(set) var registrationStatus = "尚未向 APNs 注册"
    @Published private(set) var registrationError: String?
    @Published private(set) var isRequesting = false
    @Published private(set) var savedNotifyURL: String
    private weak var service: MatrixService?
    private var pendingToken: Data?
    private var prepared = false

    private init() {
        savedNotifyURL = UserDefaults.standard.string(forKey: Self.notifyURLKey)
            ?? Bundle.main.object(forInfoDictionaryKey: Self.notifyURLKey) as? String ?? ""
    }

    func prepare(service: MatrixService) async {
        self.service = service
        guard !prepared else { return }
        prepared = true
        if !savedNotifyURL.isEmpty {
            do { try await configure(savedNotifyURL) }
            catch { registrationError = "推送地址无效，请在推送诊断中重新保存。" }
        }
        if let pendingToken { service.receivePushDeviceToken(pendingToken) }
#if os(iOS)
        // Re-register each launch only if permission was already granted.
        // First-time permission remains an explicit diagnostic-page action.
        let settings = await UNUserNotificationCenter.current().notificationSettings()
        updateAuthorization(settings.authorizationStatus)
        if permitsNotifications(settings.authorizationStatus) {
            registrationStatus = "正在向 APNs 注册…"
            UIApplication.shared.registerForRemoteNotifications()
        }
#else
        authorizationStatus = "此开发版本仅支持 iOS 真机 APNs"
        registrationStatus = "macOS 暂不支持 APNs 注册"
#endif
    }

    func saveNotifyURL(_ value: String) async throws {
        let normalized = try PushNotifyURLPolicy.validate(value.trimmingCharacters(in: .whitespacesAndNewlines), allowHTTP: true)
        try await configure(normalized.absoluteString)
        savedNotifyURL = normalized.absoluteString
        UserDefaults.standard.set(savedNotifyURL, forKey: Self.notifyURLKey)
    }

    private func configure(_ value: String) async throws {
        guard let service else { return }
#if os(iOS)
        let deviceName = UIDevice.current.name
#else
        let deviceName = "Mac"
#endif
        try await service.configurePush(notifyURL: value, deviceDisplayName: deviceName, allowHTTP: true)
    }

    func requestAuthorization() async {
        guard !isRequesting else { return }
        isRequesting = true
        registrationError = nil
        defer { isRequesting = false }
#if os(iOS)
        do {
            let center = UNUserNotificationCenter.current()
            let granted = try await center.requestAuthorization(options: [.alert, .badge, .sound])
            let settings = await center.notificationSettings()
            updateAuthorization(settings.authorizationStatus)
            guard granted else {
                registrationStatus = "通知权限未获准"
                registrationError = "请在系统设置中允许 botville 通知，然后重新注册。"
                return
            }
            registrationStatus = "正在向 APNs 注册…"
            UIApplication.shared.registerForRemoteNotifications()
        } catch {
            // Do not show raw transport errors or log device tokens.
            registrationError = "通知权限申请失败，请稍后重试。"
        }
#else
        registrationStatus = "macOS 暂不支持 APNs 注册，请使用已签名的 iPhone Debug 包。"
#endif
    }

    func didRegister(_ token: Data) {
        pendingToken = token
        registrationError = nil
        registrationStatus = "APNs 注册成功"
        service?.receivePushDeviceToken(token)
    }

    func didFailRegistration(_ error: Error) {
        registrationStatus = "APNs 注册失败"
        registrationError = "注册失败（代码 \((error as NSError).code)），请检查真机签名、推送权限及网络。"
    }

    private func permitsNotifications(_ status: UNAuthorizationStatus) -> Bool {
        if status == .authorized || status == .provisional { return true }
#if os(iOS)
        if status == .ephemeral { return true }
#endif
        return false
    }

    private func updateAuthorization(_ status: UNAuthorizationStatus) {
        switch status {
        case .notDetermined: authorizationStatus = "尚未申请"
        case .denied: authorizationStatus = "已拒绝"
        case .authorized: authorizationStatus = "已允许"
        case .provisional: authorizationStatus = "临时授权"
#if os(iOS)
        case .ephemeral: authorizationStatus = "短期授权"
#endif
        @unknown default: authorizationStatus = "未知状态"
        }
    }
}

#if os(iOS)
@MainActor
final class PushApplicationDelegate: NSObject, UIApplicationDelegate {
    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        PushDeviceController.shared.didRegister(deviceToken)
    }

    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        PushDeviceController.shared.didFailRegistration(error)
    }
}
#endif

@MainActor
struct PushProfileView: View {
    @ObservedObject var service: MatrixService
    @ObservedObject var device: PushDeviceController

    var body: some View {
        NavigationStack {
            List {
                LabeledContent("账号", value: service.userID ?? "尚未登录")
                NavigationLink("设置") {
                    List {
                        NavigationLink("推送诊断") {
                            PushDiagnosticsView(service: service, device: device)
                        }
                    }
                    .navigationTitle("设置")
                }
            }
            .navigationTitle("我")
        }
    }
}

@MainActor
struct PushDiagnosticsView: View {
    @ObservedObject var service: MatrixService
    @ObservedObject var device: PushDeviceController
    @State private var notifyURL = ""
    @State private var configurationMessage: String?
    @State private var isSaving = false
    @State private var isChecking = false
    @State private var didCopy = false

    var body: some View {
        Form {
            Section("通知与 APNs") {
                LabeledContent("通知权限", value: device.authorizationStatus)
                Text(device.registrationStatus)
                if let error = device.registrationError {
                    Text(error).foregroundStyle(.red)
                }
                Button("申请权限并注册 / 重试") {
                    Task { await device.requestAuthorization() }
                }
                .disabled(device.isRequesting)
            }
            Section("Device token（Base64 / pushkey）") {
                if let token = service.pushToken {
                    Text(token)
                        .font(.system(.caption, design: .monospaced))
                        .textSelection(.enabled)
                    Button(didCopy ? "已复制" : "复制 Base64 token") {
                        // Explicit user action only. On iOS keep the clipboard
                        // local to this device, with a short expiration.
#if os(iOS)
                        UIPasteboard.general.setItems([["public.utf8-plain-text": token]], options: [
                            .localOnly: true, .expirationDate: Date().addingTimeInterval(120)
                        ])
#elseif os(macOS)
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(token, forType: .string)
#endif
                        didCopy = true
                    }
                } else {
                    Text("尚未收到 token；需要已签名、开启推送能力的 iPhone 包。")
                        .foregroundStyle(.secondary)
                }
            }
            Section("Sygnal notify URL") {
                TextField("https://sygnal.example.invalid/_matrix/push/v1/notify", text: $notifyURL)
                    .autocorrectionDisabled()
#if os(iOS)
                    .textInputAutocapitalization(.never)
                    .keyboardType(.URL)
#endif
                Text("DEBUG 支持 HTTP；不可包含凭据、query 或 fragment，路径必须为 /_matrix/push/v1/notify。地址必须能被 homeserver 访问。")
                    .font(.caption).foregroundStyle(.secondary)
                Button(isSaving ? "正在保存…" : "保存并注册 pusher") {
                    isSaving = true
                    Task {
                        defer { isSaving = false }
                        do {
                            try await device.saveNotifyURL(notifyURL)
                            notifyURL = device.savedNotifyURL
                            configurationMessage = "地址已保存；登录并收到 token 后自动注册。"
                        } catch {
                            configurationMessage = "地址无效：请检查协议、凭据和固定路径。"
                        }
                    }
                }
                .disabled(isSaving || notifyURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                if let configurationMessage { Text(configurationMessage).font(.caption) }
            }
            Section("Matrix pusher") {
                Text(service.pushRegistrationStatus)
                if let error = service.pushRegistrationError {
                    Text(error).foregroundStyle(.red)
                }
                Button(isChecking ? "正在核对…" : "核对") {
                    isChecking = true
                    Task {
                        defer { isChecking = false }
                        await service.verifyPushRegistration()
                    }
                }
                .disabled(isChecking || !service.isLoggedIn || service.pushToken == nil)
                Text(service.pushVerificationStatus ?? "尚未核对。")
            }
            Section {
                Text("仅用于 DEBUG 真机诊断。token 仅保留在内存并用于 pusher 请求，不写入日志或磁盘。核对成功仅表示服务器存在匹配记录，不代表 APNs 已送达。")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .navigationTitle("推送诊断")
        .onAppear { notifyURL = device.savedNotifyURL }
        .onChange(of: service.pushToken) { _, _ in didCopy = false }
    }
}
#endif
