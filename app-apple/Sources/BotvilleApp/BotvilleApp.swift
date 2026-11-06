// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import BotvilleCore
import SwiftUI
#if os(iOS)
import UIKit
import UserNotifications
import OSLog
#elseif os(macOS)
import AppKit
#endif

@main
@MainActor
struct BotvilleApp: App {
    @StateObject private var matrixService = MatrixService()
#if os(iOS)
    @StateObject private var pushRoute = PushNotificationRouteStore.shared
    @UIApplicationDelegateAdaptor(PushApplicationDelegate.self) private var pushDelegate
#endif
#if DEBUG
    @StateObject private var pushDevice = PushDeviceController.shared
#endif
    @State private var selectedTab = Self.initialTab
    @State private var debugPairingPresented = Self.opensPairConfirm

    private static var initialTab: Int {
#if DEBUG
        ProcessInfo.processInfo.arguments.contains("-dynamic-ui-mock") ? 1 : 0
#else
        0
#endif
    }

    private static var opensPairEntry: Bool {
#if DEBUG
        ProcessInfo.processInfo.arguments.contains("-pair-entry")
#else
        false
#endif
    }

    private static var opensPairConfirm: Bool {
#if DEBUG
        ProcessInfo.processInfo.arguments.contains("-pair-confirm")
#else
        false
#endif
    }

    private var root: some View {
#if DEBUG
        // Screenshot/demo routes must not restore a user's session in the
        // background. They are intentionally isolated from normal startup.
        Group {
            if Self.opensPairConfirm {
            Color.clear
            } else if Self.opensPairEntry {
                LoginView(service: matrixService)
            } else {
                normalRoot
            }
        }
#else
        normalRoot
#endif
    }

    private var normalRoot: some View {
        TabView(selection: $selectedTab) {
#if os(iOS)
            AppRootView(
                service: matrixService,
                pushRoute: pushRoute,
                onOpenChat: { selectedTab = 0 })
                .tabItem { Label("会话", systemImage: "bubble.left.and.bubble.right") }
                .tag(0)
#else
            AppRootView(service: matrixService, onOpenChat: { selectedTab = 0 })
                .tabItem { Label("会话", systemImage: "bubble.left.and.bubble.right") }
                .tag(0)
#endif
            if showsGatewayFeatures {
                DynamicUIView(
                    onOpenChat: { selectedTab = 0 },
                    matrixService: matrixService)
                    .tabItem { Label("动态", systemImage: "square.grid.2x2") }
                    .tag(1)
            }
#if DEBUG
            PushProfileView(service: matrixService, device: pushDevice)
                .tabItem { Label("我", systemImage: "person.crop.circle") }
                .tag(2)
#endif
        }
        .onChange(of: showsGatewayFeatures) { _, visible in
            if !visible && selectedTab == 1 { selectedTab = 0 }
        }
#if os(iOS)
        // This observer belongs to the tab container so a notification can
        // leave the Dynamic tab before its chat child becomes visible.
        .onReceive(pushRoute.$pendingRoute.compactMap { $0 }) { _ in
            selectedTab = 0
        }
#endif
#if DEBUG
        .task { await pushDevice.prepare(service: matrixService) }
#endif
    }

    private var showsGatewayFeatures: Bool {
#if DEBUG
        if ProcessInfo.processInfo.arguments.contains("-dynamic-ui-mock") { return true }
#endif
        // Missing gateway means absent features: keep only the chat entry.
        return matrixService.hasConfirmedGateway
    }

    var body: some Scene {
#if os(macOS)
        Window("botville", id: "main") {
            root
                .sheet(isPresented: $debugPairingPresented) {
#if DEBUG
                    PairingFlowView(
                        matrixService: matrixService, isPresented: $debugPairingPresented,
                        initialURI: Self.opensPairConfirm ? PairingMockData.exampleURI : nil)
#else
                    EmptyView()
#endif
                }
        }
#else
        WindowGroup {
            root
                .sheet(isPresented: $debugPairingPresented) {
#if DEBUG
                    PairingFlowView(
                        matrixService: matrixService, isPresented: $debugPairingPresented,
                        initialURI: Self.opensPairConfirm ? PairingMockData.exampleURI : nil)
#else
                    EmptyView()
#endif
                }
        }
#endif
    }
}

@MainActor
private struct AppRootView: View {
    @ObservedObject var service: MatrixService
    @State private var selectedRoomID: String?
    @State private var receivedPushRoute = false
    @State private var preferredCompactColumn: NavigationSplitViewColumn = .sidebar
#if os(iOS)
    @ObservedObject var pushRoute: PushNotificationRouteStore
#endif
    let onOpenChat: () -> Void

