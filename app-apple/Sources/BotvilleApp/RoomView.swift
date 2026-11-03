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
    @State private var isRefreshingSecurity = false
    @State private var isEnablingEncryption = false
    @State private var isPresentingEncryptionConfirmation = false
    @State private var isPresentingInvite = false
    @State private var encryptionConsent = EncryptionConsent()

    private var currentRoom: MatrixRoom {
        service.rooms.first(where: { $0.id == room.id }) ?? room
    }

    private var encryptionPresentation: EncryptionPresentation {
        EncryptionPresentation(encryption: currentRoom.encryption, participants: currentRoom.participants)
    }

    private var canSend: Bool {
        !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && !isSending
            && service.activeRoomID == room.id
    }

    var body: some View {
        VStack(spacing: 0) {
            roomSecurityCard
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
                                MessageBubble(message: message, retry: {
                                    Task { await actOnMessage(message.id, retry: true) }
                                }, cancel: {
                                    Task { await actOnMessage(message.id, retry: false) }
                                })
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
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Menu {
                    Button {
                        isPresentingInvite = true
                    } label: {
                        Label("邀请成员", systemImage: "person.badge.plus")
                    }
                    Button {
                        Task { await refreshSecurity() }
                    } label: {
                        Label("刷新加密状态", systemImage: "arrow.clockwise")
                    }
                    .disabled(isRefreshingSecurity)
                } label: {
                    Image(systemName: "ellipsis.circle")
                }
            }
        }
        .task(id: room.id) {
            async let open: Void = openRoom()
            async let security: Void = refreshSecurity()
            _ = await (open, security)
        }
        .onDisappear {
            service.closeRoom(room.id)
        }
        .sheet(isPresented: $isPresentingInvite) {
            InviteMemberSheet(service: service, room: currentRoom)
        }
        .alert("确认开启端对端加密？", isPresented: $isPresentingEncryptionConfirmation) {
            Button("开启加密") { enableEncryptionAfterConfirmation() }
            Button("返回", role: .cancel) { encryptionConsent.cancel() }
        } message: {
            Text("开启后不可关闭，AI 将无法读取消息；未验证设备或未设置密钥备份时，换设备可能无法查看历史消息。")
        }
    }

    private var roomSecurityCard: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(alignment: .firstTextBaseline) {
                Label(encryptionPresentation.title, systemImage: encryptionPresentation.systemImage)
                    .font(.subheadline.weight(.semibold))
                Spacer()
                Button {
                    Task { await refreshSecurity() }
                } label: {
                    if isRefreshingSecurity { ProgressView().controlSize(.small) }
                    else { Image(systemName: "arrow.clockwise") }
                }
                .buttonStyle(.borderless)
                .disabled(isRefreshingSecurity)
                .accessibilityLabel("刷新加密状态")
            }
            Text(encryptionPresentation.detail)
                .font(.footnote)
                .foregroundStyle(.secondary)
            if encryptionPresentation.canEnable {
                Button(isEnablingEncryption ? "正在开启…" : "开启端对端加密") {
                    encryptionConsent.request(for: .existingRoom(room.id))
                    isPresentingEncryptionConfirmation = true
                }
                .buttonStyle(.bordered)
                .disabled(isEnablingEncryption)
            }
            if currentRoom.encryption == .encrypted {
                Text("加密已开启且不能关闭。")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                ForEach(service.cryptoSafetyWarnings, id: \.self) { warning in
                    Label(warning, systemImage: "exclamationmark.shield")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(.horizontal)
        .padding(.vertical, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.thinMaterial)
    }

    private func refreshSecurity() async {
        guard !isRefreshingSecurity else { return }
        isRefreshingSecurity = true
        defer { isRefreshingSecurity = false }
        await service.refreshRoomSecurity(room.id)
    }

    private func enableEncryptionAfterConfirmation() {
        let target = EncryptionTarget.existingRoom(room.id)
        guard encryptionConsent.confirm(for: target), !isEnablingEncryption else { return }
        isEnablingEncryption = true
        Task {
            defer { isEnablingEncryption = false }
            do {
                try await service.enableEncryption(roomID: room.id, consent: encryptionConsent)
            } catch {
                errorMessage = error.localizedDescription
            }
        }
    }

    private func actOnMessage(_ messageID: String, retry: Bool) async {
        errorMessage = nil
        do {
            if retry { try await service.retryMessage(messageID) }
            else { try await service.cancelMessage(messageID) }
        } catch { errorMessage = error.localizedDescription }
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
    let retry: () -> Void
    let cancel: () -> Void

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
                        Text(deliveryState.displayName)
                    }
                }
                .font(.caption2)
                .foregroundStyle(.secondary)
            }
            .padding(10)
            .background(message.isOwn ? Color.accentColor.opacity(0.15) : Color.secondary.opacity(0.12))
            .clipShape(RoundedRectangle(cornerRadius: 12))

            if message.isOwn, let deliveryState = message.deliveryState, deliveryState.canRetry {
                VStack(spacing: 6) {
                    Button("重试", action: retry).font(.caption)
                    if deliveryState.canCancel {
                        Button("取消", role: .destructive, action: cancel).font(.caption)
                    }
                }
            }

            if !message.isOwn { Spacer(minLength: 40) }
        }
    }
}

