// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import BotvilleCore
import SwiftUI

@MainActor
struct RoomView: View {
    @ObservedObject var service: MatrixService
    let room: MatrixRoom

    @State private var draft = ""
    @State private var isSending = false
    @State private var isOpening = false
    @State private var isLoadingOlderMessages = false
    @State private var errorMessage: String?

    private var canSend: Bool {
        !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && !isSending
            && service.activeRoomID == room.id
    }

    var body: some View {
        VStack(spacing: 0) {
            if isOpening {
                ProgressView("正在打开房间…")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if service.messages.isEmpty {
                VStack(spacing: 12) {
                    ContentUnavailableView(
                        "还没有文本消息",
                        systemImage: "text.bubble",
                        description: Text("此房间的新消息会在同步后显示。")
                    )
                    if service.canLoadOlderMessages {
                        olderMessagesButton
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 12) {
                            if service.canLoadOlderMessages {
                                olderMessagesButton
                            }

                            ForEach(service.messages) { message in
                                MessageBubble(message: message)
                                    .id(message.id)
                            }
                        }
                        .padding()
                    }
                    .onAppear {
                        scrollToLatest(using: proxy)
                    }
                    .onChange(of: service.messages.last?.id) { _, _ in
                        scrollToLatest(using: proxy)
                    }
                }
            }

            if let errorMessage {
                HStack(alignment: .firstTextBaseline) {
                    Text(errorMessage)
                        .font(.footnote)
                        .foregroundStyle(.red)
                    Spacer()
                    Button("重试") {
                        Task { await openRoom() }
                    }
                    .font(.footnote)
                    .disabled(isOpening)
                }
                .padding(.horizontal)
                .padding(.bottom, 4)
            }

            Divider()
            HStack(alignment: .bottom, spacing: 10) {
                TextField("发送消息", text: $draft, axis: .vertical)
                    .lineLimit(1...5)
                    .textFieldStyle(.roundedBorder)

                Button(action: send) {
                    if isSending {
                        ProgressView().controlSize(.small)
                    } else {
                        Image(systemName: "arrow.up.circle.fill")
                            .font(.title2)
                    }
                }
                .buttonStyle(.plain)
                .disabled(!canSend)
                .accessibilityLabel("发送")
            }
            .padding()
        }
        .navigationTitle(room.name)
        .task(id: room.id) {
            await openRoom()
        }
        .onDisappear {
            service.closeRoom(room.id)
        }
    }

    private func send() {
        let draftSnapshot = draft
        let text = draftSnapshot.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !isSending else { return }

        isSending = true
        errorMessage = nil
        Task {
            defer { isSending = false }
            do {
                try await service.send(text: text, to: room.id)
                if draft == draftSnapshot {
                    draft = ""
                }
            } catch {
                errorMessage = error.localizedDescription
            }
        }
    }

    private func loadOlderMessages() async {
        guard !isLoadingOlderMessages else { return }
        isLoadingOlderMessages = true
        defer { isLoadingOlderMessages = false }
        errorMessage = nil
        do {
            try await service.loadOlderMessages()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func openRoom() async {
        isOpening = true
        errorMessage = nil
        defer { isOpening = false }

        do {
            try await service.openRoom(room.id)
            await loadOlderMessages()
        } catch is CancellationError {
            return
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private var olderMessagesButton: some View {
        Button {
            Task { await loadOlderMessages() }
        } label: {
            if isLoadingOlderMessages {
                ProgressView()
                    .controlSize(.small)
            } else {
                Text("加载更早的消息")
            }
        }
        .disabled(isLoadingOlderMessages)
        .frame(maxWidth: .infinity)
    }

    private func scrollToLatest(using proxy: ScrollViewProxy) {
        guard let latestMessageID = service.messages.last?.id else { return }
        Task { @MainActor in
            await Task.yield()
            withAnimation {
                proxy.scrollTo(latestMessageID, anchor: .bottom)
            }
        }
    }
}

@MainActor
private struct MessageBubble: View {
    let message: MatrixMessage

    var body: some View {
        HStack {
            if message.isOwn { Spacer(minLength: 40) }

            VStack(alignment: .leading, spacing: 4) {
                Text(message.sender)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Text(message.body)
                    .textSelection(.enabled)
                HStack(spacing: 6) {
                    Text(message.timestamp, format: .dateTime.month().day().hour().minute())
                    if let deliveryState = message.deliveryState {
                        Text(deliveryState)
                    }
                }
                .font(.caption2)
                .foregroundStyle(.secondary)
            }
            .padding(10)
            .background(message.isOwn ? Color.accentColor.opacity(0.15) : Color.secondary.opacity(0.12))
            .clipShape(RoundedRectangle(cornerRadius: 12))

            if !message.isOwn { Spacer(minLength: 40) }
        }
    }
}
