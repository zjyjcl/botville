// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

@testable import BotvilleCore
import Foundation
import MatrixRustSDK
import XCTest
import Combine

@MainActor
final class MatrixServiceLifecycleTests: XCTestCase {
    func testLoginPersistsSessionAndRestoreUsesSameDeviceAndStore() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let sessionStore = InMemorySessionStore()
        let firstClient = FakeClient()
        let first = makeService(sessionStore: sessionStore, directory: temporary, client: firstClient)

        try await first.login(homeserver: "https://matrix.test", username: "c21_alpha", password: "test-password")

        let saved = try XCTUnwrap(sessionStore.load())
        XCTAssertEqual(saved.userID, "@c21_alpha:matrix.test")
        XCTAssertEqual(saved.deviceID, "C21TEST")
        XCTAssertTrue(FileManager.default.fileExists(atPath: temporary.appendingPathComponent(saved.storeID.uuidString).path))

        let restoredClient = FakeClient()
        let restored = makeService(sessionStore: sessionStore, directory: temporary, client: restoredClient)
        await restored.restoreSession()

        XCTAssertTrue(restored.isLoggedIn)
        XCTAssertEqual(restored.userID, saved.userID)
        XCTAssertEqual(restoredClient.restoredSession, saved.sdkSession)
        XCTAssertEqual(restoredClient.restoredSession?.deviceId, saved.deviceID)
        XCTAssertEqual(try sessionStore.load()?.storeID, saved.storeID)
        await restored.logout()
    }

    func testOfflineRestorePublishesLocalRoomsBeforeFirstSync() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let saved = testCredentials(userID: "@offline-rooms:matrix.test")
        let sessionStore = InMemorySessionStore(saved)
        _ = try SessionStoreDirectory(baseURL: temporary).create(for: saved.storeID)
        let client = FakeClient(rooms: [FakeRoom(id: "!cached:matrix.test", name: "Cached room")])
        let service = makeService(sessionStore: sessionStore, directory: temporary, client: client)

        await service.restoreSession()
        try await waitUntil { client.syncCalls > 0 }

        XCTAssertTrue(service.isLoggedIn)
        XCTAssertEqual(service.rooms.map(\.id), ["!cached:matrix.test"])
        XCTAssertEqual(client.lifecycleEvents.first, "rooms")
        XCTAssertEqual(client.lifecycleEvents.prefix(3), ["rooms", "enable-true", "sync"])
    }

    func testCancelledRestoreAfterLocalRoomsArePublishedDetachesClientAndKeepsCredentials() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let saved = testCredentials(userID: "@cancel-after-rooms:matrix.test")
        let store = InMemorySessionStore(saved)
        _ = try SessionStoreDirectory(baseURL: temporary).create(for: saved.storeID)
        let client = FakeClient(
            rooms: [FakeRoom(id: "!cached:matrix.test", name: "Cached room")],
            blockQueueEnable: true
        )
        let service = makeService(sessionStore: store, directory: temporary, client: client)

        let restore = Task { @MainActor in await service.restoreSession() }
        try await waitUntil { client.lifecycleEvents.contains("enable-true") }
        XCTAssertEqual(service.rooms.map(\.id), ["!cached:matrix.test"])

        service.cancelAuthentication()
        await restore.value
        try await waitUntil { !service.isRestoring && !service.isLoggedIn }

        XCTAssertEqual(service.rooms, [])
        XCTAssertEqual(try store.load(), saved)
        XCTAssertTrue(FileManager.default.fileExists(atPath: temporary.appendingPathComponent(saved.storeID.uuidString).path))
    }

    func testSuccessfulLogoutClearsKeychainValueAndOnlyItsStoreDirectory() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let sessionStore = InMemorySessionStore()
        let client = FakeClient()
        let service = makeService(sessionStore: sessionStore, directory: temporary, client: client)

        try await service.login(homeserver: "https://matrix.test", username: "c21_logout", password: "test-password")
        let saved = try XCTUnwrap(sessionStore.load())
        let unrelated = temporary.appendingPathComponent("unrelated", isDirectory: true)
        try FileManager.default.createDirectory(at: unrelated, withIntermediateDirectories: true)

        await service.logout()

        XCTAssertTrue(client.didLogout)
        XCTAssertFalse(service.isLoggedIn)
        XCTAssertNil(try sessionStore.load())
        XCTAssertFalse(FileManager.default.fileExists(atPath: temporary.appendingPathComponent(saved.storeID.uuidString).path))
        XCTAssertTrue(FileManager.default.fileExists(atPath: unrelated.path))
    }

    func testFailedLogoutKeepsLocalSessionForRetry() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let sessionStore = InMemorySessionStore()
        let client = FakeClient(logoutError: ClientError.Generic(msg: "offline", details: nil))
        let service = makeService(sessionStore: sessionStore, directory: temporary, client: client)

        try await service.login(homeserver: "https://matrix.test", username: "c21_retry", password: "test-password")
        let saved = try XCTUnwrap(sessionStore.load())
        await service.logout()

        XCTAssertTrue(service.isLoggedIn)
        XCTAssertEqual(try sessionStore.load(), saved)
        XCTAssertTrue(FileManager.default.fileExists(atPath: temporary.appendingPathComponent(saved.storeID.uuidString).path))
    }

    func testRestoreTransportFailureKeepsCredentialsAndStoreForRetry() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let saved = testCredentials(userID: "@c21_transport:matrix.test")
        let sessionStore = InMemorySessionStore(saved)
        let directory = SessionStoreDirectory(baseURL: temporary)
        _ = try directory.create(for: saved.storeID)
        let service = MatrixService(sessionStore: sessionStore, storeDirectory: directory, discovery: PasswordDiscovery(), monitorNetwork: false)
        service.clientFactory = { _, _, _ in throw URLError(.notConnectedToInternet) }

        await service.restoreSession()

        XCTAssertFalse(service.isLoggedIn)
        XCTAssertEqual(try sessionStore.load(), saved)
        XCTAssertTrue(FileManager.default.fileExists(atPath: directory.url(for: saved.storeID).path))
        XCTAssertEqual(service.sessionError, "会话恢复暂时失败，请检查网络后重试。")
    }

    func testLoginStorageFailureLogsOutCandidateAndDoesNotActivateSession() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let sessionStore = FailingSaveSessionStore()
        let client = FakeClient()
        let authentication = FakePasswordAuthentication()
        let service = makeService(sessionStore: sessionStore, directory: temporary, client: client, authentication: authentication)

        do {
            try await service.login(homeserver: "https://matrix.test", username: "c21_storage", password: "test-password")
            XCTFail("Expected storage failure")
        } catch MatrixServiceError.storageUnavailable {
            // Expected: candidate has already received logout.
        }
        XCTAssertEqual(authentication.discardCalls, 1)
        XCTAssertFalse(service.isLoggedIn)
        XCTAssertNil(try sessionStore.load())
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: temporary.path), [])
    }

    func testLogoutThenLoginCreatesFreshStoreAndDoesNotReusePreviousAccount() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let sessionStore = InMemorySessionStore()
        let firstClient = FakeClient(session: fakeSession(userID: "@c21_first:matrix.test", deviceID: "FIRST"))
        let secondClient = FakeClient(session: fakeSession(userID: "@c21_second:matrix.test", deviceID: "SECOND"))
        let sequence = ClientSequence([firstClient, secondClient])
        let authentication = FakePasswordAuthentication()
        let sessions = SessionSequence([
            fakeSession(userID: "@c21_first:matrix.test", deviceID: "FIRST"),
            fakeSession(userID: "@c21_second:matrix.test", deviceID: "SECOND")
        ])
        authentication.sessionForLogin = { _ in sessions.next() }
        let service = MatrixService(sessionStore: sessionStore, storeDirectory: SessionStoreDirectory(baseURL: temporary), discovery: PasswordDiscovery(), monitorNetwork: false, passwordAuthentication: authentication)
        service.clientFactory = { _, _, _ in try sequence.next() }

        try await service.login(homeserver: "https://matrix.test", username: "c21_first", password: "test-password")
        let first = try XCTUnwrap(sessionStore.load())
        await service.logout()
        try await service.login(homeserver: "https://matrix.test", username: "c21_second", password: "test-password")
        let second = try XCTUnwrap(sessionStore.load())

        XCTAssertEqual(second.userID, "@c21_second:matrix.test")
        XCTAssertEqual(second.deviceID, "SECOND")
        XCTAssertNotEqual(first.storeID, second.storeID)
        XCTAssertFalse(FileManager.default.fileExists(atPath: temporary.appendingPathComponent(first.storeID.uuidString).path))
        XCTAssertTrue(FileManager.default.fileExists(atPath: temporary.appendingPathComponent(second.storeID.uuidString).path))
        await service.logout()
    }

    func testUnknownTokenDuringSyncClearsSessionAndReturnsToLogin() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let sessionStore = InMemorySessionStore()
        let expired = ClientError.MatrixApi(kind: .unknownToken(softLogout: false), code: "M_UNKNOWN_TOKEN", msg: "expired", details: nil)
        let client = FakeClient()
        let service = makeService(sessionStore: sessionStore, directory: temporary, client: client)

        try await service.login(homeserver: "https://matrix.test", username: "c21_expired", password: "test-password")
        let saved = try XCTUnwrap(sessionStore.load())
        client.syncError = expired
        try await service.sync()
        let cleared = expectation(description: "unknown token clears the local session")
        Task { @MainActor in
            while service.isLoggedIn { try? await Task.sleep(for: .milliseconds(5)) }
            cleared.fulfill()
        }
        await fulfillment(of: [cleared], timeout: 1)

        XCTAssertFalse(service.isLoggedIn)
        XCTAssertNil(try sessionStore.load())
        XCTAssertFalse(FileManager.default.fileExists(atPath: temporary.appendingPathComponent(saved.storeID.uuidString).path))
        XCTAssertEqual(service.sessionError, "会话已失效，请重新登录。")
    }

    func testRestoreUnknownTokenClearsStoredSession() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let saved = testCredentials(userID: "@restore-expired:matrix.test")
        let store = InMemorySessionStore(saved)
        let directory = SessionStoreDirectory(baseURL: temporary)
        _ = try directory.create(for: saved.storeID)
        let expired = ClientError.MatrixApi(kind: .unknownToken(softLogout: false), code: "M_UNKNOWN_TOKEN", msg: "expired", details: nil)
        let client = FakeClient(restoreError: expired)
        let service = makeService(sessionStore: store, directory: temporary, client: client)

        await service.restoreSession()

        XCTAssertFalse(service.isLoggedIn)
        XCTAssertNil(try store.load())
        XCTAssertFalse(FileManager.default.fileExists(atPath: directory.url(for: saved.storeID).path))
        XCTAssertEqual(service.sessionError, "会话已失效，请重新登录。")
    }

    func testLogoutUnknownTokenStillClearsLocalSession() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let expired = ClientError.MatrixApi(kind: .unknownToken(softLogout: false), code: "M_UNKNOWN_TOKEN", msg: "expired", details: nil)
        let client = FakeClient(logoutError: expired)
        let store = InMemorySessionStore()
        let service = makeService(sessionStore: store, directory: temporary, client: client)
        try await service.login(homeserver: "https://matrix.test", username: "logout-expired", password: "test-password")

        await service.logout()

        XCTAssertFalse(service.isLoggedIn)
        XCTAssertNil(try store.load())
    }

    func testCorruptKeychainCanBeClearedWithAllStoresAndLoginCanResume() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let store = MalformedSessionStore()
        let directory = SessionStoreDirectory(baseURL: temporary)
        _ = try directory.create(for: UUID())
        let client = FakeClient()
        let service = makeService(sessionStore: store, directory: temporary, client: client)

        await service.restoreSession()
        XCTAssertTrue(service.canClearLocalSession)
        try await service.clearLocalSession()
        XCTAssertFalse(FileManager.default.fileExists(atPath: temporary.path))
        store.isMalformed = false
        try await service.login(homeserver: "https://matrix.test", username: "after-clear", password: "test-password")
        XCTAssertTrue(service.isLoggedIn)
        await service.logout()
    }

    func testFailedLocalClearKeepsRecoveryExitForRetry() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let store = FailFirstDeleteMalformedStore()
        let service = makeService(sessionStore: store, directory: temporary, client: FakeClient())
        await service.restoreSession()

        do { try await service.clearLocalSession(); XCTFail("first delete should fail") }
        catch MatrixServiceError.storageUnavailable {}
        XCTAssertTrue(service.canClearLocalSession)
        XCTAssertThrowsError(try store.load())

        try await service.clearLocalSession()
        XCTAssertFalse(service.canClearLocalSession)
        XCTAssertNil(try store.load())
    }

    func testStoreKeyAndDirectoryFailuresAreStorageErrorsWithoutOrphan() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let keyClient = FakeClient()
        let keyService = makeService(sessionStore: InMemorySessionStore(), directory: temporary, client: keyClient)
        keyService.storeKeyFactory = { throw SessionStoreError.unavailable }
        await assertStorageFailure(keyService)
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: temporary.path), [])

        let file = temporary.appendingPathComponent("not-a-directory")
        try Data("x".utf8).write(to: file)
        let directoryClient = FakeClient()
        let directoryService = makeService(sessionStore: InMemorySessionStore(), directory: file, client: directoryClient)
        await assertStorageFailure(directoryService)
        XCTAssertFalse(directoryClient.didLogout)
    }

    func testRateLimitIsVisibleAndAuthenticationCanBeCancelled() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let authentication = FakePasswordAuthentication()
        authentication.error = AuthenticationError.rateLimited(retryAfter: 30)
        let service = MatrixService(sessionStore: InMemorySessionStore(), storeDirectory: SessionStoreDirectory(baseURL: temporary), discovery: PasswordDiscovery(), monitorNetwork: false, passwordAuthentication: authentication, authenticationTimeout: 1)
        service.clientFactory = { _, _, _ in FakeClient() }
        let task = Task { @MainActor in
            try await service.login(homeserver: "https://matrix.test", username: "limited", password: "test-password")
        }
        try await Task.sleep(for: .milliseconds(20))
        XCTAssertTrue(service.authenticationStatus?.contains("30 秒") == true)
        service.cancelAuthentication()
        do { try await task.value; XCTFail("expected cancellation") }
        catch is CancellationError {}
    }

    func testRestoreRateLimitIsVisibleAndCancellationKeepsStoredSession() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let saved = testCredentials(userID: "@restore-limited:matrix.test")
        let store = InMemorySessionStore(saved)
        _ = try SessionStoreDirectory(baseURL: temporary).create(for: saved.storeID)
        let client = FakeClient(restoreError: AuthenticationError.rateLimited(retryAfter: 20))
        let service = makeService(sessionStore: store, directory: temporary, client: client)
        let task = Task { @MainActor in await service.restoreSession() }
        try await Task.sleep(for: .milliseconds(20))
        XCTAssertTrue(service.authenticationStatus?.contains("20 秒") == true)
        service.cancelAuthentication()
        await task.value
        XCTAssertEqual(try store.load(), saved)
        XCTAssertTrue(service.canRetryRestore)
        XCTAssertTrue(service.canClearLocalSession)
    }

    func testAuthenticationTimeoutCleansLateCandidateWithoutActivation() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let authentication = LatePasswordAuthentication()
        let probe = QueueProbe()
        var client: FakeClient? = FakeClient(probe: probe)
        weak var releasedClient = client
        let store = InMemorySessionStore()
        let service = MatrixService(sessionStore: store, storeDirectory: SessionStoreDirectory(baseURL: temporary), discovery: PasswordDiscovery(), monitorNetwork: false, passwordAuthentication: authentication, authenticationTimeout: 0.05)
        service.clientFactory = { [weak client] _, _, _ in
            guard let client else { throw SessionStoreError.unavailable }
            return client
        }

        do { try await service.login(homeserver: "https://matrix.test", username: "late", password: "test-password"); XCTFail("expected timeout") }
        catch let error as AuthenticationError { XCTAssertEqual(error, .timedOut) }
        XCTAssertTrue(service.isAuthenticating)
        client = nil
        let cleaned = expectation(description: "late authentication worker cleans up")
        let observation = service.$isAuthenticating.dropFirst().filter { !$0 }.sink { _ in cleaned.fulfill() }
        XCTAssertTrue(authentication.complete(), "the cancelled worker still owns the pending result")
        await fulfillment(of: [cleaned], timeout: 1)
        withExtendedLifetime(observation) {}
        XCTAssertFalse(service.isAuthenticating)
        XCTAssertFalse(service.isLoggedIn)
        XCTAssertTrue(probe.values.contains(false))
        XCTAssertEqual(authentication.discardCalls, 1)
        XCTAssertNil(releasedClient)
        XCTAssertNil(try store.load())
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: temporary.path), [])
    }

    func testForegroundAndNetworkRecoverySyncWithoutResumingPausedQueues() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let client = FakeClient(successfulSync: true)
        let service = makeService(sessionStore: InMemorySessionStore(), directory: temporary, client: client)
        try await service.login(homeserver: "https://matrix.test", username: "foreground", password: "test-password")
        try await Task.sleep(for: .milliseconds(15))
        let beforeForeground = client.syncCalls
        let initialEnableCount = client.queueEnableValues.filter { $0 }.count

        await service.applicationDidBecomeActive()
        try await Task.sleep(for: .milliseconds(15))
        XCTAssertGreaterThan(client.syncCalls, beforeForeground)
        XCTAssertEqual(client.queueEnableValues.filter { $0 }.count, initialEnableCount)

        let beforeNetwork = client.syncCalls
        await service.networkChanged(available: false)
        await service.networkChanged(available: true)
        try await Task.sleep(for: .milliseconds(15))
        XCTAssertGreaterThan(client.syncCalls, beforeNetwork)
        XCTAssertEqual(client.queueEnableValues.filter { $0 }.count, initialEnableCount)
        await service.logout()
    }

    private func assertStorageFailure(_ service: MatrixService) async {
        do {
            try await service.login(homeserver: "https://matrix.test", username: "storage", password: "test-password")
            XCTFail("expected storage failure")
        } catch MatrixServiceError.storageUnavailable {}
        catch { XCTFail("unexpected error \(error)") }
    }

    func testRoomSortingUsesActivityAndDirectFlagDoesNotDependOnDisplayName() {
        let older = Date(timeIntervalSince1970: 1)
        let newer = Date(timeIntervalSince1970: 2)
        let rooms = [
            MatrixRoom(id: "!group:matrix.test", name: "Ada", isDirect: false, latestActivity: newer),
            MatrixRoom(id: "!dm:matrix.test", name: "Team chat", isDirect: true, latestActivity: older),
            MatrixRoom(id: "!idle:matrix.test", name: "Later", isDirect: false, latestActivity: nil)
        ]

        XCTAssertEqual(MatrixRoom.sortedByActivity(rooms).map(\.id), ["!group:matrix.test", "!dm:matrix.test", "!idle:matrix.test"])
        XCTAssertFalse(rooms[0].isDirect)
        XCTAssertTrue(rooms[1].isDirect)
    }

    private func makeService(sessionStore: any SessionStore, directory: URL, client: FakeClient,
                             authentication: FakePasswordAuthentication = FakePasswordAuthentication()) -> MatrixService {
        let service = MatrixService(
            sessionStore: sessionStore,
            storeDirectory: SessionStoreDirectory(baseURL: directory),
            discovery: PasswordDiscovery(),
            monitorNetwork: false,
            passwordAuthentication: authentication,
            authenticationTimeout: 5
        )
        service.clientFactory = { _, _, _ in client }
        return service
    }

    private func makeTemporaryDirectory() throws -> URL {
        let directory = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent(".build/lifecycle-tests/\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    private func testCredentials(userID: String) -> SessionCredentials {
        SessionCredentials(accessToken: "fake-access", refreshToken: nil, userID: userID, deviceID: "C21TEST", homeserverURL: "https://matrix.test", oauthData: nil, storeID: UUID(), storeKey: Data(repeating: 7, count: 32))
    }

    private func waitUntil(_ condition: @escaping () -> Bool) async throws {
        for _ in 0 ..< 100 {
            if condition() { return }
            try await Task.sleep(for: .milliseconds(5))
        }
        XCTFail("Timed out waiting for expected lifecycle state")
    }
}

