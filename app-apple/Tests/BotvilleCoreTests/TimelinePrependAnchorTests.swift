// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation
import XCTest
@testable import BotvilleCore

final class TimelinePrependAnchorTests: XCTestCase {
    private let appearance = UUID()
    private let operation = UUID()
    private let ids = ["a", "b", "c"]

    private func input(_ ids: [String], appearance: UUID? = nil, visible: Bool = true,
                       own: Bool = false) -> TimelineScrollInput {
        let appearance = appearance ?? self.appearance
        return .init(request: .make(appearanceID: appearance, completedAppearanceID: appearance,
                                   isVisible: visible, isOpening: false, roomID: "room", activeRoomID: "room",
                                   latestMessageID: ids.last), messageIDs: ids, latestMessageIsOwn: own)
    }

    private func frame(_ id: String = "a", _ y: Double = 60, _ height: Double = 100) -> TimelineMessageFrame {
        .init(id: id, minY: y, height: height)
    }

    private func started(offset: Double = 60, height: Double = 100) -> TimelinePrependAnchor {
        var state = TimelinePrependAnchor()
        state.begin(operation: operation, input: input(ids),
                    frames: [frame("a", offset, height), frame("b", offset + height + 12)], viewportHeight: 600)
        return state
    }

    private func layout(_ state: inout TimelinePrependAnchor, _ ids: [String],
                        _ frames: [TimelineMessageFrame]) -> TimelinePrependAnchor.Action? {
        let action = state.layout(input: input(ids), generation: state.layoutGeneration, frames: frames)
        if action != nil { state.didExecute(generation: state.layoutGeneration) }
        return action
    }

    func testLargePrependMaterializesThenRestoresExactOffsetWithoutTimer() {
        var state = started()
        let next = (0..<300).map { "old-\($0)" } + ids
        state.update(input(next))
        state.finished(operation: operation)
        XCTAssertNil(layout(&state, next, [])) // Content-only first sample is not a locate.
        XCTAssertEqual(layout(&state, next, []), .locate("a"))
        XCTAssertTrue(state.blocksFollowing)
        // LazyVStack has not materialized the row yet: do not prematurely finish.
        XCTAssertNil(layout(&state, next, []))
        XCTAssertTrue(state.blocksFollowing)
        XCTAssertEqual(layout(&state, next, [frame("a", 0)]), .restore(id: "a", offset: 60))
        XCTAssertNil(layout(&state, next, [frame()]))
        XCTAssertFalse(state.blocksFollowing)
    }

    func testAtMostTwoCorrectionsAfterFreshGeometryIncludingLazyHeightEstimateChange() {
        var state = started()
        state.finished(operation: operation)
        XCTAssertEqual(layout(&state, ids, [frame("a", 500)]), .restore(id: "a", offset: 60))
        XCTAssertEqual(layout(&state, ids, [frame("a", 65)]), .restore(id: "a", offset: 60))
        XCTAssertNil(layout(&state, ids, [frame("a", 61)]))
        XCTAssertFalse(state.blocksFollowing)
        XCTAssertNil(layout(&state, ids, [frame("a", 500)]))
    }

    func testPartiallyVisibleLongMessageKeepsNegativeOffsetEvenWhenTallerThanViewport() {
        var state = started(offset: -240, height: 900)
        state.finished(operation: operation)
        XCTAssertEqual(state.targetID, "a")
        XCTAssertEqual(state.targetOffset, -240)
        XCTAssertEqual(layout(&state, ids, [frame("a", 0, 900)]), .restore(id: "a", offset: -240))
        XCTAssertNil(layout(&state, ids, [frame("a", -240, 900)]))
        XCTAssertFalse(state.blocksFollowing)
    }

    func testRowExactlyViewportHeightNeedsNoDivisionByZero() {
        var state = started(offset: -80, height: 600)
        state.finished(operation: operation)
        XCTAssertEqual(layout(&state, ids, [frame("a", 20, 600)]), .restore(id: "a", offset: -80))
        XCTAssertNil(layout(&state, ids, [frame("a", -80, 600)]))
    }

    func testOffscreenRowsAndButtonAreNotAnchorsAndOrderIsMessageOrder() {
        var state = TimelinePrependAnchor()
        state.begin(operation: operation, input: input(ids),
                    frames: [frame("button", 0), frame("c", 150), frame("b", -20), frame("a", -100)],
                    viewportHeight: 600)
        XCTAssertEqual(state.targetID, "b")
        XCTAssertEqual(state.targetOffset, -20)
    }

    func testInvalidAndUnknownGeometryCannotCaptureAnAnchor() {
        for frames in [[], [frame("a", .nan)], [frame("a", 600)], [frame("a", -100)]] {
            var state = TimelinePrependAnchor()
            state.begin(operation: operation, input: input(ids), frames: frames, viewportHeight: 600)
            XCTAssertNil(state.targetID)
            XCTAssertFalse(state.blocksFollowing)
        }
    }

