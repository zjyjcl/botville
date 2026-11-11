// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import BotvilleCore
import SwiftUI
#if os(iOS)
import UIKit
import UserNotifications
import os
#elseif os(macOS)
import AppKit
#endif

@main
@MainActor
struct BotvilleApp: App {
    @StateObject private var matrixService = MatrixService()
    @StateObject private var foregroundBanner = ForegroundInAppBannerStore()
#if os(iOS)
    @StateObject private var pushRoute = PushNotificationRouteStore.shared
    @UIApplicationDelegateAdaptor(PushApplicationDelegate.self) private var pushDelegate
#endif
#if DEBUG
    @StateObject private var pushDevice = PushDeviceController.shared
#endif
    @State private var selectedTab = Self.initialTab
    @State private var debugPairingPresented = Self.opensPairConfirm
    @State private var foregroundRoute: PushNotificationRoute?
    @State private var visibleRoomID: String?
    @State private var isRoomInputActive = false
    @Environment(\.scenePhase) private var scenePhase
#if os(iOS)
    // The top overlay is above NavigationSplitView; leave space for the
    // standard large title and navigation controls instead of covering them.
    @ScaledMetric(relativeTo: .body) private var bannerTopClearance: CGFloat = 96
#else
    @ScaledMetric(relativeTo: .body) private var bannerTopClearance: CGFloat = 20
#endif

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