    var body: some View {
        Group {
            if service.isRestoring {
                ProgressView("正在恢复会话…")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if service.isLoggedIn {
                NavigationSplitView(preferredCompactColumn: $preferredCompactColumn) {
                    RoomListView(service: service, selection: $selectedRoomID)
                } detail: {
                    if let selectedRoom = service.rooms.first(where: { $0.id == selectedRoomID && $0.membership == .joined }) {
                        RoomView(service: service, room: selectedRoom)
                            .id(selectedRoom.id)
                    } else {
                        ContentUnavailableView(
                            "选择一个房间",
                            systemImage: "bubble.left.and.bubble.right",
                            description: Text("从左侧列表选择 Matrix 房间。")
                        )
                    }
                }
                .task(id: service.rooms.map(\.id)) {
                    if !receivedPushRoute,
                       (selectedRoomID == nil || !service.rooms.contains(where: { $0.id == selectedRoomID && $0.membership == .joined })) {
                        selectedRoomID = service.rooms.first(where: { $0.membership == .joined })?.id
                    }
                }
            } else {
                LoginView(service: service)
            }
        }
        .task {
            await service.restoreSession()
#if os(iOS)
            applyPendingPushRoute()
#endif
        }
        .onChange(of: service.isLoggedIn) { _, loggedIn in
            if !loggedIn { selectedRoomID = nil }
#if os(iOS)
            if loggedIn { applyPendingPushRoute() }
#endif
        }
#if os(iOS)
        // @Published emits in willSet. Consume only after SwiftUI observes the
        // committed value; consuming synchronously in onReceive reads stale state.
        .onChange(of: pushRoute.pendingRoute, initial: true) { _, _ in
            applyPendingPushRoute()
        }
        .onChange(of: service.isRestoring) { _, restoring in
            if !restoring { applyPendingPushRoute() }
        }
        .onReceive(NotificationCenter.default.publisher(for: UIApplication.didBecomeActiveNotification)) { _ in
            Task { await service.applicationDidBecomeActive() }
        }
#elseif os(macOS)
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
            Task { await service.applicationDidBecomeActive() }
        }
#endif
    }

#if os(iOS)
    private func applyPendingPushRoute() {
        guard service.isLoggedIn, !service.isRestoring,
              let route = pushRoute.consume() else { return }
        apply(route)
    }
#endif

    private func apply(_ route: PushNotificationRoute) {
        receivedPushRoute = true
        onOpenChat()
        switch route {
        case .room(let roomID):
            selectedRoomID = roomID
            preferredCompactColumn = .detail
        case .roomList:
            selectedRoomID = nil
            preferredCompactColumn = .sidebar
        }
    }
}

#if os(iOS)
@MainActor
final class PushNotificationRouteStore: ObservableObject {
    static let shared = PushNotificationRouteStore()
    @Published private(set) var pendingRoute: PushNotificationRoute?

    func receive(_ userInfo: [AnyHashable: Any]) {
        pendingRoute = PushNotificationRoute.parse(userInfo)
    }

    func receive(_ route: PushNotificationRoute) {
        pendingRoute = route
    }

    func consume() -> PushNotificationRoute? {
        defer { pendingRoute = nil }
        return pendingRoute
    }
}

final class PushApplicationDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    // UNUserNotificationCenter.delegate is weak; keep this object as the
    // UIApplicationDelegateAdaptor instance and re-assert ownership early and
    // on every foreground activation so willPresent is not skipped.
    override init() {
        super.init()
        Self.installNotificationDelegate(self)
    }

    func application(_ application: UIApplication, didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        Self.installNotificationDelegate(self)
        if let userInfo = launchOptions?[.remoteNotification] as? [AnyHashable: Any] {
            let route = PushNotificationRoute.parse(userInfo)
            Task { @MainActor in PushNotificationRouteStore.shared.receive(route) }
        }
        return true
    }

    func applicationDidBecomeActive(_ application: UIApplication) {
        Self.installNotificationDelegate(self)
    }

    private static func installNotificationDelegate(_ delegate: UNUserNotificationCenterDelegate) {
        UNUserNotificationCenter.current().delegate = delegate
#if DEBUG
        WillPresentProbe.recordDelegateInstall(installed: delegate)
#endif
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                            withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
#if DEBUG
        WillPresentProbe.recordWillPresent(notification: notification)
#endif
        // Map Core policy → UN bits (banner=1<<4, list=1<<3). Call on main; some
        // iOS 17–27 builds suppress heads-up if the handler races off-main.
        let options = UNNotificationPresentationOptions(
            rawValue: PushNotificationPresentation.foreground.unNotificationPresentationRawValue)
        DispatchQueue.main.async {
            completionHandler(options)
        }
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                            withCompletionHandler completionHandler: @escaping () -> Void) {
        let route = PushNotificationRoute.parse(response.notification.request.content.userInfo)
        completionHandler()
        Task { @MainActor in PushNotificationRouteStore.shared.receive(route) }
    }