private struct PasswordDiscovery: LoginCapabilityDiscovering {
    func discover(at homeserver: URL) async throws -> LoginCapabilities {
        LoginCapabilities(supportsPassword: true)
    }
}

private final class FakeClient: Client, @unchecked Sendable {
    private let configuredLogoutError: Error?
    private let configuredRestoreError: Error?
    var syncError: Error?
    private let successfulSync: Bool
    private let probe: QueueProbe?
    private let configuredRooms: [Room]
    private let blockQueueEnable: Bool
    private(set) var syncCalls = 0
    private(set) var queueEnableValues: [Bool] = []
    private(set) var didLogout = false
    private(set) var restoredSession: Session?
    private(set) var lifecycleEvents: [String] = []
    private let configuredSession: Session

    init(session: Session = fakeSession(userID: "@c21_alpha:matrix.test", deviceID: "C21TEST"), logoutError: Error? = nil, restoreError: Error? = nil, syncError: Error? = nil, successfulSync: Bool = false, probe: QueueProbe? = nil, rooms: [Room] = [], blockQueueEnable: Bool = false) {
        configuredLogoutError = logoutError
        configuredRestoreError = restoreError
        self.syncError = syncError
        self.successfulSync = successfulSync
        self.probe = probe
        configuredRooms = rooms
        self.blockQueueEnable = blockQueueEnable
        configuredSession = session
        super.init(noHandle: .init())
    }

