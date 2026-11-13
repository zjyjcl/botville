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
#if DEBUG
    @State private var tracedLastRow: TimelineLastRowTrace?
#endif
    @StateObject private var layoutVersion = TimelineLayoutVersion()
    @State private var timelineGeometry: TimelineGeometry?
    @State private var anchorGeometryCache = TimelineAnchorGeometryCache()
    @State private var headerFootprint = TimelineHeaderFootprint()
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
    @State private var olderMessagesOperation: UUID?
    @State private var olderMessagesLayoutError: String?
    private var isLoadingOlderMessages: Bool { olderMessagesOperation != nil }
    @State private var errorMessage: String?
    @State private var isRefreshingSecurity = false
    @State private var isEnablingEncryption = false
    @State private var isPresentingEncryptionConfirmation = false
    @State private var isPresentingInvite = false
    @State private var isPresentingSettings = false
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
            if isOpening || (service.activeRoomID == room.id && !service.hasReceivedTimelineSnapshot) {
                ProgressView(isOpening ? "正在打开房间…" : "正在同步消息…")
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
        .onChange(of: TimelineLayoutSnapshot(input: scrollInput,
                     generation: scrollState.prependAnchor.layoutGeneration), initial: true) { _, snapshot in
            layoutVersion.update(snapshot)
        }
        .navigationTitle(room.name)
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Menu {
                    Button { isPresentingSettings = true } label: {
                        Label("房间信息", systemImage: "info.circle")
                    }
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
        .sheet(isPresented: $isPresentingSettings) {
            RoomNotificationSettingsSheet(service: service, controller: service.roomNotifications, room: currentRoom)
        }
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
                        if let olderMessagesLayoutError {
                            Text(olderMessagesLayoutError).font(.footnote).foregroundStyle(.secondary)
                        }
                        // Keep both identity AND measured footprint. At the top,
                        // removing this height would require an unreachable
                        // negative content offset to retain the first row.
                        VStack(spacing: 0) {
                            if service.canLoadOlderMessages {
                                olderMessagesButton
                            }
                        }
                        .frame(minHeight: service.canLoadOlderMessages ? 0 : headerFootprint.height)
                        .background(GeometryReader { header in
                            Color.clear.preference(key: TimelineHeaderHeightKey.self, value: header.size.height)
                        })
                        ForEach(service.messages) { message in
                            MessageBubble(message: message, retry: {
                                Task { await actOnMessage(message.id, retry: true) }
                            }, cancel: {
                                Task { await actOnMessage(message.id, retry: false) }
                            })
                            .overlay(alignment: .top) {
                                // A zero-height target makes offset / viewportHeight
                                // exact even for a partly visible, viewport-tall row.
                                Color.clear.frame(height: 0)
                                    .id(TimelinePrependPoint(messageID: message.id))
                                    .allowsHitTesting(false)
                            }
                            .background(TimelineMessageMeasurement(input: input, messageID: message.id,
                                                                   layoutVersion: layoutVersion))
                            .id(message.id)
                        }
                    }
                    .padding()
                    .background(TimelineContentMeasurement(layoutVersion: layoutVersion,
                                                           viewportHeight: viewport.size.height))
                }
                .coordinateSpace(name: "roomTimeline")
                // Prepend is restored explicitly from measured row geometry. A
                // scrollPosition binding would compete with the pixel correction.
                .simultaneousGesture(DragGesture()
                    .onChanged { _ in
#if DEBUG
                        if !isDraggingTimeline, let operation = scrollState.prependAnchor.operation {
                            TimelineTrace.record(.anchorResult, reason: .gesture, scope: operation)
                        }
#endif
                        isDraggingTimeline = true
                        scrollState.userBeganDragging()
                    }
                    .onEnded { _ in
                        isDraggingTimeline = false
                        recordTimelineGeometry()
                    })
#if DEBUG
                .onChange(of: service.canLoadOlderMessages) { _, visible in
                    TimelineTrace.record(.headerLayout, scope: roomOpenGeneration,
                                         visible: visible, offset: headerFootprint.height)
                }
