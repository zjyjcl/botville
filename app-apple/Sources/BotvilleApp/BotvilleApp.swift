// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import BotvilleCore
import SwiftUI
#if os(iOS)
import UIKit
import UserNotifications
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
    func application(_ application: UIApplication, didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        if let userInfo = launchOptions?[.remoteNotification] as? [AnyHashable: Any] {
            let route = PushNotificationRoute.parse(userInfo)
            Task { @MainActor in PushNotificationRouteStore.shared.receive(route) }
        }
        return true
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                            withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        let policy = PushNotificationPresentation.foreground
        var options: UNNotificationPresentationOptions = []
        if policy.contains(.banner) { options.insert(.banner) }
        if policy.contains(.list) { options.insert(.list) }
        if policy.contains(.sound) { options.insert(.sound) }
        if policy.contains(.badge) { options.insert(.badge) }
        completionHandler(options)
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
#endif
