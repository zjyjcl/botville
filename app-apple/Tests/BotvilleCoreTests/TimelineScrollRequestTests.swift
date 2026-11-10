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

    private func input(_ ids: [String], appearance: UUID? = nil, own: Bool = false) -> TimelineScrollInput {
        let appearance = appearance ?? first
        return TimelineScrollInput(request: request(appearance: appearance, completed: appearance, latest: ids.last),
                                   messageIDs: ids, latestMessageIsOwn: own)
    }

    private func opened(_ ids: [String] = ["one", "two"]) throws -> TimelineScrollState {
        var state = TimelineScrollState()
        state.update(input(ids))
        let initial = try XCTUnwrap(state.pending)
        XCTAssertEqual(initial.reason, .appearance)
        XCTAssertFalse(initial.animated)
        state.didScroll(initial)
        return state
    }

    func testHistoryPrependDoesNotTriggerButNewLastMessageDoes() throws {
        var state = try opened()
        let prepended = input(["older", "one", "two"])
        XCTAssertEqual(TimelineScrollState.change(from: ["one", "two"], to: prepended.messageIDs), .prepend)
        state.update(prepended)
        XCTAssertNil(state.pending)
        state.recordGeometry(for: prepended, distanceFromBottom: 20, viewportHeight: 600)
        state.update(input(["older", "one", "two", "new"]))
        XCTAssertEqual(state.pending?.request.latestMessageID, "new")
        XCTAssertEqual(state.pending?.reason, .newMessage)
        XCTAssertEqual(state.pending?.animated, true)
    }

    func testProximityIncludesExactThresholdButNotJustBeyondIt() {
        XCTAssertTrue(TimelineBottomProximity.isNearBottom(distanceFromBottom: 80, viewportHeight: 400))
        XCTAssertFalse(TimelineBottomProximity.isNearBottom(distanceFromBottom: 80.01, viewportHeight: 400))
        XCTAssertTrue(TimelineBottomProximity.isNearBottom(distanceFromBottom: 120, viewportHeight: 800))
        XCTAssertFalse(TimelineBottomProximity.isNearBottom(distanceFromBottom: 120.01, viewportHeight: 800))
    }

    func testUnknownZeroAndNonFiniteGeometryDoesNotFollow() {
        for height: Double? in [nil, 0, -1, .infinity, .nan] {
            XCTAssertFalse(TimelineBottomProximity.isNearBottom(distanceFromBottom: 0, viewportHeight: height))
        }
        for distance: Double? in [nil, .infinity, -.infinity, .nan] {
            XCTAssertFalse(TimelineBottomProximity.isNearBottom(distanceFromBottom: distance, viewportHeight: 600))
        }
    }

    func testShortContentAndBottomOverscrollAreNearBottom() {
        XCTAssertTrue(TimelineBottomProximity.isNearBottom(distanceFromBottom: 200 - 600, viewportHeight: 600))
        XCTAssertTrue(TimelineBottomProximity.isNearBottom(distanceFromBottom: -20, viewportHeight: 600))
    }

    func testReadingHistoryDoesNotFollowAppendOrMixedPrependAppend() throws {
        for ids in [["one", "two", "new"], ["older", "one", "two", "new"]] {
            var state = try opened()
            state.recordGeometry(for: input(["one", "two"]), distanceFromBottom: 91, viewportHeight: 600)
            state.update(input(ids))
            XCTAssertNil(state.pending)
        }
    }

    func testNearBottomFollowsMixedPrependAppend() throws {
        var state = try opened()
        state.recordGeometry(for: input(["one", "two"]), distanceFromBottom: 90, viewportHeight: 600)
        state.update(input(["older", "one", "two", "new"]))
        XCTAssertEqual(state.pending?.request.latestMessageID, "new")
        XCTAssertEqual(state.pending?.animated, true)
    }

    func testOwnNewMessageFollowsFromHistoryButUnchangedOwnMessageDoesNot() throws {
        var state = try opened()
        state.recordGeometry(for: input(["one", "two"]), distanceFromBottom: 500, viewportHeight: 600)
        let own = input(["one", "two", "mine"], own: true)
        state.update(own)
        let decision = try XCTUnwrap(state.pending)
        XCTAssertEqual(decision.reason, .ownMessage)
        XCTAssertTrue(decision.animated)
        state.didScroll(decision)
        state.recordGeometry(for: own, distanceFromBottom: 500, viewportHeight: 600)
        state.update(own) // Delivery/ack changes with a stable timeline ID do not scroll again.
        XCTAssertNil(state.pending)
    }

    func testReopenFromHistoryWithSameLastMessageScrollsWithoutAnimation() throws {
        var state = try opened()
        state.recordGeometry(for: input(["one", "two"]), distanceFromBottom: 500, viewportHeight: 600)
        state.update(input(["one", "two"], appearance: reopened))
        XCTAssertEqual(state.pending?.request.appearanceID, reopened)
        XCTAssertEqual(state.pending?.request.latestMessageID, "two")
        XCTAssertEqual(state.pending?.animated, false)
    }

    func testHiddenOpeningAndWrongRoomCancelPendingScroll() {
        for candidate in [request(appearance: first, completed: first, visible: false),
                          request(appearance: first, completed: first, opening: true),
                          request(appearance: first, completed: first, activeRoom: "other"),
                          request(appearance: first, completed: reopened)] {
            var state = TimelineScrollState()
            state.update(input(["one", "two"]))
            XCTAssertNotNil(state.pending)
            state.update(TimelineScrollInput(request: candidate, messageIDs: ["one", "two"], latestMessageIsOwn: false))
            XCTAssertNil(state.pending)
        }
    }

    func testFirstMessageAfterEmptyTimelineScrollsWithoutAnimation() throws {
        var state = try opened()
        state.update(input([]))
        XCTAssertNil(state.pending)
        state.update(input(["new"]))
        XCTAssertEqual(state.pending?.request.latestMessageID, "new")
        XCTAssertEqual(state.pending?.animated, false)
    }

    func testDeletionAndReplacementDoNotMasqueradeAsNewMessages() throws {
        for ids in [["one"], ["different"]] {
            var state = try opened()
            XCTAssertEqual(TimelineScrollState.change(from: ["one", "two"], to: ids), .replacement)
            state.update(input(ids, own: true))
            XCTAssertNil(state.pending)
        }
    }

    func testPostAppendGeometryCannotChangePreAppendDecisionInEitherCallbackOrder() throws {
        for geometryFirst in [false, true] {
            var state = try opened()
            let old = input(["one", "two"])
            let new = input(["one", "two", "tall"])
            state.recordGeometry(for: old, distanceFromBottom: 20, viewportHeight: 600)
            if geometryFirst { state.recordGeometry(for: new, distanceFromBottom: 900, viewportHeight: 600) }
            state.update(new)
            if !geometryFirst { state.recordGeometry(for: new, distanceFromBottom: 900, viewportHeight: 600) }
            XCTAssertEqual(state.pending?.request.latestMessageID, "tall")
            XCTAssertTrue(state.isNearBottom)
        }
    }

    func testStaleGeometryCannotOptHistoryReaderIntoFollowing() throws {
        var state = try opened()
        let old = input(["one", "two"])
        state.recordGeometry(for: old, distanceFromBottom: 500, viewportHeight: 600)
        state.update(input(["older", "one", "two"]))
        state.recordGeometry(for: old, distanceFromBottom: 0, viewportHeight: 600)
        state.recordGeometry(for: input(["older", "one", "two"], appearance: reopened),
                             distanceFromBottom: 0, viewportHeight: 600)
        state.update(input(["older", "one", "two", "new"]))
        XCTAssertNil(state.pending)
    }

    func testRapidAppendCoalescesAndStaleCompletionCannotConsumeNewRequest() throws {
        var state = try opened()
        state.update(input(["one", "two", "three"]))
        let old = try XCTUnwrap(state.pending)
        state.update(input(["one", "two", "three", "four"]))
        state.didScroll(old)
        XCTAssertEqual(state.pending?.request.latestMessageID, "four")
    }

    func testUserGestureCancelsQueuedFollowAndPrependDoesNotReplayIt() throws {
        var state = try opened()
        state.update(input(["one", "two", "three"]))
        state.userBeganDragging()
        XCTAssertNil(state.pending)
        state.update(input(["one", "two", "three", "four"]))
        XCTAssertNil(state.pending)

        state.recordGeometry(for: input(["one", "two", "three", "four"]),
                             distanceFromBottom: 0, viewportHeight: 600)
        state.update(input(["one", "two", "three", "four", "five"]))
        XCTAssertNotNil(state.pending)
        state.update(input(["older", "one", "two", "three", "four", "five"]))
        XCTAssertNil(state.pending)
    }
}
