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
    @Published public private(set) var isAuthenticating = false
    @Published public private(set) var authenticationStatus: String?
    @Published public private(set) var canClearLocalSession = false
    @Published public private(set) var canRetryRestore = false
    /// The server retained the account but requires its password again.  The
    /// encrypted local store remains available for re-authentication.
    @Published public private(set) var requiresPasswordReauthentication = false
    @Published public private(set) var reauthenticationHomeserver: String?
    @Published public private(set) var reauthenticationUserID: String?
    private let passwordAuthentication: any PasswordAuthenticating
    private let authenticationTimeout: TimeInterval
    private var authenticationAttempt: AuthenticationAttempt?
    var storeKeyFactory: () throws -> Data = SessionCredentials.makeStoreKey
    private var isLoggingOut = false
    private var softLogoutCredentials: SessionCredentials?
    private var paginationGeneration: UUID?

    public init(sessionStore: any SessionStore = KeychainSessionStore(),
                storeDirectory: SessionStoreDirectory? = nil,
                discovery: any LoginCapabilityDiscovering = MatrixLoginCapabilityDiscovery(),
                monitorNetwork: Bool = true,
                passwordAuthentication: any PasswordAuthenticating = MatrixPasswordAuthentication(),
                authenticationTimeout: TimeInterval = 60) {
        self.sessionStore = sessionStore
        self.storeDirectory = storeDirectory
        self.discovery = discovery
        self.passwordAuthentication = passwordAuthentication
        self.authenticationTimeout = authenticationTimeout.isFinite && authenticationTimeout > 0 ? authenticationTimeout : 60
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
        // Soft logout is a re-authentication of the saved account, never a
        // chance to bind its device ID and encrypted store to caller supplied
        // homeserver or username values.
        let saved = softLogoutCredentials
        let requestedHomeserver = saved?.homeserverURL ?? homeserver
        let requestedUsername = saved?.userID ?? username
        let requestedAllowLocalHTTP = saved?.allowLocalHTTP ?? allowLocalHTTP
        // The Debug UI is not this boundary: callers can invoke this public
        // API directly, so the binary configuration gates HTTP here as well.
        let effectiveAllowLocalHTTP = RestoredHomeserverPolicy(storedAllowLocalHTTP: requestedAllowLocalHTTP).allowLocalHTTP
        let server = try HomeserverPolicy(allowLocalHTTP: effectiveAllowLocalHTTP).validatedURL(requestedHomeserver)
        let username = requestedUsername.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !username.isEmpty, !password.isEmpty else { throw MatrixServiceError.missingCredentials }
        isAuthenticating = true
        sessionError = nil
        let attempt = AuthenticationAttempt()
        authenticationAttempt = attempt
        do {
            try await attempt.run(timeout: authenticationTimeout) { [self] in
                defer { isAuthenticating = false; authenticationAttempt = nil; authenticationStatus = nil }
                try await performLogin(server: server, username: username, password: password, allowLocalHTTP: effectiveAllowLocalHTTP)
            }
        } catch {
            sessionError = error is CancellationError ? "已取消登录。" : error.localizedDescription
            throw error
        }
    }

    private func performLogin(server: URL, username: String, password: String, allowLocalHTTP: Bool) async throws {
        let previous = softLogoutCredentials
        let storeID = previous?.storeID ?? UUID()
        var candidate: Client?
        var remoteSession: Session?
        do {
            try Task.checkCancellation()
            // A soft logout is the one intentional exception: re-authenticate
            // against the same origin/device while retaining its encrypted
            // store.  A normal login never overwrites another account.
            // A pending cleanup may have already removed part of the retained
            // store. Do not write its old Keychain record back over that state.
            if cleanupRequired {
                guard previous == nil else { throw MatrixServiceError.storageUnavailable }
                try cleanStoredSession()
            }
            if let previous {
                guard try sessionStore.load() == previous,
                      server.absoluteString == previous.homeserverURL else {
                    throw MatrixServiceError.storageUnavailable
                }
            } else {
                guard try sessionStore.load() == nil else { throw MatrixServiceError.storageUnavailable }
            }
            let capabilities = try await retryRateLimits { try await self.discovery.discover(at: server) }
            guard capabilities.supportsPassword else { throw AuthenticationError.unsupportedLoginMethod }
            let key: Data
            if let previous { key = previous.storeKey }
            else { key = try storeKeyFactory() }
            let path = try directory().create(for: storeID)
            let newClient = try await retryRateLimits { try await self.clientFactory(server.absoluteString, path, key) }
            candidate = newClient
            try Task.checkCancellation()
            let session = try await retryRateLimits {
                try await self.passwordAuthentication.login(at: server, username: username, password: password, deviceID: previous?.deviceID)
            }
            remoteSession = session
            if let previous, (session.userId != previous.userID || session.deviceId != previous.deviceID) {
                throw MatrixServiceError.storageUnavailable
            }
            try Task.checkCancellation()
            try await retryRateLimits { try await newClient.restoreSession(session: session) }
            try Task.checkCancellation()
            let stored = SessionCredentials(session: session, storeID: storeID, storeKey: key, allowLocalHTTP: allowLocalHTTP)
            // A new encrypted store must have its recovery record before it is
            // made active. Soft logout deliberately keeps the old record until
            // its replacement has activated successfully.
            if previous == nil { try sessionStore.save(stored) }
            credentials = stored
            try await activate(newClient, stored: stored)
            // Preserve the old Keychain record until the replacement session
            // has been restored and activated.  A failed soft re-login can
            // therefore still be retried with its original local store.
            if previous != nil {
                try sessionStore.save(stored)
            }
            softLogoutCredentials = nil
            requiresPasswordReauthentication = false
            reauthenticationHomeserver = nil
            reauthenticationUserID = nil
            canClearLocalSession = false
            canRetryRestore = false
        } catch {
            await candidate?.enableAllSendQueues(enable: false)
            candidate = nil
            if let previous {
                // A bad password, cancellation, or failed replacement token
                // must not destroy a valid soft-logout store.
                // The replacement is saved only after activation. SessionStore
                // save is atomic, so a failed save leaves the previous record.
                credentials = previous
                softLogoutCredentials = previous
                requiresPasswordReauthentication = true
                canClearLocalSession = true
                if client != nil { await detachClient() }
            } else if credentials?.storeID == storeID { await clearSession() }
            else {
                do { try directory().remove(for: storeID) }
                catch { cleanupRequired = true; canClearLocalSession = true }
            }
            // A separate bounded request can revoke credentials even when the
            // authentication task was cancelled. It never logs response bodies.
            if previous == nil, let remoteSession {
                let transport = passwordAuthentication
                await Task { await transport.discard(remoteSession) }.value
            }
            if error is CancellationError { throw error }
            if error is SessionStoreError || error is CocoaError {
                canClearLocalSession = true
                throw MatrixServiceError.storageUnavailable
            }
            if let error = error as? MatrixServiceError {
                if case .storageUnavailable = error { canClearLocalSession = true }
                throw error
            }
            throw PasswordLoginResultClassifier.error(for: error)
        }
    }

    public func restoreSession() async {
        // A soft logout must stay on the password screen. In particular, an
        // app foreground event must not briefly restore the expired session.
        guard softLogoutCredentials == nil, !requiresPasswordReauthentication,
              !didAttemptRestore, !isAuthenticating, !isRestoring, !isLoggedIn, !isLoggingOut else { return }
        didAttemptRestore = true
        isRestoring = true
        sessionError = nil
        canRetryRestore = false
        let attempt = AuthenticationAttempt()
        authenticationAttempt = attempt
        do {
            try await attempt.run(timeout: authenticationTimeout) { [self] in
                defer { isRestoring = false; authenticationAttempt = nil; authenticationStatus = nil }
                try await performRestore()
            }
        } catch {
            if let securityError = error as? SessionRestoreSecurityError {
                await clearSession()
                canRetryRestore = false
                canClearLocalSession = cleanupRequired
                sessionError = cleanupRequired ? MatrixServiceError.storageUnavailable.localizedDescription : securityError.localizedDescription
            } else if isSoftLogoutError(error) {
                await enterSoftLogout()
                canRetryRestore = false
            } else if PasswordLoginResultClassifier.isExpiredSession(error) {
                await invalidateSession()
                canRetryRestore = false
            } else {
                didAttemptRestore = false
                canRetryRestore = true
                canClearLocalSession = true
                if error is CancellationError { sessionError = "已取消恢复会话，本地会话仍保留。" }
                else if error is SessionStoreError || error is CocoaError { sessionError = MatrixServiceError.storageUnavailable.localizedDescription }
                else if let error = error as? AuthenticationError { sessionError = error.localizedDescription }
                else { sessionError = "会话恢复暂时失败，请检查网络后重试。" }
            }
        }
    }

    private func performRestore() async throws {
        var candidate: Client?
        do {
            try Task.checkCancellation()
            if cleanupRequired { try cleanStoredSession() }
            guard let stored = try sessionStore.load() else { return }
            credentials = stored
            // `allowLocalHTTP` is an old, persisted development choice.  A
            // Release binary must not inherit it from a Debug install.
            _ = try RestoredHomeserverPolicy(storedAllowLocalHTTP: stored.allowLocalHTTP).validatedURL(stored.homeserverURL)
            let path = try directory().create(for: stored.storeID)
            let restored = try await retryRateLimits { try await self.clientFactory(stored.homeserverURL, path, stored.storeKey) }
            candidate = restored
            try Task.checkCancellation()
            try await retryRateLimits { try await restored.restoreSession(session: stored.sdkSession) }
            try Task.checkCancellation()
            try await activate(restored, stored: stored)
            softLogoutCredentials = nil
            requiresPasswordReauthentication = false
            reauthenticationHomeserver = nil
            reauthenticationUserID = nil
            canClearLocalSession = false
        } catch {
            await candidate?.enableAllSendQueues(enable: false)
            candidate = nil
            // Keep the original credentials and encrypted store for offline retry.
            if client != nil { await detachClient() }
            throw error
        }
    }

    private func retryRateLimits<T>(_ operation: () async throws -> T) async throws -> T {
        while true {
            try Task.checkCancellation()
            do {
                let result = try await operation()
                authenticationStatus = nil
                return result
            } catch {
                guard case .rateLimited(let delay) = PasswordLoginResultClassifier.error(for: error) else { throw error }
                authenticationStatus = AuthenticationError.rateLimited(retryAfter: delay).localizedDescription
                // Unknown Retry-After uses a conservative pause; the overall
                // deadline still bounds all attempts and this sleep is cancellable.
                try await Task.sleep(for: .seconds(min(31_536_000, max(1, delay ?? 5))))
            }
        }
    }

    public func cancelAuthentication() {
        authenticationAttempt?.cancel()
    }

    /// Invoked only after the UI's destructive confirmation. No successful
    /// load() is required, so malformed Keychain items can be deleted too.
    public func clearLocalSession() async throws {
        guard !isAuthenticating, !isRestoring, !isLoggingOut else { throw MatrixServiceError.busy }
        isLoggingOut = true
        defer { isLoggingOut = false }
        await detachClient()
        cleanupRequired = true
        var failed = false
        do { try sessionStore.delete() } catch { failed = true }
        do { try directory().removeAll() } catch { failed = true }
        guard !failed else {
            canClearLocalSession = true
            sessionError = MatrixServiceError.storageUnavailable.localizedDescription
            throw MatrixServiceError.storageUnavailable
        }
        credentials = nil
        softLogoutCredentials = nil
        requiresPasswordReauthentication = false
        reauthenticationHomeserver = nil
        reauthenticationUserID = nil
        cleanupRequired = false
        didAttemptRestore = false
        canRetryRestore = false
        canClearLocalSession = false
        sessionError = nil
    }

    private func activate(_ client: Client, stored: SessionCredentials) async throws {
        try Task.checkCancellation()
        self.client = client
        credentials = stored
        sessionGeneration = UUID()
        let generation = sessionGeneration
        let (stream, continuation) = AsyncStream<Bool>.makeStream()
        let delegate = AuthErrorBridge(continuation)
        authDelegate = delegate
        authHandle = try client.setDelegate(delegate: delegate)
        authTask = Task { [weak self] in
            for await isSoftLogout in stream {
                guard let self, generation == self.sessionGeneration, !self.isLoggingOut else { return }
                if isSoftLogout { await self.enterSoftLogout() }
                else { await self.invalidateSession() }
                return
            }
        }
        // `restoreSession` only restores local SDK state. Make the active
        // session visible before reading that local room store so an offline
        // restore can render rooms without waiting for its first `/sync`.
        userID = stored.userID
        isLoggedIn = true
        await refreshRooms(from: client, session: generation)
        try Task.checkCancellation()
        guard generation == sessionGeneration else { throw CancellationError() }
        await client.enableAllSendQueues(enable: true)
        try Task.checkCancellation()
        guard generation == sessionGeneration else { throw CancellationError() }
        sessionError = nil
        try await sync()
        try Task.checkCancellation()
    }

    public func applicationDidBecomeActive() async {
        if isLoggedIn { try? await sync() }
        else if !requiresPasswordReauthentication { await restoreSession() }
    }

    func networkChanged(available: Bool) async {
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
                    // SDK sendingFailed means retries are exhausted. Keep its
                    // disabled room queue paused even if /sync recovers; only
                    // an explicit user retry may resume it in this session.
                    initial = false
                    backoff.reset()
                    try await Task.sleep(for: .milliseconds(100))
                } catch {
                    guard !Task.isCancelled, let self, self.syncGeneration == generation else { return }
                    if isSoftLogoutError(error) {
                        self.syncTask = nil
                        await self.enterSoftLogout()
                        return
                    }
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
        guard let timeline else { throw MatrixServiceError.sendNoLongerFailed }
        guard !messageActions.contains(messageID) else { throw MatrixServiceError.busy }
        if timelineState.pausedSendTransactionID(for: messageID) != nil {
            // 26.10.27 disables the room after recoverable retries exhaust.
            // retrySend only unwedges unrecoverable items; resume this room's
            // existing transactions instead. Never abort a recoverable item:
            // the SDK may turn an in-flight abort into a server redaction.
            guard retry, let roomID = activeRoomID, let client,
                  let room = try client.getRoom(roomId: roomID) else {
                throw MatrixServiceError.sendNoLongerFailed
            }
            room.enableSendQueue(enable: true)
            return
        }
        guard let transactionID = timelineState.failedSendTransactionID(for: messageID) else { throw MatrixServiceError.sendNoLongerFailed }
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
        if isSoftLogoutError(error) {
            if session == sessionGeneration { await enterSoftLogout() }
            return MatrixServiceError.notLoggedIn
        }
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

    private func enterSoftLogout() async {
        guard !isLoggingOut, let saved = credentials else { return }
        isLoggingOut = true
        await detachClient()
        credentials = saved
        softLogoutCredentials = saved
        requiresPasswordReauthentication = true
        reauthenticationHomeserver = saved.homeserverURL
        reauthenticationUserID = saved.userID
        canClearLocalSession = true
        canRetryRestore = false
        didAttemptRestore = true
        sessionError = "会话需要重新输入密码；将使用当前设备 ID 重新登录，已加密的本地数据会保留。"
        isLoggingOut = false
    }

    private func clearSession() async {
        await detachClient()
        cleanupRequired = true
        do { try cleanStoredSession() }
        catch { canClearLocalSession = true; sessionError = MatrixServiceError.storageUnavailable.localizedDescription }
    }

    private func detachClient() async {
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
    }

    private func cleanStoredSession() throws {
        if let stored = try credentials ?? sessionStore.load() {
            try directory().remove(for: stored.storeID)
        }
        try sessionStore.delete()
        credentials = nil
        softLogoutCredentials = nil
        requiresPasswordReauthentication = false
        reauthenticationHomeserver = nil
        reauthenticationUserID = nil
        cleanupRequired = false
    }
}

private func isSoftLogoutError(_ error: Error) -> Bool {
    guard case let ClientError.MatrixApi(kind, code, _, _) = error,
          code == "M_UNKNOWN_TOKEN",
          case .unknownToken(softLogout: true) = kind else { return false }
    return true
}

private final class AuthErrorBridge: ClientDelegate, Sendable {
    let continuation: AsyncStream<Bool>.Continuation
    init(_ continuation: AsyncStream<Bool>.Continuation) { self.continuation = continuation }
    func didReceiveAuthError(isSoftLogout: Bool) { continuation.yield(isSoftLogout) }
    func onBackgroundTaskErrorReport(taskName: String, error: BackgroundTaskFailureReason) {}
}