#endif
                .onPreferenceChange(TimelineHeaderHeightKey.self) { height in
                    headerFootprint.record(height: height, buttonVisible: service.canLoadOlderMessages)
#if DEBUG
                    TimelineTrace.record(.headerLayout, scope: roomOpenGeneration,
                                         visible: service.canLoadOlderMessages, offset: height)
#endif
                }
                .onPreferenceChange(TimelineGeometryKey.self) { geometry in
                    guard let geometry, geometry.input == scrollInput else { return }
                    timelineGeometry = geometry
#if DEBUG
                    let last = geometry.frames.first { $0.id == geometry.input.request?.latestMessageID }
                    let visible = last?.intersects(viewportHeight: geometry.viewportHeight ?? 0) == true
                    let trace = TimelineLastRowTrace(id: geometry.input.request?.latestMessageID,
                                                     materialized: last != nil, visible: visible)
                    if trace != tracedLastRow {
                        tracedLastRow = trace
                        TimelineTrace.record(.lastRow, reason: last == nil ? .unmaterialized : .none,
                                             scope: roomOpenGeneration, targetID: trace.id,
                                             count: geometry.input.messageIDs.count, generation: geometry.generation,
                                             visible: visible, offset: last?.minY)
                    }
#endif
                    anchorGeometryCache.record(input: geometry.input, frames: geometry.frames,
                                               viewportHeight: geometry.viewportHeight ?? 0)
                    recordTimelineGeometry()
                }
                .task(id: scrollWork) {
                    let work = scrollWork
#if DEBUG
                    var lastTraceGate: TimelineTrace.Reason?
#endif
                    // One bounded driver per operation, not a task cancelled by
                    // every geometry frame. It also expires when no frames arrive.
                    for _ in 0..<160 {
                        guard !Task.isCancelled, work == scrollWork, !isDraggingTimeline, isVisible else { return }
                        let now = ProcessInfo.processInfo.systemUptime
                        scrollState.expireScrollWork(now: now)
                        guard scrollState.prependAnchor.operation != nil ||
                                scrollState.bottomSettlement.decision != nil else {
                            recordTimelineGeometry()
                            return
                        }
#if DEBUG
                        if let operation = scrollState.prependAnchor.operation {
                            let gate: TimelineTrace.Reason
                            if !scrollState.prependAnchor.pageRevisionApplied {
                                gate = .awaitingPage
                            } else if let geometry = timelineGeometry {
                                if geometry.input != scrollInput || geometry.generation != scrollState.prependAnchor.layoutGeneration {
                                    gate = .staleGeometry
                                } else if let height = geometry.viewportHeight, height.isFinite, height > 0 {
                                    gate = .none
                                } else { gate = .invalidViewport }
                            } else { gate = .noGeometry }
                            if gate != lastTraceGate {
                                lastTraceGate = gate
                                TimelineTrace.record(.anchorWaiting, reason: gate, scope: operation,
                                                     count: timelineGeometry?.frames.count,
                                                     generation: scrollState.prependAnchor.layoutGeneration,
                                                     pageApplied: scrollState.prependAnchor.pageRevisionApplied)
                            }
                        }
#endif
                        let geometry = timelineGeometry.flatMap {
                            $0.input == scrollInput && $0.generation == scrollState.prependAnchor.layoutGeneration
                                && ($0.viewportHeight ?? 0).isFinite && ($0.viewportHeight ?? 0) > 0 ? $0 : nil
                        }
                        let action: TimelinePrependAnchor.Action?
                        if let geometry {
                            action = scrollState.restorePrepend(for: geometry.input,
                                generation: geometry.generation, frames: geometry.frames)
                        } else {
                            action = scrollState.recoverMissingPrependGeometry()
                        }
                        if let action {
                            let generation = scrollState.prependAnchor.layoutGeneration
                            var transaction = Transaction(animation: nil)
                            transaction.disablesAnimations = true
                            withTransaction(transaction) {
                                switch action {
                                case .locate(let id): proxy.scrollTo(id, anchor: .top)
                                case .restore(let id, let offset):
                                    if let height = geometry?.viewportHeight {
                                        proxy.scrollTo(TimelinePrependPoint(messageID: id),
                                                       anchor: UnitPoint(x: 0.5, y: offset / height))
                                    }
                                }
                            }
                            scrollState.didExecutePrepend(generation: generation)
                        } else if let geometry, let height = geometry.viewportHeight,
                                  let decision = scrollState.correctBottom(for: geometry.input,
                                    frames: geometry.frames, viewportHeight: height, now: now) {
                            let duration = decision.animationDuration(reduceMotion: reduceMotion)
                            withAnimation(duration.map { .easeOut(duration: $0) }) {
                                proxy.scrollTo(decision.request.latestMessageID, anchor: .bottom)
                            }
                        }
                        layoutVersion.update(.init(input: scrollInput,
                                                   generation: scrollState.prependAnchor.layoutGeneration))
                        recordTimelineGeometry()
                        do { try await Task.sleep(for: .milliseconds(50)) }
                        catch { return }
                    }
                    // Defensive iteration cap, independent of clock/geometry.
                    if work == scrollWork { scrollState.expireScrollWork(now: .infinity) }
                }
                .task(id: scrollState.followDecision) {
                    guard let decision = scrollState.followDecision else { return }
                    await Task.yield()
                    guard !Task.isCancelled, scrollState.followDecision == decision,
                          latestScrollRequest == decision.request else { return }
                    let duration = decision.animationDuration(reduceMotion: reduceMotion)
                    withAnimation(duration.map { .easeOut(duration: $0) }) {
                        proxy.scrollTo(decision.request.latestMessageID, anchor: .bottom)
                    } completion: {
                        scrollState.didScroll(decision)
                        recordTimelineGeometry()
                    }
                }
            }
        }
    }

    private var scrollWork: TimelineScrollWork {
        .init(prepend: scrollState.prependAnchor.operation, bottom: scrollState.bottomSettlement.decision)
    }

    private func recordTimelineGeometry() {
        guard !isDraggingTimeline, scrollState.bottomSettlement.decision == nil, let geometry = timelineGeometry,
              geometry.generation == scrollState.prependAnchor.layoutGeneration,
              let height = geometry.viewportHeight else { return }
        // LazyVStack's estimated content extent can say "not near bottom" even
        // while the actual last row is visible. Preserve follow for late sync.
        let last = geometry.frames.first {
            $0.id == geometry.input.request?.latestMessageID && $0.minY.isFinite && $0.height.isFinite
                && $0.height > 0 && $0.minY + $0.height > 0
        }
        let distance = last.map { $0.minY + $0.height - height } ?? .infinity
        scrollState.recordGeometry(for: geometry.input,
                                   distanceFromBottom: distance,
                                   viewportHeight: height)
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
        let operation = UUID()
        let appearance = roomOpenGeneration
        var pageCompletion: TimelinePageCompletion?
        let input = scrollInput
        let geometry = timelineGeometry.flatMap { $0.input == input ? $0 : nil }
        let capture = anchorGeometryCache.capture(input: input, frames: geometry?.frames ?? [],
                                                  viewportHeight: geometry?.viewportHeight ?? 0)
        // No valid capture has ever arrived: keep the page intact and allow retry
        // after layout, instead of performing an unanchored prepend.
        guard input.messageIDs.isEmpty || (input.request != nil &&
              (!capture.frames.isEmpty || scrollState.prependAnchor.blocksFollowing)) else {
#if DEBUG
            TimelineTrace.record(.anchorResult, reason: .noCapture, scope: operation, context: appearance)
#endif
            olderMessagesLayoutError = "消息布局尚未就绪，请重试加载更早的消息。"
            return
        }
        olderMessagesLayoutError = nil
        scrollState.beginPrepend(operation: operation, input: input,
                                 frames: capture.frames, viewportHeight: capture.viewportHeight)
        olderMessagesOperation = operation
        defer {
            if olderMessagesOperation == operation, roomOpenGeneration == appearance {
                olderMessagesOperation = nil
                scrollState.update(scrollInput)
                scrollState.completePrepend(operation: operation, result: pageCompletion)
            }
        }
        errorMessage = nil
        do {
            pageCompletion = try await service.loadOlderMessages(operation: operation) { completedAt in
                guard olderMessagesOperation == operation, roomOpenGeneration == appearance else { return }
                scrollState.update(scrollInput)
                scrollState.finishPrepend(operation: operation, completedAt: completedAt, now: completedAt)
            }
        } catch {
            if olderMessagesOperation == operation, roomOpenGeneration == appearance {
                errorMessage = error.localizedDescription
            }
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
            // First presentation belongs to the live listener. An automatic
            // backwards page delayed appearance and could mutate an unmeasured
            // timeline before a scroll request even existed.
            completedRoomOpenGeneration = generation
        } catch is CancellationError {
            return
        } catch {
            if roomTimelineLease == lease { errorMessage = error.localizedDescription }
        }
    }

    private func openRoomForCurrentAppearance() {
        guard isVisible else { return }
#if DEBUG
        if let operation = scrollState.prependAnchor.operation {
            TimelineTrace.record(.anchorResult, reason: .appearance, scope: operation)
        }
        tracedLastRow = nil
#endif
        roomOpenTask?.cancel()
        let generation = UUID()
        roomOpenGeneration = generation
        completedRoomOpenGeneration = nil
        isOpening = true
        scrollState = TimelineScrollState()
        olderMessagesOperation = nil
        olderMessagesLayoutError = nil
        timelineGeometry = nil
        anchorGeometryCache = TimelineAnchorGeometryCache()
        headerFootprint = TimelineHeaderFootprint()
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
#if DEBUG
        if let operation = scrollState.prependAnchor.operation {
            TimelineTrace.record(.anchorResult, reason: .appearance, scope: operation)
        }
        tracedLastRow = nil
#endif
        scrollState.cancelScrollWork()
        olderMessagesOperation = nil
        olderMessagesLayoutError = nil
        timelineGeometry = nil
        anchorGeometryCache = TimelineAnchorGeometryCache()
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
            Text("加载更早的消息")
                .opacity(isLoadingOlderMessages ? 0 : 1)
                .overlay {
                    if isLoadingOlderMessages { ProgressView().controlSize(.small) }
                }
                .accessibilityLabel(isLoadingOlderMessages ? "正在加载更早的消息" : "加载更早的消息")
        }
        .disabled(isLoadingOlderMessages)
        .frame(maxWidth: .infinity)
    }

}

