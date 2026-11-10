// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation
import XCTest
@testable import BotvilleCore

final class TimelineBottomSettlementTests: XCTestCase {
    private let appearance = UUID()
    private func input(_ ids: [String] = ["a", "last"], own: Bool = false,
                       appearance: UUID? = nil) -> TimelineScrollInput {
        let appearance = appearance ?? self.appearance
        return .init(request: .make(appearanceID: appearance, completedAppearanceID: appearance,
                                   isVisible: true, isOpening: false, roomID: "room", activeRoomID: "room",
                                   latestMessageID: ids.last), messageIDs: ids, latestMessageIsOwn: own)
    }
    private func frame(_ y: Double, _ height: Double = 100) -> [TimelineMessageFrame] {
        [.init(id: "last", minY: y, height: height)]
    }
    private func opened(_ input: TimelineScrollInput) throws -> TimelineScrollState {
        var state = TimelineScrollState()
        state.update(input)
        state.didScroll(try XCTUnwrap(state.followDecision), now: 10)
        return state
    }
    private func correct(_ state: inout TimelineScrollState, _ input: TimelineScrollInput,
                         _ frames: [TimelineMessageFrame], now: Double) -> TimelineScrollDecision? {
        state.correctBottom(for: input, frames: frames, viewportHeight: 600, now: now)
    }

    func testDeepHistoryHeightGrowthAfterInitialLandingCorrectsAgain() throws {
        let input = input((0..<300).map { "old-\($0)" } + ["last"])
        var state = try opened(input)
        // First command landed, then lazy long rows above changed the estimate.
        XCTAssertNil(correct(&state, input, frame(500), now: 10.11))
        XCTAssertEqual(correct(&state, input, frame(1700, 900), now: 10.3)?.reason, .appearance)
        XCTAssertEqual(correct(&state, input, frame(650, 900), now: 10.5)?.request.latestMessageID, "last")
        XCTAssertNil(correct(&state, input, frame(-300, 900), now: 10.7))
        XCTAssertEqual(state.bottomSettlement.corrections, 2)
    }

    func testAlreadyAtBottomAndShortContentDoNotScrollAgain() throws {
        for y in [500.0, 30, -300] {
            var state = try opened(input())
            XCTAssertNil(correct(&state, input(), frame(y, y == -300 ? 900 : 100), now: 10.2))
            XCTAssertEqual(state.bottomSettlement.corrections, 0)
        }
    }

    func testMissingLastRowDoesNotExhaustRecoveryBeforeDeadline() throws {
        var state = try opened(input())
        for index in 0..<20 {
            XCTAssertNotNil(correct(&state, input(), [], now: 10.11 + Double(index) * 0.15))
        }
        XCTAssertNotNil(state.bottomSettlement.decision)
        XCTAssertNil(correct(&state, input(), frame(500), now: 13.3))
        XCTAssertNil(correct(&state, input(), frame(500), now: 13.7))
        XCTAssertTrue(state.bottomSettlement.isConfirmed)
        XCTAssertNil(correct(&state, input(), frame(2000), now: 14))
        XCTAssertNil(state.bottomSettlement.decision)
    }

    func testVisibleButChangingLongRowIsNotConfirmedAndLaterGrowthStillCorrects() throws {
        var state = try opened(input())
        XCTAssertNil(correct(&state, input(), frame(500), now: 10.2))
        XCTAssertFalse(state.bottomSettlement.isConfirmed)
        XCTAssertNil(correct(&state, input(), frame(400, 200), now: 10.5))
        XCTAssertNil(correct(&state, input(), frame(300, 300), now: 10.8))
        XCTAssertFalse(state.bottomSettlement.isConfirmed)
        XCTAssertNil(correct(&state, input(), frame(300, 300), now: 11.2))
        XCTAssertTrue(state.bottomSettlement.isConfirmed)
        XCTAssertNotNil(correct(&state, input(), frame(300, 1200), now: 12.5))
        XCTAssertFalse(state.bottomSettlement.isConfirmed)
        XCTAssertNil(correct(&state, input(), frame(-600, 1200), now: 12.8))
        XCTAssertNil(correct(&state, input(), frame(-600, 1200), now: 13.2))
        XCTAssertTrue(state.bottomSettlement.isConfirmed)
    }

    func testLateCachedSnapshotReplacementRetargetsWithoutRenewingAppearanceDeadline() throws {
        var state = try opened(input())
        let replacement = input(["cache-a", "cache-b", "newest"])
        state.update(replacement)
        XCTAssertEqual(state.bottomSettlement.deadline, 14)
        XCTAssertEqual(correct(&state, replacement, [], now: 12.5)?.request.latestMessageID, "newest")
        XCTAssertEqual(state.bottomSettlement.decision?.reason, .appearance)
        state.userBeganDragging()
        state.update(input(["new-a", "new-b", "new-c", "later"]))
        XCTAssertNil(state.bottomSettlement.decision)
        XCTAssertNil(state.followDecision)
    }

