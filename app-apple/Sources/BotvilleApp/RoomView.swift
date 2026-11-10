// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import BotvilleCore
import SwiftUI

@MainActor
struct RoomView: View {
    @ObservedObject var service: MatrixService
    let room: MatrixRoom
    @Binding var isInputActive: Bool
    let isVisible: Bool

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var scrollState = TimelineScrollState()
    @State private var visibleMessageID: String?
    @State private var timelineGeometry: TimelineGeometry?
    @State private var isDraggingTimeline = false
    @State private var draft = ""
    @FocusState private var isInputFocused: Bool
    @State private var isSending = false
    @State private var isOpening = false
    // TabView can keep this detail view alive while it is off-screen. Keep the
    // open operation tied to every appearance instead of relying on `.task(id:)`
    // being recreated when the user returns to the chat tab.
    @State private var roomOpenTask: Task<Void, Never>?
    @State private var roomOpenGeneration = UUID()
    @State private var completedRoomOpenGeneration: UUID?
    @State private var roomTimelineLease: RoomTimelineLease?
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

    private var latestScrollRequest: TimelineScrollRequest? {
        TimelineScrollRequest.make(
            appearanceID: roomOpenGeneration, completedAppearanceID: completedRoomOpenGeneration,
            isVisible: isVisible, isOpening: isOpening,
            roomID: room.id, activeRoomID: service.activeRoomID,
            latestMessageID: service.messages.last?.id
        )
    }

    private var scrollInput: TimelineScrollInput {
        TimelineScrollInput(request: latestScrollRequest,
                            messageIDs: service.messages.map(\.id),
                            latestMessageIsOwn: service.messages.last?.isOwn == true)
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
                timeline
            }