@MainActor
private final class TimelineLayoutVersion: ObservableObject {
    @Published private(set) var snapshot: TimelineLayoutSnapshot?
    func update(_ next: TimelineLayoutSnapshot) {
        if snapshot != next { snapshot = next }
    }
    var generation: UInt { snapshot?.generation ?? 0 }
    var input: TimelineScrollInput? { snapshot?.input }
}

private struct TimelineScrollWork: Equatable {
    let prepend: UUID?
    let bottom: TimelineScrollDecision?
}

private struct TimelineLayoutSnapshot: Equatable {
    let input: TimelineScrollInput
    let generation: UInt
}

private struct TimelineContentMeasurement: View {
    @ObservedObject var layoutVersion: TimelineLayoutVersion
    let viewportHeight: Double

    var body: some View {
        GeometryReader { content in
            if let input = layoutVersion.input {
                Color.clear.preference(key: TimelineGeometryKey.self, value: TimelineGeometry(
                    input: input, generation: layoutVersion.generation,
                    distanceFromBottom: content.frame(in: .named("roomTimeline")).maxY - viewportHeight,
                    viewportHeight: viewportHeight))
            }
        }
        .id(layoutVersion.generation)
    }
}

private struct TimelineMessageMeasurement: View {
    let input: TimelineScrollInput
    let messageID: String
    // LazyVStack can materialize a cached row closure AFTER a scroll command.
    // Observe a shared reference, not the cached closure's old value snapshot.
    @ObservedObject var layoutVersion: TimelineLayoutVersion

