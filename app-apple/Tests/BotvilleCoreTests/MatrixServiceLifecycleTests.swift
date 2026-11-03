// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

@testable import BotvilleCore
import Foundation
import MatrixRustSDK
import XCTest

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
        let service = makeService(sessionStore: sessionStore, directory: temporary, client: client)

        do {
            try await service.login(homeserver: "https://matrix.test", username: "c21_storage", password: "test-password")
            XCTFail("Expected storage failure")
        } catch MatrixServiceError.storageUnavailable {
            // Expected: candidate has already received logout.
        }
        XCTAssertTrue(client.didLogout)
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
        let service = MatrixService(sessionStore: sessionStore, storeDirectory: SessionStoreDirectory(baseURL: temporary), discovery: PasswordDiscovery(), monitorNetwork: false)
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
        for _ in 0..<1_000 {
            if try sessionStore.load() == nil && service.sessionError == "会话已失效，请重新登录。" { break }
            await Task.yield()
        }

        XCTAssertFalse(service.isLoggedIn)
        XCTAssertNil(try sessionStore.load())
        XCTAssertFalse(FileManager.default.fileExists(atPath: temporary.appendingPathComponent(saved.storeID.uuidString).path))
        XCTAssertEqual(service.sessionError, "会话已失效，请重新登录。")
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

    private func makeService(sessionStore: any SessionStore, directory: URL, client: FakeClient) -> MatrixService {
        let service = MatrixService(
            sessionStore: sessionStore,
            storeDirectory: SessionStoreDirectory(baseURL: directory),
            discovery: PasswordDiscovery(),
            monitorNetwork: false
        )
        service.clientFactory = { _, _, _ in client }
        return service
    }

    private func makeTemporaryDirectory() throws -> URL {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    private func testCredentials(userID: String) -> SessionCredentials {
        SessionCredentials(accessToken: "fake-access", refreshToken: nil, userID: userID, deviceID: "C21TEST", homeserverURL: "https://matrix.test", oauthData: nil, storeID: UUID(), storeKey: Data(repeating: 7, count: 32))
    }
}

private struct PasswordDiscovery: LoginCapabilityDiscovering {
    func discover(at homeserver: URL) async throws -> LoginCapabilities {
        LoginCapabilities(supportsPassword: true)
    }
}

private final class FakeClient: Client, @unchecked Sendable {
    private let configuredLogoutError: Error?
    var syncError: Error?
    private(set) var didLogout = false
    private(set) var restoredSession: Session?
    private let configuredSession: Session

    init(session: Session = fakeSession(userID: "@c21_alpha:matrix.test", deviceID: "C21TEST"), logoutError: Error? = nil, syncError: Error? = nil) {
        configuredLogoutError = logoutError
        self.syncError = syncError
        configuredSession = session
        super.init(noHandle: .init())
    }

    required init(unsafeFromHandle handle: UInt64) {
        configuredLogoutError = nil
        syncError = nil
        configuredSession = fakeSession(userID: "@c21_unexpected:matrix.test", deviceID: "UNEXPECTED")
        super.init(unsafeFromHandle: handle)
    }

    override func login(username: String, password: String, initialDeviceName: String?, deviceId: String?) async throws {}

    override func session() throws -> Session {
        configuredSession
    }

    override func restoreSession(session: Session) async throws {
        restoredSession = session
    }

    override func logout() async throws {
        if let configuredLogoutError { throw configuredLogoutError }
        didLogout = true
    }

    override func setDelegate(delegate: ClientDelegate?) throws -> TaskHandle? { nil }
    override func rooms() -> [Room] { [] }
    override func enableAllSendQueues(enable: Bool) async {}

    override func syncOnceV2(settings: SyncSettingsV2) async throws -> SyncResponseV2 {
        if let syncError { throw syncError }
        throw ClientError.Generic(msg: "offline", details: nil)
    }
}

private final class FailingSaveSessionStore: SessionStore, @unchecked Sendable {
    func load() throws -> SessionCredentials? { nil }
    func save(_ credentials: SessionCredentials) throws { throw SessionStoreError.unavailable }
    func delete() throws {}
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

private func fakeSession(userID: String, deviceID: String) -> Session {
    Session(accessToken: "fake-access", refreshToken: nil, userId: userID, deviceId: deviceID, homeserverUrl: "https://matrix.test", oauthData: nil, slidingSyncVersion: .none)
}
