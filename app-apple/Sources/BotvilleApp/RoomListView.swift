// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import BotvilleCore
import SwiftUI

@MainActor
struct RoomListView: View {
    @ObservedObject var service: MatrixService
    @Binding var selection: String?

    var body: some View {
        List(selection: $selection) {
            let invitations = service.rooms.filter { $0.membership == .invited }
            let joined = service.rooms.filter { $0.membership == .joined }
            if joined.isEmpty && invitations.isEmpty {
                ContentUnavailableView(
                    "还没有房间",
                    systemImage: "tray",
                    description: Text("同步完成后，已加入的 Matrix 房间会显示在这里。")
                )
            } else {
                if !invitations.isEmpty {
                    Section("邀请") {
                        ForEach(invitations) { room in
                            InvitationRow(service: service, room: room)
                        }
                    }
                }
                Section("会话") {
                    ForEach(joined) { room in
                        NavigationLink(value: room.id) {
                            RoomRow(room: room)
                        }
                        .tag(room.id)
                    }
                }
            }
        }
        .navigationTitle("房间")
        .toolbar {
            ToolbarItem(placement: .automatic) {
                Label(service.connectionStatus.displayName, systemImage: service.isSyncing ? "arrow.triangle.2.circlepath" : "wifi")
                    .font(.caption)
                    .foregroundStyle(service.isSyncing ? .secondary : .primary)
            }
            ToolbarItem(placement: .primaryAction) {
                Button {
                    Task { try? await service.sync() }
                } label: {
                    if service.isSyncing {
                        ProgressView()
                    } else {
                        Image(systemName: "arrow.clockwise")
                    }
                }
                .disabled(service.isSyncing)
                .accessibilityLabel("同步房间")
            }

            ToolbarItem(placement: .automatic) {
                Button("退出") {
                    Task { await service.logout() }
                }
            }
        }
        .safeAreaInset(edge: .bottom) {
            if let syncError = service.syncError ?? service.sessionError {
                HStack(spacing: 8) {
                    Image(systemName: service.isSyncing ? "arrow.triangle.2.circlepath" : "wifi.exclamationmark")
                    Text(syncError)
                }
                    .font(.footnote)
                    .foregroundStyle(.red)
                    .padding(8)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(.bar)
            }
        }
    }
}

@MainActor
private struct RoomRow: View {
    let room: MatrixRoom

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: room.isDirect ? "person.crop.circle" : "person.2.circle")
                .foregroundStyle(.secondary)
            Text(room.name)
                .lineLimit(1)
            Spacer()
            if room.unreadCount > 0 {
                Text(room.unreadCount, format: .number)
                    .font(.caption.bold())
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(.tint, in: Capsule())
                    .foregroundStyle(.white)
            }
        }
    }
}

@MainActor
private struct InvitationRow: View {
    @ObservedObject var service: MatrixService
    let room: MatrixRoom
    @State private var isResponding = false
    @State private var errorMessage: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            RoomRow(room: room)
            HStack {
                Button("拒绝", role: .destructive) { respond(accept: false) }
                Button("接受") { respond(accept: true) }
                    .buttonStyle(.borderedProminent)
                if isResponding { ProgressView().controlSize(.small) }
            }
            .disabled(isResponding)
            if let errorMessage {
                Text(errorMessage).font(.caption).foregroundStyle(.red)
            }
        }
    }

    private func respond(accept: Bool) {
        guard !isResponding else { return }
        isResponding = true
        errorMessage = nil
        Task {
            defer { isResponding = false }
            do {
                if accept { try await service.acceptInvitation(room.id) }
                else { try await service.rejectInvitation(room.id) }
            } catch { errorMessage = error.localizedDescription }
        }
    }
}