    func testConsecutivePagesCaptureRestoredPositionAndIgnoreOldCompletion() {
        var state = started()
        let page1 = ["older"] + ids
        state.update(input(page1))
        state.finished(operation: operation)
        _ = layout(&state, page1, [frame()])
        let second = UUID()
        state.begin(operation: second, input: input(page1), frames: [frame("older", 50), frame("a", 162)],
                    viewportHeight: 600)
        XCTAssertEqual(state.targetID, "older")
        state.finished(operation: operation)
        XCTAssertTrue(state.blocksFollowing)
        let page2 = ["oldest"] + page1
        state.update(input(page2))
        state.finished(operation: second)
        XCTAssertEqual(layout(&state, page2, [frame("older", 300)]), .restore(id: "older", offset: 50))
        XCTAssertNil(layout(&state, page2, [frame("older", 50)]))
        XCTAssertFalse(state.blocksFollowing)
    }

    func testRepeatedPageWhileRestoringChainsIntendedAnchorAndRejectsOldGeometry() {
        var state = started()
        let oldGeneration = state.layoutGeneration
        let second = UUID()
        state.begin(operation: second, input: input(ids), frames: [frame("b", 0)], viewportHeight: 600)
        XCTAssertEqual(state.targetID, "a")
        XCTAssertEqual(state.targetOffset, 60)
        XCTAssertNil(state.layout(input: input(ids), generation: oldGeneration, frames: [frame("a", 1000)]))
        state.finished(operation: operation)
        XCTAssertEqual(state.operation, second)
        XCTAssertTrue(state.blocksFollowing)
    }

    func testReachingStartRemovingButtonRestoresOffsetEvenWithoutNewTextRows() {
        var state = started()
        state.finished(operation: operation)
        XCTAssertEqual(layout(&state, ids, [frame("a", 16)]), .restore(id: "a", offset: 60))
        XCTAssertNil(layout(&state, ids, [frame()]))
        XCTAssertFalse(state.blocksFollowing)
    }

    func testPaginationCompletionBeforeListenerDiffStillRestoresLatePrepend() {
        var state = started()
        state.finished(operation: operation)
        XCTAssertNil(layout(&state, ids, [frame()]))
        XCTAssertFalse(state.blocksFollowing)
        let next = ["late"] + ids
        state.update(input(next))
        XCTAssertTrue(state.blocksFollowing)
        XCTAssertEqual(layout(&state, next, [frame("a", 500)]), .restore(id: "a", offset: 60))
        XCTAssertNil(layout(&state, next, [frame()]))
        XCTAssertFalse(state.blocksFollowing)
    }

    func testQueuedActionRequiresExecutionBeforeAnotherLayoutCanCorrect() {
        var state = started()
        state.finished(operation: operation)
        XCTAssertNil(state.layout(input: input(ids), generation: state.layoutGeneration, frames: []))
        let action = state.layout(input: input(ids), generation: state.layoutGeneration, frames: [])
        XCTAssertEqual(action, .locate("a"))
        let queuedGeneration = state.layoutGeneration
        XCTAssertFalse(state.needsLayout)
        XCTAssertNil(state.layout(input: input(ids), generation: queuedGeneration, frames: [frame("a", 0)]))
        state.didExecute(generation: queuedGeneration - 1)
        XCTAssertFalse(state.needsLayout)
        state.didExecute(generation: queuedGeneration)
        XCTAssertTrue(state.needsLayout)
        XCTAssertEqual(layout(&state, ids, [frame("a", 0)]), .restore(id: "a", offset: 60))
        state.cancel()
        state.didExecute(generation: state.layoutGeneration - 1)
        XCTAssertFalse(state.needsLayout)
    }

    func testLayoutFromOldMessagesOrOldGenerationCannotRestore() {
        var state = started()
        let oldGeneration = state.layoutGeneration
        let next = ["older"] + ids
        state.update(input(next))
        XCTAssertNil(state.layout(input: input(ids), generation: state.layoutGeneration, frames: [frame("a", 900)]))
        XCTAssertNil(state.layout(input: input(next), generation: oldGeneration, frames: [frame("a", 900)]))
        XCTAssertEqual(layout(&state, next, [frame("a", 900)]), .restore(id: "a", offset: 60))
    }

    func testTabLeaveAndReopenCancelAndOldCallbacksDoNothing() {
        for next in [input(ids, visible: false), input(ids, appearance: UUID())] {
            var state = started()
            state.update(next)
            state.finished(operation: operation)
            XCTAssertNil(state.targetID)
            XCTAssertFalse(state.blocksFollowing)
            XCTAssertNil(layout(&state, ids, [frame("a", 900)]))
        }
    }

