// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import XCTest
@testable import BotvilleCore

final class SharedPushCredentialsTests: XCTestCase {
    private let credentials = SharedPushCredentials(
        homeserverURL: "https://matrix.example.test",
        userID: "@alice:example.test",
        accessToken: "test-access-token",
        deviceID: "BOTVILLE"
    )

    func testInMemoryStoreRoundTripsMinimumNSECredentials() throws {
        let store = InMemorySharedPushCredentialsStore()
        XCTAssertNil(try store.load())

        try store.save(credentials)

        XCTAssertEqual(try store.load(), credentials)
    }

    func testInMemoryStoreDeleteClearsCredentials() throws {
        let store = InMemorySharedPushCredentialsStore(credentials)

        try store.delete()

        XCTAssertNil(try store.load())
    }

    func testKeychainStoreAddsWhenUpdateDoesNotFindItem() throws {
        let backend = FakeSharedPushCredentialsKeychainBackend(updateResult: .itemNotFound, addResult: .success)
        let store = KeychainSharedPushCredentialsStore(accessGroup: "group.test.shared", backend: backend)

        try store.save(credentials)

        XCTAssertEqual(backend.updateCalls, 1)
        XCTAssertEqual(backend.addCalls, 1)
        XCTAssertEqual(backend.accessGroups, ["group.test.shared", "group.test.shared"])
        let savedData = try XCTUnwrap(backend.savedData)
        XCTAssertEqual(try JSONDecoder().decode(SharedPushCredentials.self, from: savedData), credentials)
        let object = try XCTUnwrap(try JSONSerialization.jsonObject(with: savedData) as? [String: Any])
        XCTAssertEqual(Set(object.keys), ["homeserverURL", "userID", "accessToken", "deviceID"])
    }

    func testKeychainStoreRejectsMalformedItem() {
        let backend = FakeSharedPushCredentialsKeychainBackend(loadResult: (.success, Data("not-json".utf8)))
        let store = KeychainSharedPushCredentialsStore(accessGroup: "group.test.shared", backend: backend)

        XCTAssertThrowsError(try store.load()) { XCTAssertEqual($0 as? SharedPushCredentialsStoreError, .malformedItem) }
    }

    func testKeychainStoreRejectsMissingAccessGroupBeforeBackendUse() {
        let backend = FakeSharedPushCredentialsKeychainBackend()
        let store = KeychainSharedPushCredentialsStore(accessGroup: "  ", backend: backend)

        XCTAssertThrowsError(try store.save(credentials)) { XCTAssertEqual($0 as? SharedPushCredentialsStoreError, .unavailable) }
        XCTAssertEqual(backend.updateCalls, 0)
    }

    func testKeychainStoreRejectsBackendLoadFailureOrMissingData() {
        for result in [(SharedPushCredentialsKeychainStatus.failure, Optional<Data>.none), (.success, Optional<Data>.none)] {
            let backend = FakeSharedPushCredentialsKeychainBackend(loadResult: result)
            let store = KeychainSharedPushCredentialsStore(accessGroup: "group.test.shared", backend: backend)
            XCTAssertThrowsError(try store.load()) { XCTAssertEqual($0 as? SharedPushCredentialsStoreError, .unavailable) }
        }
    }

    func testKeychainStoreReportsDeleteFailure() {
        let backend = FakeSharedPushCredentialsKeychainBackend(deleteResult: .failure)
        let store = KeychainSharedPushCredentialsStore(accessGroup: "group.test.shared", backend: backend)

        XCTAssertThrowsError(try store.delete()) { XCTAssertEqual($0 as? SharedPushCredentialsStoreError, .unavailable) }
        XCTAssertEqual(backend.deleteCalls, 1)
    }

    func testKeychainStoreHandlesUpdateAddAndDeleteStatuses() throws {
        let updateFailure = FakeSharedPushCredentialsKeychainBackend(updateResult: .failure)
        let updateFailureStore = KeychainSharedPushCredentialsStore(accessGroup: "group.test.shared", backend: updateFailure)
        XCTAssertThrowsError(try updateFailureStore.save(credentials)) { XCTAssertEqual($0 as? SharedPushCredentialsStoreError, .unavailable) }
        XCTAssertEqual(updateFailure.addCalls, 0)

        let addFailure = FakeSharedPushCredentialsKeychainBackend(updateResult: .itemNotFound, addResult: .failure)
        let addFailureStore = KeychainSharedPushCredentialsStore(accessGroup: "group.test.shared", backend: addFailure)
        XCTAssertThrowsError(try addFailureStore.save(credentials)) { XCTAssertEqual($0 as? SharedPushCredentialsStoreError, .unavailable) }
        XCTAssertEqual(addFailure.addCalls, 1)

        let absent = FakeSharedPushCredentialsKeychainBackend(deleteResult: .itemNotFound)
        let absentStore = KeychainSharedPushCredentialsStore(accessGroup: "group.test.shared", backend: absent)
        XCTAssertNoThrow(try absentStore.delete())
    }

    func testCredentialDescriptionsRedactAccessToken() {
        XCTAssertFalse(credentials.description.contains(credentials.accessToken))
        XCTAssertFalse(credentials.debugDescription.contains(credentials.accessToken))
        XCTAssertFalse(credentials.description.contains(credentials.userID))
        XCTAssertFalse(credentials.debugDescription.contains(credentials.deviceID))
    }
}

private final class FakeSharedPushCredentialsKeychainBackend: SharedPushCredentialsKeychainBackend, @unchecked Sendable {
    var loadResult: (SharedPushCredentialsKeychainStatus, Data?)
    let updateResult: SharedPushCredentialsKeychainStatus
    let addResult: SharedPushCredentialsKeychainStatus
    let deleteResult: SharedPushCredentialsKeychainStatus
    private(set) var updateCalls = 0
    private(set) var addCalls = 0
    private(set) var deleteCalls = 0
    private(set) var accessGroups: [String] = []
    private(set) var savedData: Data?

    init(loadResult: (SharedPushCredentialsKeychainStatus, Data?) = (.itemNotFound, nil), updateResult: SharedPushCredentialsKeychainStatus = .itemNotFound, addResult: SharedPushCredentialsKeychainStatus = .success, deleteResult: SharedPushCredentialsKeychainStatus = .success) {
        self.loadResult = loadResult
        self.updateResult = updateResult
        self.addResult = addResult
        self.deleteResult = deleteResult
    }

    func load(service: String, account: String, accessGroup: String) -> (SharedPushCredentialsKeychainStatus, Data?) {
        accessGroups.append(accessGroup)
        return loadResult
    }

    func update(data: Data, service: String, account: String, accessGroup: String) -> SharedPushCredentialsKeychainStatus {
        updateCalls += 1
        accessGroups.append(accessGroup)
        savedData = data
        return updateResult
    }

    func add(data: Data, service: String, account: String, accessGroup: String) -> SharedPushCredentialsKeychainStatus {
        addCalls += 1
        accessGroups.append(accessGroup)
        savedData = data
        return addResult
    }

    func delete(service: String, account: String, accessGroup: String) -> SharedPushCredentialsKeychainStatus {
        deleteCalls += 1
        accessGroups.append(accessGroup)
        return deleteResult
    }
}
