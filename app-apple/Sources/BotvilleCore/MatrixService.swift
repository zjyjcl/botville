// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Combine
import Foundation
import MatrixRustSDK
import Network
import OSLog
#if os(iOS)
import UIKit
import UserNotifications
#endif

/// Identifies one visible owner of a room timeline. It is process-local and
/// deliberately carries no Matrix event or credential data.
public struct RoomTimelineLease: Equatable, Sendable {
    fileprivate let id: UUID
}

/// Serializes the UI ownership of the single SDK timeline. A room ID alone is
/// insufficient: an old SwiftUI view can disappear after another instance has
/// reopened the same room.
struct TimelineLeaseOwnership {
    private(set) var activeRoomID: String?
    private(set) var activeLease: RoomTimelineLease?

    mutating func open(roomID: String) -> RoomTimelineLease {
        let lease = RoomTimelineLease(id: UUID())
        activeRoomID = roomID
        activeLease = lease
        return lease
    }

    mutating func close(roomID: String, lease: RoomTimelineLease) -> Bool {
        guard activeRoomID == roomID, activeLease == lease else { return false }
        activeRoomID = nil
        activeLease = nil
        return true
    }

    func isCurrent(roomID: String, lease: RoomTimelineLease) -> Bool {
        activeRoomID == roomID && activeLease == lease
    }

    mutating func invalidate() {
        activeRoomID = nil
        activeLease = nil
    }
}

/// SDK ownership and all UI-facing state live on the main actor. Async SDK
/// operations run in Rust; the same client retains the /sync since token.
@MainActor
public final class MatrixService: ObservableObject {
    @Published public private(set) var isLoggedIn = false
    @Published public private(set) var isSyncing = false
    @Published public private(set) var rooms: [MatrixRoom] = []
    /// Ephemeral local-sync events, never populated from APNs/NSE payloads or logged.
    public let foregroundBannerCandidates = PassthroughSubject<ForegroundBannerCandidate, Never>()
    private var bannerResumeState = ForegroundBannerResumeState()
    private var bannerResumeTask: Task<Void, Never>?
    private var deliveredBackgroundEvents = [PushEventReference]()
    private var bannerNotificationListener: ForegroundSyncNotificationBridge?
    private var bannerNotificationTask: Task<Void, Never>?
    var deliveredBannerEvents: @Sendable (Date) async -> [PushEventReference] = { activeEdge in
#if os(iOS)
        return await UNUserNotificationCenter.current().deliveredNotifications()
            .filter { $0.date <= activeEdge }
            .compactMap { PushEventReference.parse($0.request.content.userInfo) }
#else
        return []
#endif
    }
    @Published public private(set) var messages: [MatrixMessage] = []
    @Published public private(set) var activeRoomID: String?
    @Published public private(set) var userID: String?
    @Published public private(set) var syncError: String?
    @Published public private(set) var canLoadOlderMessages = false
    @Published public private(set) var cryptoSafetyWarnings: [String] = []
    public let aiParticipantPolicy: AIParticipantPolicy

    @Published public private(set) var isRestoring = false
    @Published public private(set) var sessionError: String?
    @Published public private(set) var connectionStatus: ConnectionStatus = .idle
    /// The Base64 device token is shared by pusher requests and diagnostics.
    /// It stays in process memory, never in the session store or UserDefaults.
    @Published public private(set) var pushToken: String?
    @Published public private(set) var pushRegistrationStatus = "尚未注册推送。"
    @Published public private(set) var pushRegistrationError: String?
    @Published public private(set) var pushVerificationStatus: String?

    private let sessionStore: any SessionStore
    private let sharedPushCredentialsStore: any SharedPushCredentialsStore
    private let sharedPushCredentialsCleanupStateStore: any SharedPushCredentialsCleanupStateStore
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
    private var securityActions = Set<String>()
    private var isCreatingRoom = false
    private var observedEncryptedRooms = Set<String>()
    private var roomRefreshGeneration = UUID()
    private var securityReadGenerations: [String: UUID] = [:]
    // A room-list refresh may suspend on SDK reads while a security action
    // publishes newer state. Preserve that publication when its older snapshot
    // eventually returns.
    private var roomSecurityRevisions: [String: UUID] = [:]

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
    @Published public private(set) var hasReceivedTimelineSnapshot = false
    private var timelineLeaseOwnership = TimelineLeaseOwnership()
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
    private let pairingAuthentication: any PairingCredentialAuthenticating
    private let authenticationTimeout: TimeInterval
    private let pusherTransport: any MatrixPusherTransport
    private var pushConfiguration: (url: URL, deviceDisplayName: String)?
    /// This is intentionally memory-only. After an app relaunch APNs normally
    /// returns the current token again; a replaced token from a prior process
    /// cannot be deleted without persisting the sensitive value.
    private var lastRegisteredPushKey: String?
    private var knownPushKeys = Set<String>()
    private var pushOperation: Task<Void, Never>?
    private var authenticationAttempt: AuthenticationAttempt?
    var storeKeyFactory: () throws -> Data = SessionCredentials.makeStoreKey
    private var isLoggingOut = false
    private var softLogoutCredentials: SessionCredentials?
    private var paginationGeneration: UUID?
    private static let sharedPushCredentialsLogger = Logger(subsystem: "com.zjyjcl.botville", category: "PushCredentials")

