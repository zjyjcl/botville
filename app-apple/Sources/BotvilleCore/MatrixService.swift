// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Combine
import Foundation
import MatrixRustSDK

/// SDK ownership and all UI-facing state live on the main actor. Async SDK
/// operations run in Rust; the same client retains the /sync since token.
@MainActor
public final class MatrixService: ObservableObject {
    @Published public private(set) var isLoggedIn = false
    @Published public private(set) var isSyncing = false
    @Published public private(set) var rooms: [MatrixRoom] = []
    @Published public private(set) var messages: [MatrixMessage] = []
    @Published public private(set) var activeRoomID: String?
    @Published public private(set) var userID: String?
    @Published public private(set) var syncError: String?
    @Published public private(set) var canLoadOlderMessages = false

    private var client: Client?
    private var syncTask: Task<Void, Never>?
    private var syncGeneration = UUID()
    private var sessionGeneration = UUID()
    private var timeline: Timeline?
    private var timelineHandle: TaskHandle?
    private var timelineListener: TimelineListenerBridge?
    private var timelineTask: Task<Void, Never>?
    private var timelineGeneration = UUID()
    private var timelineState = TimelineState()
    private var isAuthenticating = false
    private var isLoggingOut = false
    private var paginationGeneration: UUID?

    public init() {}

    deinit {
        syncTask?.cancel()
        timelineHandle?.cancel()
        timelineListener?.continuation.finish()
        timelineTask?.cancel()
    }

    public func login(homeserver: String, username: String, password: String) async throws {
        guard !isAuthenticating, !isLoggedIn else { throw MatrixServiceError.busy }
        let server = try InputValidation.homeserver(homeserver)
        let username = username.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !username.isEmpty, !password.isEmpty else { throw MatrixServiceError.missingCredentials }
        isAuthenticating = true
        defer { isAuthenticating = false }

        let newClient = try await ClientBuilder().homeserverUrl(url: server).inMemoryStore().build()
        try Task.checkCancellation()
        try await newClient.login(username: username, password: password,
                                  initialDeviceName: "botville C0.1", deviceId: nil)
        client = newClient
        sessionGeneration = UUID()
        userID = try newClient.userId()
        isLoggedIn = true
        await newClient.enableAllSendQueues(enable: true)
        try await sync()
    }

    /// Start/restart a single standard /sync loop. The first request is immediate;
    /// subsequent requests long-poll. Network failures retry with bounded backoff.
    public func sync() async throws {
        guard let client, !isLoggingOut else { throw MatrixServiceError.notLoggedIn }
        let session = sessionGeneration
        let generation = UUID()
        syncGeneration = generation
        isSyncing = true
        syncError = nil
        let previous = syncTask
        previous?.cancel()
        await previous?.value
        guard session == sessionGeneration, generation == syncGeneration else { return }

        syncTask = Task { [weak self] in
            var initial = true
            var retryDelay: UInt64 = 1
            while !Task.isCancelled {
                do {
                    _ = try await client.syncOnceV2(settings: SyncSettingsV2(timeoutMs: initial ? 0 : 30_000))
                    guard !Task.isCancelled, let self, self.syncGeneration == generation else { return }
                    self.refreshRooms(from: client)
                    self.isSyncing = false
                    self.syncError = nil
                    initial = false
                    retryDelay = 1
                    // Avoid a busy loop against a server returning immediately.
                    try await Task.sleep(for: .milliseconds(100))
                } catch {
                    guard !Task.isCancelled, let self, self.syncGeneration == generation else { return }
                    self.isSyncing = false
                    self.syncError = "同步失败，将自动重试：\(error.localizedDescription)"
                    do { try await Task.sleep(nanoseconds: retryDelay * 1_000_000_000) }
                    catch { return }
                    retryDelay = min(retryDelay * 2, 30)
                }
            }
        }
    }

