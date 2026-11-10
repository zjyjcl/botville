// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

@testable import BotvilleCore
import Combine
import XCTest

@MainActor
final class ForegroundInAppBannerTests: XCTestCase {
    private let date = Date(timeIntervalSince1970: 1_700_000_000)

    func testDetectorUsesFirstRefreshAsBaselineThenEmitsLatestActivityChange() {
        var detector = ForegroundBannerDetector()
        XCTAssertTrue(detector.candidates(in: [snapshot()], isOnlineSync: true).isEmpty)
        let newer = candidate(eventID: "$two", date: date.addingTimeInterval(1))
        XCTAssertEqual(detector.candidates(in: [snapshot(candidate: newer, activity: newer.latestActivity)], isOnlineSync: true), [newer])
        XCTAssertTrue(detector.candidates(in: [snapshot(candidate: newer, activity: newer.latestActivity)], isOnlineSync: true).isEmpty)
    }

    func testDetectorEmitsUnreadIncreaseAndSkipsOwnOrInvitedRooms() {
        var detector = ForegroundBannerDetector()
        _ = detector.candidates(in: [snapshot()], isOnlineSync: true)
        let unreadOnly = candidate(eventID: "$unread", date: date)
        XCTAssertEqual(detector.candidates(in: [snapshot(candidate: unreadOnly, unread: 1)], isOnlineSync: true), [unreadOnly])
        let own = candidate(eventID: "$own", date: date.addingTimeInterval(1), own: true)
        XCTAssertTrue(detector.candidates(in: [snapshot(candidate: own, unread: 1, activity: own.latestActivity)], isOnlineSync: true).isEmpty)
        let invited = snapshot(candidate: candidate(eventID: "$invite", date: date.addingTimeInterval(2)), unread: 2,
                               membership: .invited, activity: date.addingTimeInterval(2))
        XCTAssertTrue(detector.candidates(in: [invited], isOnlineSync: true).isEmpty)
    }

    func testDetectorTreatsNewRoomAndResetAsBaselines() {
        var detector = ForegroundBannerDetector()
        _ = detector.candidates(in: [snapshot()], isOnlineSync: true)
        let added = candidate(roomID: "!new:test", eventID: "$new", date: date.addingTimeInterval(1))
        XCTAssertTrue(detector.candidates(in: [snapshot(), snapshot(roomID: "!new:test", candidate: added,
                                                               unread: 1, activity: added.latestActivity)], isOnlineSync: true).isEmpty)
        detector.reset()
        XCTAssertTrue(detector.candidates(in: [snapshot(candidate: added, unread: 1,
                                                         activity: added.latestActivity)], isOnlineSync: true).isEmpty)
    }

    func testDetectorUsesTimestampWhenAnEventIDIsUnavailable() {
        var detector = ForegroundBannerDetector()
        _ = detector.candidates(in: [snapshot()], isOnlineSync: true)
        let first = ForegroundBannerCandidate(roomID: "!room:test", latestActivity: date.addingTimeInterval(1),
                                              title: "Room", preview: "Body")
        XCTAssertEqual(detector.candidates(in: [snapshot(candidate: first, unread: 1, activity: first.latestActivity)], isOnlineSync: true), [first])
        XCTAssertTrue(detector.candidates(in: [snapshot(candidate: first, unread: 2, activity: first.latestActivity)], isOnlineSync: true).isEmpty)
    }

    func testDetectorSortsSimultaneousRoomsFromOldestToNewest() {
        var detector = ForegroundBannerDetector()
        _ = detector.candidates(in: [snapshot(roomID: "!a:test"), snapshot(roomID: "!b:test")], isOnlineSync: true)
        let first = candidate(roomID: "!a:test", eventID: "$a", date: date.addingTimeInterval(1))
        let second = candidate(roomID: "!b:test", eventID: "$b", date: date.addingTimeInterval(2))
        XCTAssertEqual(detector.candidates(in: [snapshot(roomID: "!b:test", candidate: second, activity: second.latestActivity),
                                              snapshot(roomID: "!a:test", candidate: first, activity: first.latestActivity)], isOnlineSync: true), [first, second])
    }

    func testStoreSuppressesOwnVisibleInputAndBackgroundCandidates() {
        let store = ForegroundInAppBannerStore(displayDuration: .seconds(100), sleep: { _ in })
        store.updateContext(isForeground: true, visibleRoomID: "!shown:test", isInputActive: false)
        store.receive(candidate(roomID: "!shown:test"))
        store.updateContext(isForeground: true, visibleRoomID: nil, isInputActive: true)
        store.receive(candidate(roomID: "!typing:test", eventID: "$typing"))
        store.updateContext(isForeground: false, visibleRoomID: nil, isInputActive: false)
        store.receive(candidate(roomID: "!background:test", eventID: "$background"))
        store.updateContext(isForeground: true, visibleRoomID: nil, isInputActive: false)
        store.receive(candidate(roomID: "!own:test", eventID: "$own", own: true))
        store.receive(candidate(roomID: "!typing:test", eventID: "$typing"))
        XCTAssertNil(store.current)
    }