    public init(sessionStore: any SessionStore = KeychainSessionStore(),
                sharedPushCredentialsStore: any SharedPushCredentialsStore = KeychainSharedPushCredentialsStore(),
                sharedPushCredentialsCleanupStateStore: any SharedPushCredentialsCleanupStateStore = UserDefaultsSharedPushCredentialsCleanupStateStore(),
                storeDirectory: SessionStoreDirectory? = nil,
                discovery: any LoginCapabilityDiscovering = MatrixLoginCapabilityDiscovery(),
                monitorNetwork: Bool = true,
                passwordAuthentication: any PasswordAuthenticating = MatrixPasswordAuthentication(),
                pairingAuthentication: any PairingCredentialAuthenticating = MatrixPairingCredentialAuthentication(),
                authenticationTimeout: TimeInterval = 60,
                pusherTransport: any MatrixPusherTransport = URLSessionMatrixPusherTransport(),
                aiParticipantPolicy: AIParticipantPolicy = .configured()) {
        self.sessionStore = sessionStore
        self.sharedPushCredentialsStore = sharedPushCredentialsStore
        self.sharedPushCredentialsCleanupStateStore = sharedPushCredentialsCleanupStateStore
        self.storeDirectory = storeDirectory
        self.discovery = discovery
        self.passwordAuthentication = passwordAuthentication
        self.pairingAuthentication = pairingAuthentication
        self.aiParticipantPolicy = aiParticipantPolicy
        self.authenticationTimeout = authenticationTimeout.isFinite && authenticationTimeout > 0 ? authenticationTimeout : 60
        self.pusherTransport = pusherTransport
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
        pushOperation?.cancel()
        authDelegate?.continuation.finish()
        syncTask?.cancel()
        bannerResumeTask?.cancel()
        bannerNotificationListener?.continuation.finish()
        bannerNotificationTask?.cancel()
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

    /// Finishes a pairing only after the confirmation view has redeemed its QR
    /// code. The QR is retained in memory solely to defend against a response
    /// that tries to move the Matrix credential exchange to another server.
    /// Pairing credentials are never written to the SessionStore.
    public func completePairing(response: PairingRedeemResponse, qrCode: PairingQRCode,
                                username: String? = nil, password: String? = nil) async throws {
        guard !isAuthenticating, !isRestoring, !isLoggedIn, !isLoggingOut else { throw MatrixServiceError.busy }
        guard PairingURLPolicy.sameEndpoint(response.homeserver, qrCode.homeserver) else {
            throw PairingError.homeserverMismatch
        }
        guard !response.isAuthExpired() else { throw PairingError.expired }
        let server = try HomeserverPolicy().validatedURL(response.homeserver.absoluteString)
        if response.auth.type == .registrationToken {
            guard let username = username?.trimmingCharacters(in: .whitespacesAndNewlines), !username.isEmpty,
                  let password, !password.isEmpty else { throw MatrixServiceError.missingCredentials }
        }
        isAuthenticating = true
        sessionError = nil
        let pairingTransport = pairingAuthentication.configuredForPairing(fingerprint: qrCode.fingerprint, host: qrCode.homeserver.host, port: qrCode.homeserver.port)
        let attempt = AuthenticationAttempt()
        authenticationAttempt = attempt
        do {
            try await attempt.run(timeout: authenticationTimeout) { [self] in
                defer { isAuthenticating = false; authenticationAttempt = nil; authenticationStatus = nil }
                try await performPairingLogin(server: server, response: response, qrCode: qrCode, username: username, password: password,
                                              authentication: pairingTransport)
            }
        } catch {
            sessionError = error is CancellationError ? "已取消配对。" : error.localizedDescription
            throw error
        }
    }

    private func performPairingLogin(server: URL, response: PairingRedeemResponse, qrCode: PairingQRCode,
                                     username: String?, password: String?,
                                     authentication: any PairingCredentialAuthenticating) async throws {
        let storeID = UUID()
        var candidate: Client?
        var remoteSession: Session?
        do {
            try Task.checkCancellation()
            if cleanupRequired { try cleanStoredSession() }
            guard try sessionStore.load() == nil else { throw MatrixServiceError.storageUnavailable }
            let key = try storeKeyFactory()
            let path = try directory().create(for: storeID)
            // Build the SDK client before consuming the one-time registration
            // token. If registration succeeds but restore later fails, this
            // method deliberately does not repeat registration.
            let newClient = try await retryRateLimits { try await self.clientFactory(server.absoluteString, path, key) }
            candidate = newClient
            try Task.checkCancellation()
            guard !response.isAuthExpired() else { throw PairingError.expired }
            let session: Session
            switch response.auth.type {
            case .registrationToken:
                guard let username = username?.trimmingCharacters(in: .whitespacesAndNewlines), !username.isEmpty,
                      let password, !password.isEmpty else { throw MatrixServiceError.missingCredentials }
                // Do not wrap a UIA registration in generic retry logic: a
                // 401 continuation may already have consumed the one-time
                // token. The UI receives a safe 429 error and can wait for a
                // server-directed next step without replaying the exchange.
                session = try await authentication.register(
                        at: server, registrationToken: response.auth.token, username: username, password: password,
                        deviceDisplayName: "botville C2.1", deadline: response.authExpiresAt
                    )
            case .loginToken:
                session = try await authentication.login(at: server, loginToken: response.auth.token,
                                                         deviceDisplayName: "botville C2.1", deadline: response.authExpiresAt)
            }
            remoteSession = session
            try Task.checkCancellation()
            try await retryRateLimits { try await newClient.restoreSession(session: session) }
            try Task.checkCancellation()
            // Option A finalized (2026-11-06, #128/#132): a QR without gw is chat-only.
            // Gateway features must stay disabled for this pairing's lifetime.
            let gateway = qrCode.gateway == nil ? nil : response.gateway
            let gatewayPin = gateway.flatMap {
                PairingURLSession.fingerprint(qrCode.fingerprint, homeserver: qrCode.homeserver, requestURL: $0)
            }
            let stored = SessionCredentials(session: session, storeID: storeID, storeKey: key,
                                            gatewayURL: gateway?.absoluteString,
                                            gatewayCertificateFingerprint: gatewayPin)
            try sessionStore.save(stored)
            credentials = stored
            try await activate(newClient, stored: stored)
            publishSharedPushCredentials(stored)
            canClearLocalSession = false
            canRetryRestore = false
        } catch {
            await candidate?.enableAllSendQueues(enable: false)
            if credentials?.storeID == storeID { await clearSession() }
            else {
                do { try directory().remove(for: storeID) }
                catch { cleanupRequired = true; canClearLocalSession = true }
            }
            // The registration or login request is never replayed after it
            // returned a session. Best-effort logout prevents an orphaned
            // remote session if local SDK restoration then fails.
            if let remoteSession {
                await MatrixPasswordAuthentication().discard(remoteSession)
                // A logout request is best-effort. Do not make a successful
                // one-time operation look retry-safe merely because its local
                // activation failed.
                throw PairingAuthenticationError.remoteStateUnknown
            }
            if error is CancellationError { throw error }
            if error is SessionStoreError || error is CocoaError {
                canClearLocalSession = true
                throw MatrixServiceError.storageUnavailable
            }
            if error is MatrixServiceError || error is PairingError { throw error }
            throw PasswordLoginResultClassifier.error(for: error)
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
            let stored = SessionCredentials(session: session, storeID: storeID, storeKey: key, allowLocalHTTP: allowLocalHTTP,
                                            gatewayURL: previous?.gatewayURL,
                                            gatewayCertificateFingerprint: previous?.gatewayCertificateFingerprint)
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
            publishSharedPushCredentials(stored)
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
            retrySharedPushCredentialsCleanupIfNeeded()
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
            publishSharedPushCredentials(stored)
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

    /// Without a confirmed gateway, features are absent and only chat is shown.
    public var hasConfirmedGateway: Bool { credentials?.gatewayURL != nil }

    /// Recreates the confirmed gateway transport with its non-secret QR pin.
    /// Pairing does not automatically activate gateway features; callers opt
    /// into this client only after a logged-in session exists.
    public func confirmedGatewayClient() throws -> HTTPGatewayClient? {
        guard let stored = credentials, let source = stored.gatewayURL, let url = URL(string: source) else { return nil }
        return try HTTPGatewayClient(baseURL: url, certificateFingerprint: stored.effectiveGatewayCertificateFingerprint)
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
        removeSharedPushCredentials()
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
        let (notifications, notificationContinuation) = AsyncStream<ForegroundSyncEvent>.makeStream()
        let notificationListener = ForegroundSyncNotificationBridge(userID: stored.userID,
                                                                    continuation: notificationContinuation)
        notificationListener.setActive(bannerResumeState.isForeground)
        bannerNotificationListener = notificationListener
        bannerNotificationTask = Task { [weak self] in
            for await event in notifications {
                guard !Task.isCancelled, let self, generation == self.sessionGeneration else { return }
                // Resume reconciliation can overlap an old long poll/drain. Keep
                // each live event queued until exact delivered IDs are known.
                await self.bannerResumeTask?.value
                guard !Task.isCancelled, generation == self.sessionGeneration,
                      !self.isLoggingOut, notificationListener.isCurrent(event),
                      self.bannerResumeState.shouldPresent(event.candidate, suppressedEvents: self.deliveredBackgroundEvents),
                      self.rooms.contains(where: { $0.id == event.candidate.roomID && $0.membership == .joined }) else { continue }
                self.foregroundBannerCandidates.send(event.candidate)
            }
        }
        await client.registerNotificationHandler(listener: notificationListener)
        try Task.checkCancellation()
        guard generation == sessionGeneration else { throw CancellationError() }
        await refreshRooms(from: client, session: generation)
        try Task.checkCancellation()
        guard generation == sessionGeneration else { throw CancellationError() }
        await client.enableAllSendQueues(enable: true)
        try Task.checkCancellation()
        guard generation == sessionGeneration else { throw CancellationError() }
        sessionError = nil
        try await sync()
        try Task.checkCancellation()
        enqueuePushRegistration()
    }

    public func applicationDidEnterBackground() {
        bannerResumeState.enteredBackground()
        bannerNotificationListener?.setActive(false)
    }

    /// Called synchronously at the scene edge, before scheduling async recovery.
    public func prepareForForeground() {
        let activeEdge = Date()
        bannerNotificationListener?.setActive(true)
        let resumedFromBackground = bannerResumeState.becameActive()
        if resumedFromBackground {
            let session = sessionGeneration
            bannerResumeTask?.cancel()
            // Capture the edge before any suspension. Never rebaseline the first
            // sync after this point: it may contain messages received in foreground.
            bannerResumeTask = Task { [weak self] in
                guard let self else { return }
                let events = await self.deliveredBannerEvents(activeEdge)
                guard session == self.sessionGeneration, !Task.isCancelled else { return }
                for event in events where !self.deliveredBackgroundEvents.contains(event) {
                    self.deliveredBackgroundEvents.append(event)
                }
                self.deliveredBackgroundEvents = Array(self.deliveredBackgroundEvents.suffix(2048))
            }
        }
    }

    public func applicationDidBecomeActive() async {
        prepareForForeground()
        updateApplicationBadge()
        if isLoggedIn {
            // A suspended long poll is still the owner of its response. Replacing
            // it here discards updates during FFI cancellation/drain.
            if syncTask == nil { try? await sync() }
        }
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
                    await self.refreshRooms(from: client, session: session, resolveUnknownEncryption: true, onlineSyncGeneration: generation)
                    guard !Task.isCancelled, self.syncGeneration == generation else { return }
                    // SDK awaits all notification callbacks before sync returns.
                    // Their eligibility was captured synchronously by the bridge,
                    // so queued cold-start events remain consumed as history.
                    self.bannerNotificationListener?.finishInitialSync()
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

    private func refreshRooms(from client: Client, session: UUID, resolveUnknownEncryption: Bool = false,
                              onlineSyncGeneration: UUID? = nil) async {
        if onlineSyncGeneration != nil { await bannerResumeTask?.value }
        let refresh = UUID()
        roomRefreshGeneration = refresh
        let securityRevisionsAtStart = roomSecurityRevisions
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
            guard session == sessionGeneration, refresh == roomRefreshGeneration else { return }
            var encryption = SDKEncryptionState.map(info?.encryptionState ?? .unknown)
            // A standard /sync may omit a nonexistent m.room.encryption event.
            // Resolve that ambiguity only online, preserving offline restore.
            if resolveUnknownEncryption, membership == .joined, encryption == .unknown,
               let latestState = try? await room.latestEncryptionState() {
                encryption = SDKEncryptionState.map(latestState)
            }
            guard session == sessionGeneration, refresh == roomRefreshGeneration else { return }
            if encryption == .encrypted { observedEncryptedRooms.insert(room.id()) }
            var participants: AIParticipantStatus = .unknown
            if membership == .joined, aiParticipantPolicy.hasConfiguredRules {
                // Listing rooms only uses the cache. Opening security details
                // and security actions fetch complete members on demand.
                let members = info == nil ? nil : try? await room.membersNoSync()
                if let members {
                    participants = SDKEncryptionState.participants(members, expectedActiveCount: info?.activeMembersCount,
                                                                   policy: aiParticipantPolicy)
                }
            }
            updated.append(MatrixRoom(id: room.id(), name: room.displayName() ?? room.id(),
                                      membership: membership, isDirect: direct,
                                      unreadCount: info?.numUnreadMessages ?? 0,
                                      latestActivity: timestamp.map { Date(timeIntervalSince1970: Double($0) / 1000) },
                                      encryption: observedEncryptedRooms.contains(room.id()) ? .encrypted : encryption,
                                      participants: participants))
        }
        guard session == sessionGeneration, refresh == roomRefreshGeneration, isLoggedIn, !isLoggingOut else { return }
        // A cancelled/replaced sync must not commit or arm the banner baseline.
        let committed = RoomRefreshCommitGate.commit(onlineGeneration: onlineSyncGeneration,
                                     currentGeneration: syncGeneration,
                                     isCancelled: Task.isCancelled) {
            rooms = MatrixRoom.sortedByActivity(Self.mergingSecurityUpdates(
                refreshed: updated, current: rooms,
                revisionsAtStart: securityRevisionsAtStart,
                currentRevisions: roomSecurityRevisions,
                observedEncryptedRooms: observedEncryptedRooms
            ))
            // Banner events come only from SyncNotificationListener. A derived
            // latestEvent readback here can lag this sync and must never replay it.
        }
        guard committed else { return }
        updateApplicationBadge()
        if rooms.contains(where: { $0.encryption == .encrypted }) { refreshCryptoSafety(from: client) }
        if let activeRoomID, !rooms.contains(where: { $0.id == activeRoomID && $0.membership == .joined }) {
            closeRoom(activeRoomID)
        }
    }

    /// `latestEvent()` reads the SDK's locally synced room state. Its current
    /// API has no event ID, so candidate identity falls back to room + timestamp.
    /// No notification payload parsing, profile fetch, or error-body logging.
    static func foregroundBannerCandidate(for room: MatrixRoom, latest: LatestEventValue,
                                          currentUserID: String?) -> ForegroundBannerCandidate? {
        guard room.membership == .joined,
              case let .remote(timestamp, sender, isOwn, profile, content) = latest else { return nil }
        let preview: String
        switch content {
        case .msgLike(let message):
            if case .redacted = message.kind { return nil }
            if room.encryption == .encrypted {
                // Match NSE privacy semantics even if this device has decrypted it.
                preview = "加密消息"
            } else {
                switch message.kind {
                case .unableToDecrypt: preview = "加密消息"
                case .message(let message):
                    preview = nonemptyBannerText(message.body) ?? PushNotificationText.placeholder.body
                case .sticker(let body, _, _):
                    preview = nonemptyBannerText(body) ?? PushNotificationText.placeholder.body
                default: preview = PushNotificationText.placeholder.body
                }
            }
        case .failedToParseMessageLike:
            preview = room.encryption == .encrypted ? "加密消息" : PushNotificationText.unavailable.body
        default:
            // Membership, profile and other state changes are not new messages.
            return nil
        }
        let displayName: String?
        if case .ready(let name, _, _, _, _) = profile { displayName = name.flatMap(nonemptyBannerText) }
        else { displayName = nil }
        let roomName = nonemptyBannerText(room.name) ?? room.id
        let title = room.isDirect ? (displayName ?? roomName) : roomName
        return ForegroundBannerCandidate(
            roomID: room.id, latestActivity: Date(timeIntervalSince1970: Double(timestamp) / 1_000),
            title: truncatedBannerText(title, limit: 120), preview: truncatedBannerText(preview, limit: 240),
            isOwn: isOwn || sender == currentUserID)
    }

    private static func nonemptyBannerText(_ text: String) -> String? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    private static func truncatedBannerText(_ text: String, limit: Int) -> String {
        text.count > limit ? String(text.prefix(limit - 1)) + "…" : text
    }

    /// The consent is bound to the exact draft; editing recipients invalidates it.
    public func createRoom(draft: RoomCreationDraft, consent: EncryptionConsent) async throws -> String {
        guard let client, isLoggedIn, !isLoggingOut else { throw MatrixServiceError.notLoggedIn }
        guard !isCreatingRoom else { throw MatrixServiceError.busy }
        try draft.validate()
        let invitees = try draft.validatedInvitees()
        let participants = aiParticipantPolicy.classify(memberIDs: invitees + [userID].compactMap { $0 })
        let encrypted = try consent.validatedEncryption(for: .newRoom(draft), participants: participants,
                                                        currentEncryption: .unencrypted)
        let session = sessionGeneration
        isCreatingRoom = true
        defer { if session == sessionGeneration { isCreatingRoom = false } }
        do {
            let name = draft.name.trimmingCharacters(in: .whitespacesAndNewlines)
            let roomID = try await client.createRoom(request: CreateRoomParameters(
                name: name.isEmpty ? nil : name, isEncrypted: encrypted, isDirect: draft.isDirect,
                visibility: .private, preset: .privateChat, invite: invitees))
            guard session == sessionGeneration, !isLoggingOut else { throw CancellationError() }
            await refreshRooms(from: client, session: session, resolveUnknownEncryption: true)
            guard session == sessionGeneration, !isLoggingOut else { throw CancellationError() }
            return roomID
        } catch { throw await operationFailure(error, session: session) }
    }

    /// There intentionally is no disable API. Re-read SDK state and all active
    /// members after confirmation, before sending m.room.encryption via the SDK.
    public func enableEncryption(roomID: String, consent: EncryptionConsent) async throws {
        guard let client, isLoggedIn, !isLoggingOut else { throw MatrixServiceError.notLoggedIn }
        guard !securityActions.contains(roomID) else { throw MatrixServiceError.busy }
        guard let room = try client.getRoom(roomId: roomID), room.membership() == .joined else {
            throw MatrixServiceError.roomUnavailable
        }
        let session = sessionGeneration
        securityActions.insert(roomID)
        securityReadGenerations[roomID] = UUID()
        defer { if session == sessionGeneration { securityActions.remove(roomID) } }
        let snapshot: (RoomEncryptionState, AIParticipantStatus)
        do { snapshot = try await readRoomSecurity(room) }
        catch { throw await operationFailure(error, session: session) }
        guard session == sessionGeneration, !isLoggingOut else { throw CancellationError() }
        publishSecurity(roomID, encryption: snapshot.0, participants: snapshot.1)
        if snapshot.0 == .encrypted || observedEncryptedRooms.contains(roomID) { return }
        guard try consent.validatedEncryption(for: .existingRoom(roomID), participants: snapshot.1,
                                              currentEncryption: snapshot.0) else {
            throw MatrixServiceError.encryptionConfirmationRequired
        }
        do {
            try await room.enableEncryption()
            guard session == sessionGeneration, !isLoggingOut else { throw CancellationError() }
            // Success means the state event was accepted. SDK readback controls
            // the badge; a failed read never claims encryption was confirmed.
            let state = try? await room.latestEncryptionState()
            guard session == sessionGeneration, !isLoggingOut else { throw CancellationError() }
            publishSecurity(roomID, encryption: state.map(SDKEncryptionState.map) ?? .unknown,
                            participants: snapshot.1)
            refreshCryptoSafety(from: client)
        } catch { throw await operationFailure(error, session: session) }
    }

    public func inviteUser(_ userID: String, to roomID: String) async throws {
        guard let client, isLoggedIn, !isLoggingOut else { throw MatrixServiceError.notLoggedIn }
        let draft = RoomCreationDraft(isDirect: true, name: "", invitees: [userID])
        let invitee = try draft.validatedInvitees()[0]
        guard !securityActions.contains(roomID) else { throw MatrixServiceError.busy }
        guard let room = try client.getRoom(roomId: roomID), room.membership() == .joined else {
            throw MatrixServiceError.roomUnavailable
        }
        let session = sessionGeneration
        securityActions.insert(roomID)
        securityReadGenerations[roomID] = UUID()
        defer { if session == sessionGeneration { securityActions.remove(roomID) } }
        do {
            let snapshot = try await readRoomSecurity(room)
            guard session == sessionGeneration, !isLoggingOut else { throw CancellationError() }
            let isOrMayBeEncrypted = snapshot.0 != .unencrypted || observedEncryptedRooms.contains(roomID)
            if isOrMayBeEncrypted {
                let candidate = aiParticipantPolicy.classify(memberIDs: [invitee])
                guard snapshot.1 == .humanOnly, candidate == .humanOnly else {
                    throw MatrixServiceError.encryptedRoomParticipantNotAllowed
                }
            }
            try await room.inviteUserById(userId: invitee)
            guard session == sessionGeneration, !isLoggingOut else { throw CancellationError() }
            // Do not retain a previous human-only result while invitation sync
            // catches up. The next member snapshot must classify the invitee.
            publishSecurity(roomID, encryption: snapshot.0, participants: .unknown)
        } catch { throw await operationFailure(error, session: session) }
    }

    public func refreshRoomSecurity(_ roomID: String) async {
        guard let client, isLoggedIn, !isLoggingOut, !securityActions.contains(roomID),
              let room = try? client.getRoom(roomId: roomID), room.membership() == .joined else { return }
        let session = sessionGeneration
        let generation = UUID()
        securityReadGenerations[roomID] = generation
        do {
            let snapshot = try await readRoomSecurity(room)
            guard session == sessionGeneration, securityReadGenerations[roomID] == generation, !isLoggingOut else { return }
            publishSecurity(roomID, encryption: snapshot.0, participants: snapshot.1)
            refreshCryptoSafety(from: client)
        } catch {
            guard session == sessionGeneration, securityReadGenerations[roomID] == generation, !isLoggingOut else { return }
            publishSecurity(roomID, encryption: SDKEncryptionState.map(room.encryptionState()), participants: .unknown)
            _ = await operationFailure(error, session: session)
        }
    }

    private func readRoomSecurity(_ room: Room) async throws -> (RoomEncryptionState, AIParticipantStatus) {
        let session = sessionGeneration
        let encryption = SDKEncryptionState.map(try await room.latestEncryptionState())
        guard session == sessionGeneration, !isLoggingOut else { throw CancellationError() }
        // A successful encrypted state read is durable for this session even
        // when the subsequent member read fails. Do not later loosen an
        // invitation gate because a transient SDK read said "unencrypted".
        if encryption == .encrypted { observedEncryptedRooms.insert(room.id()) }
        let members = try await room.members()
        return (encryption, SDKEncryptionState.participants(members, expectedActiveCount: room.activeMembersCount(),
                                                           policy: aiParticipantPolicy, membersAreComplete: true))
    }

    private func publishSecurity(_ roomID: String, encryption: RoomEncryptionState, participants: AIParticipantStatus) {
        if encryption == .encrypted { observedEncryptedRooms.insert(roomID) }
        roomSecurityRevisions[roomID] = UUID()
        rooms = rooms.map {
            $0.id == roomID ? $0.withSecurity(encryption: observedEncryptedRooms.contains(roomID) ? .encrypted : encryption,
                                            participants: participants) : $0
        }
    }

    /// Preserves a security publication that landed while a list refresh was
    /// suspended. Other room metadata comes from the refreshed snapshot.
    static func mergingSecurityUpdates(
        refreshed: [MatrixRoom],
        current: [MatrixRoom],
        revisionsAtStart: [String: UUID],
        currentRevisions: [String: UUID],
        observedEncryptedRooms: Set<String>
    ) -> [MatrixRoom] {
        let currentByID = Dictionary(uniqueKeysWithValues: current.map { ($0.id, $0) })
        return refreshed.map { room in
            let security = currentRevisions[room.id] != revisionsAtStart[room.id]
                ? currentByID[room.id] ?? room
                : room
            return room.withSecurity(
                encryption: observedEncryptedRooms.contains(room.id) ? .encrypted : security.encryption,
                participants: security.participants
            )
        }
    }

    private func refreshCryptoSafety(from client: Client) {
        let encryption = client.encryption()
        cryptoSafetyWarnings = SDKEncryptionState.warnings(verification: encryption.verificationState(),
                                                           backup: encryption.backupState(), recovery: encryption.recoveryState())
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

    /// Claims timeline ownership before a SwiftUI appearance starts its async
    /// open. A disappearing older view can then close only its own lease.
    public func beginRoomOpen(_ roomID: String) -> RoomTimelineLease {
        let lease = timelineLeaseOwnership.open(roomID: roomID)
        clearTimeline()
        activeRoomID = roomID
        return lease
    }

    @discardableResult
    public func openRoom(_ roomID: String) async throws -> RoomTimelineLease {
        let lease = beginRoomOpen(roomID)
        return try await openRoom(roomID, lease: lease)
    }

    @discardableResult
    public func openRoom(_ roomID: String, lease: RoomTimelineLease) async throws -> RoomTimelineLease {
        guard let client, !isLoggingOut else { throw MatrixServiceError.notLoggedIn }
        guard let room = try client.getRoom(roomId: roomID), room.membership() == .joined else {
            throw MatrixServiceError.roomUnavailable
        }
        guard timelineLeaseOwnership.isCurrent(roomID: roomID, lease: lease), activeRoomID == roomID else {
            throw CancellationError()
        }
        updateApplicationBadge()
        let session = sessionGeneration
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
                    self.hasReceivedTimelineSnapshot = true
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
            Task { [weak self] in
                await self?.markOpenedRoomRead(roomID: roomID, generation: generation, session: session) {
                    try await newTimeline.markAsRead(receiptType: .read)
                }
            }
            return lease
        } catch {
            if generation == timelineGeneration,
               timelineLeaseOwnership.close(roomID: roomID, lease: lease) {
                clearTimeline()
            }
            throw await operationFailure(error, session: session)
        }
    }

    public func closeRoom(_ roomID: String, lease: RoomTimelineLease) {
        guard timelineLeaseOwnership.close(roomID: roomID, lease: lease), activeRoomID == roomID else { return }
        clearTimeline()
    }

    public func closeRoom(_ roomID: String) {
        guard activeRoomID == roomID else { return }
        timelineLeaseOwnership.invalidate()
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
        hasReceivedTimelineSnapshot = false
        canLoadOlderMessages = false
        paginationGeneration = nil
        sendingGeneration = nil
        messageActions.removeAll()
    }

    private func updateApplicationBadge() {
#if os(iOS)
        UIApplication.shared.applicationIconBadgeNumber = PushBadge.total(unreadCounts: rooms.map(\.unreadCount))
#endif
    }

    /// Read receipts are best-effort: an offline receipt failure must not
    /// prevent opening the room or falsely clear its list/badge state.
    private func markOpenedRoomRead(roomID: String, generation: UUID, session: UUID,
                                    operation: @escaping () async throws -> Void) async {
        guard generation == timelineGeneration, session == sessionGeneration, activeRoomID == roomID, !isLoggingOut else { return }
        let succeeded: Bool
        do {
            try await operation()
            succeeded = true
        } catch {
            succeeded = false
        }
        guard generation == timelineGeneration, session == sessionGeneration, activeRoomID == roomID, !isLoggingOut else { return }
        rooms = Self.roomsAfterReadReceipt(rooms, roomID: roomID, succeeded: succeeded)
        updateApplicationBadge()
    }

    static func roomsAfterReadReceipt(_ rooms: [MatrixRoom], roomID: String, succeeded: Bool) -> [MatrixRoom] {
        guard succeeded else { return rooms }
        return rooms.map { $0.id == roomID ? $0.withUnreadCount(0) : $0 }
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

    /// Configures the Sygnal endpoint for this running process. The app target
    /// owns persistence of the non-secret URL; the access token and APNs token
    /// never leave memory except in the authenticated Matrix request.
    public func configurePush(notifyURL: String, deviceDisplayName: String, allowHTTP: Bool = false) async throws {
        let permitsHTTP = PushHTTPPolicy.permitsHTTP(allowHTTP)
        let url = try PushNotifyURLPolicy.validatedURL(notifyURL, allowHTTP: permitsHTTP)
        pushConfiguration = (url, deviceDisplayName)
        pushRegistrationError = nil
        pushVerificationStatus = nil
        pushRegistrationStatus = pushToken == nil ? "服务地址已保存，等待设备令牌。" : "服务地址已保存，正在注册推送。"
        if pushToken != nil { enqueuePushRegistration() }
    }

    /// Called by the iOS application delegate after APNs succeeds. Conversion
    /// happens before publication so diagnostics display and copy the pushkey.
    public func receivePushDeviceToken(_ token: Data) {
        let value = DevicePushToken.base64(token)
        guard !value.isEmpty else { return }
        pushToken = value
        pushRegistrationStatus = "已取得设备令牌，等待注册。"
        pushRegistrationError = nil
        pushVerificationStatus = nil
        enqueuePushRegistration()
    }

    /// Reads the server pusher list and reports only whether the in-memory
    /// token has a matching app identifier; no pusher details are published.
    public func verifyPushRegistration() async {
        guard let credentials, let key = pushToken, isLoggedIn else {
            pushVerificationStatus = "尚无可核对的登录会话或设备令牌。"
            return
        }
        guard let homeserver = URL(string: credentials.homeserverURL) else {
            pushVerificationStatus = "无法核对推送注册。"
            return
        }
        let generation = sessionGeneration
        do {
            let response = try await pusherTransport.pushers(homeserver: homeserver, accessToken: credentials.accessToken)
            guard generation == sessionGeneration, !isLoggingOut, pushToken == key,
                  self.credentials?.userID == credentials.userID else { return }
            pushVerificationStatus = response.contains(appID: Self.pusherAppID, pushKey: key) ? "服务器中存在匹配的推送注册。" : "服务器中未找到匹配的推送注册。"
        } catch {
            guard generation == sessionGeneration, !isLoggingOut, pushToken == key,
                  self.credentials?.userID == credentials.userID else { return }
            pushVerificationStatus = "无法核对推送注册。"
        }
    }

    private static let pusherAppID = "com.zjyjcl.botville.apns"

    private func enqueuePushRegistration() {
        let preceding = pushOperation
        let generation = sessionGeneration
        let task = Task { @MainActor [weak self] in
            await preceding?.value
            guard let self, self.sessionGeneration == generation, !self.isLoggingOut else { return }
            await self.registerCurrentPush()
        }
        pushOperation = task
    }

    private func waitForPushOperations() async {
        let operation = pushOperation
        await operation?.value
        // Any operation queued while logout is in progress observes
        // `isLoggingOut` and returns without issuing a request.
        pushOperation = nil
    }

    private func registerCurrentPush() async {
        guard let credentials, let configuration = pushConfiguration, let key = pushToken, isLoggedIn,
              let homeserver = URL(string: credentials.homeserverURL) else { return }
        let generation = sessionGeneration
        do {
            for previous in knownPushKeys.subtracting([key]) {
                guard generation == sessionGeneration, !isLoggingOut else { return }
                let deletion = PushRegistration.deletion(appID: Self.pusherAppID, pushKey: previous, appDisplayName: "botville")
                try await pusherTransport.setPusher(homeserver: homeserver, accessToken: credentials.accessToken, registration: deletion)
                guard generation == sessionGeneration, !isLoggingOut else { return }
                knownPushKeys.remove(previous)
            }
            guard generation == sessionGeneration, !isLoggingOut else { return }
            // Record before sending because the server may accept a request
            // whose response is lost; logout must still delete it.
            knownPushKeys.insert(key)
            let data = try PushPusherData(url: configuration.url, allowHTTP: configuration.url.scheme?.lowercased() == "http")
            let registration = PushRegistration(appID: Self.pusherAppID, pushKey: key, appDisplayName: "botville", data: data,
                                                append: false, deviceDisplayName: configuration.deviceDisplayName, language: "zh-CN")
            try await pusherTransport.setPusher(homeserver: homeserver, accessToken: credentials.accessToken, registration: registration)
            guard generation == sessionGeneration, !isLoggingOut else { return }
            lastRegisteredPushKey = key
            pushRegistrationStatus = "已注册推送。"
            pushRegistrationError = nil
        } catch {
            guard generation == sessionGeneration, !isLoggingOut else { return }
            // Do not surface transport error strings: they can contain server
            // request context and must never include device credentials in UI.
            pushRegistrationStatus = "推送注册未完成。"
            pushRegistrationError = "推送注册失败，请检查服务地址和网络。"
        }
    }

    private func unregisterCurrentPush() async throws {
        guard let credentials,
              let homeserver = URL(string: credentials.homeserverURL) else { return }
        var keys = knownPushKeys
        if let lastRegisteredPushKey { keys.insert(lastRegisteredPushKey) }
        if let pushToken { keys.insert(pushToken) }
        for key in keys {
            let deletion = PushRegistration.deletion(appID: Self.pusherAppID, pushKey: key, appDisplayName: "botville")
            try await pusherTransport.setPusher(homeserver: homeserver, accessToken: credentials.accessToken, registration: deletion)
            knownPushKeys.remove(key)
        }
        lastRegisteredPushKey = nil
        pushRegistrationStatus = "已删除推送注册。"
        pushRegistrationError = nil
    }

    public func logout() async {
        guard let client, !isLoggingOut else { return }
        isLoggingOut = true
        defer { isLoggingOut = false }
        await waitForPushOperations()
        do { try await unregisterCurrentPush() }
        catch {
            pushRegistrationError = "推送注销失败；会话仍保留，请联网后重试。"
            syncError = "推送注销失败；会话仍保留，请联网后重试。"
            return
        }
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
        removeSharedPushCredentials()
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
        bannerResumeState = ForegroundBannerResumeState()
        bannerResumeTask?.cancel()
        bannerResumeTask = nil
        deliveredBackgroundEvents.removeAll()
        bannerNotificationListener?.continuation.finish()
        bannerNotificationListener = nil
        bannerNotificationTask?.cancel()
        bannerNotificationTask = nil
        rooms = []
        updateApplicationBadge()
        userID = nil
        isLoggedIn = false
        isSyncing = false
        connectionStatus = .idle
        syncError = nil
        invitationActions.removeAll()
        securityActions.removeAll()
        securityReadGenerations.removeAll()
        roomSecurityRevisions.removeAll()
        isCreatingRoom = false
        observedEncryptedRooms.removeAll()
        roomRefreshGeneration = UUID()
        cryptoSafetyWarnings = []
        lastRegisteredPushKey = nil
        knownPushKeys.removeAll()
        pushVerificationStatus = nil
        await client?.enableAllSendQueues(enable: false)
        await previous?.value
        client = nil
    }

    private func cleanStoredSession() throws {
        // Try to clear extension-readable credentials first. If Keychain is
        // unavailable, a durable non-secret marker retries removal at startup.
        removeSharedPushCredentials()
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

    /// Publish only after the caller has persisted and activated the session.
    /// Missing extension entitlements (including ad-hoc simulator builds) are
    /// diagnostic-only and never change the primary login/restore result.
    private func publishSharedPushCredentials(_ stored: SessionCredentials) {
        do {
            try sharedPushCredentialsStore.save(SharedPushCredentials(
                homeserverURL: stored.homeserverURL,
                userID: stored.userID,
                accessToken: stored.accessToken,
                deviceID: stored.deviceID
            ))
            // A successful save supersedes any stale credential whose delete
            // previously failed, so it also resolves the pending cleanup.
            sharedPushCredentialsCleanupStateStore.setCleanupRequired(false)
        } catch {
            Self.sharedPushCredentialsLogger.warning("Could not save shared push credentials.")
        }
    }

    private func removeSharedPushCredentials() {
        do {
            try sharedPushCredentialsStore.delete()
            sharedPushCredentialsCleanupStateStore.setCleanupRequired(false)
        } catch {
            // Keep this independent from `cleanupRequired`: a soft logout
            // must retain its primary session and encrypted store even when
            // the extension-readable Keychain item cannot be removed.
            sharedPushCredentialsCleanupStateStore.setCleanupRequired(true)
            Self.sharedPushCredentialsLogger.warning("Could not remove shared push credentials.")
        }
    }

    private func retrySharedPushCredentialsCleanupIfNeeded() {
        guard sharedPushCredentialsCleanupStateStore.isCleanupRequired else { return }
        removeSharedPushCredentials()
    }
}

/// Persists only whether removal of extension-readable credentials must be
/// retried. It deliberately contains no token or other session material.
public protocol SharedPushCredentialsCleanupStateStore: Sendable {
    var isCleanupRequired: Bool { get }
    func setCleanupRequired(_ required: Bool)
}

public final class UserDefaultsSharedPushCredentialsCleanupStateStore: SharedPushCredentialsCleanupStateStore, @unchecked Sendable {
    private static let key = "com.zjyjcl.botville.shared-push-credentials-cleanup-required"
    private let defaults: UserDefaults

    public init(defaults: UserDefaults = .standard) { self.defaults = defaults }
    public var isCleanupRequired: Bool { defaults.bool(forKey: Self.key) }
    public func setCleanupRequired(_ required: Bool) { defaults.set(required, forKey: Self.key) }
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