            if let errorMessage {
                HStack(alignment: .firstTextBaseline) {
                    Text(errorMessage)
                        .font(.footnote)
                        .foregroundStyle(.red)
                    Spacer()
                    Button("重试") {
                        openRoomForCurrentAppearance()
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
                    .focused($isInputFocused)

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
        .onChange(of: scrollInput, initial: true) { _, next in scrollState.update(next) }
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
        .task(id: room.id) { await refreshSecurity() }
        .onDisappear {
            releaseRoomForCurrentAppearance()
        }
        .onAppear {
            updateInputActivity()
            if isVisible { openRoomForCurrentAppearance() }
        }
        .onChange(of: isVisible) { _, visible in
            if visible { openRoomForCurrentAppearance() }
            else { releaseRoomForCurrentAppearance() }
        }
        .onChange(of: isInputFocused) { _, _ in updateInputActivity() }
        .onChange(of: draft) { _, _ in updateInputActivity() }
        .sheet(isPresented: $isPresentingInvite) {
            InviteMemberSheet(service: service, room: currentRoom)
        }
        .alert("确认开启端对端加密？", isPresented: $isPresentingEncryptionConfirmation) {
            Button("开启加密") { enableEncryptionAfterConfirmation() }
            Button("返回", role: .cancel) { encryptionConsent.cancel() }
        } message: {
            Text("本客户端在开启后不提供关闭入口。botville 的 AI 接入目前不支持加密会话；当前尚未配置只向可信设备分享密钥。未验证设备或未设置密钥备份时，换设备可能无法查看历史消息。")
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
                Text("加密已开启；本客户端不提供关闭入口。")
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

    private var timeline: some View {
        // Capture one revision so geometry and decisions refer to the same data.
        let input = scrollInput
        return GeometryReader { viewport in
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
                    .scrollTargetLayout()
                    .padding()
                    .background {
                        GeometryReader { content in
                            Color.clear.preference(key: TimelineGeometryKey.self, value: TimelineGeometry(
                                input: input,
                                distanceFromBottom: content.frame(in: .named("roomTimeline")).maxY - viewport.size.height,
                                viewportHeight: viewport.size.height))
                        }
                    }
                }
                .coordinateSpace(name: "roomTimeline")
                // No defaultScrollAnchor: its size-change behavior can pull history
                // to the bottom. The top visible ID is maintained through prepend.
                .scrollPosition(id: $visibleMessageID)
                .simultaneousGesture(DragGesture()
                    .onChanged { _ in
                        isDraggingTimeline = true
                        scrollState.userBeganDragging()
                    }
                    .onEnded { _ in
                        isDraggingTimeline = false
                        recordTimelineGeometry()
                    })
                .onPreferenceChange(TimelineGeometryKey.self) { geometry in
                    timelineGeometry = geometry
                    recordTimelineGeometry()
                }
                .task(id: scrollState.pending) {
                    guard let decision = scrollState.pending else { return }
                    await Task.yield()
                    guard !Task.isCancelled, scrollState.pending == decision,
                          latestScrollRequest == decision.request else { return }
                    withAnimation(decision.animated && !reduceMotion ? .easeOut(duration: 0.2) : nil) {
                        proxy.scrollTo(decision.request.latestMessageID, anchor: .bottom)
                    } completion: {
                        scrollState.didScroll(decision)
                        recordTimelineGeometry()
                    }
                }
            }
        }
    }

    private func recordTimelineGeometry() {
        guard !isDraggingTimeline, let geometry = timelineGeometry else { return }
        scrollState.recordGeometry(for: geometry.input,
                                   distanceFromBottom: geometry.distanceFromBottom,
                                   viewportHeight: geometry.viewportHeight)
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
        let confirmedConsent = encryptionConsent
        encryptionConsent.cancel()
        isEnablingEncryption = true
        Task {
            defer { isEnablingEncryption = false }
            do {
                try await service.enableEncryption(roomID: room.id, consent: confirmedConsent)
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

    private func updateInputActivity() {
        isInputActive = isVisible
            && (isInputFocused || !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
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

    private func openRoom(lease: RoomTimelineLease, generation: UUID) async {
        guard !Task.isCancelled else { return }
        isOpening = true
        errorMessage = nil
        defer {
            if roomTimelineLease == lease { isOpening = false }
        }

        do {
            _ = try await service.openRoom(room.id, lease: lease)
            guard roomTimelineLease == lease, !Task.isCancelled else { return }
            await loadOlderMessages()
            guard roomTimelineLease == lease, !Task.isCancelled else { return }
            completedRoomOpenGeneration = generation
        } catch is CancellationError {
            return
        } catch {
            if roomTimelineLease == lease { errorMessage = error.localizedDescription }
        }
    }

    private func openRoomForCurrentAppearance() {
        guard isVisible else { return }
        roomOpenTask?.cancel()
        let generation = UUID()
        roomOpenGeneration = generation
        scrollState = TimelineScrollState()
        visibleMessageID = nil
        timelineGeometry = nil
        isDraggingTimeline = false
        let lease = service.beginRoomOpen(room.id)
        roomTimelineLease = lease
        roomOpenTask = Task {
            await openRoom(lease: lease, generation: generation)
            guard roomOpenGeneration == generation else { return }
            roomOpenTask = nil
        }
    }

    private func releaseRoomForCurrentAppearance() {
        isInputActive = false
        roomOpenGeneration = UUID()
        roomOpenTask?.cancel()
        roomOpenTask = nil
        if let roomTimelineLease {
            service.closeRoom(room.id, lease: roomTimelineLease)
            self.roomTimelineLease = nil
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

}

private struct TimelineGeometry: Equatable {
    let input: TimelineScrollInput
    let distanceFromBottom: Double
    let viewportHeight: Double
}

private struct TimelineGeometryKey: PreferenceKey {
    static let defaultValue: TimelineGeometry? = nil
    static func reduce(value: inout TimelineGeometry?, nextValue: () -> TimelineGeometry?) {
        value = nextValue() ?? value
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
                        Label("本客户端不提供关闭入口；botville 的 AI 接入目前不支持加密会话。", systemImage: "lock.fill")
                            .font(.footnote)
                        Label("AI 或身份未确认的成员不能加入此房间。", systemImage: "exclamationmark.triangle")
                            .font(.footnote)
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
        }
    }

    private func prepareInvite() {
        errorMessage = nil
        let trimmed = userID.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        invite()
    }

    private func invite() {
        guard !isInviting else { return }
        isInviting = true
        let target = userID.trimmingCharacters(in: .whitespacesAndNewlines)
        Task {
            defer { isInviting = false }
            do {
                try await service.inviteUser(target, to: room.id)
                dismiss()
            } catch {
                errorMessage = error.localizedDescription
            }
        }
    }
}