@MainActor
private struct InviteMemberSheet: View {
    @Environment(\.dismiss) private var dismiss
    @ObservedObject var service: MatrixService
    let room: MatrixRoom

    @State private var userID = ""
    @State private var isInviting = false
    @State private var errorMessage: String?
    @State private var isShowingEncryptionWarning = false
    @State private var warningUserID: String?

    private var invitedParticipantStatus: AIParticipantStatus {
        service.aiParticipantPolicy.classify(memberIDs: [userID])
    }

    private var mustAcknowledgeEncryptionWarning: Bool {
        room.encryption == .encrypted && invitedParticipantStatus != .humanOnly
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("Matrix ID") {
                    TextField("例如 @alice:example.org", text: $userID)
#if os(iOS)
                        .textInputAutocapitalization(.never)
#endif
                        .autocorrectionDisabled()
                }
                if room.encryption == .encrypted {
                    Section("加密房间限制") {
                        Label("已加密房间不能降级；AI 无法读取其中消息。", systemImage: "lock.fill")
                            .font(.footnote)
                        if invitedParticipantStatus != .humanOnly {
                            Label("此 ID 是 AI 或尚无法确认是否为 AI。邀请前需要确认其无法读取加密消息。", systemImage: "exclamationmark.triangle")
                                .font(.footnote)
                        }
                    }
                }
                if let errorMessage { Text(errorMessage).foregroundStyle(.red) }
            }
            .navigationTitle("邀请成员")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消") { dismiss() }.disabled(isInviting)
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button(isInviting ? "邀请中…" : "邀请") { prepareInvite() }
                        .disabled(isInviting || userID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
            .alert("确认邀请可能为 AI 的成员？", isPresented: $isShowingEncryptionWarning) {
                Button("确认邀请") { inviteAfterWarning() }
                Button("返回", role: .cancel) { warningUserID = nil }
            } message: {
                Text("该房间已启用端对端加密，AI 将无法读取消息；加密也不能关闭。请确认此邀请符合会话用途。")
            }
            .onChange(of: userID) { _, _ in
                warningUserID = nil
                isShowingEncryptionWarning = false
            }
        }
    }

    private func prepareInvite() {
        errorMessage = nil
        let trimmed = userID.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        if mustAcknowledgeEncryptionWarning {
            warningUserID = trimmed
            isShowingEncryptionWarning = true
        } else {
            invite(acknowledgedWarning: false)
        }
    }

    private func inviteAfterWarning() {
        let target = userID.trimmingCharacters(in: .whitespacesAndNewlines)
        guard warningUserID == target else {
            errorMessage = "邀请对象已变化，请重新确认。"
            return
        }
        warningUserID = nil
        invite(acknowledgedWarning: true)
    }

    private func invite(acknowledgedWarning: Bool) {
        guard !isInviting else { return }
        isInviting = true
        let target = userID.trimmingCharacters(in: .whitespacesAndNewlines)
        Task {
            defer { isInviting = false }
            do {
                try await service.inviteUser(target, to: room.id, acknowledgedEncryptionWarning: acknowledgedWarning)
                dismiss()
            } catch {
                if case MatrixServiceError.aiInvitationConfirmationRequired = error {
                    warningUserID = target
                    isShowingEncryptionWarning = true
                } else {
                    errorMessage = error.localizedDescription
                }
            }
        }
    }
}