    var body: some View {
        GeometryReader { row in
            Color.clear.preference(key: TimelineGeometryKey.self, value: TimelineGeometry(
                input: input, generation: layoutVersion.generation,
                frames: [.init(id: messageID, minY: row.frame(in: .named("roomTimeline")).minY,
                               height: row.size.height)]))
        }
    }
}

private struct TimelineGeometry: Equatable {
    let input: TimelineScrollInput
    let generation: UInt
    var distanceFromBottom: Double? = nil
    var viewportHeight: Double? = nil
    var frames: [TimelineMessageFrame] = []
}

private struct TimelineHeaderHeightKey: PreferenceKey {
    static let defaultValue: Double = 0
    static func reduce(value: inout Double, nextValue: () -> Double) { value = max(value, nextValue()) }
}

private struct TimelineGeometryKey: PreferenceKey {
    static let defaultValue: TimelineGeometry? = nil
    static func reduce(value: inout TimelineGeometry?, nextValue: () -> TimelineGeometry?) {
        guard let next = nextValue() else { return }
        guard let previous = value else { value = next; return }
        // Cached lazy rows may still carry an older input array even though
        // their actual frame has just been measured. Only the content reader
        // supplies the rendered data revision/viewport; rows cannot replace it.
        if previous.generation != next.generation {
            value = previous.generation > next.generation ? previous : next
            return
        }
        var merged = next.viewportHeight != nil ? next : previous
        merged.frames = previous.frames + next.frames
        value = merged
    }
}