    func testMissingAnchorUsesMeasuredSurvivorThenOldSuccessorThenNoScroll() {
        var state = started()
        state.update(input(["b", "c"]))
        XCTAssertEqual(state.targetID, "b")
        XCTAssertEqual(state.targetOffset, 172)
        state.update(input(["c"]))
        XCTAssertEqual(state.targetID, "c")
        XCTAssertEqual(state.targetOffset, 0)
        state.update(input(["replacement"]))
        XCTAssertNil(state.targetID)
        XCTAssertFalse(state.blocksFollowing)
    }

    private func opened(nearBottom: Bool) throws -> TimelineScrollState {
        var state = TimelineScrollState()
        state.update(input(ids))
        state.didScroll(try XCTUnwrap(state.followDecision))
        state.recordGeometry(for: input(ids), distanceFromBottom: nearBottom ? 10 : 900, viewportHeight: 600)
        state.beginPrepend(operation: operation, input: input(ids), frames: [frame()], viewportHeight: 600)
        return state
    }

    func testAppendDuringLoadingFollowsOriginalProximityOnlyAfterRestoration() throws {
        for nearBottom in [false, true] {
            var state = try opened(nearBottom: nearBottom)
            let next = ["older"] + ids + ["new"]
            state.update(input(next))
            XCTAssertNil(state.followDecision)
            XCTAssertEqual(state.pending != nil, nearBottom)
            state.recordGeometry(for: input(next), distanceFromBottom: 2000, viewportHeight: 600)
            XCTAssertEqual(state.isNearBottom, nearBottom)
            state.finishPrepend(operation: operation)
            XCTAssertEqual(state.restorePrepend(for: input(next), generation: state.prependAnchor.layoutGeneration,
                                               frames: [frame("a", 900)]), .restore(id: "a", offset: 60))
            XCTAssertNil(state.followDecision)
            state.didExecutePrepend(generation: state.prependAnchor.layoutGeneration)
            _ = state.restorePrepend(for: input(next), generation: state.prependAnchor.layoutGeneration, frames: [frame()])
            XCTAssertEqual(state.followDecision?.request.latestMessageID, nearBottom ? "new" : nil)
        }
    }

    func testPurePrependDoesNotEraseDeferredAppendAndOwnMessageStillFollows() throws {
        for own in [false, true] {
            var state = try opened(nearBottom: !own)
            state.update(input(ids + ["new"], own: own))
            let queued = try XCTUnwrap(state.pending)
            state.didScroll(queued) // Stale animation completion during restoration.
            XCTAssertEqual(state.pending, queued)
            let next = ["older"] + ids + ["new"]
            state.update(input(next, own: own))
            XCTAssertEqual(state.pending, queued)
            state.finishPrepend(operation: operation)
            _ = state.restorePrepend(for: input(next, own: own), generation: state.prependAnchor.layoutGeneration,
                                     frames: [frame()])
            XCTAssertEqual(state.followDecision?.reason, own ? .ownMessage : .newMessage)
        }
    }

    func testReopenDuringLoadingStillLandsAtLatestAndCancelsOldAnchor() throws {
        var state = try opened(nearBottom: false)
        let reopened = UUID()
        state.update(input(ids, appearance: reopened))
        state.finishPrepend(operation: operation)
        XCTAssertNil(state.prependAnchor.targetID)
        XCTAssertEqual(state.followDecision?.reason, .appearance)
        XCTAssertEqual(state.followDecision?.request.appearanceID, reopened)
    }

    func testUserDragCancelsLateAnchorAndDeferredFollow() throws {
        var state = try opened(nearBottom: true)
        state.update(input(ids + ["new"]))
        state.userBeganDragging()
        state.finishPrepend(operation: operation)
        XCTAssertNil(state.prependAnchor.targetID)
        XCTAssertNil(state.followDecision)
        XCTAssertFalse(state.isNearBottom)
    }

    func testEmptyFirstGenerationSampleDoesNotLocateVisibleLongAnchor() {
        var state = started(offset: -240, height: 900)
        let next = ["older"] + ids
        state.update(input(next))
        state.finished(operation: operation)
        let generation = state.layoutGeneration
        XCTAssertNil(layout(&state, next, []))
        XCTAssertEqual(state.layoutGeneration, generation)
        XCTAssertEqual(layout(&state, next, [frame("a", -100, 900)]), .restore(id: "a", offset: -240))
        XCTAssertEqual(layout(&state, next, [frame("a", -230, 900)]), .restore(id: "a", offset: -240))
        XCTAssertNil(layout(&state, next, [frame("a", -240, 900)]))
        XCTAssertFalse(state.blocksFollowing)
    }