    init() {
#if os(iOS)
        // Process-level singleton outlives Scene/@UIApplicationDelegateAdaptor churn.
        PushNotificationCenter.shared.install(reason: "BotvilleApp.init")
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
                foregroundRoute: $foregroundRoute,
                visibleRoomID: $visibleRoomID,
                isRoomInputActive: $isRoomInputActive,
                isChatTab: selectedTab == 0,
                pushRoute: pushRoute,
                onOpenChat: { selectedTab = 0 })
                .tabItem { Label("会话", systemImage: "bubble.left.and.bubble.right") }
                .tag(0)
#else
            AppRootView(service: matrixService, foregroundRoute: $foregroundRoute,
                        visibleRoomID: $visibleRoomID, isRoomInputActive: $isRoomInputActive,
                        isChatTab: selectedTab == 0,
                        onOpenChat: { selectedTab = 0 })
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
        .onChange(of: matrixService.roomNotifications.states) { _, _ in updateBannerContext() }
        .onReceive(matrixService.foregroundBannerCandidates) { candidate in
            updateBannerContext()
            foregroundBanner.receive(candidate)
        }
        .overlay(alignment: .top) {
            ForegroundInAppBannerView(candidate: foregroundBanner.current) {
                guard let route = foregroundBanner.takeRoute() else { return }
                selectedTab = 0
                foregroundRoute = route
            } dismiss: {
                foregroundBanner.dismiss()
            }
            .safeAreaPadding(.top)
            .padding(.top, bannerTopClearance)
        }
        .onChange(of: scenePhase, initial: true) { _, phase in
            if phase == .background { matrixService.applicationDidEnterBackground() }
            if phase == .active {
                matrixService.prepareForForeground()
                Task { await matrixService.applicationDidBecomeActive() }
            }
            updateBannerContext()
        }
        .onChange(of: selectedTab) { _, _ in updateBannerContext() }
        .onChange(of: visibleRoomID) { _, _ in updateBannerContext() }
        .onChange(of: isRoomInputActive) { _, _ in updateBannerContext() }
        .onChange(of: matrixService.isLoggedIn) { _, loggedIn in
            if !loggedIn { foregroundBanner.reset() }
            updateBannerContext()
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

    private func updateBannerContext() {
        let chatVisible = selectedTab == 0
        foregroundBanner.updateContext(
            isForeground: scenePhase == .active && matrixService.isLoggedIn,
            visibleRoomID: ForegroundBannerVisibility.roomID(
                isLoggedIn: matrixService.isLoggedIn, selectedRoomID: visibleRoomID,
                isCompact: false, showsDetail: true,
                isSceneActive: scenePhase == .active, isChatTab: chatVisible,
                joinedRoomIDs: matrixService.rooms.filter { $0.membership == .joined }.map(\.id)),
            isInputActive: chatVisible && visibleRoomID != nil && isRoomInputActive,
            mutedRoomIDs: Set(matrixService.rooms.filter { matrixService.roomNotifications.isMuted($0.id) }.map(\.id)))
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
    @Binding var foregroundRoute: PushNotificationRoute?
    @Binding var visibleRoomID: String?
    @Binding var isRoomInputActive: Bool
    let isChatTab: Bool
    @State private var selectedRoomID: String?
    @State private var receivedPushRoute = false
    @State private var preferredCompactColumn: NavigationSplitViewColumn = .sidebar
#if os(iOS)
    @ObservedObject var pushRoute: PushNotificationRouteStore
    @Environment(\.horizontalSizeClass) private var horizontalSizeClass
#endif
    let onOpenChat: () -> Void

    var body: some View {
        Group {
            if service.isRestoring {
                ProgressView("正在恢复会话…")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if service.isLoggedIn {
                NavigationSplitView(preferredCompactColumn: compactColumnBinding) {
                    RoomListView(service: service, selection: roomSelectionBinding)
                } detail: {
                    roomDetail
                }
                .task(id: service.rooms.map(\.id)) {
                    if !receivedPushRoute,
                       (selectedRoomID == nil || !service.rooms.contains(where: { $0.id == selectedRoomID && $0.membership == .joined })) {
                        roomSelectionBinding.wrappedValue = service.rooms.first(where: { $0.membership == .joined })?.id
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
            if !loggedIn {
                selectedRoomID = nil
                isRoomInputActive = false
            }
#if os(iOS)
            if loggedIn { applyPendingPushRoute() }
#endif
        }
        .onChange(of: selectedRoomID) { _, _ in publishVisibleRoom() }
#if os(iOS)
        .onChange(of: horizontalSizeClass) { _, _ in publishVisibleRoom() }
#endif
        .onAppear { publishVisibleRoom() }
        .onChange(of: foregroundRoute, initial: true) { _, route in
            guard let route else { return }
            apply(route)
            foregroundRoute = nil
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
        isRoomInputActive = false
        publishVisibleRoom()
    }

    private func publishVisibleRoom() {
        visibleRoomID = currentVisibleRoomID
    }

    private var compactColumnBinding: Binding<NavigationSplitViewColumn> {
        Binding(get: { preferredCompactColumn }, set: { column in
            preferredCompactColumn = column
            if column != .detail { isRoomInputActive = false }
            publishVisibleRoom()
        })
    }

    // Explicitly select detail for every selection, including cold restoration.
    // SwiftUI writes sidebar back on compact Back; no onDisappear is required.
    private var roomSelectionBinding: Binding<String?> {
        Binding(get: { selectedRoomID }, set: { roomID in
            selectedRoomID = roomID
            preferredCompactColumn = roomID == nil ? .sidebar : .detail
            publishVisibleRoom()
        })
    }

    private var currentVisibleRoomID: String? {
#if os(iOS)
        let compact = horizontalSizeClass == .compact
#else
        let compact = false
#endif
        return ForegroundBannerVisibility.roomID(
            isLoggedIn: service.isLoggedIn, selectedRoomID: selectedRoomID,
            isCompact: compact, showsDetail: preferredCompactColumn == .detail,
            isSceneActive: true, isChatTab: true,
            joinedRoomIDs: service.rooms.filter { $0.membership == .joined }.map(\.id))
    }

    @ViewBuilder
    private var roomDetail: some View {
        if let selectedRoom = selectedJoinedRoom {
            RoomView(service: service, room: selectedRoom, isInputActive: $isRoomInputActive,
                     isVisible: isChatTab && currentVisibleRoomID == selectedRoom.id)
                .id(selectedRoom.id)
        } else {
            ContentUnavailableView(
                "选择一个房间",
                systemImage: "bubble.left.and.bubble.right",
                description: Text("从左侧列表选择 Matrix 房间。")
            )
        }
    }

    private var selectedJoinedRoom: MatrixRoom? {
        service.rooms.first { $0.id == selectedRoomID && $0.membership == .joined }
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

/// Process-retained UNUserNotificationCenter delegate. The center keeps only a
/// weak reference; relying solely on `@UIApplicationDelegateAdaptor` left
/// foreground `willPresent` unverified after #144, so this singleton is installed
/// from `BotvilleApp.init`, launch, and every become-active.
final class PushNotificationCenter: NSObject, UNUserNotificationCenterDelegate {
    static let shared = PushNotificationCenter()

#if DEBUG
    private static let logger = Logger(subsystem: "com.zjyjcl.botville", category: "PushForeground")
#endif

    private override init() {
        super.init()
    }

    func install(reason: String) {
        UNUserNotificationCenter.current().delegate = self
#if DEBUG
        Self.probe("installNotificationDelegate", detail: "reason=\(reason)")
#endif
    }

    @objc(userNotificationCenter:willPresentNotification:withCompletionHandler:)
    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        let options = UNNotificationPresentationOptions(
            rawValue: PushNotificationPresentation.foreground(mutedRoomHashes: (try? SharedRoomMuteStore().load()) ?? []).unNotificationPresentationRawValue)
#if DEBUG
        Self.probe("willPresent", detail: "optionsRaw=\(options.rawValue)")
#endif
        // Call immediately on the callback thread. #144 deferred to main.async and
        // still saw no foreground banner; Apple's docs complete synchronously.
        completionHandler(options)
    }

    @objc(userNotificationCenter:didReceiveNotificationResponse:withCompletionHandler:)
    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        let route = PushNotificationRoute.parse(response.notification.request.content.userInfo)
        completionHandler()
        Task { @MainActor in PushNotificationRouteStore.shared.receive(route) }
    }

#if DEBUG
    private static func probe(_ event: String, detail: String = "") {
        if detail.isEmpty {
            logger.log("\(event, privacy: .public)")
        } else {
            logger.log("\(event, privacy: .public) \(detail, privacy: .public)")
        }
        PushForegroundProbeLog.append(event: event, detail: detail)
    }
#endif
}

final class PushApplicationDelegate: NSObject, UIApplicationDelegate {
    override init() {
        super.init()
        PushNotificationCenter.shared.install(reason: "PushApplicationDelegate.init")
    }

    func application(_ application: UIApplication, didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        PushNotificationCenter.shared.install(reason: "didFinishLaunching")
#if DEBUG
        PushForegroundProbeLog.append(event: "didFinishLaunching")
        Logger(subsystem: "com.zjyjcl.botville", category: "PushForeground")
            .log("didFinishLaunching")
#endif
        if let userInfo = launchOptions?[.remoteNotification] as? [AnyHashable: Any] {
            let route = PushNotificationRoute.parse(userInfo)
            Task { @MainActor in PushNotificationRouteStore.shared.receive(route) }
        }
        return true
    }

    func applicationDidBecomeActive(_ application: UIApplication) {
        PushNotificationCenter.shared.install(reason: "didBecomeActive")
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
