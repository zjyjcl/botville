// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

@testable import BotvilleCore
import Foundation
import XCTest

final class KeychainSessionStoreTests: XCTestCase {
    func testSaveUpdatesExistingItemWithRequiredAccessibility() throws {
        let backend = FakeKeychainBackend(updateResult: .success)
        let store = KeychainSessionStore(service: "test.update", account: "session", backend: backend)

        try store.save(credentials())

        XCTAssertEqual(backend.updateCalls, 1)
        XCTAssertEqual(backend.addCalls, 0)
        XCTAssertEqual(backend.accessibilities, [.afterFirstUnlockThisDeviceOnly])
    }

    func testSaveAddsWhenUpdateDoesNotFindItem() throws {
        let backend = FakeKeychainBackend(updateResult: .itemNotFound, addResult: .success)
        let store = KeychainSessionStore(service: "test.add", account: "session", backend: backend)

        try store.save(credentials())

        XCTAssertEqual(backend.updateCalls, 1)
        XCTAssertEqual(backend.addCalls, 1)
        XCTAssertEqual(backend.accessibilities, [.afterFirstUnlockThisDeviceOnly, .afterFirstUnlockThisDeviceOnly])
    }

    func testSaveFailsWhenUpdateFails() {
        let backend = FakeKeychainBackend(updateResult: .failure)
        let store = KeychainSessionStore(service: "test.failure", account: "session", backend: backend)
        XCTAssertThrowsError(try store.save(credentials())) { XCTAssertEqual($0 as? SessionStoreError, .unavailable) }
        XCTAssertEqual(backend.addCalls, 0)
    }

    func testSaveFailsWhenAddAfterMissingUpdateFails() {
        let backend = FakeKeychainBackend(updateResult: .itemNotFound, addResult: .failure)
        let store = KeychainSessionStore(service: "test.add-failure", account: "session", backend: backend)
        XCTAssertThrowsError(try store.save(credentials())) { XCTAssertEqual($0 as? SessionStoreError, .unavailable) }
        XCTAssertEqual(backend.updateCalls, 1)
        XCTAssertEqual(backend.addCalls, 1)
    }

    func testLoadDeleteAndMalformedItem() throws {
        let encoded = try JSONEncoder().encode(credentials())
        let backend = FakeKeychainBackend(loadResult: (.success, encoded), deleteResult: .success)
        let store = KeychainSessionStore(service: "test.load", account: "session", backend: backend)
        XCTAssertEqual(try store.load(), credentials())
        try store.delete()
        XCTAssertEqual(backend.deleteCalls, 1)

        backend.loadResult = (.success, Data("not-json".utf8))
        XCTAssertThrowsError(try store.load()) { XCTAssertEqual($0 as? SessionStoreError, .malformedItem) }
    }

    func testLoadNotFoundAndDeleteNotFoundAreSuccessful() throws {
        let backend = FakeKeychainBackend(loadResult: (.itemNotFound, nil), deleteResult: .itemNotFound)
        let store = KeychainSessionStore(service: "test.none", account: "session", backend: backend)
        XCTAssertNil(try store.load())
        XCTAssertNoThrow(try store.delete())
    }

    func testLoadFailureOrSuccessWithoutDataAreUnavailable() {
        for result in [(KeychainBackendStatus.failure, Optional<Data>.none), (.success, Optional<Data>.none)] {
            let backend = FakeKeychainBackend(loadResult: result)
            let store = KeychainSessionStore(service: "test.load-failure", account: "session", backend: backend)
            XCTAssertThrowsError(try store.load()) { XCTAssertEqual($0 as? SessionStoreError, .unavailable) }
        }
    }

    func testDeleteFailureIsUnavailable() {
        let backend = FakeKeychainBackend(deleteResult: .failure)
        let store = KeychainSessionStore(service: "test.delete-failure", account: "session", backend: backend)
        XCTAssertThrowsError(try store.delete()) { XCTAssertEqual($0 as? SessionStoreError, .unavailable) }
        XCTAssertEqual(backend.deleteCalls, 1)
    }

    func testCreateFailureRemovesOnlyNewSessionDirectory() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("botville-session-store-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let storeID = UUID()
        let directory = SessionStoreDirectory(baseURL: root)
        let fileManager = FailingAttributesFileManager(failingPath: directory.url(for: storeID).path)

        XCTAssertThrowsError(try directory.create(for: storeID, fileManager: fileManager))
        XCTAssertFalse(FileManager.default.fileExists(atPath: directory.url(for: storeID).path))
    }