    func testStoreReplacesNewEventAndOldTimerCannotDismissIt() async {
        let clock = TestClock()
        let store = ForegroundInAppBannerStore(displayDuration: .seconds(3), sleep: { duration in await clock.sleep(duration) })
        store.updateContext(isForeground: true, visibleRoomID: nil, isInputActive: false)
        let first = candidate(eventID: "$one")
        let second = candidate(eventID: "$two", date: date.addingTimeInterval(1))
        store.receive(first)
        await clock.waitForSleeps(1)
        let firstDurations = await clock.durations()
        XCTAssertEqual(firstDurations, [.seconds(3)])
        store.receive(second)
        await clock.waitForSleeps(2)
        let secondDurations = await clock.durations()
        XCTAssertEqual(secondDurations, [.seconds(3), .seconds(3)])
        let dismissed = expectation(description: "replacement banner expires")
        let observation = store.$current.dropFirst().sink { value in
            if value == nil { dismissed.fulfill() }
        }
        await clock.advance()
        await Task.yield()
        XCTAssertEqual(store.current, second)
        await clock.advance()
        await fulfillment(of: [dismissed], timeout: 1)
        withExtendedLifetime(observation) { XCTAssertNil(store.current) }
    }

    func testStoreDoesNotRestartTimerForTheSameCandidate() async {
        let clock = TestClock()
        let store = ForegroundInAppBannerStore(displayDuration: .seconds(3), sleep: { duration in await clock.sleep(duration) })
        store.updateContext(isForeground: true, visibleRoomID: nil, isInputActive: false)
        let value = candidate()
        store.receive(value)
        await clock.waitForSleeps(1)
        store.receive(value)
        let durations = await clock.durations()
        XCTAssertEqual(durations, [.seconds(3)])
        let dismissed = expectation(description: "original banner expires")
        let observation = store.$current.dropFirst().sink { value in
            if value == nil { dismissed.fulfill() }
        }
        await clock.advance()
        await fulfillment(of: [dismissed], timeout: 1)
        withExtendedLifetime(observation) { XCTAssertNil(store.current) }
    }

    func testContextChangeDismissesCurrentBanner() {
        let store = ForegroundInAppBannerStore(displayDuration: .seconds(100), sleep: { _ in })
        store.updateContext(isForeground: true, visibleRoomID: nil, isInputActive: false)
        let value = candidate()
        store.receive(value)
        XCTAssertEqual(store.current, value)
        store.updateContext(isForeground: true, visibleRoomID: value.roomID, isInputActive: false)
        XCTAssertNil(store.current)
    }

    func testStoreRequiresExplicitForegroundActivationAndResetRequiresItAgain() {
        let store = ForegroundInAppBannerStore(displayDuration: .seconds(100), sleep: { _ in })
        let value = candidate()
        store.receive(value)
        XCTAssertNil(store.current)
        store.updateContext(isForeground: true, visibleRoomID: nil, isInputActive: false)
        store.reset()
        store.receive(candidate(eventID: "$after-reset"))
        XCTAssertNil(store.current)
    }

    func testStoreRoutesAndResetClearsDeduplication() {
        let store = ForegroundInAppBannerStore(displayDuration: .seconds(100), sleep: { _ in })
        store.updateContext(isForeground: true, visibleRoomID: nil, isInputActive: false)
        let value = candidate()
        store.receive(value)
        XCTAssertEqual(store.takeRoute(), .room("!room:test"))
        XCTAssertNil(store.current)
        store.receive(value)
        XCTAssertNil(store.current)
        store.reset()
        store.updateContext(isForeground: true, visibleRoomID: nil, isInputActive: false)
        store.receive(value)
        XCTAssertEqual(store.current, value)
    }

    func testCachedRestoreWaitsForFirstOnlineSnapshotIncludingEmptyCache() {
        for cached in [[], [snapshot()]] {
            var detector = ForegroundBannerDetector()
            XCTAssertTrue(detector.candidates(in: cached, isOnlineSync: false).isEmpty)
            let offline = candidate(eventID: "$offline", date: date.addingTimeInterval(10))
            let catchUp = snapshot(candidate: offline, unread: 1)
            XCTAssertTrue(detector.candidates(in: [catchUp], isOnlineSync: false).isEmpty)
            XCTAssertTrue(detector.candidates(in: [catchUp], isOnlineSync: true).isEmpty)
            let live = candidate(eventID: "$live", date: date.addingTimeInterval(11))
            // A local refresh racing ahead must not consume the live online change.
            XCTAssertTrue(detector.candidates(in: [snapshot(candidate: live, unread: 2)], isOnlineSync: false).isEmpty)
            XCTAssertEqual(detector.candidates(in: [snapshot(candidate: live, unread: 2)], isOnlineSync: true), [live])
        }
    }