    required init(unsafeFromHandle handle: UInt64) {
        configuredLogoutError = nil
        configuredRestoreError = nil
        syncError = nil
        successfulSync = false
        probe = nil
        configuredRooms = []
        blockQueueEnable = false
        configuredSession = fakeSession(userID: "@c21_unexpected:matrix.test", deviceID: "UNEXPECTED")
        super.init(unsafeFromHandle: handle)
    }

    override func login(username: String, password: String, initialDeviceName: String?, deviceId: String?) async throws {}

    override func session() throws -> Session {
        configuredSession
    }

    override func restoreSession(session: Session) async throws {
        if let configuredRestoreError { throw configuredRestoreError }
        restoredSession = session
    }

    override func logout() async throws {
        if let configuredLogoutError { throw configuredLogoutError }
        didLogout = true
    }

    override func setDelegate(delegate: ClientDelegate?) throws -> TaskHandle? { nil }
    override func rooms() -> [Room] {
        lifecycleEvents.append("rooms")
        return configuredRooms
    }
    override func enableAllSendQueues(enable: Bool) async {
        lifecycleEvents.append(enable ? "enable-true" : "enable-false")
        queueEnableValues.append(enable)
        probe?.values.append(enable)
        if enable && blockQueueEnable { try? await Task.sleep(for: .seconds(60)) }
    }

