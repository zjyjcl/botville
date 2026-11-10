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
        state.finished(operation: operation, expectedMessageIDs: next)
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
        state.finished(operation: operation, expectedMessageIDs: ids, unchangedConfirmed: true)
        XCTAssertEqual(layout(&state, ids, [frame("a", 500)]), .restore(id: "a", offset: 60))
        XCTAssertEqual(layout(&state, ids, [frame("a", 65)]), .restore(id: "a", offset: 60))
        XCTAssertNil(layout(&state, ids, [frame("a", 61)]))
        XCTAssertFalse(state.blocksFollowing)
        XCTAssertNil(layout(&state, ids, [frame("a", 500)]))
    }

    func testPartiallyVisibleLongMessageKeepsNegativeOffsetEvenWhenTallerThanViewport() {
        var state = started(offset: -240, height: 900)
        state.finished(operation: operation, expectedMessageIDs: ids, unchangedConfirmed: true)
        XCTAssertEqual(state.targetID, "a")
        XCTAssertEqual(state.targetOffset, -240)
        XCTAssertEqual(layout(&state, ids, [frame("a", 0, 900)]), .restore(id: "a", offset: -240))
        XCTAssertNil(layout(&state, ids, [frame("a", -240, 900)]))
        XCTAssertFalse(state.blocksFollowing)
    }

    func testRowExactlyViewportHeightNeedsNoDivisionByZero() {
        var state = started(offset: -80, height: 600)
        state.finished(operation: operation, expectedMessageIDs: ids, unchangedConfirmed: true)
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
        state.finished(operation: operation, expectedMessageIDs: page1)
        _ = layout(&state, page1, [frame()])
        let second = UUID()
        state.begin(operation: second, input: input(page1), frames: [frame("older", 50), frame("a", 162)],
                    viewportHeight: 600)
        XCTAssertEqual(state.targetID, "older")
        state.finished(operation: operation, expectedMessageIDs: ids, unchangedConfirmed: true)
        XCTAssertTrue(state.blocksFollowing)
        let page2 = ["oldest"] + page1
        state.update(input(page2))
        state.finished(operation: second, expectedMessageIDs: page2)
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
        state.finished(operation: operation, expectedMessageIDs: ids, unchangedConfirmed: true)
        XCTAssertEqual(state.operation, second)
        XCTAssertTrue(state.blocksFollowing)
    }

    func testReachingStartRemovingButtonRestoresOffsetEvenWithoutNewTextRows() {
        var state = started()
        state.finished(operation: operation, expectedMessageIDs: ids, unchangedConfirmed: true)
        XCTAssertEqual(layout(&state, ids, [frame("a", 16)]), .restore(id: "a", offset: 60))
        XCTAssertNil(layout(&state, ids, [frame()]))
        XCTAssertFalse(state.blocksFollowing)
    }

    func testPaginationCompletionBeforeListenerDiffStillRestoresLatePrepend() {
        var state = started()
        let next = ["late"] + ids
        state.finished(operation: operation, expectedMessageIDs: next)
        XCTAssertNil(layout(&state, ids, [frame()]))
        XCTAssertTrue(state.blocksFollowing)
        XCTAssertFalse(state.pageRevisionApplied)
        state.update(input(next))
        XCTAssertTrue(state.blocksFollowing)
        XCTAssertEqual(layout(&state, next, [frame("a", 500)]), .restore(id: "a", offset: 60))
        XCTAssertNil(layout(&state, next, [frame()]))
        XCTAssertFalse(state.blocksFollowing)
    }

    func testQueuedActionRequiresExecutionBeforeAnotherLayoutCanCorrect() {
        var state = started()
        state.finished(operation: operation, expectedMessageIDs: ids, unchangedConfirmed: true)
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
        state.finished(operation: operation, expectedMessageIDs: next)
        XCTAssertNil(state.layout(input: input(ids), generation: state.layoutGeneration, frames: [frame("a", 900)]))
        XCTAssertNil(state.layout(input: input(next), generation: oldGeneration, frames: [frame("a", 900)]))
        XCTAssertEqual(layout(&state, next, [frame("a", 900)]), .restore(id: "a", offset: 60))
    }

    func testTabLeaveAndReopenCancelAndOldCallbacksDoNothing() {
        for next in [input(ids, visible: false), input(ids, appearance: UUID())] {
            var state = started()
            state.update(next)
            state.finished(operation: operation, expectedMessageIDs: ids, unchangedConfirmed: true)
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
            state.finishPrepend(operation: operation, expectedMessageIDs: next)
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
            state.finishPrepend(operation: operation, expectedMessageIDs: next)
            _ = state.restorePrepend(for: input(next, own: own), generation: state.prependAnchor.layoutGeneration,
                                     frames: [frame()])
            XCTAssertEqual(state.followDecision?.reason, own ? .ownMessage : .newMessage)
        }
    }

    func testReopenDuringLoadingStillLandsAtLatestAndCancelsOldAnchor() throws {
        var state = try opened(nearBottom: false)
        let reopened = UUID()
        state.update(input(ids, appearance: reopened))
        state.finishPrepend(operation: operation, expectedMessageIDs: ids, unchangedConfirmed: true)
        XCTAssertNil(state.prependAnchor.targetID)
        XCTAssertEqual(state.followDecision?.reason, .appearance)
        XCTAssertEqual(state.followDecision?.request.appearanceID, reopened)
    }

    func testUserDragCancelsLateAnchorAndDeferredFollow() throws {
        var state = try opened(nearBottom: true)
        state.update(input(ids + ["new"]))
        state.userBeganDragging()
        state.finishPrepend(operation: operation, expectedMessageIDs: ids, unchangedConfirmed: true)
        XCTAssertNil(state.prependAnchor.targetID)
        XCTAssertNil(state.followDecision)
        XCTAssertFalse(state.isNearBottom)
    }

    func testEmptyFirstGenerationSampleDoesNotLocateVisibleLongAnchor() {
        var state = started(offset: -240, height: 900)
        let next = ["older"] + ids
        state.update(input(next))
        state.finished(operation: operation, expectedMessageIDs: next)
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
        XCTAssertNil(layout(&state, next, []))
        state.expire(now: 14.99)
        XCTAssertTrue(state.blocksFollowing)
        state.expire(now: 15)
        XCTAssertFalse(state.blocksFollowing)
        state.finished(operation: operation, expectedMessageIDs: ids, unchangedConfirmed: true)
        state.update(input(["late"] + next))
        XCTAssertNil(layout(&state, ["late"] + next, [frame("a", 0)]))
        XCTAssertNil(state.targetID)
    }

    func testWrongGenerationAndAwaitingExecutionCannotExtendDeadline() {
        for execute in [false, true] {
            var state = TimelinePrependAnchor()
            state.begin(operation: operation, input: input(ids), frames: [frame()], viewportHeight: 600, now: 10)
            state.finished(operation: operation, expectedMessageIDs: ids, unchangedConfirmed: true, now: 10.1)
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
        state.finished(operation: operation, expectedMessageIDs: ids, unchangedConfirmed: true, now: 14.9)
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
        state.finished(operation: operation, expectedMessageIDs: ids, unchangedConfirmed: true, now: 16)
        state.update(input(["late"] + ids))
        XCTAssertEqual(state.deadline, 16.9)
        state.expire(now: 16.9)
        XCTAssertNil(layout(&state, ["late"] + ids, [frame("a", 900)]))
    }

    func testMissingTargetRetriesRegistrationButCannotRestoreAfterCompletionDeadline() {
        var state = TimelinePrependAnchor()
        state.begin(operation: operation, input: input(ids), frames: [frame()], viewportHeight: 600, now: 10)
        state.finished(operation: operation, expectedMessageIDs: ids, unchangedConfirmed: true, now: 10.5)
        var actions = 0
        for _ in 0..<20 {
            if layout(&state, ids, []) == .locate("a") { actions += 1 }
        }
        XCTAssertEqual(actions, 3)
        state.expire(now: 12.5)
        state.finished(operation: operation, expectedMessageIDs: ids, unchangedConfirmed: true, now: 12.6)
        XCTAssertNil(layout(&state, ids, [frame("a", 900)]))
        XCTAssertNil(state.operation)
    }

    func testCompletionExactlyAtLoadingDeadlineCannotRevive() {
        var state = TimelinePrependAnchor()
        state.begin(operation: operation, input: input(ids), frames: [frame()], viewportHeight: 600, now: 10)
        state.finished(operation: operation, expectedMessageIDs: ids, unchangedConfirmed: true, now: 15)
        XCTAssertNil(state.operation)
        XCTAssertNil(state.deadline)
        XCTAssertNil(layout(&state, ids, [frame("a", 900)]))
    }

    func testMaterializationAfterLocateBudgetCanStillRestoreWithinWindow() {
        var state = TimelinePrependAnchor()
        state.begin(operation: operation, input: input(ids), frames: [frame()], viewportHeight: 600, now: 10)
        state.finished(operation: operation, expectedMessageIDs: ids, unchangedConfirmed: true, now: 11)
        for _ in 0..<20 { _ = layout(&state, ids, []) }
        state.expire(now: 12.9)
        XCTAssertEqual(layout(&state, ids, [frame("a", 900)]), .restore(id: "a", offset: 60))
        XCTAssertNil(layout(&state, ids, [frame()]))
        XCTAssertFalse(state.blocksFollowing)
        XCTAssertEqual(state.deadline, 13)
    }

    func testCompletionAfterLoadingDeadlineCannotReviveEvenWithoutDriverTick() {
        var state = TimelinePrependAnchor()
        state.begin(operation: operation, input: input(ids), frames: [frame()], viewportHeight: 600, now: 10)
        state.finished(operation: operation, expectedMessageIDs: ids, unchangedConfirmed: true, now: 15.01)
        XCTAssertNil(state.operation)
    }

    func testGestureThenLatePrependAndCompletionDoNotRestoreOrFollow() throws {
        var state = try opened(nearBottom: true)
        state.update(input(ids + ["new"]))
        state.userBeganDragging()
        let late = input(["late"] + ids + ["new"])
        state.update(late)
        state.finishPrepend(operation: operation, expectedMessageIDs: ids, unchangedConfirmed: true)
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

    func testLoadingOldZeroDeltaCannotSettleOrLocateAndExpiresAtFiveSeconds() {
        var state = TimelinePrependAnchor()
        state.begin(operation: operation, input: input(ids), frames: [frame()], viewportHeight: 600, now: 10)
        for _ in 0..<10 {
            XCTAssertNil(layout(&state, ids, [frame()]))
            XCTAssertNil(state.missingGeometry())
            XCTAssertTrue(state.needsLayout)
            XCTAssertTrue(state.blocksFollowing)
        }
        state.expire(now: 15)
        state.finished(operation: operation, expectedMessageIDs: ["late"] + ids, now: 26.191)
        state.update(input(["late"] + ids))
        XCTAssertNil(state.operation)
        XCTAssertNil(layout(&state, ["late"] + ids, [frame("a", 900)]))
    }

    func testDiffBeforeFinishedStillRequiresPostFinishedGeometry() {
        var state = started()
        let next = ["older"] + ids
        state.update(input(next))
        let preCompletion = state.layoutGeneration
        XCTAssertNil(layout(&state, next, [frame()]))
        XCTAssertTrue(state.needsLayout)
        state.finished(operation: operation, expectedMessageIDs: next)
        XCTAssertTrue(state.pageRevisionApplied)
        XCTAssertNil(state.layout(input: input(next), generation: preCompletion, frames: [frame()]))
        XCTAssertTrue(state.blocksFollowing)
        XCTAssertEqual(layout(&state, next, [frame("a", 400)]), .restore(id: "a", offset: 60))
        XCTAssertNil(layout(&state, next, [frame()]))
        XCTAssertFalse(state.blocksFollowing)
    }

    func testEmptyOrStatusOnlyPageNeedsExplicitFenceAndPostFinishedGeometry() {
        for statusDiff in [false, true] {
            var state = started()
            let oldGeneration = state.layoutGeneration
            if statusDiff { state.update(input(ids)) }
            state.finished(operation: operation, expectedMessageIDs: ids, unchangedConfirmed: true)
            XCTAssertTrue(state.pageRevisionApplied)
            XCTAssertNil(state.layout(input: input(ids), generation: oldGeneration, frames: [frame()]))
            XCTAssertTrue(state.blocksFollowing)
            XCTAssertNil(layout(&state, ids, [frame()]))
            XCTAssertFalse(state.blocksFollowing)
        }
    }

    func testUnknownPageFenceOrUnrelatedAppendCannotAcknowledgePage() {
        var state = started()
        state.finished(operation: operation) // e.g. failed snapshot/pagination
        for _ in 0..<10 { XCTAssertNil(layout(&state, ids, [frame()])) }
        XCTAssertFalse(state.pageRevisionApplied)
        XCTAssertTrue(state.blocksFollowing)
        state = started()
        state.finished(operation: operation, expectedMessageIDs: ["older"] + ids)
        state.update(input(ids + ["live"]))
        XCTAssertFalse(state.pageRevisionApplied)
        XCTAssertNil(layout(&state, ids + ["live"], [frame()]))
        state.update(input(["older"] + ids + ["live"]))
        XCTAssertTrue(state.pageRevisionApplied)
        XCTAssertFalse(TimelinePrependAnchor.containsPageRevision(["b", "a"], in: ids))
    }

    func testAbsentNewGenerationActivelyLocatesUsingOneSharedBudgetThenTimesOut() {
        var state = TimelinePrependAnchor()
        state.begin(operation: operation, input: input(ids), frames: [frame()], viewportHeight: 600, now: 10)
        let next = ["older"] + ids
        state.finished(operation: operation, expectedMessageIDs: next, now: 10.1)
        // Before the diff even the fallback must remain idle.
        for _ in 0..<10 { XCTAssertNil(state.missingGeometry()) }
        state.update(input(next))
        var actions = 0
        for tick in 0..<30 {
            if let action = state.missingGeometry() {
                XCTAssertEqual(action, .locate("a"))
                actions += 1
                state.didExecute(generation: state.layoutGeneration)
            }
            if tick == 10 { state.update(input(next + ["live"])) }
        }
        XCTAssertEqual(actions, 3)
        for _ in 0..<10 { XCTAssertNil(layout(&state, next + ["live"], [])) }
        XCTAssertEqual(state.deadline, 12.1)
        state.expire(now: 12.1)
        XCTAssertNil(state.missingGeometry())
        XCTAssertNil(state.operation)
    }

    func testFreshGeometryAfterActiveLocateCanRestoreWithinOriginalWindow() {
        var state = started()
        let next = ["older"] + ids
        state.finished(operation: operation, expectedMessageIDs: next)
        state.update(input(next))
        XCTAssertNil(state.missingGeometry())
        XCTAssertNil(state.missingGeometry())
        XCTAssertEqual(state.missingGeometry(), .locate("a"))
        let generation = state.layoutGeneration
        XCTAssertNil(state.missingGeometry()) // await execution
        state.didExecute(generation: generation)
        XCTAssertEqual(layout(&state, next, [frame("a", 0)]), .restore(id: "a", offset: 60))
        XCTAssertNil(layout(&state, next, [frame()]))
        XCTAssertFalse(state.blocksFollowing)
        state.cancel()
        for _ in 0..<10 { XCTAssertNil(state.missingGeometry()) }
    }

    func testFinalHeaderFootprintPreventsUnreachableTopOffsetAndPersistsAfterExpiry() {
        let buttonHeight = 61.0 / 3
        let paddingAndSpacing = 28.0
        let original = paddingAndSpacing + buttonHeight
        // Observed SwiftUI layout: top clamping prevents scrollTo from moving
        // the first row below its natural origin after removing the button.
        let withoutSlot = paddingAndSpacing
        XCTAssertEqual(withoutSlot - original, -buttonHeight, accuracy: 0.0001)
        var header = TimelineHeaderFootprint()
        header.record(height: buttonHeight, buttonVisible: true)
        for value in [0.0, Double.nan, Double.infinity, -1] {
            header.record(height: value, buttonVisible: false)
        }
        var state = started(offset: original)
        state.finished(operation: operation, expectedMessageIDs: ids, unchangedConfirmed: true)
        XCTAssertNil(layout(&state, ids, [frame("a", paddingAndSpacing + header.height)]))
        XCTAssertFalse(state.blocksFollowing)
        state.expire(now: .infinity)
        XCTAssertEqual(header.height, buttonHeight)
        state.cancel()
        XCTAssertEqual(header.height, buttonHeight)
        // A new appearance gets a fresh footprint, not another room's metric.
        header = TimelineHeaderFootprint()
        XCTAssertEqual(header.height, 0)
    }

    func testSnapshotRemovalMustReachInputBeforeItsSurvivorsCanSettle() {
        var state = started()
        let next = ["a", "b"] // Snapshot has removed c; old a/b alone are not proof.
        state.finished(operation: operation, expectedMessageIDs: next)
        XCTAssertFalse(state.pageRevisionApplied)
        XCTAssertNil(layout(&state, ids, [frame()]))
        state.update(input(next))
        XCTAssertTrue(state.pageRevisionApplied)
        XCTAssertNil(layout(&state, next, [frame()]))
        XCTAssertFalse(state.blocksFollowing)
    }

    func testLocateAndRevisionChangesCannotResetTwoCorrectionBudget() {
        var state = started()
        state.finished(operation: operation, expectedMessageIDs: ids, unchangedConfirmed: true)
        XCTAssertEqual(layout(&state, ids, [frame("a", 500)]), .restore(id: "a", offset: 60))
        for _ in 0..<2 { XCTAssertNil(state.missingGeometry()) }
        XCTAssertEqual(state.missingGeometry(), .locate("a"))
        state.didExecute(generation: state.layoutGeneration)
        XCTAssertEqual(layout(&state, ids, [frame("a", 0)]), .restore(id: "a", offset: 60))
        for _ in 0..<10 { XCTAssertNil(state.missingGeometry()) }
        let next = ids + ["live"]
        state.update(input(next))
        XCTAssertNil(layout(&state, next, [frame("a", 50)]))
        XCTAssertFalse(state.blocksFollowing) // correctionLimit, never success.
    }

    func testAppliedPageFenceSurvivesLaterDeletionAndRestoresMeasuredSurvivor() {
        var state = started()
        let next = ["older"] + ids
        state.finished(operation: operation, expectedMessageIDs: next)
        state.update(input(next))
        XCTAssertTrue(state.pageRevisionApplied)
        let removed = ["older", "b", "c"]
        state.update(input(removed))
        XCTAssertTrue(state.pageRevisionApplied)
        XCTAssertEqual(state.targetID, "b")
        XCTAssertEqual(layout(&state, removed, [frame("b", 800)]), .restore(id: "b", offset: 172))
        XCTAssertNil(layout(&state, removed, [frame("b", 172)]))
        XCTAssertFalse(state.blocksFollowing)
    }

    func testOldSnapshotAndZeroDeltaCannotSettleBeforeDelayedPrepend() throws {
        // c1p9 plus the five old-reset / late-inc-diff orderings, through pageEnd.
        for delay in [0.0817, 0.0739, 0.08, 0.09, 0.07, 0.0677] {
            var state = TimelineScrollState()
            state.beginPrepend(operation: operation, input: input(ids), frames: [frame()], viewportHeight: 600, now: 10)
            state.finishPrepend(operation: operation, completedAt: 10.035, now: 10.035)
            var confirmation = TimelinePageConfirmation(original: ids, completedAt: 10.035)
            XCTAssertNil(confirmation.observe(snapshot: ids, live: ids, now: 10.058))
            let oldGeneration = state.prependAnchor.layoutGeneration
            XCTAssertFalse(state.prependAnchor.pageRevisionApplied)
            XCTAssertNil(state.restorePrepend(for: input(ids), generation: oldGeneration, frames: [frame()]))
            XCTAssertTrue(state.prependAnchor.blocksFollowing)
            for _ in 0..<3 { XCTAssertNil(state.recoverMissingPrependGeometry()) }
            let next = ["older"] + ids
            state.update(input(next))
            XCTAssertFalse(state.prependAnchor.pageRevisionApplied)
            let result = try XCTUnwrap(confirmation.observe(snapshot: ids, live: next, now: 10 + delay))
            state.completePrepend(operation: operation, result: result, now: 10 + delay)
            XCTAssertTrue(state.prependAnchor.pageRevisionApplied)
            XCTAssertEqual(state.prependAnchor.deadline, 12.035)
            XCTAssertNil(state.restorePrepend(for: input(ids), generation: oldGeneration, frames: [frame()]))
            XCTAssertTrue(state.prependAnchor.blocksFollowing)
            XCTAssertEqual(state.restorePrepend(for: input(next), generation: state.prependAnchor.layoutGeneration,
                                                frames: [frame("a", 500)]), .restore(id: "a", offset: 60))
            state.didExecutePrepend(generation: state.prependAnchor.layoutGeneration)
            XCTAssertNil(state.restorePrepend(for: input(next), generation: state.prependAnchor.layoutGeneration,
                                              frames: [frame()]))
            XCTAssertFalse(state.prependAnchor.blocksFollowing)
        }
    }

    func testUnchangedSnapshotDoesNotOpenOnUnrelatedLiveAppend() {
        var state = TimelineScrollState()
        state.beginPrepend(operation: operation, input: input(ids), frames: [frame()], viewportHeight: 600, now: 10)
        state.finishPrepend(operation: operation, completedAt: 10.035, now: 10.035)
        let next = ids + ["live"]
        state.update(input(next))
        var confirmation = TimelinePageConfirmation(original: ids, completedAt: 10.035)
        XCTAssertNil(confirmation.observe(snapshot: ids, live: next, now: 10.058))
        // Defend even against an unconfirmed old completion passed by a caller.
        state.completePrepend(operation: operation,
                              result: .init(expectedMessageIDs: ids, unchangedConfirmed: false, completedAt: 10.035),
                              now: 10.058)
        XCTAssertFalse(state.prependAnchor.pageRevisionApplied)
        XCTAssertNil(state.restorePrepend(for: input(next), generation: state.prependAnchor.layoutGeneration,
                                          frames: [frame()]))
        XCTAssertTrue(state.prependAnchor.blocksFollowing)
    }

    func testTailSnapshotWaitsAndLaterPrependReleasesDeferredOwnMessage() throws {
        var state = TimelineScrollState()
        state.update(input(ids))
        state.didScroll(try XCTUnwrap(state.followDecision))
        state.recordGeometry(for: input(ids), distanceFromBottom: 900, viewportHeight: 600)
        state.beginPrepend(operation: operation, input: input(ids), frames: [frame()], viewportHeight: 600, now: 10)
        state.finishPrepend(operation: operation, completedAt: 10.035, now: 10.035)
        var confirmation = TimelinePageConfirmation(original: ids, completedAt: 10.035)
        let tail = ids + ["tail"]
        state.update(input(tail, own: true))
        XCTAssertEqual(state.pending?.reason, .ownMessage)
        XCTAssertNil(confirmation.observe(snapshot: tail, live: tail, now: 10.058))
        XCTAssertNil(confirmation.observe(snapshot: tail, live: tail, now: 10.10))
        XCTAssertFalse(state.prependAnchor.pageRevisionApplied)
        XCTAssertNil(state.restorePrepend(for: input(tail, own: true), generation: state.prependAnchor.layoutGeneration,
                                          frames: [frame()])) // Fresh geometry with zero delta is still fenced.
        XCTAssertTrue(state.prependAnchor.blocksFollowing)
        XCTAssertNil(state.followDecision)
        let next = ["older"] + tail
        let result = try XCTUnwrap(confirmation.observe(snapshot: next, live: tail, now: 10.12))
        XCTAssertFalse(result.unchangedConfirmed)
        state.completePrepend(operation: operation, result: result, now: 10.12)
        XCTAssertFalse(state.prependAnchor.pageRevisionApplied) // Snapshot alone is not main-stream application.
        state.update(input(next, own: true))
        XCTAssertTrue(state.prependAnchor.pageRevisionApplied)
        XCTAssertEqual(state.prependAnchor.deadline, 12.035)
        XCTAssertEqual(state.restorePrepend(for: input(next, own: true), generation: state.prependAnchor.layoutGeneration,
                                            frames: [frame("a", 500)]), .restore(id: "a", offset: 60))
        XCTAssertNil(state.followDecision)
        state.didExecutePrepend(generation: state.prependAnchor.layoutGeneration)
        XCTAssertNil(state.restorePrepend(for: input(next, own: true), generation: state.prependAnchor.layoutGeneration,
                                          frames: [frame()]))
        XCTAssertFalse(state.prependAnchor.blocksFollowing)
        XCTAssertEqual(state.followDecision?.reason, .ownMessage)
    }

    func testUnconfirmedTailCompletionCannotOpenEitherAnchorEntryPoint() {
        let tail = ids + ["tail"]
        XCTAssertFalse(TimelinePrependAnchor.containsPageRevision(tail, in: tail, replacing: ids))
        for finishedFirst in [false, true] {
            var state = TimelinePrependAnchor()
            state.begin(operation: operation, input: input(ids), frames: [frame()], viewportHeight: 600, now: 10)
            if finishedFirst { state.finished(operation: operation, completedAt: 10.035, now: 10.035) }
            state.update(input(tail))
            state.confirmPage(operation: operation,
                              result: .init(expectedMessageIDs: tail, unchangedConfirmed: false, completedAt: 10.035),
                              now: 10.058)
            XCTAssertFalse(state.pageRevisionApplied)
            XCTAssertNil(layout(&state, tail, [frame()]))
            XCTAssertTrue(state.blocksFollowing)
            state.update(input(tail + ["tail-2"]))
            XCTAssertFalse(state.pageRevisionApplied)
            let next = ["older"] + tail
            state.update(input(next))
            XCTAssertTrue(state.pageRevisionApplied)
            XCTAssertNil(layout(&state, next, [frame()]))
            XCTAssertFalse(state.blocksFollowing)
        }
    }

    func testZeroTextConfirmationIgnoresIndependentTailAppends() throws {
        for reachedStart in [false, true] {
            var state = TimelineScrollState()
            state.beginPrepend(operation: operation, input: input(ids), frames: [frame()], viewportHeight: 600, now: 10)
            state.finishPrepend(operation: operation, completedAt: 10.035, now: 10.035)
            var confirmation = TimelinePageConfirmation(original: ids, completedAt: 10.035)
            let tail = ids + ["tail"]
            state.update(input(tail))
            XCTAssertNil(confirmation.observe(snapshot: tail, live: tail, now: 10.058))
            XCTAssertNil(confirmation.observe(snapshot: ids, live: tail, now: 10.10))
            let next = tail + ["tail-2"]
            state.update(input(next))
            XCTAssertNil(confirmation.observe(snapshot: next, live: next, now: 10.20)) // < 150ms
            let result = try XCTUnwrap(confirmation.observe(snapshot: tail, live: next, now: 10.21))
            XCTAssertTrue(result.unchangedConfirmed, "reachedStart=\(reachedStart)")
            XCTAssertEqual(result.expectedMessageIDs, ids) // Fence only the unchanged front.
            let oldGeneration = state.prependAnchor.layoutGeneration
            state.completePrepend(operation: operation, result: result, now: 10.21)
            XCTAssertTrue(state.prependAnchor.pageRevisionApplied)
            XCTAssertEqual(state.prependAnchor.deadline, 12.035)
            XCTAssertNil(state.restorePrepend(for: input(next), generation: oldGeneration, frames: [frame()]))
            XCTAssertTrue(state.prependAnchor.blocksFollowing)
            XCTAssertNil(state.restorePrepend(for: input(next), generation: state.prependAnchor.layoutGeneration,
                                              frames: [frame()]))
            XCTAssertFalse(state.prependAnchor.blocksFollowing)
        }
    }

    func testMissingSnapshotAcceptsLivePrependButNotTail() throws {
        var confirmation = TimelinePageConfirmation(original: ids, completedAt: 10)
        let tail = ids + ["tail"]
        XCTAssertNil(confirmation.observe(snapshot: nil, live: tail, now: 10.05))
        let next = ["older"] + tail
        let result = try XCTUnwrap(confirmation.observe(snapshot: nil, live: next, now: 10.10))
        XCTAssertEqual(result.expectedMessageIDs, next)
        XCTAssertFalse(result.unchangedConfirmed)
        XCTAssertEqual(result.completedAt, 10)
    }

    func testUnchangedFrontRequiresContinuousLiveAgreementAndFreshSnapshots() throws {
        var confirmation = TimelinePageConfirmation(original: ids, completedAt: 10)
        let tail = ids + ["tail"]
        XCTAssertNil(confirmation.observe(snapshot: tail, live: tail, now: 10.01))
        XCTAssertNil(confirmation.observe(snapshot: tail, live: ["a", "replacement", "c"], now: 10.10))
        XCTAssertNil(confirmation.observe(snapshot: tail, live: tail, now: 10.20))
        XCTAssertNil(confirmation.observe(snapshot: nil, live: tail, now: 10.36))
        XCTAssertNil(confirmation.observe(snapshot: ids, live: tail, now: 10.40))
        XCTAssertNil(confirmation.observe(snapshot: tail, live: tail, now: 10.54))
        XCTAssertTrue(try XCTUnwrap(confirmation.observe(snapshot: tail, live: tail, now: 10.56)).unchangedConfirmed)
    }

    func testP11AndP12RequireRepeatedUnchangedSnapshotsAndFinishedGeometry() {
        // The same confirmation applies whether reachedStart is false or true.
        for reachedStart in [false, true] {
            var confirmation = TimelinePageConfirmation(original: ids, completedAt: 10.035)
            XCTAssertNil(confirmation.observe(snapshot: ids, live: ids, now: 10.058))
            XCTAssertNil(confirmation.observe(snapshot: ids, live: ids, now: 10.10))
            let result = confirmation.observe(snapshot: ids, live: ids, now: 10.21)
            XCTAssertEqual(result?.unchangedConfirmed, true, "reachedStart=\(reachedStart)")
            var state = TimelinePrependAnchor()
            state.begin(operation: operation, input: input(ids), frames: [frame()], viewportHeight: 600, now: 10)
            let oldGeneration = state.layoutGeneration
            state.finished(operation: operation, expectedMessageIDs: result?.expectedMessageIDs,
                           unchangedConfirmed: result?.unchangedConfirmed == true,
                           completedAt: result?.completedAt, now: 10.21)
            XCTAssertEqual(state.deadline, 12.035)
            XCTAssertNil(state.layout(input: input(ids), generation: oldGeneration, frames: [frame()]))
            XCTAssertTrue(state.blocksFollowing)
            XCTAssertNil(layout(&state, ids, [frame()]))
            XCTAssertFalse(state.blocksFollowing)
        }
    }

    func testSnapshotRetryReplacesOldFenceAndMissingIsNeverEmpty() {
        var confirmation = TimelinePageConfirmation(original: ids, completedAt: 10)
        XCTAssertNil(confirmation.observe(snapshot: nil, live: ids, now: 10))
        XCTAssertNil(confirmation.observe(snapshot: ids, live: ids, now: 10.05))
        let next = ["older"] + ids
        let result = confirmation.observe(snapshot: ids, live: next, now: 10.08)
        XCTAssertEqual(result?.expectedMessageIDs, next)
        XCTAssertEqual(result?.unchangedConfirmed, false)
        // Production order: finish immediately, then confirm the retried view.
        var state = TimelinePrependAnchor()
        state.begin(operation: operation, input: input(ids), frames: [frame()], viewportHeight: 600, now: 9.9)
        state.finished(operation: operation, now: 10)
        XCTAssertNil(layout(&state, ids, [frame()]))
        state.update(input(next))
        let preConfirmation = state.layoutGeneration
        XCTAssertFalse(state.pageRevisionApplied)
        state.confirmPage(operation: operation, result: result!, now: 10.08)
        XCTAssertTrue(state.pageRevisionApplied)
        XCTAssertEqual(state.deadline, 12)
        XCTAssertNil(state.layout(input: input(next), generation: preConfirmation, frames: [frame()]))
        XCTAssertTrue(state.blocksFollowing)
        XCTAssertEqual(layout(&state, next, [frame("a", 500)]), .restore(id: "a", offset: 60))
        XCTAssertNil(layout(&state, next, [frame()]))
        XCTAssertFalse(state.blocksFollowing)
        confirmation = TimelinePageConfirmation(original: ids, completedAt: 10)
        XCTAssertNil(confirmation.observe(snapshot: ids, live: ids, now: 10))
        XCTAssertNil(confirmation.observe(snapshot: nil, live: ids, now: 10.2))
        XCTAssertNil(confirmation.observe(snapshot: ids, live: ids, now: 10.25))
        XCTAssertEqual(confirmation.observe(snapshot: ["a", "b", "replacement"], live: ids, now: 10.3)?.expectedMessageIDs,
                       ["a", "b", "replacement"]) // Same count, genuinely changed IDs.
    }

    func testFailedPaginationCancelsImmediatelyButMissingSnapshotRetainsRecovery() {
        var state = TimelineScrollState()
        state.beginPrepend(operation: operation, input: input(ids), frames: [frame()], viewportHeight: 600, now: 10)
        state.completePrepend(operation: UUID(), result: nil, now: 10.1)
        XCTAssertTrue(state.prependAnchor.blocksFollowing) // stale failure
        state.completePrepend(operation: operation, result: nil, now: 10.1)
        XCTAssertFalse(state.prependAnchor.blocksFollowing)
        state.beginPrepend(operation: operation, input: input(ids), frames: [frame()], viewportHeight: 600, now: 11)
        state.completePrepend(operation: operation,
                              result: .init(expectedMessageIDs: nil, unchangedConfirmed: false, completedAt: 11.1), now: 11.6)
        XCTAssertTrue(state.prependAnchor.blocksFollowing)
        XCTAssertFalse(state.prependAnchor.pageRevisionApplied)
        XCTAssertEqual(state.prependAnchor.deadline, 13.1)
        state.cancelPrepend()
        state.completePrepend(operation: operation,
                              result: .init(expectedMessageIDs: ["older"] + ids, unchangedConfirmed: false, completedAt: 11.1), now: 11.7)
        XCTAssertFalse(state.prependAnchor.blocksFollowing)
    }

    func testRetryAfterFiveSecondLoadingBoundaryUsesOriginalCompletionDeadline() {
        var state = TimelinePrependAnchor()
        state.begin(operation: operation, input: input(ids), frames: [frame()], viewportHeight: 600, now: 10)
        state.finished(operation: operation, completedAt: 14.9, now: 14.9)
        state.expire(now: 15.05)
        XCTAssertNotNil(state.operation)
        let oldGeneration = state.layoutGeneration
        state.confirmPage(operation: operation,
                          result: .init(expectedMessageIDs: ids, unchangedConfirmed: true, completedAt: 14.9), now: 15.1)
        XCTAssertEqual(state.deadline, 16.9)
        XCTAssertTrue(state.pageRevisionApplied)
        XCTAssertNil(state.layout(input: input(ids), generation: oldGeneration, frames: [frame()]))
        XCTAssertTrue(state.blocksFollowing)
        XCTAssertNil(layout(&state, ids, [frame()]))
        XCTAssertFalse(state.blocksFollowing)
        state.expire(now: 17)
        state.confirmPage(operation: operation,
                          result: .init(expectedMessageIDs: ids, unchangedConfirmed: true, completedAt: 14.9), now: 17.1)
        XCTAssertNil(state.operation)
    }

}
