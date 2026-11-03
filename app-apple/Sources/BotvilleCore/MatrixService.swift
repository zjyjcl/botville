// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Combine
import Foundation
import MatrixRustSDK
import Network

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

    @Published public private(set) var isRestoring = false
    @Published public private(set) var sessionError: String?
    @Published public private(set) var connectionStatus: ConnectionStatus = .idle

    private let sessionStore: any SessionStore
    private var storeDirectory: SessionStoreDirectory?
    private let discovery: any LoginCapabilityDiscovering
    private var credentials: SessionCredentials?
    private var didAttemptRestore = false
    private var cleanupRequired = false
    private var networkMonitor: NWPathMonitor?
    private var networkAvailable = true
    private var authDelegate: AuthErrorBridge?
    private var authHandle: TaskHandle?
    private var authTask: Task<Void, Never>?
    private var sendingGeneration: UUID?
    private var messageActions = Set<String>()
    private var invitationActions = Set<String>()

    // Injectable for lifecycle tests; production always uses encrypted SQLite.
    var clientFactory: (String, URL, Data) async throws -> Client = { server, directory, key in
        try await ClientBuilder()
            .homeserverUrl(url: server)
            .disableWellKnownLookup(disableWellKnownLookup: true)
            .disableAutomaticTokenRefresh()
            .sqliteStore(config: SqliteStoreBuilder(dataPath: directory.path, cachePath: directory.appendingPathComponent("cache").path).key(key: key))
            .requestConfig(config: RequestConfig(retryLimit: 0, timeout: 40_000,
                                                maxConcurrentRequests: nil, maxRetryTime: nil))
            .build()
    }

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

    public init(sessionStore: any SessionStore = KeychainSessionStore(),
                storeDirectory: SessionStoreDirectory? = nil,
                discovery: any LoginCapabilityDiscovering = MatrixLoginCapabilityDiscovery(),
                monitorNetwork: Bool = true) {
        self.sessionStore = sessionStore
        self.storeDirectory = storeDirectory
        self.discovery = discovery
        if monitorNetwork {
            let monitor = NWPathMonitor()
            networkMonitor = monitor
            monitor.pathUpdateHandler = { [weak self] path in
                let available = path.status == .satisfied
                Task { @MainActor [weak self] in await self?.networkChanged(available: available) }
            }
            monitor.start(queue: DispatchQueue(label: "botville.network"))
        }
    }

    deinit {
        networkMonitor?.cancel()
        authHandle?.cancel()
        authTask?.cancel()
        authDelegate?.continuation.finish()
        syncTask?.cancel()
        timelineHandle?.cancel()
        timelineListener?.continuation.finish()
        timelineTask?.cancel()
    }

    private func directory() throws -> SessionStoreDirectory {
        if let storeDirectory { return storeDirectory }
        let value = try SessionStoreDirectory()
        storeDirectory = value
        return value
    }

    public func login(homeserver: String, username: String, password: String,
                      allowLocalHTTP: Bool = false) async throws {
        guard !isAuthenticating, !isRestoring, !isLoggedIn, !isLoggingOut else { throw MatrixServiceError.busy }
        let server = try HomeserverPolicy(allowLocalHTTP: allowLocalHTTP).validatedURL(homeserver)
        let username = username.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !username.isEmpty, !password.isEmpty else { throw MatrixServiceError.missingCredentials }
        isAuthenticating = true
        defer { isAuthenticating = false }
        // Never overwrite an unreadable or uncleaned previous account.
        if cleanupRequired { try cleanStoredSession() }
        guard try sessionStore.load() == nil else { throw MatrixServiceError.storageUnavailable }
        sessionError = nil
        let capabilities = try await discovery.discover(at: server)
        guard capabilities.supportsPassword else { throw AuthenticationError.unsupportedLoginMethod }
        let storeID = UUID()
        let key = try SessionCredentials.makeStoreKey()
        let path = try directory().create(for: storeID)
        var candidate: Client?
        var loggedIn = false
        do {
            let newClient = try await clientFactory(server.absoluteString, path, key)
            candidate = newClient
            try await newClient.login(username: username, password: password,
                                      initialDeviceName: "botville C2.1", deviceId: nil)
            loggedIn = true
            let stored = SessionCredentials(session: try newClient.session(), storeID: storeID,
                                            storeKey: key, allowLocalHTTP: allowLocalHTTP)
            _ = try HomeserverPolicy(allowLocalHTTP: allowLocalHTTP).validatedURL(stored.homeserverURL)
            try sessionStore.save(stored)
            credentials = stored
            try await activate(newClient, stored: stored)
        } catch {
            if loggedIn { try? await candidate?.logout() }
            await candidate?.enableAllSendQueues(enable: false)
            candidate = nil
            // A failed Keychain write/activation must never become an active UI session.
            if credentials?.storeID == storeID { await clearSession() }
            else { try? directory().remove(for: storeID) }
            if error is SessionStoreError { throw MatrixServiceError.storageUnavailable }
            if let error = error as? MatrixServiceError { throw error }
            throw PasswordLoginResultClassifier.error(for: error)
        }
    }

    public func restoreSession() async {
        guard !didAttemptRestore, !isAuthenticating, !isLoggedIn, !isLoggingOut else { return }
        didAttemptRestore = true
        isRestoring = true
        defer { isRestoring = false }
        do {
            if cleanupRequired { try cleanStoredSession() }
            guard let stored = try sessionStore.load() else { return }
            credentials = stored
            _ = try HomeserverPolicy(allowLocalHTTP: stored.allowLocalHTTP).validatedURL(stored.homeserverURL)
            let path = try directory().create(for: stored.storeID)
            let restored = try await clientFactory(stored.homeserverURL, path, stored.storeKey)
            try await restored.restoreSession(session: stored.sdkSession)
            try await activate(restored, stored: stored)
        } catch {
            if PasswordLoginResultClassifier.isExpiredSession(error) {
                await invalidateSession()
            } else {
                // Offline startup is retryable; never discard a valid token for transport errors.
                sessionError = "会话恢复暂时失败，请检查网络后重试。"
                didAttemptRestore = false
            }
        }
    }

    private func activate(_ client: Client, stored: SessionCredentials) async throws {
        self.client = client
        credentials = stored
        sessionGeneration = UUID()
        let generation = sessionGeneration
        let (stream, continuation) = AsyncStream<Void>.makeStream()
        let delegate = AuthErrorBridge(continuation)
        authDelegate = delegate
        authHandle = try client.setDelegate(delegate: delegate)
        authTask = Task { [weak self] in
            for await _ in stream {
                guard let self, generation == self.sessionGeneration, !self.isLoggingOut else { return }
                await self.invalidateSession()
                return
            }
        }
        userID = stored.userID
        isLoggedIn = true
        sessionError = nil
        await refreshRooms(from: client, session: generation)
        guard generation == sessionGeneration, isLoggedIn else { throw CancellationError() }
        await client.enableAllSendQueues(enable: true)
        guard generation == sessionGeneration, isLoggedIn else { throw CancellationError() }
        try await sync()
    }

    public func applicationDidBecomeActive() async {
        if isLoggedIn { try? await sync() }
        else { await restoreSession() }
    }

    private func networkChanged(available: Bool) async {
        let wasAvailable = networkAvailable
        networkAvailable = available
        guard isLoggedIn else { return }
        if !available { connectionStatus = .offline }
        else if !wasAvailable { try? await sync() }
    }

    /// A single standard /sync loop; foreground and path recovery interrupt backoff.
    public func sync() async throws {
        guard let client, isLoggedIn, !isLoggingOut else { throw MatrixServiceError.notLoggedIn }
        let session = sessionGeneration
        let generation = UUID()
        syncGeneration = generation
        isSyncing = true
        connectionStatus = networkAvailable ? .syncing : .offline
        syncError = nil
        let previous = syncTask
        previous?.cancel()
        await previous?.value
        guard session == sessionGeneration, generation == syncGeneration else { return }
        syncTask = Task { [weak self] in
            var initial = true
            var backoff = ReconnectPolicy()
            while !Task.isCancelled {
                do {
                    _ = try await client.syncOnceV2(settings: SyncSettingsV2(timeoutMs: initial ? 0 : 30_000))
                    guard !Task.isCancelled, let self, self.syncGeneration == generation else { return }
                    await self.refreshRooms(from: client, session: session)
                    guard !Task.isCancelled, self.syncGeneration == generation else { return }
                    self.isSyncing = false
                    self.syncError = nil
                    self.connectionStatus = .connected
                    // Recoverable send errors disable queues; successful sync proves reachability.
                    await client.enableAllSendQueues(enable: true)
                    initial = false
                    backoff.reset()
                    try await Task.sleep(for: .milliseconds(100))
                } catch {
                    guard !Task.isCancelled, let self, self.syncGeneration == generation else { return }
                    if PasswordLoginResultClassifier.isExpiredSession(error) {
                        // Do not await/cancel this task from inside itself during cleanup.
                        self.syncTask = nil
                        await self.invalidateSession()
                        return
                    }
                    self.isSyncing = false
                    let delay = backoff.nextDelay()
                    self.connectionStatus = self.networkAvailable ? .reconnecting(after: delay) : .offline
                    self.syncError = "同步连接中断，恢复后将自动补拉消息。"
                    do { try await Task.sleep(for: .seconds(delay)) } catch { return }
                    initial = true
                }
            }
        }
    }

    private func refreshRooms(from client: Client, session: UUID) async {
        var updated: [MatrixRoom] = []
        for room in client.rooms() {
            let membership: MatrixRoomMembership
            switch room.membership() {
            case .joined: membership = .joined
            case .invited: membership = .invited
            default: continue
            }
            let info = try? await room.roomInfo()
            let latest = await room.latestEvent()
            let timestamp: UInt64?
            switch latest {
            case .remote(let time, _, _, _, _), .remoteInvite(let time, _, _), .local(let time, _, _, _, _): timestamp = time
            case .none: timestamp = nil
            }
            let direct = await room.isDirect()
            updated.append(MatrixRoom(id: room.id(), name: room.displayName() ?? room.id(),
                                      membership: membership, isDirect: direct,
                                      unreadCount: info?.numUnreadMessages ?? 0,
                                      latestActivity: timestamp.map { Date(timeIntervalSince1970: Double($0) / 1000) }))
        }
        guard session == sessionGeneration, isLoggedIn, !isLoggingOut else { return }
        rooms = MatrixRoom.sortedByActivity(updated)
        if let activeRoomID, !rooms.contains(where: { $0.id == activeRoomID && $0.membership == .joined }) {
            closeRoom(activeRoomID)
        }
    }

    public func acceptInvitation(_ roomID: String) async throws { try await respondToInvitation(roomID, accept: true) }
    public func rejectInvitation(_ roomID: String) async throws { try await respondToInvitation(roomID, accept: false) }

    private func respondToInvitation(_ roomID: String, accept: Bool) async throws {
        guard let client, !isLoggingOut else { throw MatrixServiceError.notLoggedIn }
        guard !invitationActions.contains(roomID) else { throw MatrixServiceError.busy }
        guard let room = try client.getRoom(roomId: roomID), room.membership() == .invited else { throw MatrixServiceError.roomUnavailable }
        let session = sessionGeneration
        invitationActions.insert(roomID)
        defer { if session == sessionGeneration { invitationActions.remove(roomID) } }
        do {
            if accept { try await room.join() } else { try await room.leave() }
            guard session == sessionGeneration else { throw CancellationError() }
            await refreshRooms(from: client, session: session)
            try await sync()
        } catch {
            throw await operationFailure(error, session: session)
        }
    }

    public func openRoom(_ roomID: String) async throws {
        guard let client, !isLoggingOut else { throw MatrixServiceError.notLoggedIn }
        guard let room = try client.getRoom(roomId: roomID), room.membership() == .joined else {
            throw MatrixServiceError.roomUnavailable
        }
        let session = sessionGeneration
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
            throw await operationFailure(error, session: session)
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
        sendingGeneration = nil
        messageActions.removeAll()
    }

    public func loadOlderMessages() async throws {
        guard let timeline, canLoadOlderMessages else { return }
        guard paginationGeneration == nil else { throw MatrixServiceError.busy }
        let generation = timelineGeneration
        paginationGeneration = generation
        defer { if paginationGeneration == generation { paginationGeneration = nil } }
        let session = sessionGeneration
        do {
            let reachedStart = try await timeline.paginateBackwards(numEvents: 30)
            if generation == timelineGeneration { canLoadOlderMessages = !reachedStart }
        } catch { throw await operationFailure(error, session: session) }
    }

    /// Completion only acknowledges SDK queue insertion, never server delivery.
    public func send(text: String, to roomID: String) async throws {
        guard isLoggedIn, !isLoggingOut else { throw MatrixServiceError.notLoggedIn }
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { throw MatrixServiceError.emptyMessage }
        guard activeRoomID == roomID, let timeline else { throw MatrixServiceError.roomUnavailable }
        guard sendingGeneration == nil else { throw MatrixServiceError.busy }
        let generation = timelineGeneration
        sendingGeneration = generation
        let session = sessionGeneration
        defer { if sendingGeneration == generation { sendingGeneration = nil } }
        do {
            let content = try messageEventContentNew(msgtype: .text(content: TextMessageContent(body: text, formatted: nil)))
            _ = try await timeline.send(msg: content)
        } catch { throw await operationFailure(error, session: session) }
    }

    public func retryMessage(_ messageID: String) async throws { try await actOnFailedMessage(messageID, retry: true) }
    public func cancelMessage(_ messageID: String) async throws { try await actOnFailedMessage(messageID, retry: false) }

    private func actOnFailedMessage(_ messageID: String, retry: Bool) async throws {
        guard isLoggedIn, !isLoggingOut else { throw MatrixServiceError.notLoggedIn }
        guard let timeline, let transactionID = timelineState.failedSendTransactionID(for: messageID) else { throw MatrixServiceError.sendNoLongerFailed }
        guard !messageActions.contains(messageID) else { throw MatrixServiceError.busy }
        let generation = timelineGeneration
        messageActions.insert(messageID)
        let session = sessionGeneration
        defer { if generation == timelineGeneration { messageActions.remove(messageID) } }
        let changed: Bool
        do {
            let actions = SDKTimelineSendActions(timeline: timeline, transactionID: transactionID)
            if retry { changed = try await actions.retry() }
            else { changed = try await actions.cancel() }
        } catch { throw await operationFailure(error, session: session) }
        guard changed else { throw MatrixServiceError.sendNoLongerFailed }
    }

    private func operationFailure(_ error: Error, session: UUID) async -> Error {
        if error is CancellationError || error is MatrixServiceError { return error }
        if PasswordLoginResultClassifier.isExpiredSession(error) {
            if session == sessionGeneration { await invalidateSession() }
            return MatrixServiceError.sessionExpired
        }
        if PasswordLoginResultClassifier.error(for: error) == .networkFailure { return AuthenticationError.networkFailure }
        return MatrixServiceError.operationFailed
    }

    public func logout() async {
        guard let client, !isLoggingOut else { return }
        isLoggingOut = true
        defer { isLoggingOut = false }
        do { try await client.logout() }
        catch {
            guard PasswordLoginResultClassifier.isExpiredSession(error) else {
                syncError = "服务端退出失败，会话仍保留，请联网后重试。"
                return
            }
        }
        await clearSession()
        sessionError = cleanupRequired ? MatrixServiceError.storageUnavailable.localizedDescription : nil
    }

    private func invalidateSession() async {
        guard !isLoggingOut else { return }
        isLoggingOut = true
        await clearSession()
        sessionError = cleanupRequired ? MatrixServiceError.storageUnavailable.localizedDescription : "会话已失效，请重新登录。"
        isLoggingOut = false
    }

    private func clearSession() async {
        sessionGeneration = UUID()
        syncGeneration = UUID()
        let previous = syncTask
        syncTask = nil
        previous?.cancel()
        authHandle?.cancel()
        authHandle = nil
        authDelegate?.continuation.finish()
        authDelegate = nil
        authTask?.cancel()
        authTask = nil
        clearTimeline()
        rooms = []
        userID = nil
        isLoggedIn = false
        isSyncing = false
        connectionStatus = .idle
        syncError = nil
        invitationActions.removeAll()
        await client?.enableAllSendQueues(enable: false)
        await previous?.value
        client = nil
        cleanupRequired = true
        do { try cleanStoredSession() }
        catch { sessionError = MatrixServiceError.storageUnavailable.localizedDescription }
    }

    private func cleanStoredSession() throws {
        if let stored = try credentials ?? sessionStore.load() {
            try directory().remove(for: stored.storeID)
        }
        try sessionStore.delete()
        credentials = nil
        cleanupRequired = false
    }
}

private final class AuthErrorBridge: ClientDelegate, Sendable {
    let continuation: AsyncStream<Void>.Continuation
    init(_ continuation: AsyncStream<Void>.Continuation) { self.continuation = continuation }
    func didReceiveAuthError(isSoftLogout: Bool) { continuation.yield(()) }
    func onBackgroundTaskErrorReport(taskName: String, error: BackgroundTaskFailureReason) {}
}