    override func syncOnceV2(settings: SyncSettingsV2) async throws -> SyncResponseV2 {
        lifecycleEvents.append("sync")
        syncCalls += 1
        if let syncError { throw syncError }
        if successfulSync {
            return SyncResponseV2(nextBatch: "test", rooms: SyncResponseRoomsV2(invited: [], joined: [], left: [], knocked: []))
        }
        throw ClientError.Generic(msg: "offline", details: nil)
    }
}

private final class FakeRoom: Room, @unchecked Sendable {
    private let roomID: String
    private let roomName: String

    init(id: String, name: String) {
        roomID = id
        roomName = name
        super.init(noHandle: .init())
    }

    required init(unsafeFromHandle handle: UInt64) {
        roomID = "!unexpected:matrix.test"
        roomName = "Unexpected"
        super.init(unsafeFromHandle: handle)
    }

    override func id() -> String { roomID }
    override func displayName() -> String? { roomName }
    override func membership() -> Membership { .joined }
    override func roomInfo() async throws -> RoomInfo { throw ClientError.Generic(msg: "unused", details: nil) }
    override func latestEvent() async -> LatestEventValue { .none }
    override func isDirect() async -> Bool { false }
}

private final class QueueProbe: @unchecked Sendable { var values: [Bool] = [] }

