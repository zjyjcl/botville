// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

@testable import BotvilleCore
import MatrixRustSDK
import XCTest

final class BotvilleCoreTests: XCTestCase {
    func testHomeserverAcceptsTrimmedHTTPSOriginAndPath() throws {
        XCTAssertEqual(try InputValidation.homeserver("  https://matrix.example.org/client  "),
                       "https://matrix.example.org/client")
        XCTAssertEqual(try InputValidation.homeserver("HTTPS://matrix.example.org"),
                       "HTTPS://matrix.example.org")
    }

    func testHomeserverRejectsUnsafeOrIncompleteURLs() {
        let invalidValues = [
            "matrix.example.org",
            "http://matrix.example.org",
            "https://",
            "https://alice:secret@matrix.example.org",
            "https://matrix.example.org?access_token=secret",
            "https://matrix.example.org#credentials"
        ]
        for value in invalidValues {
            do {
                _ = try InputValidation.homeserver(value)
                XCTFail("Expected invalidHomeserver for \(value)")
            } catch MatrixServiceError.invalidHomeserver {
                // Expected.
            } catch {
                XCTFail("Expected invalidHomeserver for \(value), got \(error)")
            }
        }
    }

    func testTimelineAppliesDiffsAgainstCompleteSDKItemList() {
        let virtual = TestTimelineItem(id: "virtual")
        let first = TestTimelineItem(id: "first")
        let replacement = TestTimelineItem(id: "replacement")
        let last = TestTimelineItem(id: "last")
        var state = TimelineState()

        state.apply([.append(values: [first]), .pushFront(value: virtual)])
        XCTAssertEqual(state.items.map { $0.uniqueId().id }, ["virtual", "first"])

        state.apply([.reset(values: [virtual, first])])
        state.apply([.insert(index: 1, value: replacement)])
        XCTAssertEqual(state.items.map { $0.uniqueId().id }, ["virtual", "replacement", "first"])

        state.apply([.set(index: 2, value: last), .remove(index: 1)])
        XCTAssertEqual(state.items.map { $0.uniqueId().id }, ["virtual", "last"])

        state.apply([.truncate(length: 1), .pushBack(value: first), .popFront, .popBack])
        XCTAssertTrue(state.items.isEmpty)

        state.apply([.pushBack(value: first)])
        XCTAssertEqual(state.items.map { $0.uniqueId().id }, ["first"])

        state.apply([.clear])
        XCTAssertTrue(state.items.isEmpty)
    }

    @MainActor
    func testUnauthenticatedOperationsAreRejectedBeforeNetworkUse() async {
        let service = MatrixService()

        await assertNotLoggedIn { try await service.sync() }
        await assertNotLoggedIn { try await service.openRoom("!room:matrix.example.org") }
        await assertNotLoggedIn { try await service.send(text: "hello", to: "!room:matrix.example.org") }
    }

    @MainActor
    private func assertNotLoggedIn(_ action: () async throws -> Void) async {
        do {
            try await action()
            XCTFail("Expected notLoggedIn error")
        } catch MatrixServiceError.notLoggedIn {
            // Expected: no client was created and no networking was attempted.
        } catch {
            XCTFail("Expected notLoggedIn, got \(error)")
        }
    }
}

private final class TestTimelineItem: TimelineItem, @unchecked Sendable {
    private let identifier: String

    required init(unsafeFromHandle handle: UInt64) {
        identifier = "unexpected-rust-item"
        super.init(unsafeFromHandle: handle)
    }

    init(id: String) {
        identifier = id
        super.init(noHandle: .init())
    }

    override func uniqueId() -> TimelineUniqueId {
        TimelineUniqueId(id: identifier)
    }

    override func asEvent() -> EventTimelineItem? {
        nil
    }
}
