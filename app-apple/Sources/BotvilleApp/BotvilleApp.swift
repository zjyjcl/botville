// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import BotvilleCore
import SwiftUI
#if os(iOS)
import UIKit
#elseif os(macOS)
import AppKit
#endif

@main
@MainActor
struct BotvilleApp: App {
    @StateObject private var matrixService = MatrixService()
#if DEBUG
    @StateObject private var pushDevice = PushDeviceController.shared
#if os(iOS)
    @UIApplicationDelegateAdaptor(PushApplicationDelegate.self) private var pushDelegate
#endif
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
            AppRootView(service: matrixService)
                .tabItem { Label("会话", systemImage: "bubble.left.and.bubble.right") }
                .tag(0)
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

    var body: some View {
        Group {
            if service.isRestoring {
                ProgressView("正在恢复会话…")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if service.isLoggedIn {
                NavigationSplitView {
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
                    if selectedRoomID == nil || !service.rooms.contains(where: { $0.id == selectedRoomID && $0.membership == .joined }) {
                        selectedRoomID = service.rooms.first(where: { $0.membership == .joined })?.id
                    }
                }
            } else {
                LoginView(service: service)
            }
        }
        .task {
            await service.restoreSession()
        }
        .onChange(of: service.isLoggedIn) { _, loggedIn in
            if !loggedIn { selectedRoomID = nil }
        }
#if os(iOS)
        .onReceive(NotificationCenter.default.publisher(for: UIApplication.didBecomeActiveNotification)) { _ in
            Task { await service.applicationDidBecomeActive() }
        }
#elseif os(macOS)
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
            Task { await service.applicationDidBecomeActive() }
        }
#endif
    }
}
