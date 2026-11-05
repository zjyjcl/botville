// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import XCTest
@testable import BotvilleCore

final class SharedPushCredentialsTests: XCTestCase {
    private let credentials = SharedPushCredentials(
        homeserverURL: "https://matrix.example.test",
        userID: "@alice:example.test",
        accessToken: "test-access-token",
        deviceID: "BOTVILLE",
        refreshToken: "test-refresh-token"
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
}