    func testCreateFailurePreservesExistingSessionDirectory() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("botville-session-store-existing-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let storeID = UUID()
        let directory = SessionStoreDirectory(baseURL: root)
        let existing = directory.url(for: storeID)
        try FileManager.default.createDirectory(at: existing, withIntermediateDirectories: true)
        let fileManager = FailingAttributesFileManager(failingPath: existing.path)

        XCTAssertThrowsError(try directory.create(for: storeID, fileManager: fileManager))
        XCTAssertTrue(FileManager.default.fileExists(atPath: existing.path))
    }

    #if os(macOS)
    func testRealKeychainRoundTripUsesUniqueServiceAndDeletesIt() throws {
        let service = "org.botville.tests.session.\(UUID().uuidString)"
        let store = KeychainSessionStore(service: service, account: "round-trip")
        let expected = credentials()
        defer { try? store.delete() }
        do { try store.save(expected) }
        catch SessionStoreError.unavailable {
            throw XCTSkip("当前 macOS 测试进程无法访问 Keychain；唯一测试 service 已在 defer 中尝试删除。")
        } catch {
            XCTFail("首次 Keychain 保存出现意外错误：\(error)")
            return
        }
        XCTAssertEqual(try store.load(), expected)
        var replacement = credentials()
        replacement = SessionCredentials(accessToken: "test-access-updated", refreshToken: replacement.refreshToken, userID: replacement.userID, deviceID: replacement.deviceID, homeserverURL: replacement.homeserverURL, oauthData: replacement.oauthData, storeID: replacement.storeID, storeKey: replacement.storeKey)
        try store.save(replacement)
        XCTAssertEqual(try store.load(), replacement)
        try store.delete()
        XCTAssertNil(try store.load())
    }
    #endif

    private func credentials() -> SessionCredentials {
        SessionCredentials(accessToken: "test-access", refreshToken: "test-refresh", userID: "@test:example.invalid", deviceID: "TEST", homeserverURL: "https://matrix.example.invalid", oauthData: nil, storeID: UUID(uuidString: "00000000-0000-0000-0000-000000000001")!, storeKey: Data(repeating: 1, count: 32))
    }
}

private final class FailingAttributesFileManager: FileManager {
    private let failingPath: String

    init(failingPath: String) {
        self.failingPath = failingPath
        super.init()
    }

    override func setAttributes(_ attributes: [FileAttributeKey: Any], ofItemAtPath path: String) throws {
        if path == failingPath { throw CocoaError(.fileWriteUnknown) }
        try super.setAttributes(attributes, ofItemAtPath: path)
    }
}

private final class FakeKeychainBackend: KeychainBackend, @unchecked Sendable {
    var loadResult: (KeychainBackendStatus, Data?)
    let updateResult: KeychainBackendStatus
    let addResult: KeychainBackendStatus
    let deleteResult: KeychainBackendStatus
    private(set) var updateCalls = 0
    private(set) var addCalls = 0
    private(set) var deleteCalls = 0
    private(set) var accessibilities: [KeychainAccessibility] = []

    init(loadResult: (KeychainBackendStatus, Data?) = (.itemNotFound, nil), updateResult: KeychainBackendStatus = .itemNotFound, addResult: KeychainBackendStatus = .success, deleteResult: KeychainBackendStatus = .success) {
        self.loadResult = loadResult
        self.updateResult = updateResult
        self.addResult = addResult
        self.deleteResult = deleteResult
    }

    func load(service: String, account: String) -> (KeychainBackendStatus, Data?) { loadResult }
    func update(data: Data, service: String, account: String, accessibility: KeychainAccessibility) -> KeychainBackendStatus {
        updateCalls += 1
        accessibilities.append(accessibility)
        return updateResult
    }
    func add(data: Data, service: String, account: String, accessibility: KeychainAccessibility) -> KeychainBackendStatus {
        addCalls += 1
        accessibilities.append(accessibility)
        return addResult
    }
    func delete(service: String, account: String) -> KeychainBackendStatus {
        deleteCalls += 1
        return deleteResult
    }
}
