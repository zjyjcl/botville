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

    var body: some Scene {
#if os(macOS)
        Window("botville", id: "main") {
            AppRootView(service: matrixService)
        }
#else
        WindowGroup {
            AppRootView(service: matrixService)
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
