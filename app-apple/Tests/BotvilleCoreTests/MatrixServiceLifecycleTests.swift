// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

@testable import BotvilleCore
import Foundation
import MatrixRustSDK
import XCTest
import Combine

@MainActor
final class MatrixServiceLifecycleTests: XCTestCase {
    func testLoginWritesAndClearLocalSessionRemovesSharedPushCredentials() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let sessionStore = InMemorySessionStore()
        let sharedStore = InMemorySharedPushCredentialsStore()
        let cleanupState = InMemorySharedPushCredentialsCleanupStateStore()
        cleanupState.setCleanupRequired(true)
        let service = makeService(
            sessionStore: sessionStore,
            directory: temporary,
            client: FakeClient(),
            sharedPushCredentialsStore: sharedStore,
            sharedPushCredentialsCleanupStateStore: cleanupState
        )

        try await service.login(homeserver: "https://matrix.test", username: "shared-credentials", password: "test-password")

        let shared = try XCTUnwrap(sharedStore.load())
        XCTAssertEqual(shared.homeserverURL, "https://matrix.test")
        XCTAssertEqual(shared.userID, "@shared-credentials:matrix.test")
        XCTAssertEqual(shared.deviceID, "C21TEST")
        XCTAssertEqual(shared.accessToken, "fake-access")
        XCTAssertFalse(cleanupState.isCleanupRequired)

        try await service.clearLocalSession()