// Separate ID type avoids collisions with message IDs used for materialization
// and bottom-follow. The marker is available after its lazy row is laid out.
private struct TimelinePrependPoint: Hashable {
    let messageID: String
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

#if DEBUG
private struct TimelineLastRowTrace: Equatable {
    let id: String?
    let materialized: Bool
    let visible: Bool
}
#endif

@MainActor
private struct RoomNotificationSettingsSheet: View {
    @Environment(\.dismiss) private var dismiss
    @ObservedObject var service: MatrixService
    @ObservedObject var controller: RoomMuteController
    let room: MatrixRoom
    @State private var presentedError: String?
    @State private var aiProfile: AIProfile?
    @State private var profileError: String?
    @State private var loadingProfile = false

    private var state: RoomMuteState { controller.state(for: room.id) }

    var body: some View {
        NavigationStack {
            Form {
                if let aiProfile {
                    Section {
                        NavigationLink {
                            AIProfileView(service: service, profile: aiProfile)
                        } label: {
                            Label("AI 资料", systemImage: "person.crop.square")
                        }
                        .tint(aiProfileAccent)
                    }
                } else if loadingProfile {
                    ProgressView("正在读取成员资料…")
                } else if let profileError {
                    Section {
                        Text(profileError).font(.footnote).foregroundStyle(.secondary)
                        Button("重试读取资料") { Task { await loadProfile() } }
                    }
                }
                Section {
                    Toggle("消息免打扰", isOn: Binding(
                        get: { state.isMuted },
                        set: { value in Task { await controller.setMuted(value, room: room) } }
                    ))
                    .tint(Color(red: 0x1A / 255.0, green: 0xAD / 255.0, blue: 0x8E / 255.0))
                    .disabled(!state.isLoaded || state.isUpdating)
                    if state.isUpdating { ProgressView("正在保存…") }
                } footer: {
                    Text("开启后不提醒此房间的新消息。会话列表保留未读红点，App 图标角标不计入此房间。")
                }
                if let error = state.errorMessage {
                    Section {
                        Text(error).font(.footnote).foregroundStyle(.red)
                        Button("重试") { Task { await controller.refresh(room) } }
                            .disabled(state.isUpdating || state.readStatus == .loading)
                    }
                } else if state.readStatus == .loading {
                    ProgressView("正在读取设置…")
                }
            }
            .navigationTitle("房间信息")
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } } }
            .task(id: room.id) { await controller.refresh(room) }
            .task(id: room.id) { await loadProfile() }
            .onChange(of: service.userID) { _, _ in
                aiProfile = nil
                dismiss()
            }
            .onChange(of: service.isLoggedIn) { _, loggedIn in
                if !loggedIn { aiProfile = nil; dismiss() }
            }
            .onChange(of: state.errorMessage, initial: true) { _, error in
                // Keep the failure visible even if the next sync already
                // reconciles the settings and clears the controller's error.
                if let error { presentedError = error }
            }
            .alert("消息免打扰", isPresented: Binding(
                get: { presentedError != nil },
                set: { if !$0 { presentedError = nil } }
            )) {
                Button("知道了", role: .cancel) { presentedError = nil }
            } message: {
                Text(presentedError ?? "")
            }
        }
    }
    private func loadProfile() async {
        guard room.isDirect, !loadingProfile else { return }
        loadingProfile = true
        aiProfile = nil
        profileError = nil
        defer { loadingProfile = false }
        do {
            let profile = try await service.aiProfile(roomID: room.id)
            guard !Task.isCancelled else { return }
            aiProfile = profile
        } catch is CancellationError {
            // A closed sheet/session must never publish an earlier profile.
        } catch {
            profileError = "暂时无法读取成员资料。"
        }
    }
}