    func testNewMessageAfterOldSnapshotLandingStillFollows() throws {
        var state = try opened(input())
        XCTAssertNil(correct(&state, input(), frame(500), now: 10.2))
        state.update(input(["a", "last", "arrived-while-in-list"]))
        XCTAssertEqual(state.followDecision?.request.latestMessageID, "arrived-while-in-list")
        XCTAssertEqual(state.followDecision?.animationDuration(reduceMotion: false), 0.2)
    }

    func testTimeoutWithoutAnyGeometryAndLateFrameDoNotScroll() throws {
        var state = try opened(input())
        state.expireScrollWork(now: 14)
        XCTAssertNil(correct(&state, input(), frame(2000), now: 14.1))
        XCTAssertNil(state.bottomSettlement.decision)
    }

    func testDeadlineCheckedBeforeIssuingCorrection() throws {
        var state = try opened(input())
        XCTAssertNil(correct(&state, input(), [], now: 14))
        XCTAssertNil(state.bottomSettlement.decision)
    }

    func testDragCancelsCorrectionAndLateAnimationCompletion() throws {
        var state = TimelineScrollState()
        state.update(input())
        let decision = try XCTUnwrap(state.followDecision)
        state.didScroll(decision, now: 10)
        state.userBeganDragging()
        state.didScroll(decision, now: 10.3)
        XCTAssertNil(correct(&state, input(), frame(2000), now: 10.4))
        XCTAssertNil(state.bottomSettlement.decision)
        XCTAssertFalse(state.isNearBottom)
    }

    func testDragCancelsQueuedAppearanceWithoutReplay() throws {
        var state = TimelineScrollState()
        state.update(input())
        let decision = try XCTUnwrap(state.followDecision)
        state.userBeganDragging()
        state.didScroll(decision, now: 10)
        state.update(input())
        XCTAssertNil(state.followDecision)
        XCTAssertNil(state.bottomSettlement.decision)
    }

    func testPrependAndReopenInvalidateOldBottomWork() throws {
        for next in [input(["older", "a", "last"]), input(appearance: UUID())] {
            var state = try opened(input())
            state.update(next)
            XCTAssertNil(correct(&state, input(), frame(2000), now: 10.2))
            XCTAssertNil(state.bottomSettlement.decision)
        }
    }

    func testBeginPrependAndLeaveCancelBottomWork() throws {
        var state = try opened(input())
        state.beginPrepend(operation: UUID(), input: input(), frames: frame(100), viewportHeight: 600)
        XCTAssertNil(state.bottomSettlement.decision)
        state.cancelScrollWork()
        XCTAssertNil(state.followDecision)
        XCTAssertNil(state.prependAnchor.operation)
    }

    func testCorrectionAnimationRespectsReasonAndReduceMotion() throws {
        for reason in [TimelineScrollDecision.Reason.appearance, .newMessage, .ownMessage] {
            let decision = TimelineScrollDecision(request: try XCTUnwrap(input().request), reason: reason)
            var settle = TimelineBottomSettlement()
            settle.begin(decision, now: 0)
            let action = try XCTUnwrap(settle.correction(input: input(), frames: [], viewportHeight: 600, now: 0.1))
            XCTAssertNil(action.animationDuration(reduceMotion: true))
            XCTAssertEqual(action.animationDuration(reduceMotion: false), reason == .appearance ? nil : 0.2)
        }
    }

    func testInvalidViewportDoesNotConsumeCorrectionBudget() throws {
        var settle = TimelineBottomSettlement()
        settle.begin(try XCTUnwrap(try opened(input()).bottomSettlement.decision), now: 0)
        for height in [0.0, -1, .nan, .infinity] {
            XCTAssertNil(settle.correction(input: input(), frames: [], viewportHeight: height, now: 0.2))
        }
        XCTAssertEqual(settle.corrections, 0)
    }

    func testCaptureFallsBackWhenFirstFramePacketIsEmptyOrViewportInvalid() {
        var cache = TimelineAnchorGeometryCache()
        cache.record(input: input(), frames: frame(-240, 900), viewportHeight: 600)
        cache.record(input: input(), frames: [], viewportHeight: 600)
        for height in [0.0, 600, .nan] {
            let captured = cache.capture(input: input(), frames: [], viewportHeight: height)
            XCTAssertEqual(captured.frames, frame(-240, 900))
            XCTAssertEqual(captured.viewportHeight, 600)
        }
    }

    func testCaptureUsesCurrentGeometryAndCannotLeakAcrossRevisionOrAppearance() {
        var cache = TimelineAnchorGeometryCache()
        cache.record(input: input(), frames: frame(100), viewportHeight: 600)
        XCTAssertEqual(cache.capture(input: input(), frames: frame(200), viewportHeight: 600).frames, frame(200))
        for next in [input(["older", "a", "last"]), input(appearance: UUID())] {
            XCTAssertTrue(cache.capture(input: next, frames: [], viewportHeight: 0).frames.isEmpty)
        }
        XCTAssertTrue(TimelineAnchorGeometryCache().capture(input: input(), frames: [], viewportHeight: 0).frames.isEmpty)
    }
}