        XCTAssertNil(try sharedStore.load())
    }

    func testRestoreWritesSharedPushCredentials() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let saved = testCredentials(userID: "@restore-shared:matrix.test")
        let sessionStore = InMemorySessionStore(saved)
        let sharedStore = InMemorySharedPushCredentialsStore()
        _ = try SessionStoreDirectory(baseURL: temporary).create(for: saved.storeID)
        let service = makeService(
            sessionStore: sessionStore,
            directory: temporary,
            client: FakeClient(),
            sharedPushCredentialsStore: sharedStore
        )

        await service.restoreSession()

        XCTAssertEqual(try sharedStore.load(), SharedPushCredentials(
            homeserverURL: saved.homeserverURL,
            userID: saved.userID,
            accessToken: saved.accessToken,
            deviceID: saved.deviceID
        ))
    }

    func testUnavailableSharedPushStoreDoesNotBlockLogin() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let service = makeService(
            sessionStore: InMemorySessionStore(),
            directory: temporary,
            client: FakeClient(),
            sharedPushCredentialsStore: UnavailableSharedPushCredentialsStore()
        )

        try await service.login(homeserver: "https://matrix.test", username: "shared-unavailable", password: "test-password")

        XCTAssertTrue(service.isLoggedIn)
    }

    func testSharedCredentialDeleteFailureIsRetriedOnNextStartupWithoutSession() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let suiteName = "MatrixServiceLifecycleTests.\(UUID().uuidString)"
        let firstDefaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        defer { firstDefaults.removePersistentDomain(forName: suiteName) }
        let sessionStore = InMemorySessionStore()
        let sharedStore = FailOnceDeleteSharedPushCredentialsStore()
        let cleanupState = UserDefaultsSharedPushCredentialsCleanupStateStore(defaults: firstDefaults)
        let service = makeService(sessionStore: sessionStore, directory: temporary, client: FakeClient(),
                                  sharedPushCredentialsStore: sharedStore, sharedPushCredentialsCleanupStateStore: cleanupState)

        try await service.login(homeserver: "https://matrix.test", username: "retry-shared-delete", password: "test-password")
        await service.logout()
        XCTAssertTrue(cleanupState.isCleanupRequired)
        XCTAssertEqual(sharedStore.deleteCalls, 1)
        XCTAssertNotNil(try sharedStore.load())
        XCTAssertNil(try sessionStore.load())

        let restartedDefaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        let restartedCleanupState = UserDefaultsSharedPushCredentialsCleanupStateStore(defaults: restartedDefaults)
        let restarted = makeService(sessionStore: sessionStore, directory: temporary, client: FakeClient(),
                                    sharedPushCredentialsStore: sharedStore, sharedPushCredentialsCleanupStateStore: restartedCleanupState)
        await restarted.restoreSession()
        XCTAssertFalse(restartedCleanupState.isCleanupRequired)
        XCTAssertEqual(sharedStore.deleteCalls, 2)
        XCTAssertNil(try sharedStore.load())
    }

    func testPairingLoginWritesSharedPushCredentials() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let sharedStore = InMemorySharedPushCredentialsStore()
        let pairing = FakePairingAuthentication(session: fakeSession(userID: "@paired:matrix.test", deviceID: "PAIR"))
        let service = makeService(sessionStore: InMemorySessionStore(), directory: temporary, client: FakeClient(),
                                  sharedPushCredentialsStore: sharedStore, pairingAuthentication: pairing)
        let expiration = Int(Date().addingTimeInterval(60).timeIntervalSince1970)
        let code = Data(repeating: 0, count: 32).base64EncodedString().replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: "")
        let qr = try PairingQRCode.parse("botville://pair?v=1&hs=https://matrix.test&code=\(code)&exp=\(expiration)")
        let response = PairingRedeemResponse(homeserver: URL(string: "https://matrix.test")!, gateway: nil,
                                             auth: PairingAuthentication(type: .loginToken, token: "test-token", expiresInMS: 30_000),
                                             featuresURL: nil, receivedAt: Date())

        try await service.completePairing(response: response, qrCode: qr)

        XCTAssertEqual(try sharedStore.load(), SharedPushCredentials(homeserverURL: "https://matrix.test", userID: "@paired:matrix.test", accessToken: "fake-access", deviceID: "PAIR"))
    }

    func testReleaseRestorePolicyRejectsStoredLoopbackHTTP() {
        XCTAssertThrowsError(
            try RestoredHomeserverPolicy(storedAllowLocalHTTP: true, isDebugBuild: false)
                .validatedURL("  http://127.0.0.1:8028  ")
        ) { error in
            XCTAssertTrue(error is SessionRestoreSecurityError)
        }
    }

    func testDebugRestorePolicyRetainsExplicitLoopbackHTTPOnly() throws {
        let policy = RestoredHomeserverPolicy(storedAllowLocalHTTP: true, isDebugBuild: true)
        XCTAssertEqual(try policy.validatedURL("http://localhost:8028").host, "localhost")
        XCTAssertThrowsError(try policy.validatedURL("http://example.org"))
    }

    #if DEBUG
    func testDebugBuildRestoreUsesPersistedLoopbackBit() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let allowed = SessionCredentials(accessToken: "test", refreshToken: nil, userID: "@debug:matrix.test", deviceID: "DEBUG", homeserverURL: "http://localhost:8028", oauthData: nil, storeID: UUID(), storeKey: Data(repeating: 4, count: 32), allowLocalHTTP: true)
        let allowedStore = InMemorySessionStore(allowed)
        _ = try SessionStoreDirectory(baseURL: temporary).create(for: allowed.storeID)
        let allowedService = makeService(sessionStore: allowedStore, directory: temporary, client: FakeClient())
        await allowedService.restoreSession()
        XCTAssertTrue(allowedService.isLoggedIn)

        let denied = SessionCredentials(accessToken: "test", refreshToken: nil, userID: "@debug:matrix.test", deviceID: "DEBUG", homeserverURL: "http://127.0.0.1:8028", oauthData: nil, storeID: UUID(), storeKey: Data(repeating: 5, count: 32), allowLocalHTTP: false)
        let deniedStore = InMemorySessionStore(denied)
        _ = try SessionStoreDirectory(baseURL: temporary).create(for: denied.storeID)
        let deniedService = makeService(sessionStore: deniedStore, directory: temporary, client: FakeClient())
        await deniedService.restoreSession()
        XCTAssertNil(try deniedStore.load())
        XCTAssertEqual(deniedService.sessionError, "已拒绝并清除旧的 HTTP 本地会话；仅可恢复 HTTPS 会话，请重新登录。")
    }
    #else
    func testReleaseBuildClearsStoredLoopbackHTTPForLocalhostAndIPAddress() async throws {
        for homeserver in ["http://localhost:8028", "http://127.0.0.1:8028"] {
            let temporary = try makeTemporaryDirectory()
            defer { try? FileManager.default.removeItem(at: temporary) }
            let saved = SessionCredentials(accessToken: "test", refreshToken: nil, userID: "@release:matrix.test", deviceID: "RELEASE", homeserverURL: homeserver, oauthData: nil, storeID: UUID(), storeKey: Data(repeating: 6, count: 32), allowLocalHTTP: true)
            let store = InMemorySessionStore(saved)
            let directory = SessionStoreDirectory(baseURL: temporary)
            _ = try directory.create(for: saved.storeID)
            let service = MatrixService(sessionStore: store, storeDirectory: directory, discovery: PasswordDiscovery(), monitorNetwork: false)
            service.clientFactory = { _, _, _ in XCTFail("Release must reject stored HTTP before creating a client"); throw SessionStoreError.unavailable }

            await service.restoreSession()

            XCTAssertFalse(service.isLoggedIn)
            XCTAssertNil(try store.load())
            XCTAssertFalse(FileManager.default.fileExists(atPath: directory.url(for: saved.storeID).path))
            XCTAssertEqual(service.sessionError, "已拒绝并清除旧的 HTTP 本地会话；仅可恢复 HTTPS 会话，请重新登录。")
        }
    }

    func testReleaseLoginRejectsExplicitLoopbackHTTPBeforeDiscoveryClientOrPasswordTransport() async throws {
        for homeserver in ["http://localhost:8028", "http://127.0.0.1:8028"] {
            let temporary = try makeTemporaryDirectory()
            defer { try? FileManager.default.removeItem(at: temporary) }
            let discovery = CountingPasswordDiscovery()
            let authentication = FakePasswordAuthentication()
            let service = MatrixService(
                sessionStore: InMemorySessionStore(),
                storeDirectory: SessionStoreDirectory(baseURL: temporary),
                discovery: discovery,
                monitorNetwork: false,
                passwordAuthentication: authentication
            )
            var clientFactoryCalls = 0
            service.clientFactory = { _, _, _ in
                clientFactoryCalls += 1
                throw SessionStoreError.unavailable
            }

            do {
                try await service.login(homeserver: homeserver, username: "release-http", password: "test-password", allowLocalHTTP: true)
                XCTFail("Release must reject explicit local HTTP")
            } catch {}

            XCTAssertEqual(discovery.calls, 0, "policy must reject before discovery")
            XCTAssertEqual(clientFactoryCalls, 0, "policy must reject before creating a client")
            XCTAssertTrue(authentication.loginCalls.isEmpty, "policy must reject before password transport")
        }
    }

    func testReleaseBuildRestoresHTTPSDespiteLegacyLocalHTTPBit() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let saved = SessionCredentials(accessToken: "test", refreshToken: nil, userID: "@release:matrix.test", deviceID: "RELEASE", homeserverURL: "https://matrix.test", oauthData: nil, storeID: UUID(), storeKey: Data(repeating: 7, count: 32), allowLocalHTTP: true)
        let store = InMemorySessionStore(saved)
        _ = try SessionStoreDirectory(baseURL: temporary).create(for: saved.storeID)
        let service = makeService(sessionStore: store, directory: temporary, client: FakeClient())
        await service.restoreSession()
        XCTAssertTrue(service.isLoggedIn)
        XCTAssertEqual(try store.load(), saved)
    }
    #endif

    func testSoftLogoutRetainsCredentialsAndEncryptedStoreForPasswordReauthentication() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let softLogout = ClientError.MatrixApi(kind: .unknownToken(softLogout: true), code: "M_UNKNOWN_TOKEN", msg: "redacted", details: nil)
        let store = InMemorySessionStore()
        let client = FakeClient(syncError: softLogout)
        let sharedStore = InMemorySharedPushCredentialsStore()
        let service = makeService(sessionStore: store, directory: temporary, client: client, sharedPushCredentialsStore: sharedStore)

        try await service.login(homeserver: "https://matrix.test", username: "c21_soft", password: "test-password")
        let saved = try XCTUnwrap(store.load())
        try await waitUntil { service.requiresPasswordReauthentication }

        XCTAssertFalse(service.isLoggedIn)
        XCTAssertNil(try sharedStore.load())
        XCTAssertTrue(service.canClearLocalSession)
        XCTAssertEqual(try store.load(), saved)
        XCTAssertTrue(FileManager.default.fileExists(atPath: temporary.appendingPathComponent(saved.storeID.uuidString).path))
        XCTAssertEqual(service.reauthenticationHomeserver, saved.homeserverURL)
        XCTAssertEqual(service.reauthenticationUserID, saved.userID)
        XCTAssertEqual(service.sessionError, "会话需要重新输入密码；将使用当前设备 ID 重新登录，已加密的本地数据会保留。")
    }

    func testSoftLogoutSharedCredentialDeleteFailureRetainsPrimarySessionAndRetriesOnRestart() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let softLogout = ClientError.MatrixApi(kind: .unknownToken(softLogout: true), code: "M_UNKNOWN_TOKEN", msg: "redacted", details: nil)
        let sessionStore = InMemorySessionStore()
        let sharedStore = FailOnceDeleteSharedPushCredentialsStore()
        let cleanupState = InMemorySharedPushCredentialsCleanupStateStore()
        let client = FakeClient(syncError: softLogout)
        let service = makeService(sessionStore: sessionStore, directory: temporary, client: client,
                                  sharedPushCredentialsStore: sharedStore, sharedPushCredentialsCleanupStateStore: cleanupState)

        try await service.login(homeserver: "https://matrix.test", username: "soft-delete-retry", password: "test-password")
        let saved = try XCTUnwrap(sessionStore.load())
        try await waitUntil { service.requiresPasswordReauthentication }
        XCTAssertTrue(cleanupState.isCleanupRequired)
        XCTAssertNotNil(try sharedStore.load())
        XCTAssertEqual(try sessionStore.load(), saved)
        XCTAssertTrue(FileManager.default.fileExists(atPath: temporary.appendingPathComponent(saved.storeID.uuidString).path))

        let restarted = makeService(sessionStore: sessionStore, directory: temporary, client: FakeClient(),
                                    sharedPushCredentialsStore: sharedStore, sharedPushCredentialsCleanupStateStore: cleanupState)
        await restarted.restoreSession()
        XCTAssertFalse(cleanupState.isCleanupRequired)
        XCTAssertEqual(try sessionStore.load(), saved)
        XCTAssertTrue(FileManager.default.fileExists(atPath: temporary.appendingPathComponent(saved.storeID.uuidString).path))
        XCTAssertNotNil(try sharedStore.load())
    }

    func testAuthDelegatePreservesSoftLogoutFlag() async throws {
        for testCase in [(isSoftLogout: true, retainsStore: true), (isSoftLogout: false, retainsStore: false)] {
            let temporary = try makeTemporaryDirectory()
            defer { try? FileManager.default.removeItem(at: temporary) }
            let saved = testCredentials(userID: "@delegate-\(testCase.isSoftLogout):matrix.test")
            let store = InMemorySessionStore(saved)
            let directory = SessionStoreDirectory(baseURL: temporary)
            _ = try directory.create(for: saved.storeID)
            let client = FakeClient()
            let service = makeService(sessionStore: store, directory: temporary, client: client)

            await service.restoreSession()
            client.emitAuthError(isSoftLogout: testCase.isSoftLogout)
            try await waitUntil {
                testCase.retainsStore ? service.requiresPasswordReauthentication : !service.isLoggedIn
            }

            if testCase.retainsStore {
                XCTAssertEqual(try store.load(), saved)
                XCTAssertTrue(FileManager.default.fileExists(atPath: directory.url(for: saved.storeID).path))
            } else {
                XCTAssertNil(try store.load())
                XCTAssertFalse(FileManager.default.fileExists(atPath: directory.url(for: saved.storeID).path))
            }
        }
    }

    func testSoftLogoutReauthenticationBindsSavedIdentityAndPreservesStoreContents() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let softLogout = ClientError.MatrixApi(kind: .unknownToken(softLogout: true), code: "M_UNKNOWN_TOKEN", msg: "redacted", details: nil)
        let store = InMemorySessionStore()
        let client = FakeClient(syncError: softLogout)
        let authentication = FakePasswordAuthentication()
        var issuedToken = 0
        authentication.sessionForLogin = { username in
            issuedToken += 1
            let userID = username.hasPrefix("@") ? username : "@\(username):matrix.test"
            return Session(accessToken: "replacement-token-\(issuedToken)", refreshToken: nil, userId: userID, deviceId: "C21TEST", homeserverUrl: "https://matrix.test", oauthData: nil, slidingSyncVersion: .none)
        }
        let service = makeService(sessionStore: store, directory: temporary, client: client, authentication: authentication)

        try await service.login(homeserver: "https://matrix.test", username: "c21_soft", password: "test-password")
        try await waitUntil { service.requiresPasswordReauthentication }
        let saved = try XCTUnwrap(store.load())
        let marker = temporary.appendingPathComponent(saved.storeID.uuidString).appendingPathComponent("cached-marker")
        try Data("cached".utf8).write(to: marker)
        client.syncError = nil

        try await service.login(homeserver: "https://attacker.invalid", username: "different-user", password: "new-password")

        let replacement = try XCTUnwrap(store.load())
        XCTAssertTrue(service.isLoggedIn)
        XCTAssertEqual(replacement.storeID, saved.storeID)
        XCTAssertEqual(replacement.storeKey, saved.storeKey)
        XCTAssertEqual(replacement.deviceID, saved.deviceID)
        XCTAssertNotEqual(replacement.accessToken, saved.accessToken)
        XCTAssertTrue(FileManager.default.fileExists(atPath: marker.path))
        XCTAssertEqual(authentication.loginCalls.last?.homeserver, saved.homeserverURL)
        XCTAssertEqual(authentication.loginCalls.last?.username, saved.userID)
        XCTAssertEqual(authentication.loginCalls.last?.deviceID, saved.deviceID)
    }

    func testSoftLogoutPasswordFailureRetainsStoreAndCanRetry() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let softLogout = ClientError.MatrixApi(kind: .unknownToken(softLogout: true), code: "M_UNKNOWN_TOKEN", msg: "redacted", details: nil)
        let store = InMemorySessionStore()
        let client = FakeClient(syncError: softLogout)
        let authentication = FakePasswordAuthentication()
        let service = makeService(sessionStore: store, directory: temporary, client: client, authentication: authentication)

        try await service.login(homeserver: "https://matrix.test", username: "c21_retry_soft", password: "test-password")
        try await waitUntil { service.requiresPasswordReauthentication }
        let saved = try XCTUnwrap(store.load())
        authentication.error = AuthenticationError.invalidCredentials
        do {
            try await service.login(homeserver: "https://wrong.invalid", username: "wrong", password: "bad-password")
            XCTFail("Expected password failure")
        } catch AuthenticationError.invalidCredentials {
            // Expected: the original store remains available for retry.
        }

        XCTAssertEqual(try store.load(), saved)
        XCTAssertTrue(service.requiresPasswordReauthentication)
        XCTAssertTrue(service.canClearLocalSession)
        XCTAssertTrue(FileManager.default.fileExists(atPath: temporary.appendingPathComponent(saved.storeID.uuidString).path))
        client.syncError = nil
        authentication.error = nil
        try await service.login(homeserver: "https://wrong.invalid", username: "wrong", password: "correct-password")
        XCTAssertTrue(service.isLoggedIn)
        XCTAssertFalse(service.requiresPasswordReauthentication)
    }

    func testSoftLogoutRejectsDifferentReturnedAccountWithoutReplacingStore() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let softLogout = ClientError.MatrixApi(kind: .unknownToken(softLogout: true), code: "M_UNKNOWN_TOKEN", msg: "redacted", details: nil)
        let store = InMemorySessionStore()
        let client = FakeClient(syncError: softLogout)
        let authentication = FakePasswordAuthentication()
        let service = makeService(sessionStore: store, directory: temporary, client: client, authentication: authentication)

        try await service.login(homeserver: "https://matrix.test", username: "c21_same", password: "test-password")
        try await waitUntil { service.requiresPasswordReauthentication }
        let saved = try XCTUnwrap(store.load())
        authentication.sessionForLogin = { _ in fakeSession(userID: "@other:matrix.test", deviceID: "OTHER") }

        do {
            try await service.login(homeserver: "https://wrong.invalid", username: "wrong", password: "new-password")
            XCTFail("Expected returned-account rejection")
        } catch MatrixServiceError.storageUnavailable {
            // Expected: a soft-logout device can only be rebound to itself.
        }
        XCTAssertEqual(try store.load(), saved)
        XCTAssertTrue(service.requiresPasswordReauthentication)
        XCTAssertTrue(FileManager.default.fileExists(atPath: temporary.appendingPathComponent(saved.storeID.uuidString).path))
    }

    func testSoftLogoutSaveFailureRestoresOldCredentialsAndCanRetry() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let saved = testCredentials(userID: "@c21_save_soft:matrix.test")
        let store = FailOnceSaveSessionStore(saved)
        let sharedStore = InMemorySharedPushCredentialsStore()
        let softLogout = ClientError.MatrixApi(kind: .unknownToken(softLogout: true), code: "M_UNKNOWN_TOKEN", msg: "redacted", details: nil)
        let first = FakeClient(syncError: softLogout)
        let replacement = FakeClient()
        let clients = ClientSequence([first, replacement, replacement])
        let authentication = FakePasswordAuthentication()
        let service = MatrixService(sessionStore: store, sharedPushCredentialsStore: sharedStore, storeDirectory: SessionStoreDirectory(baseURL: temporary), discovery: PasswordDiscovery(), monitorNetwork: false, passwordAuthentication: authentication)
        service.clientFactory = { _, _, _ in try clients.next() }
        _ = try SessionStoreDirectory(baseURL: temporary).create(for: saved.storeID)

        await service.restoreSession()
        try await waitUntil { service.requiresPasswordReauthentication }
        first.syncError = nil
        store.failNextSave = true
        do {
            try await service.login(homeserver: "https://wrong.invalid", username: "wrong", password: "new-password")
            XCTFail("Expected replacement Keychain save failure")
        } catch MatrixServiceError.storageUnavailable {
            // Expected: old Keychain value was restored.
        }
        XCTAssertEqual(try store.load(), saved)
        XCTAssertTrue(service.requiresPasswordReauthentication)
        XCTAssertNil(try sharedStore.load(), "A failed replacement session must not publish a token to the NSE")

        try await service.login(homeserver: "https://wrong.invalid", username: "wrong", password: "new-password")
        XCTAssertTrue(service.isLoggedIn)
        XCTAssertFalse(service.requiresPasswordReauthentication)
        XCTAssertEqual(try sharedStore.load()?.accessToken, try store.load()?.accessToken)
    }

    func testCancelledSoftLogoutReauthenticationRetainsStoreDuringActivation() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let saved = testCredentials(userID: "@c21_cancel_soft:matrix.test")
        let store = InMemorySessionStore(saved)
        let softLogout = ClientError.MatrixApi(kind: .unknownToken(softLogout: true), code: "M_UNKNOWN_TOKEN", msg: "redacted", details: nil)
        let first = FakeClient(syncError: softLogout)
        let blocked = FakeClient(blockQueueEnable: true)
        let clients = ClientSequence([first, blocked])
        let service = MatrixService(sessionStore: store, storeDirectory: SessionStoreDirectory(baseURL: temporary), discovery: PasswordDiscovery(), monitorNetwork: false, passwordAuthentication: FakePasswordAuthentication())
        service.clientFactory = { _, _, _ in try clients.next() }
        _ = try SessionStoreDirectory(baseURL: temporary).create(for: saved.storeID)

        await service.restoreSession()
        try await waitUntil { service.requiresPasswordReauthentication }
        let attempt = Task { @MainActor in
            try? await service.login(homeserver: "https://wrong.invalid", username: "wrong", password: "new-password")
        }
        try await waitUntil { blocked.lifecycleEvents.contains("enable-true") }
        service.cancelAuthentication()
        await attempt.value
        try await waitUntil { !service.isAuthenticating && !service.isLoggedIn }

        XCTAssertEqual(try store.load(), saved)
        XCTAssertTrue(service.requiresPasswordReauthentication)
        XCTAssertTrue(FileManager.default.fileExists(atPath: temporary.appendingPathComponent(saved.storeID.uuidString).path))
    }

    func testSoftLogoutRestoreFailureRetainsStoreForRetry() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let saved = testCredentials(userID: "@c21_restore_soft:matrix.test")
        let store = InMemorySessionStore(saved)
        let softLogout = ClientError.MatrixApi(kind: .unknownToken(softLogout: true), code: "M_UNKNOWN_TOKEN", msg: "redacted", details: nil)
        let first = FakeClient(syncError: softLogout)
        let failing = FakeClient(restoreError: ClientError.Generic(msg: "transport", details: nil))
        let replacement = FakeClient()
        let clients = ClientSequence([first, failing, replacement])
        let authentication = FakePasswordAuthentication()
        let service = MatrixService(sessionStore: store, storeDirectory: SessionStoreDirectory(baseURL: temporary), discovery: PasswordDiscovery(), monitorNetwork: false, passwordAuthentication: authentication)
        service.clientFactory = { _, _, _ in try clients.next() }
        _ = try SessionStoreDirectory(baseURL: temporary).create(for: saved.storeID)

        await service.restoreSession()
        try await waitUntil { service.requiresPasswordReauthentication }
        do {
            try await service.login(homeserver: "https://wrong.invalid", username: "wrong", password: "new-password")
            XCTFail("Expected candidate restore failure")
        } catch AuthenticationError.serverFailure {
            // Expected: no replacement was committed.
        }
        XCTAssertEqual(try store.load(), saved)
        XCTAssertTrue(service.requiresPasswordReauthentication)
        XCTAssertEqual(authentication.discardCalls, 0)

        try await service.login(homeserver: "https://wrong.invalid", username: "wrong", password: "new-password")
        XCTAssertTrue(service.isLoggedIn)
    }

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

    func testRoomRefreshDoesNotPublishBannerWithoutSyncNotification() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let saved = testCredentials(userID: "@banner:matrix.test")
        _ = try SessionStoreDirectory(baseURL: temporary).create(for: saved.storeID)
        let room = FakeRoom(id: "!banner:matrix.test", name: "本地房间")
        func event(_ timestamp: UInt64) -> LatestEventValue {
            .remote(timestamp: timestamp, sender: "@peer:matrix.test", isOwn: false, profile: .unavailable,
                    content: .msgLike(content: MsgLikeContent(kind: .message(content: MessageContent(
                        msgType: .text(content: TextMessageContent(body: "同步消息", formatted: nil)),
                        body: "同步消息", isEdited: false, mentions: nil)),
                        inReplyTo: nil, threadRoot: nil, threadSummary: nil)))
        }
        room.latest = event(1_000)
        let client = FakeClient(successfulSync: true, rooms: [room])
        let service = makeService(sessionStore: InMemorySessionStore(saved), directory: temporary, client: client)
        var candidates: [ForegroundBannerCandidate] = []
        let observation = service.foregroundBannerCandidates.sink { candidates.append($0) }
        defer { observation.cancel() }
        await service.restoreSession()
        try await waitUntil { service.connectionStatus == .connected }
        XCTAssertTrue(candidates.isEmpty)

        room.latest = event(2_000)
        try await service.sync()
        try await Task.sleep(for: .milliseconds(20))
        XCTAssertTrue(candidates.isEmpty, "latestEvent is room-list state, not a foreground notification source")
        let listener = try XCTUnwrap(client.notificationListener)
        client.emit(notification: syncNotification(eventID: "$authoritative", body: "SDK callback", timestamp: 2_000, roomID: room.id()), roomID: room.id())
        try await waitUntil { candidates.count == 1 }
        XCTAssertEqual(candidates.first?.roomID, room.id())
        XCTAssertEqual(candidates.first?.preview, "SDK callback")
        XCTAssertEqual(candidates.first?.eventID, "$authoritative")
        withExtendedLifetime(listener) {}
        try await service.clearLocalSession()
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
        let sharedStore = InMemorySharedPushCredentialsStore()
        let client = FakeClient()
        let service = makeService(sessionStore: sessionStore, directory: temporary, client: client, sharedPushCredentialsStore: sharedStore)

        try await service.login(homeserver: "https://matrix.test", username: "c21_logout", password: "test-password")
        let saved = try XCTUnwrap(sessionStore.load())
        XCTAssertNotNil(try sharedStore.load())
        let unrelated = temporary.appendingPathComponent("unrelated", isDirectory: true)
        try FileManager.default.createDirectory(at: unrelated, withIntermediateDirectories: true)

        await service.logout()

        XCTAssertTrue(client.didLogout)
        XCTAssertFalse(service.isLoggedIn)
        XCTAssertNil(try sessionStore.load())
        XCTAssertNil(try sharedStore.load())
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

    func testNormalLoginPersistsSessionBeforeActivationAndCleansItWhenActivationFails() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let store = InMemorySessionStore()
        let authentication = FakePasswordAuthentication()
        var sawPersistedSessionAtActivation = false
        let client = FakeClient(onSetDelegate: {
            sawPersistedSessionAtActivation = (try store.load()) != nil
            throw SessionStoreError.unavailable
        })
        let service = makeService(sessionStore: store, directory: temporary, client: client, authentication: authentication)

        do {
            try await service.login(homeserver: "https://matrix.test", username: "activation-order", password: "test-password")
            XCTFail("Expected activation failure")
        } catch MatrixServiceError.storageUnavailable {}

        XCTAssertTrue(sawPersistedSessionAtActivation)
        XCTAssertNil(try store.load())
        XCTAssertEqual(authentication.discardCalls, 1)
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: temporary.path), [])
    }

    func testNormalLoginRestoreFailureDiscardsNewRemoteSession() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let authentication = FakePasswordAuthentication()
        let client = FakeClient(restoreError: ClientError.Generic(msg: "transport", details: nil))
        let service = makeService(sessionStore: InMemorySessionStore(), directory: temporary, client: client, authentication: authentication)

        do {
            try await service.login(homeserver: "https://matrix.test", username: "restore-failure", password: "test-password")
            XCTFail("Expected candidate restore failure")
        } catch AuthenticationError.serverFailure {}

        XCTAssertEqual(authentication.discardCalls, 1)
        XCTAssertFalse(service.isLoggedIn)
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

    func testRestoreSoftLogoutKeepsClearExitAndForegroundDoesNotRestoreExpiredSession() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let saved = testCredentials(userID: "@restore-soft:matrix.test")
        let store = InMemorySessionStore(saved)
        let directory = SessionStoreDirectory(baseURL: temporary)
        _ = try directory.create(for: saved.storeID)
        let softLogout = ClientError.MatrixApi(kind: .unknownToken(softLogout: true), code: "M_UNKNOWN_TOKEN", msg: "redacted", details: nil)
        let client = FakeClient(restoreError: softLogout)
        let service = makeService(sessionStore: store, directory: temporary, client: client)

        await service.restoreSession()

        XCTAssertFalse(service.isLoggedIn)
        XCTAssertTrue(service.requiresPasswordReauthentication)
        XCTAssertTrue(service.canClearLocalSession)
        XCTAssertEqual(try store.load(), saved)
        XCTAssertTrue(FileManager.default.fileExists(atPath: directory.url(for: saved.storeID).path))
        XCTAssertEqual(client.restoreCalls, 1)
        await service.applicationDidBecomeActive()
        XCTAssertFalse(service.isLoggedIn)
        XCTAssertTrue(service.requiresPasswordReauthentication)
        XCTAssertEqual(try store.load(), saved)
        XCTAssertEqual(client.restoreCalls, 1)

        try await service.clearLocalSession()
        XCTAssertNil(try store.load())
        XCTAssertFalse(FileManager.default.fileExists(atPath: directory.url(for: saved.storeID).path))
        XCTAssertFalse(service.requiresPasswordReauthentication)
        XCTAssertFalse(service.canClearLocalSession)
    }

    func testSoftLogoutWithFailedLocalClearDoesNotRewriteRecordForDeletedStore() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let saved = testCredentials(userID: "@soft-partial-clear:matrix.test")
        let store = FailFirstDeleteSessionStore(saved)
        let directory = SessionStoreDirectory(baseURL: temporary)
        _ = try directory.create(for: saved.storeID)
        let softLogout = ClientError.MatrixApi(kind: .unknownToken(softLogout: true), code: "M_UNKNOWN_TOKEN", msg: "redacted", details: nil)
        let client = FakeClient(syncError: softLogout)
        let authentication = FakePasswordAuthentication()
        let service = MatrixService(sessionStore: store, storeDirectory: directory, discovery: PasswordDiscovery(), monitorNetwork: false, passwordAuthentication: authentication)
        service.clientFactory = { _, _, _ in client }

        await service.restoreSession()
        try await waitUntil { service.requiresPasswordReauthentication }
        do { try await service.clearLocalSession(); XCTFail("first local clear should fail") }
        catch MatrixServiceError.storageUnavailable {}
        XCTAssertEqual(try store.load(), saved)
        XCTAssertFalse(FileManager.default.fileExists(atPath: directory.url(for: saved.storeID).path))
        let saveCallsAfterFailedClear = store.saveCalls

        do {
            try await service.login(homeserver: "https://matrix.test", username: "soft-partial-clear", password: "test-password")
            XCTFail("partial cleanup must block soft reauthentication")
        } catch MatrixServiceError.storageUnavailable {}
        XCTAssertTrue(authentication.loginCalls.isEmpty)
        XCTAssertEqual(try store.load(), saved)
        XCTAssertEqual(store.saveCalls, saveCallsAfterFailedClear)
        XCTAssertFalse(FileManager.default.fileExists(atPath: directory.url(for: saved.storeID).path))

        try await service.clearLocalSession()
        XCTAssertNil(try store.load())
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

    func testPushTokenRotationDeletesOldPusherBeforeSettingNewAndLogoutDeletesKnownKey() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let transport = RecordingPusherTransport()
        let service = MatrixService(sessionStore: InMemorySessionStore(), storeDirectory: SessionStoreDirectory(baseURL: temporary), discovery: PasswordDiscovery(), monitorNetwork: false, passwordAuthentication: FakePasswordAuthentication(), pusherTransport: transport)
        service.clientFactory = { _, _, _ in FakeClient() }
        try await service.configurePush(notifyURL: "https://sygnal.example.invalid/_matrix/push/v1/notify", deviceDisplayName: "test")
        try await service.login(homeserver: "https://matrix.test", username: "push", password: "test-password")
        service.receivePushDeviceToken(Data([1]))
        try await waitUntil { transport.calls.count == 1 }
        XCTAssertEqual(service.pushToken, "AQ==")
        service.receivePushDeviceToken(Data([2]))
        try await waitUntil { transport.calls.count == 3 }
        XCTAssertEqual(service.pushToken, "Ag==")
        let calls = transport.calls
        XCTAssertEqual(calls.map(\.pushKey), ["AQ==", "AQ==", "Ag=="])
        XCTAssertEqual(calls.map(\.kind), ["http", nil, "http"])

        await service.logout()
        try await waitUntil { transport.calls.count == 4 }
        XCTAssertEqual(transport.calls.last?.kind, nil)
        XCTAssertEqual(transport.calls.last?.pushKey, "Ag==")
    }

    func testPushLogoutWaitsForSuspendedSetAndNeverSetsRotatedTokenAfterLogout() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let transport = BlockingPusherTransport()
        let service = MatrixService(sessionStore: InMemorySessionStore(), storeDirectory: SessionStoreDirectory(baseURL: temporary), discovery: PasswordDiscovery(), monitorNetwork: false, passwordAuthentication: FakePasswordAuthentication(), pusherTransport: transport)
        service.clientFactory = { _, _, _ in FakeClient() }
        try await service.configurePush(notifyURL: "https://sygnal.example.invalid/_matrix/push/v1/notify", deviceDisplayName: "test")
        try await service.login(homeserver: "https://matrix.test", username: "push-race", password: "test-password")
        service.receivePushDeviceToken(Data([1]))
        try await waitUntil { transport.isFirstSetBlocked }
        service.receivePushDeviceToken(Data([2]))
        let started = expectation(description: "logout has entered the main actor")
        let logout = Task {
            started.fulfill()
            await service.logout()
        }
        await fulfillment(of: [started], timeout: 1)
        transport.releaseFirstSet()
        await logout.value
        XCTAssertFalse(transport.calls.contains { $0.kind == "http" && $0.pushKey == "Ag==" })
        XCTAssertTrue(transport.calls.contains { $0.kind == nil && $0.pushKey == "AQ==" })
        XCTAssertFalse(service.isLoggedIn)
    }

    func testPushDeletionFailurePreventsReplacementAndPreservesSessionUntilRetry() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let transport = RecordingPusherTransport()
        let store = InMemorySessionStore()
        let service = MatrixService(sessionStore: store, storeDirectory: SessionStoreDirectory(baseURL: temporary), discovery: PasswordDiscovery(), monitorNetwork: false, passwordAuthentication: FakePasswordAuthentication(), pusherTransport: transport)
        service.clientFactory = { _, _, _ in FakeClient() }
        try await service.configurePush(notifyURL: "https://sygnal.example.invalid/_matrix/push/v1/notify", deviceDisplayName: "test")
        try await service.login(homeserver: "https://matrix.example.invalid", username: "push-failure", password: "test-password")
        service.receivePushDeviceToken(Data([1]))
        try await waitUntil { transport.calls.count == 1 }
        transport.rejectDeletions = true
        service.receivePushDeviceToken(Data([2]))
        try await waitUntil { service.pushRegistrationError != nil }
        XCTAssertEqual(transport.calls.map(\.kind), ["http", nil])
        XCTAssertEqual(transport.calls.map(\.pushKey), ["AQ==", "AQ=="])
        await service.logout()
        XCTAssertTrue(service.isLoggedIn)
        XCTAssertNotNil(try store.load())
        XCTAssertNotNil(service.pushRegistrationError)
        transport.rejectDeletions = false
        await service.logout()
        XCTAssertFalse(service.isLoggedIn)
        XCTAssertNil(try store.load())
        XCTAssertFalse(transport.calls.contains { $0.kind == "http" && $0.pushKey == "Ag==" })
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
        client.suspendSyncUntilReleased = true
        let initialSync = expectation(description: "initial sync started")
        let recoveredSync = expectation(description: "network recovery sync started")
        client.onSync = { [weak client] in
            switch client?.syncCalls {
            case 1: initialSync.fulfill()
            case 2: recoveredSync.fulfill()
            default: XCTFail("unexpected additional sync")
            }
        }
        let service = makeService(sessionStore: InMemorySessionStore(), directory: temporary, client: client)
        try await service.login(homeserver: "https://matrix.test", username: "foreground", password: "test-password")
        await fulfillment(of: [initialSync], timeout: 5)
        XCTAssertEqual(client.syncCalls, 1)
        XCTAssertEqual(client.cancelledSuspendedSyncCalls, 0)
        let initialEnableCount = client.queueEnableValues.filter { $0 }.count
        XCTAssertEqual(initialEnableCount, 1)

        // Keep the same request in flight: an immediately successful fake can
        // legitimately start another loop iteration before a timed assertion.
        await service.applicationDidBecomeActive()
        XCTAssertEqual(client.syncCalls, 1)
        XCTAssertEqual(client.cancelledSuspendedSyncCalls, 0, "foreground must preserve the in-flight sync")
        XCTAssertEqual(client.queueEnableValues.filter { $0 }.count, initialEnableCount)

        await service.networkChanged(available: false)
        XCTAssertEqual(client.syncCalls, 1)
        XCTAssertEqual(client.cancelledSuspendedSyncCalls, 0)
        await service.networkChanged(available: true)
        await fulfillment(of: [recoveredSync], timeout: 5)
        XCTAssertEqual(client.syncCalls, 2)
        XCTAssertEqual(client.cancelledSuspendedSyncCalls, 1, "network recovery must drain the previous sync")
        XCTAssertEqual(client.queueEnableValues.filter { $0 }.count, initialEnableCount)
        // Also exercise the successful-sync path: recovery must not enable a
        // queue when its response is applied, not just when the request starts.
        let connected = expectation(description: "recovery response applied")
        let observation = service.$connectionStatus.filter { $0 == .connected }.prefix(1).sink { _ in connected.fulfill() }
        client.onSync = nil
        client.releaseSuspendedSync()
        await fulfillment(of: [connected], timeout: 5)
        withExtendedLifetime(observation) {
            XCTAssertEqual(client.queueEnableValues.filter { $0 }.count, initialEnableCount)
        }
        await service.logout()
        XCTAssertFalse(client.isSyncSuspended, "logout must release any subsequent long poll")
    }

    func testSyncNotificationMappingPreservesPrivacyAndIdentity() throws {
        var notification = syncNotification(eventID: "$private", body: "decrypted text", timestamp: 1_700_000_000_000)
        notification.roomInfo.isEncrypted = true
        let candidate = try XCTUnwrap(ForegroundSyncNotificationBridge.candidate(
            notification: notification, roomID: "!push:matrix.test", userID: "@me:matrix.test"))
        XCTAssertEqual(candidate.preview, "加密消息")
        XCTAssertEqual(candidate.eventID, "$private")
        XCTAssertNil(ForegroundSyncNotificationBridge.candidate(
            notification: notification, roomID: "!push:matrix.test", userID: "@sender:matrix.test"))
        XCTAssertNil(ForegroundSyncNotificationBridge.candidate(
            notification: notification, roomID: "!different:matrix.test", userID: "@me:matrix.test"))
        notification.event = .invite(sender: "@sender:matrix.test")
        XCTAssertNil(ForegroundSyncNotificationBridge.candidate(
            notification: notification, roomID: "!push:matrix.test", userID: "@me:matrix.test"))
    }

    func testSyncNotificationsEmitEveryCurrentEventWithoutLatestEventReadback() async throws {
        let temporary = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: temporary) }
        let credentials = testCredentials(userID: "@me:matrix.test")
        _ = try SessionStoreDirectory(baseURL: temporary).create(for: credentials.storeID)
        let room = FakeRoom(id: "!push:matrix.test", name: "Push")
        let client = FakeClient(session: fakeSession(userID: "@me:matrix.test", deviceID: "C21TEST"), successfulSync: true, rooms: [room])
        let service = makeService(sessionStore: InMemorySessionStore(credentials), directory: temporary, client: client)
        await service.restoreSession()
        let listener = try XCTUnwrap(client.notificationListener, "sync notification handler must be registered during activation")
        try await waitUntil { service.connectionStatus == .connected }
        var received = [ForegroundBannerCandidate]()
        let expected = expectation(description: "six current sync events")
        expected.expectedFulfillmentCount = 6
        let observation = service.foregroundBannerCandidates.sink { candidate in
            received.append(candidate)
            expected.fulfill()
        }
        // Three ten-second gaps, then three events inside two seconds.  These
        // are Matrix millisecond timestamps, not wall-clock test delays.
        for (index, offset) in [10_000, 20_000, 30_000, 40_000, 41_000, 41_900].enumerated() {
            client.emit(notification: syncNotification(eventID: "$event-\(index)", body: "body-\(index)", timestamp: 1_700_000_000_000 + UInt64(offset)),
                        roomID: "!push:matrix.test")
        }
        await fulfillment(of: [expected], timeout: 1)
        withExtendedLifetime((listener, observation)) {
            XCTAssertEqual(received.map(\.eventID), (0 ..< 6).map { "$event-\($0)" })
            XCTAssertEqual(received.map(\.preview), (0 ..< 6).map { "body-\($0)" })
        }
        await service.logout()
    }

    func testInitialSyncNotificationIsHistoryButNextNotificationIsLive() async throws {
        let temporary = try makeTemporaryDirectory(); defer { try? FileManager.default.removeItem(at: temporary) }
        let credentials = testCredentials(userID: "@me:matrix.test")
        _ = try SessionStoreDirectory(baseURL: temporary).create(for: credentials.storeID)
        let client = FakeClient(session: fakeSession(userID: "@me:matrix.test", deviceID: "C21TEST"), successfulSync: true,
                                rooms: [FakeRoom(id: "!push:matrix.test", name: "Push")])
        client.onSync = { [weak client] in
            client?.emit(notification: syncNotification(eventID: "$history", body: "history", timestamp: 1_700_000_000_000), roomID: "!push:matrix.test")
            client?.onSync = nil
        }
        let service = makeService(sessionStore: InMemorySessionStore(credentials), directory: temporary, client: client)
        var received = [ForegroundBannerCandidate]()
        let observation = service.foregroundBannerCandidates.sink { received.append($0) }
        await service.restoreSession()
        try await waitUntil { service.connectionStatus == .connected }
        XCTAssertTrue(received.isEmpty, "first sync notification is history")
        client.emit(notification: syncNotification(eventID: "$live", body: "live", timestamp: 1_700_000_001_000), roomID: "!push:matrix.test")
        try await waitUntil { received.count == 1 }
        withExtendedLifetime(observation) { XCTAssertEqual(received.first?.eventID, "$live") }
        await service.logout()
    }

    func testBackgroundSuppressesOnlyDeliveredEventIDsAndAllowsSameTimestampNewEvents() async throws {
        let temporary = try makeTemporaryDirectory(); defer { try? FileManager.default.removeItem(at: temporary) }
        let credentials = testCredentials(userID: "@me:matrix.test")
        _ = try SessionStoreDirectory(baseURL: temporary).create(for: credentials.storeID)
        let client = FakeClient(session: fakeSession(userID: "@me:matrix.test", deviceID: "C21TEST"), successfulSync: true,
                                rooms: [FakeRoom(id: "!push:matrix.test", name: "Push")])
        let service = makeService(sessionStore: InMemorySessionStore(credentials), directory: temporary, client: client)
        var received = [ForegroundBannerCandidate]()
        let observation = service.foregroundBannerCandidates.sink { received.append($0) }
        await service.restoreSession(); try await waitUntil { service.connectionStatus == .connected }
        service.applicationDidEnterBackground()
        client.emit(notification: syncNotification(eventID: "$background", body: "background", timestamp: 1_700_000_010_000), roomID: "!push:matrix.test")
        service.deliveredBannerEvents = { _ in
            try? await Task.sleep(for: .milliseconds(20))
            return [PushEventReference(roomID: "!push:matrix.test", eventID: "$delivered-only")]
        }
        await service.applicationDidBecomeActive()
        // This event was delivered by NSE while suspended and has never gone
        // through the app's bridge. It must await reconciliation and be consumed.
        client.emit(notification: syncNotification(eventID: "$delivered-only", body: "old", timestamp: 1_700_000_010_000), roomID: "!push:matrix.test")
        client.emit(notification: syncNotification(eventID: "$new-now", body: "new-now", timestamp: 1_700_000_010_000), roomID: "!push:matrix.test")
        client.emit(notification: syncNotification(eventID: "$new-later", body: "new-later", timestamp: 1_700_000_020_000), roomID: "!push:matrix.test")
        try await waitUntil { received.count == 2 }
        withExtendedLifetime(observation) { XCTAssertEqual(received.map(\.eventID), ["$new-now", "$new-later"]) }
        let staleListener = try XCTUnwrap(client.notificationListener)
        await service.logout()
        staleListener.onNotification(notification: syncNotification(eventID: "$stale", body: "stale", timestamp: 1_700_000_030_000), roomId: "!push:matrix.test")
        try await Task.sleep(for: .milliseconds(10))
        XCTAssertEqual(received.count, 2)
    }

    func testConcurrentFakeSyncAndQueueRecordingKeepsEveryEvent() async throws {
        let client = FakeClient(successfulSync: true)
        try await withThrowingTaskGroup(of: Void.self) { group in
            for _ in 0 ..< 200 {
                group.addTask {
                    await client.enableAllSendQueues(enable: false)
                    _ = try await client.syncOnceV2(settings: SyncSettingsV2(timeoutMs: 0))
                }
            }
            try await group.waitForAll()
        }
        XCTAssertEqual(client.syncCalls, 200)
        XCTAssertEqual(client.queueEnableValues, Array(repeating: false, count: 200))
        XCTAssertEqual(client.lifecycleEvents.filter { $0 == "sync" }.count, 200)
        XCTAssertEqual(client.lifecycleEvents.filter { $0 == "enable-false" }.count, 200)
    }

    func testBlockedFakeSyncHasOneReleaseOwner() async throws {
        let client = FakeClient(successfulSync: true)
        let blocked = expectation(description: "continuation installed before concurrent release")
        client.onSyncBlocked = { blocked.fulfill() }
        client.blockNextSync = true
        let sync = Task { try await client.syncOnceV2(settings: SyncSettingsV2(timeoutMs: 0)) }
        await fulfillment(of: [blocked], timeout: 5)
        await withTaskGroup(of: Void.self) { group in
            for _ in 0 ..< 32 { group.addTask { client.releaseBlockedSync() } }
        }
        _ = try await sync.value
        XCTAssertFalse(client.isSyncBlocked)
        XCTAssertEqual(client.syncCalls, 1)
    }

    func testNotificationDuringCancelledLongPollDrainStillPublishes() async throws {
        let temporary = try makeTemporaryDirectory(); defer { try? FileManager.default.removeItem(at: temporary) }
        let credentials = testCredentials(userID: "@me:matrix.test")
        _ = try SessionStoreDirectory(baseURL: temporary).create(for: credentials.storeID)
        let client = FakeClient(session: fakeSession(userID: "@me:matrix.test", deviceID: "C21TEST"), successfulSync: true,
                                rooms: [FakeRoom(id: "!push:matrix.test", name: "Push")])
        let service = makeService(sessionStore: InMemorySessionStore(credentials), directory: temporary, client: client)
        var received = [ForegroundBannerCandidate]()
        let published = expectation(description: "notification published during drain")
        let observation = service.foregroundBannerCandidates.sink { received.append($0); published.fulfill() }
        let connected = expectation(description: "initial sync completed")
        let connection = service.$connectionStatus.filter { $0 == .connected }.prefix(1).sink { _ in connected.fulfill() }
        await service.restoreSession()
        await fulfillment(of: [connected], timeout: 5)
        let blocked = expectation(description: "long-poll continuation installed")
        let cancelled = expectation(description: "long poll cancelled but not drained")
        client.onSyncBlocked = { blocked.fulfill() }
        client.onBlockedSyncCancelled = { cancelled.fulfill() }
        client.blockNextSync = true
        await fulfillment(of: [blocked], timeout: 5)
        defer { client.releaseBlockedSync() }
        service.applicationDidEnterBackground()
        let replacement = Task { @MainActor in try await service.sync() }
        await fulfillment(of: [cancelled], timeout: 5)
        XCTAssertTrue(client.isSyncBlocked, "FFI still draining after cancellation")
        await service.applicationDidBecomeActive()
        client.emit(notification: syncNotification(eventID: "$during-drain", body: "during-drain", timestamp: 1_700_000_040_000), roomID: "!push:matrix.test")
        await fulfillment(of: [published], timeout: 5)
        client.releaseBlockedSync()
        _ = try await replacement.value
        withExtendedLifetime((observation, connection)) { XCTAssertEqual(received.first?.eventID, "$during-drain") }
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
                             authentication: FakePasswordAuthentication = FakePasswordAuthentication(),
                             sharedPushCredentialsStore: any SharedPushCredentialsStore = InMemorySharedPushCredentialsStore(),
                             sharedPushCredentialsCleanupStateStore: any SharedPushCredentialsCleanupStateStore = InMemorySharedPushCredentialsCleanupStateStore(),
                             pairingAuthentication: any PairingCredentialAuthenticating = MatrixPairingCredentialAuthentication()) -> MatrixService {
        let service = MatrixService(
            sessionStore: sessionStore,
            sharedPushCredentialsStore: sharedPushCredentialsStore,
            sharedPushCredentialsCleanupStateStore: sharedPushCredentialsCleanupStateStore,
            storeDirectory: SessionStoreDirectory(baseURL: directory),
            discovery: PasswordDiscovery(),
            monitorNetwork: false,
            passwordAuthentication: authentication,
            pairingAuthentication: pairingAuthentication,
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

private final class CountingPasswordDiscovery: LoginCapabilityDiscovering, @unchecked Sendable {
    private(set) var calls = 0

    func discover(at homeserver: URL) async throws -> LoginCapabilities {
        calls += 1
        return LoginCapabilities(supportsPassword: true)
    }
}

private final class RecordingPusherTransport: MatrixPusherTransport, @unchecked Sendable {
    private let lock = NSLock()
    private var storedCalls: [PushRegistration] = []
    private var rejects = false
    var calls: [PushRegistration] { lock.withLock { storedCalls } }
    var rejectDeletions: Bool {
        get { lock.withLock { rejects } }
        set { lock.withLock { rejects = newValue } }
    }

    func setPusher(homeserver: URL, accessToken: String, registration: PushRegistration) async throws {
        let reject = lock.withLock {
            storedCalls.append(registration)
            return rejects && registration.kind == nil
        }
        if reject { throw MatrixPusherTransportError.serverRejected }
    }

    func pushers(homeserver: URL, accessToken: String) async throws -> MatrixPushersResponse {
        MatrixPushersResponse(pushers: [])
    }
}

private final class BlockingPusherTransport: MatrixPusherTransport, @unchecked Sendable {
    private let lock = NSLock()
    private var storedCalls: [PushRegistration] = []
    private var continuation: CheckedContinuation<Void, Never>?
    private var didBlock = false
    var calls: [PushRegistration] { lock.withLock { storedCalls } }
    var isFirstSetBlocked: Bool { lock.withLock { didBlock && continuation != nil } }

    func setPusher(homeserver: URL, accessToken: String, registration: PushRegistration) async throws {
        let shouldBlock = lock.withLock {
            storedCalls.append(registration)
            guard !didBlock, registration.kind == "http" else { return false }
            didBlock = true
            return true
        }
        if shouldBlock {
            await withCheckedContinuation { continuation in
                lock.withLock { self.continuation = continuation }
            }
        }
    }

    func releaseFirstSet() {
        let pending = lock.withLock { let value = continuation; continuation = nil; return value }
        pending?.resume()
    }

    func pushers(homeserver: URL, accessToken: String) async throws -> MatrixPushersResponse {
        MatrixPushersResponse(pushers: [])
    }
}

private final class FakeClient: Client, @unchecked Sendable {
    private let configuredLogoutError: Error?
    private let configuredRestoreError: Error?
    @TestLocked var syncError: Error? = nil
    private let successfulSync: Bool
    private let probe: QueueProbe?
    private let configuredRooms: [Room]
    private let blockQueueEnable: Bool
    private let onSetDelegate: (() throws -> Void)?
    @TestLocked private(set) var syncCalls = 0
    @TestLocked private(set) var queueEnableValues: [Bool] = []
    @TestLocked private(set) var didLogout = false
    @TestLocked private(set) var restoreCalls = 0
    @TestLocked private(set) var restoredSession: Session? = nil
    @TestLocked private(set) var lifecycleEvents: [String] = []
    private let configuredSession: Session
    @TestLocked private var delegate: ClientDelegate? = nil
    @TestLocked private(set) var notificationListener: SyncNotificationListener? = nil
    @TestLocked var onSync: (@Sendable () -> Void)? = nil
    @TestLocked var suspendSyncUntilReleased = false
    @TestLocked var blockNextSync = false
    @TestLocked private var syncContinuation: CheckedContinuation<Void, Never>? = nil
    var isSyncBlocked: Bool { syncContinuation != nil }
    @TestLocked var onSyncBlocked: (@Sendable () -> Void)? = nil
    @TestLocked var onBlockedSyncCancelled: (@Sendable () -> Void)? = nil

    init(session: Session = fakeSession(userID: "@c21_alpha:matrix.test", deviceID: "C21TEST"), logoutError: Error? = nil, restoreError: Error? = nil, syncError: Error? = nil, successfulSync: Bool = false, probe: QueueProbe? = nil, rooms: [Room] = [], blockQueueEnable: Bool = false, onSetDelegate: (() throws -> Void)? = nil) {
        configuredLogoutError = logoutError
        configuredRestoreError = restoreError
        self._syncError = TestLocked(wrappedValue: syncError)
        self.successfulSync = successfulSync
        self.probe = probe
        configuredRooms = rooms
        self.blockQueueEnable = blockQueueEnable
        self.onSetDelegate = onSetDelegate
        configuredSession = session
        super.init(noHandle: .init())
    }

    required init(unsafeFromHandle handle: UInt64) {
        configuredLogoutError = nil
        configuredRestoreError = nil
        _syncError = TestLocked(wrappedValue: nil)
        successfulSync = false
        probe = nil
        configuredRooms = []
        blockQueueEnable = false
        onSetDelegate = nil
        configuredSession = fakeSession(userID: "@c21_unexpected:matrix.test", deviceID: "UNEXPECTED")
        super.init(unsafeFromHandle: handle)
    }

    override func login(username: String, password: String, initialDeviceName: String?, deviceId: String?) async throws {}

    override func session() throws -> Session {
        configuredSession
    }

    override func restoreSession(session: Session) async throws {
        restoreCalls += 1
        if let configuredRestoreError { throw configuredRestoreError }
        restoredSession = session
    }

    override func logout() async throws {
        if let configuredLogoutError { throw configuredLogoutError }
        didLogout = true
    }

    override func setDelegate(delegate: ClientDelegate?) throws -> TaskHandle? {
        try onSetDelegate?()
        self.delegate = delegate
        return nil
    }
    func emitAuthError(isSoftLogout: Bool) { delegate?.didReceiveAuthError(isSoftLogout: isSoftLogout) }
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
        if suspendSyncUntilReleased {
            // Each request owns its stream; cancellation ends iteration even
            // when it arrives before the iterator starts waiting.
            let (pending, continuation) = AsyncStream<Void>.makeStream()
            syncBlockLock.withLock { suspendedSyncContinuation = continuation }
            defer {
                continuation.finish()
                syncBlockLock.withLock { suspendedSyncContinuation = nil }
            }
            onSync?()
            await withTaskCancellationHandler {
                for await _ in pending {}
            } onCancel: {
                self.syncBlockLock.withLock { self.suspendedSyncCancellationCount += 1 }
            }
            try Task.checkCancellation()
        } else {
            onSync?()
        }
        let shouldBlock = $blockNextSync.withValue { value in
            let pending = value
            value = false
            return pending
        }
        if shouldBlock {
            await withTaskCancellationHandler {
                await withCheckedContinuation { continuation in
                    syncContinuation = continuation
                    onSyncBlocked?()
                }
            } onCancel: {
                self.syncBlockLock.withLock { self.blockedSyncCancelled = true }
                self.onBlockedSyncCancelled?()
            }
        }
        if let syncError { throw syncError }
        if successfulSync {
            return SyncResponseV2(nextBatch: "test", rooms: SyncResponseRoomsV2(invited: [], joined: [], left: [], knocked: []))
        }
        throw ClientError.Generic(msg: "offline", details: nil)
    }

    override func registerNotificationHandler(listener: SyncNotificationListener) async {
        notificationListener = listener
    }

    func emit(notification: NotificationItem, roomID: String) {
        notificationListener?.onNotification(notification: notification, roomId: roomID)
    }
    func releaseBlockedSync() {
        // Take ownership once, then resume outside the lock. A concurrent or
        // deferred cleanup cannot overwrite or resume the same continuation.
        let continuation = $syncContinuation.withValue { pending in
            let result = pending
            pending = nil
            return result
        }
        continuation?.resume()
    }
    private let syncBlockLock = NSLock()
    private var blockedSyncCancelled = false
    private var suspendedSyncCancellationCount = 0
    private var suspendedSyncContinuation: AsyncStream<Void>.Continuation?
    var isSyncSuspended: Bool { syncBlockLock.withLock { suspendedSyncContinuation != nil } }
    func releaseSuspendedSync() {
        let continuation = syncBlockLock.withLock { suspendedSyncContinuation }
        continuation?.finish()
    }
    var cancelledSuspendedSyncCalls: Int { syncBlockLock.withLock { suspendedSyncCancellationCount } }
    var isBlockedSyncCancelled: Bool { syncBlockLock.withLock { blockedSyncCancelled } }
}

private final class FakeRoom: Room, @unchecked Sendable {
    private let roomID: String
    private let roomName: String
    @TestLocked var latest: LatestEventValue = .none

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
    override func latestEvent() async -> LatestEventValue { latest }
    override func isDirect() async -> Bool { false }
    override func latestEncryptionState() async throws -> EncryptionState { .unknown }
}

private final class FakeTimelineEvent: TimelineEvent, @unchecked Sendable {
    private let id: String
    private let sender: String
    private let time: UInt64
    init(id: String, sender: String, timestamp: UInt64) {
        self.id = id
        self.sender = sender
        time = timestamp
        super.init(noHandle: .init())
    }
    required init(unsafeFromHandle handle: UInt64) { fatalError("test only") }
    override func eventId() -> String { id }
    override func senderId() -> String { sender }
    override func timestamp() -> Timestamp { time }
    override func content() throws -> TimelineEventContent { .messageLike(content: .sticker) }
}

private func syncNotification(eventID: String, body: String, timestamp: UInt64, roomID: String = "!push:matrix.test") -> NotificationItem {
    let event = FakeTimelineEvent(id: eventID, sender: "@sender:matrix.test", timestamp: timestamp)
    let raw = "{\"type\":\"m.sticker\",\"event_id\":\"\(eventID)\",\"room_id\":\"\(roomID)\",\"sender\":\"@sender:matrix.test\",\"content\":{\"body\":\"\(body)\"}}"
    return NotificationItem(event: .timeline(event: event), rawEvent: raw,
                            senderInfo: NotificationSenderInfo(displayName: "Sender", avatarUrl: nil, isNameAmbiguous: false),
                            roomInfo: NotificationRoomInfo(displayName: "Push", avatarUrl: nil, canonicalAlias: nil, topic: nil, joinRule: nil,
                                                           joinedMembersCount: 2, activeServiceMembersCount: 0, serviceMembers: [], isEncrypted: false,
                                                           isDirect: true, isSpace: false, isDm: true),
                            isNoisy: true, hasMention: false, threadId: nil, actions: nil)
}

private final class QueueProbe: @unchecked Sendable { @TestLocked var values: [Bool] = [] }

private final class FakePasswordAuthentication: PasswordAuthenticating, @unchecked Sendable {
    struct LoginCall: Equatable {
        let homeserver: String
        let username: String
        let deviceID: String?
    }
    var error: Error?
    var discardCalls = 0
    private(set) var loginCalls: [LoginCall] = []
    var sessionForLogin: ((String) -> Session) = { username in
        fakeSession(userID: username.hasPrefix("@") ? username : "@\(username):matrix.test", deviceID: "C21TEST")
    }

    func login(at server: URL, username: String, password: String, deviceID: String?) async throws -> Session {
        loginCalls.append(LoginCall(homeserver: server.absoluteString, username: username, deviceID: deviceID))
        if let error { throw error }
        return sessionForLogin(username)
    }
    func discard(_ session: Session) async { discardCalls += 1 }
}

private final class LatePasswordAuthentication: PasswordAuthenticating, @unchecked Sendable {
    private(set) var discardCalls = 0
    private let lock = NSLock()
    private var pending: CheckedContinuation<Session, Never>?
    func login(at server: URL, username: String, password: String, deviceID: String?) async throws -> Session {
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

private final class FailOnceSaveSessionStore: SessionStore, @unchecked Sendable {
    private let lock = NSLock()
    private var value: SessionCredentials?
    var failNextSave = false

    init(_ value: SessionCredentials) { self.value = value }

    func load() throws -> SessionCredentials? { lock.withLock { value } }
    func save(_ credentials: SessionCredentials) throws {
        try lock.withLock {
            if failNextSave {
                failNextSave = false
                throw SessionStoreError.unavailable
            }
            value = credentials
        }
    }
    func delete() throws { lock.withLock { value = nil } }
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

private final class FailFirstDeleteSessionStore: SessionStore, @unchecked Sendable {
    private var value: SessionCredentials?
    private var shouldFailDelete = true
    private(set) var saveCalls = 0

    init(_ value: SessionCredentials) { self.value = value }

    func load() throws -> SessionCredentials? { value }
    func save(_ credentials: SessionCredentials) throws {
        saveCalls += 1
        value = credentials
    }
    func delete() throws {
        if shouldFailDelete {
            shouldFailDelete = false
            throw SessionStoreError.unavailable
        }
        value = nil
    }
}

private final class UnavailableSharedPushCredentialsStore: SharedPushCredentialsStore, @unchecked Sendable {
    func load() throws -> SharedPushCredentials? { throw SharedPushCredentialsStoreError.unavailable }
    func save(_ credentials: SharedPushCredentials) throws { throw SharedPushCredentialsStoreError.unavailable }
    func delete() throws { throw SharedPushCredentialsStoreError.unavailable }
}

private final class FailOnceDeleteSharedPushCredentialsStore: SharedPushCredentialsStore, @unchecked Sendable {
    private var value: SharedPushCredentials?
    private var shouldFailDelete = true
    private(set) var deleteCalls = 0

    func load() throws -> SharedPushCredentials? { value }
    func save(_ credentials: SharedPushCredentials) throws { value = credentials }
    func delete() throws {
        deleteCalls += 1
        if shouldFailDelete {
            shouldFailDelete = false
            throw SharedPushCredentialsStoreError.unavailable
        }
        value = nil
    }
}

private final class InMemorySharedPushCredentialsCleanupStateStore: SharedPushCredentialsCleanupStateStore, @unchecked Sendable {
    var isCleanupRequired = false
    func setCleanupRequired(_ required: Bool) { isCleanupRequired = required }
}

private final class FakePairingAuthentication: PairingCredentialAuthenticating, @unchecked Sendable {
    private let session: Session
    init(session: Session) { self.session = session }
    func register(at homeserver: URL, registrationToken: String, username: String, password: String,
                  deviceDisplayName: String, deadline: Date) async throws -> Session { session }
    func login(at homeserver: URL, loginToken: String, deviceDisplayName: String, deadline: Date) async throws -> Session { session }
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