    func testMissingMaterializedRowExpiresAndLatePrependCannotResurrectIt() {
        var state = TimelinePrependAnchor()
        state.begin(operation: operation, input: input(ids), frames: [frame()], viewportHeight: 600, now: 10)
        let next = ["older"] + ids
        state.update(input(next))
        XCTAssertNil(layout(&state, next, []))
        XCTAssertEqual(layout(&state, next, []), .locate("a"))
        state.expire(now: 14.99)
        XCTAssertTrue(state.blocksFollowing)
        state.expire(now: 15)
        XCTAssertFalse(state.blocksFollowing)
        state.finished(operation: operation)
        state.update(input(["late"] + next))
        XCTAssertNil(layout(&state, ["late"] + next, [frame("a", 0)]))
        XCTAssertNil(state.targetID)
    }

    func testWrongGenerationAndAwaitingExecutionCannotExtendDeadline() {
        for execute in [false, true] {
            var state = TimelinePrependAnchor()
            state.begin(operation: operation, input: input(ids), frames: [frame()], viewportHeight: 600, now: 10)
            let action = state.layout(input: input(ids), generation: state.layoutGeneration, frames: [frame("a", 900)])
            XCTAssertNotNil(action)
            if execute { state.didExecute(generation: state.layoutGeneration) }
            XCTAssertNil(state.layout(input: input(ids), generation: state.layoutGeneration + 10, frames: [frame()]))
            state.expire(now: 15)
            XCTAssertFalse(state.blocksFollowing)
            XCTAssertNil(state.operation)
        }
    }

    func testCompletionNearDeadlineGetsOneBoundedRecoveryWindow() {
        var state = TimelinePrependAnchor()
        state.begin(operation: operation, input: input(ids), frames: [frame()], viewportHeight: 600, now: 10)
        state.finished(operation: operation, now: 14.9)
        XCTAssertEqual(state.deadline, 16.9)
        // Button disappeared without any new text rows. The target is seven
        // screens away and is absent from the first post-completion packets.
        XCTAssertNil(layout(&state, ids, []))
        XCTAssertEqual(layout(&state, ids, []), .locate("a"))
        state.expire(now: 15.1)
        XCTAssertTrue(state.blocksFollowing)
        XCTAssertEqual(layout(&state, ids, [frame("a", 0)]), .restore(id: "a", offset: 60))
        XCTAssertNil(layout(&state, ids, [frame()]))
        XCTAssertFalse(state.blocksFollowing)
        state.finished(operation: operation, now: 16)
        state.update(input(["late"] + ids))
        XCTAssertEqual(state.deadline, 16.9)
        state.expire(now: 16.9)
        XCTAssertNil(layout(&state, ["late"] + ids, [frame("a", 900)]))
    }

    func testMissingTargetRetriesRegistrationButCannotRestoreAfterCompletionDeadline() {
        var state = TimelinePrependAnchor()
        state.begin(operation: operation, input: input(ids), frames: [frame()], viewportHeight: 600, now: 10)
        state.finished(operation: operation, now: 10.5)
        var actions = 0
        for _ in 0..<20 {
            if layout(&state, ids, []) == .locate("a") { actions += 1 }
        }
        XCTAssertEqual(actions, 3)
        state.expire(now: 12.5)
        state.finished(operation: operation, now: 12.6)
        XCTAssertNil(layout(&state, ids, [frame("a", 900)]))
        XCTAssertNil(state.operation)
    }

    func testCompletionAfterLoadingDeadlineCannotReviveEvenWithoutDriverTick() {
        var state = TimelinePrependAnchor()
        state.begin(operation: operation, input: input(ids), frames: [frame()], viewportHeight: 600, now: 10)
        state.finished(operation: operation, now: 15.01)
        XCTAssertNil(state.operation)
    }

    func testGestureThenLatePrependAndCompletionDoNotRestoreOrFollow() throws {
        var state = try opened(nearBottom: true)
        state.update(input(ids + ["new"]))
        state.userBeganDragging()
        let late = input(["late"] + ids + ["new"])
        state.update(late)
        state.finishPrepend(operation: operation)
        XCTAssertNil(state.restorePrepend(for: late, generation: state.prependAnchor.layoutGeneration,
                                          frames: [frame("a", 900)]))
        XCTAssertNil(state.followDecision)
        XCTAssertFalse(state.prependAnchor.blocksFollowing)
    }

    func testTimeoutReleasesDeferredOwnFollowEvenIfSDKStillLoading() throws {
        var state = try opened(nearBottom: false)
        state.update(input(ids + ["mine"], own: true))
        XCTAssertNil(state.followDecision)
        state.expireScrollWork(now: .infinity)
        XCTAssertEqual(state.followDecision?.reason, .ownMessage)
        XCTAssertNil(state.prependAnchor.operation)
    }

}