#if DEBUG
    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        Task { @MainActor in PushDeviceController.shared.didRegister(deviceToken) }
    }

    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        Task { @MainActor in PushDeviceController.shared.didFailRegistration(error) }
    }
#endif
}

#if DEBUG
/// Temporary DEBUG-only evidence that `willPresent` actually fires and that
/// `UNUserNotificationCenter.current().delegate` remains non-nil. Writes an
/// App Group sidecar file (no tokens / room bodies) and mirrors via os.Logger.
enum WillPresentProbe {
    static let appGroupID = "group.com.zjyjcl.botville"
    static let logFileName = "willpresent-probe.log"
    private static let logger = Logger(subsystem: "com.zjyjcl.botville", category: "WillPresentProbe")

    static var logFileURL: URL? {
        FileManager.default
            .containerURL(forSecurityApplicationGroupIdentifier: appGroupID)?
            .appendingPathComponent(logFileName, isDirectory: false)
    }

    static func recordDelegateInstall(installed: UNUserNotificationCenterDelegate) {
        let center = UNUserNotificationCenter.current()
        let current = center.delegate
        let installedType = String(describing: type(of: installed))
        let currentType = current.map { String(describing: type(of: $0)) } ?? "nil"
        let currentObject = current as AnyObject?
        let installedObject = installed as AnyObject
        let sameInstance = currentObject != nil && currentObject === installedObject
        let line = [
            "delegate-install",
            "ts=\(isoNow())",
            "installedType=\(installedType)",
            "centerDelegateType=\(currentType)",
            "centerDelegateNonNil=\(current != nil)",
            "sameInstance=\(sameInstance)",
        ].joined(separator: " ")
        append(line)
        logger.info("\(line, privacy: .public)")
    }

    static func recordWillPresent(notification: UNNotification) {
        // Identifier only — never dump userInfo / pushkey / tokens.
        let requestID = notification.request.identifier
        let hasRoomHint = notification.request.content.userInfo.keys.contains { key in
            String(describing: key) == "room_id"
        }
        let line = [
            "willPresent",
            "ts=\(isoNow())",
            "requestID=\(sanitize(requestID))",
            "hasTopLevelRoomID=\(hasRoomHint)",
            "thread=\(Thread.isMainThread ? "main" : "background")",
        ].joined(separator: " ")
        append(line)
        logger.info("\(line, privacy: .public)")
    }

    @MainActor
    static func readLogText() -> String {
        guard let url = logFileURL else {
            return "(App Group container unavailable for \(appGroupID))"
        }
        guard FileManager.default.fileExists(atPath: url.path) else {
            return "(no \(logFileName) yet — cold start then trigger a foreground push)"
        }
        do {
            let text = try String(contentsOf: url, encoding: .utf8)
            return text.isEmpty ? "(empty log)" : text
        } catch {
            return "(failed to read \(logFileName))"
        }
    }

    @MainActor
    static func clearLog() {
        guard let url = logFileURL else { return }
        try? FileManager.default.removeItem(at: url)
        logger.info("cleared \(logFileName, privacy: .public)")
    }

    private static func append(_ line: String) {
        guard let url = logFileURL else {
            logger.error("App Group container missing; cannot write \(logFileName, privacy: .public)")
            return
        }
        let payload = Data((line + "\n").utf8)
        do {
            if !FileManager.default.fileExists(atPath: url.path) {
                try payload.write(to: url, options: .atomic)
            } else if let handle = try? FileHandle(forWritingTo: url) {
                defer { try? handle.close() }
                try handle.seekToEnd()
                try handle.write(contentsOf: payload)
            } else {
                try payload.write(to: url, options: .atomic)
            }
        } catch {
            logger.error("failed writing \(logFileName, privacy: .public)")
        }
    }

    private static func isoNow() -> String {
        ISO8601DateFormatter().string(from: Date())
    }

    private static func sanitize(_ value: String) -> String {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.count <= 64 { return trimmed.isEmpty ? "-" : trimmed }
        return String(trimmed.prefix(61)) + "..."
    }
}
#endif
#endif