    func testForegroundCatchUpAbsorbsBackgroundHistoryThenEmitsLiveMessages() {
        var detector = ForegroundBannerDetector()
        _ = detector.candidates(in: [snapshot()], isOnlineSync: true)
        detector.beginCatchUp()
        let background = candidate(eventID: "$background", date: date.addingTimeInterval(20))
        XCTAssertTrue(detector.candidates(in: [snapshot(candidate: background, unread: 1)], isOnlineSync: true).isEmpty)
        let live = candidate(eventID: "$live", date: date.addingTimeInterval(21))
        XCTAssertEqual(detector.candidates(in: [snapshot(candidate: live, unread: 2)], isOnlineSync: true), [live])
    }

    private func visible(compact: Bool = true, detail: Bool = true,
                         active: Bool = true, chat: Bool = true) -> String? {
        ForegroundBannerVisibility.roomID(
            isLoggedIn: true, selectedRoomID: "!room:test",
            isCompact: compact, showsDetail: detail,
            isSceneActive: active, isChatTab: chat, joinedRoomIDs: ["!room:test"])
    }

    func testPushAndColdAutomaticSelectionShowDetail() {
        // Both selection paths explicitly request detail, including cold launch.
        XCTAssertEqual(visible(), "!room:test")
        let store = ForegroundInAppBannerStore()
        store.updateContext(isForeground: true, visibleRoomID: visible(), isInputActive: false)
        store.receive(candidate())
        XCTAssertNil(store.current)
        let other = candidate(roomID: "!other:test")
        store.receive(other)
        XCTAssertEqual(store.current, other)
    }

    func testCompactBackShowsBannerForRetainedSelectedRoom() {
        let store = ForegroundInAppBannerStore()
        store.updateContext(isForeground: true, visibleRoomID: visible(detail: false), isInputActive: false)
        store.receive(candidate())
        XCTAssertEqual(store.current, candidate())
    }

    func testRoomSheetRetainsRoomContextAndSuppressesSameRoom() {
        // Invite sheet is an operation on this room, not navigation away.
        // It deliberately does not change selection or the requested column.
        let store = ForegroundInAppBannerStore()
        store.updateContext(isForeground: true, visibleRoomID: visible(), isInputActive: false)
        store.receive(candidate())
        XCTAssertNil(store.current)
    }

    func testTabAndSceneVisibility() {
        XCTAssertNil(visible(chat: false))
        XCTAssertNil(visible(active: false))
        XCTAssertEqual(visible(chat: true), "!room:test")
    }

    func testCompactRegularTransitions() {
        XCTAssertNil(visible(compact: true, detail: false))
        XCTAssertEqual(visible(compact: false, detail: false), "!room:test")
        XCTAssertNil(visible(compact: true, detail: false))
        XCTAssertEqual(visible(compact: true, detail: true), "!room:test")
    }

    func testTransientInactivityDoesNotRearmBaseline() {
        var resume = ForegroundBannerResumeState()
        var detector = ForegroundBannerDetector()
        _ = detector.candidates(in: [snapshot()], isOnlineSync: true)
        XCTAssertFalse(resume.becameActive())
        let live = candidate(eventID: "$live", date: date.addingTimeInterval(1))
        XCTAssertEqual(detector.candidates(in: [snapshot(candidate: live)], isOnlineSync: true), [live])
    }

    func testBackgroundResumePreservesBaselineAndSuppressesOnlyDeliveredEvents() {
        var resume = ForegroundBannerResumeState()
        var detector = ForegroundBannerDetector()
        _ = detector.candidates(in: [snapshot()], isOnlineSync: true)
        resume.enteredBackground()
        // Merely marking background must not reset the detector while old sync drains.
        let first = candidate(eventID: "$first", date: date.addingTimeInterval(1))
        XCTAssertEqual(detector.candidates(in: [snapshot(candidate: first)], isOnlineSync: true), [first])
        XCTAssertFalse(resume.shouldPresent(first, suppressedEvents: []))
        XCTAssertTrue(resume.becameActive())
        let background = candidate(eventID: "$background", date: date.addingTimeInterval(2))
        let delivered = [PushEventReference(roomID: background.roomID, eventID: "$background")]
        XCTAssertFalse(resume.shouldPresent(background, suppressedEvents: delivered))
        XCTAssertFalse(resume.becameActive())
        let live = candidate(eventID: "$live", date: date.addingTimeInterval(3))
        XCTAssertEqual(detector.candidates(in: [snapshot(candidate: live)], isOnlineSync: true), [live])
        XCTAssertTrue(resume.shouldPresent(live, suppressedEvents: delivered))
    }