private final class FakePasswordAuthentication: PasswordAuthenticating, @unchecked Sendable {
    var error: Error?
    var discardCalls = 0
    var sessionForLogin: ((String) -> Session) = { username in
        fakeSession(userID: "@\(username):matrix.test", deviceID: "C21TEST")
    }

    func login(at server: URL, username: String, password: String) async throws -> Session {
        if let error { throw error }
        return sessionForLogin(username)
    }
    func discard(_ session: Session) async { discardCalls += 1 }
}

private final class LatePasswordAuthentication: PasswordAuthenticating, @unchecked Sendable {
    private(set) var discardCalls = 0
    private let lock = NSLock()
    private var pending: CheckedContinuation<Session, Never>?
    func login(at server: URL, username: String, password: String) async throws -> Session {
        // A manually completed future deliberately ignores Task cancellation.
        await withCheckedContinuation { continuation in lock.withLock { pending = continuation } }
    }
    func complete() -> Bool {
        let continuation = lock.withLock { let value = pending; pending = nil; return value }
        continuation?.resume(returning: fakeSession(userID: "@late:matrix.test", deviceID: "LATE"))
        return continuation != nil
    }
    func discard(_ session: Session) async { discardCalls += 1 }
}

private final class FailingSaveSessionStore: SessionStore, @unchecked Sendable {
    func load() throws -> SessionCredentials? { nil }
    func save(_ credentials: SessionCredentials) throws { throw SessionStoreError.unavailable }
    func delete() throws {}
}

