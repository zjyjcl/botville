// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation
import XCTest
@testable import BotvilleCore

final class TimelineScrollRequestTests: XCTestCase {
    private let first = UUID()
    private let reopened = UUID()

    private func request(
        appearance: UUID, completed: UUID?, visible: Bool = true,
        opening: Bool = false, activeRoom: String? = "room", latest: String? = "last"
    ) -> TimelineScrollRequest? {
        .make(appearanceID: appearance, completedAppearanceID: completed,
              isVisible: visible, isOpening: opening, roomID: "room",
              activeRoomID: activeRoom, latestMessageID: latest)
    }

    func testFirstOpenWaitsForLoadingThenScrollsToLatest() {
        XCTAssertNil(request(appearance: first, completed: nil))
        XCTAssertNil(request(appearance: first, completed: first, opening: true))
        XCTAssertEqual(request(appearance: first, completed: first)?.latestMessageID, "last")
    }

    func testReopenWithUnchangedLastMessageProducesNewRequest() throws {
        let initial = try XCTUnwrap(request(appearance: first, completed: first))
        XCTAssertNil(request(appearance: reopened, completed: first))
        let next = try XCTUnwrap(request(appearance: reopened, completed: reopened))
        XCTAssertNotEqual(initial, next)
        XCTAssertEqual(initial.latestMessageID, next.latestMessageID)
    }

    func testHiddenOrDifferentRoomCannotScroll() {
        XCTAssertNil(request(appearance: first, completed: first, visible: false))
        XCTAssertNil(request(appearance: first, completed: first, activeRoom: "other"))
        XCTAssertNil(request(appearance: first, completed: first, activeRoom: nil))
    }

    func testEmptyRoomWaitsForFirstMessage() {
        XCTAssertNil(request(appearance: first, completed: first, latest: nil))
        XCTAssertNotNil(request(appearance: first, completed: first, latest: "new"))
    }

    func testHistoryPrependDoesNotTriggerButNewLastMessageDoes() {
        let original = request(appearance: first, completed: first)
        XCTAssertEqual(original, request(appearance: first, completed: first))
        XCTAssertNotEqual(original, request(appearance: first, completed: first, latest: "new"))
    }
}
