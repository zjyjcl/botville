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
            if service.rooms.isEmpty {
                ContentUnavailableView(
                    "还没有房间",
                    systemImage: "tray",
                    description: Text("同步完成后，已加入的 Matrix 房间会显示在这里。")
                )
            } else {
                ForEach(service.rooms) { room in
                    NavigationLink(value: room.id) {
                        Text(room.name)
                    }
                    .tag(room.id)
                }
            }
        }
        .navigationTitle("房间")
        .toolbar {
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
            if let syncError = service.syncError {
                Text(syncError)
                    .font(.footnote)
                    .foregroundStyle(.red)
                    .padding(8)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(.bar)
            }
        }
    }
}