private final class MalformedSessionStore: SessionStore, @unchecked Sendable {
    var isMalformed = true
    func load() throws -> SessionCredentials? {
        if isMalformed { throw SessionStoreError.malformedItem }
        return nil
    }
    func save(_ credentials: SessionCredentials) throws {}
    func delete() throws { isMalformed = false }
}

private final class FailFirstDeleteMalformedStore: SessionStore, @unchecked Sendable {
    private var shouldFailDelete = true
    private var isMalformed = true
    func load() throws -> SessionCredentials? {
        if isMalformed { throw SessionStoreError.malformedItem }
        return nil
    }
    func save(_ credentials: SessionCredentials) throws {}
    func delete() throws {
        if shouldFailDelete {
            shouldFailDelete = false
            throw SessionStoreError.unavailable
        }
        isMalformed = false
    }
}

private final class ClientSequence: @unchecked Sendable {
    private let lock = NSLock()
    private var clients: [Client]

    init(_ clients: [Client]) { self.clients = clients }

    func next() throws -> Client {
        try lock.withLock {
            guard !clients.isEmpty else { throw SessionStoreError.unavailable }
            return clients.removeFirst()
        }
    }
}

private final class SessionSequence: @unchecked Sendable {
    private let lock = NSLock()
    private var sessions: [Session]
    init(_ sessions: [Session]) { self.sessions = sessions }
    func next() -> Session {
        lock.withLock {
            precondition(!sessions.isEmpty, "test session sequence exhausted")
            return sessions.removeFirst()
        }
    }
}

private func fakeSession(userID: String, deviceID: String) -> Session {
    Session(accessToken: "fake-access", refreshToken: nil, userId: userID, deviceId: deviceID, homeserverUrl: "https://matrix.test", oauthData: nil, slidingSyncVersion: .none)
}