    private func refreshRooms(from client: Client) {
        rooms = client.rooms().filter { $0.membership() == .joined }.map {
            MatrixRoom(id: $0.id(), name: $0.displayName() ?? $0.id())
        }.sorted {
            $0.name == $1.name ? $0.id < $1.id : $0.name.localizedStandardCompare($1.name) == .orderedAscending
        }
        if let activeRoomID, !rooms.contains(where: { $0.id == activeRoomID }) {
            closeRoom(activeRoomID)
        }
    }

    public func openRoom(_ roomID: String) async throws {
        guard let client, !isLoggingOut else { throw MatrixServiceError.notLoggedIn }
        guard let room = try client.getRoom(roomId: roomID), room.membership() == .joined else {
            throw MatrixServiceError.roomUnavailable
        }
        clearTimeline()
        activeRoomID = roomID
        let generation = timelineGeneration
        do {
            let newTimeline = try await room.timeline()
            try Task.checkCancellation()
            guard generation == timelineGeneration else { throw CancellationError() }
            timeline = newTimeline
            let (stream, continuation) = AsyncStream<[TimelineDiff]>.makeStream()
            let listener = TimelineListenerBridge(continuation)
            timelineListener = listener
            timelineTask = Task { [weak self] in
                for await updates in stream {
                    guard !Task.isCancelled, let self, self.timelineGeneration == generation else { return }
                    self.timelineState.apply(updates)
                    self.messages = self.timelineState.messages
                }
            }
            let handle = await newTimeline.addListener(listener: listener)
            guard !Task.isCancelled, generation == timelineGeneration else {
                handle.cancel()
                continuation.finish()
                throw CancellationError()
            }
            timelineHandle = handle
            canLoadOlderMessages = true
        } catch {
            if generation == timelineGeneration { clearTimeline() }
            throw error
        }
    }

    public func closeRoom(_ roomID: String) {
        guard activeRoomID == roomID else { return }
        clearTimeline()
    }

    private func clearTimeline() {
        timelineGeneration = UUID()
        timelineHandle?.cancel()
        timelineHandle = nil
        timelineListener?.continuation.finish()
        timelineListener = nil
        timelineTask?.cancel()
        timelineTask = nil
        timeline = nil
        timelineState = TimelineState()
        activeRoomID = nil
        messages = []
        canLoadOlderMessages = false
        paginationGeneration = nil
    }

    public func loadOlderMessages() async throws {
        guard let timeline, canLoadOlderMessages else { return }
        guard paginationGeneration == nil else { throw MatrixServiceError.busy }
        let generation = timelineGeneration
        paginationGeneration = generation
        defer { if paginationGeneration == generation { paginationGeneration = nil } }
        let reachedStart = try await timeline.paginateBackwards(numEvents: 30)
        if generation == timelineGeneration { canLoadOlderMessages = !reachedStart }
    }

    /// Completion means the SDK accepted a local echo into its send queue.
    /// Server acknowledgement/failure arrives later via the timeline listener.
    public func send(text: String, to roomID: String) async throws {
        guard isLoggedIn, !isLoggingOut else { throw MatrixServiceError.notLoggedIn }
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw MatrixServiceError.emptyMessage
        }
        guard activeRoomID == roomID, let timeline else { throw MatrixServiceError.roomUnavailable }
        let content = try messageEventContentNew(msgtype: .text(content: TextMessageContent(body: text, formatted: nil)))
        _ = try await timeline.send(msg: content)
    }

    public func logout() async {
        guard let client, !isLoggingOut else { return }
        isLoggingOut = true
        defer { isLoggingOut = false }
        do {
            try await client.logout()
        } catch {
            syncError = "退出失败，会话仍保留，请重试：\(error.localizedDescription)"
            return
        }
        sessionGeneration = UUID()
        syncGeneration = UUID()
        syncTask?.cancel()
        syncTask = nil
        clearTimeline()
        self.client = nil
        rooms = []
        userID = nil
        isLoggedIn = false
        isSyncing = false
        syncError = nil
    }
}