    func testRejectedSyncCannotPublishOrEstablishOrAdvanceBaseline() {
        for cancelled in [false, true] {
            var detector = ForegroundBannerDetector()
            let current = UUID()
            let generation = cancelled ? current : UUID()
            var published = 0
            let reject = { (snapshots: [ForegroundBannerSnapshot]) in
                RoomRefreshCommitGate.commit(onlineGeneration: generation, currentGeneration: current,
                                             isCancelled: cancelled) {
                    published += 1
                    _ = detector.candidates(in: snapshots, isOnlineSync: true)
                }
            }
            XCTAssertFalse(reject([snapshot()]))
            let baseline = candidate(eventID: "$baseline", date: date.addingTimeInterval(1))
            XCTAssertTrue(detector.candidates(in: [snapshot(candidate: baseline)], isOnlineSync: true).isEmpty)
            let live = candidate(eventID: "$live", date: date.addingTimeInterval(2))
            XCTAssertFalse(reject([snapshot(candidate: live)]))
            XCTAssertEqual(published, 0)
            var emitted: [ForegroundBannerCandidate] = []
            XCTAssertTrue(RoomRefreshCommitGate.commit(onlineGeneration: current, currentGeneration: current,
                                                      isCancelled: false) {
                published += 1
                emitted = detector.candidates(in: [snapshot(candidate: live)], isOnlineSync: true)
            })
            XCTAssertEqual(emitted, [live])
            XCTAssertEqual(published, 1)
        }
    }

    func testUnreadArrivingBeforeLatestEventDoesNotLoseLaterCandidate() {
        var detector = ForegroundBannerDetector()
        let old = candidate()
        _ = detector.candidates(in: [snapshot(candidate: old)], isOnlineSync: true)
        XCTAssertTrue(detector.candidates(in: [snapshot(candidate: old, unread: 1)], isOnlineSync: true).isEmpty)
        let live = candidate(eventID: "$live", date: date.addingTimeInterval(10))
        XCTAssertEqual(detector.candidates(in: [snapshot(candidate: live, unread: 1)], isOnlineSync: true), [live])
    }

    func testConsecutiveTimestampCandidatesAtTenAndTwoSecondIntervals() {
        var detector = ForegroundBannerDetector()
        _ = detector.candidates(in: [snapshot()], isOnlineSync: true)
        let store = ForegroundInAppBannerStore(displayDuration: .seconds(100), sleep: { _ in })
        store.updateContext(isForeground: true, visibleRoomID: nil, isInputActive: false)
        for offset in [10.0, 20.0, 22.0, 23.0, 33.0] {
            let live = ForegroundBannerCandidate(roomID: "!room:test", latestActivity: date.addingTimeInterval(offset),
                                                title: "Room", preview: "Body")
            let candidates = detector.candidates(in: [snapshot(candidate: live, unread: 1)], isOnlineSync: true)
            XCTAssertEqual(candidates, [live])
            candidates.forEach { store.receive($0) }
            XCTAssertEqual(store.current, live)
        }
    }

    private func candidate(roomID: String = "!room:test", eventID: String = "$one", date: Date? = nil, own: Bool = false) -> ForegroundBannerCandidate {
        ForegroundBannerCandidate(roomID: roomID, eventID: eventID, latestActivity: date ?? self.date,
                                  title: "Room", preview: "Body", isOwn: own)
    }

    private func snapshot(roomID: String = "!room:test", candidate: ForegroundBannerCandidate? = nil,
                          unread: UInt64 = 0, membership: MatrixRoomMembership = .joined,
                          activity: Date? = nil) -> ForegroundBannerSnapshot {
        let room = MatrixRoom(id: roomID, name: "Room", membership: membership, unreadCount: unread,
                              latestActivity: activity ?? candidate?.latestActivity ?? date)
        return ForegroundBannerSnapshot(room: room, candidate: candidate)
    }
}

private actor TestClock {
    private var continuations = [CheckedContinuation<Void, Never>]()
    private var registrationWaiters = [CheckedContinuation<Void, Never>]()
    private var recordedDurations = [Duration]()

    func sleep(_ duration: Duration) async {
        recordedDurations.append(duration)
        let waiters = registrationWaiters
        registrationWaiters.removeAll()
        waiters.forEach { $0.resume() }
        await withCheckedContinuation { continuations.append($0) }
    }

    func waitForSleeps(_ count: Int) async {
        while recordedDurations.count < count {
            await withCheckedContinuation { registrationWaiters.append($0) }
        }
    }

    func durations() -> [Duration] { recordedDurations }

    func advance() {
        guard !continuations.isEmpty else { return }
        continuations.removeFirst().resume()
    }
}
