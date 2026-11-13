// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

@testable import BotvilleCore
import Combine
import XCTest

@MainActor
final class ForegroundInAppBannerTests: XCTestCase {
    private let date = Date(timeIntervalSince1970: 1_700_000_000)

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

    func testTemporarySuppressionDoesNotConsumePresentationKey() {
        for background in [true, false] {
            let store = ForegroundInAppBannerStore()
            let value = candidate()
            store.updateContext(isForeground: !background, visibleRoomID: nil, isInputActive: false,
                                mutedRoomIDs: background ? [] : [value.roomID])
            store.receive(value)
            XCTAssertNil(store.current)
            store.updateContext(isForeground: true, visibleRoomID: nil, isInputActive: false)
            XCTAssertNil(store.current) // Context recovery never actively replays.
            store.receive(value)
            XCTAssertEqual(store.current, value)
            store.dismiss()
            store.receive(value)
            XCTAssertNil(store.current) // A presented event remains consumed.
        }
    }

    func testInputAndVisibleRoomNeverReplayAfterContextRecovery() {
        for input in [true, false] {
            let store = ForegroundInAppBannerStore()
            let value = candidate()
            store.updateContext(isForeground: true, visibleRoomID: input ? nil : value.roomID, isInputActive: input)
            store.receive(value)
            store.updateContext(isForeground: true, visibleRoomID: nil, isInputActive: false)
            store.receive(value)
            XCTAssertNil(store.current)
        }
    }

    func testRepeatedSuppressionDoesNotPrematurelyEvictConsumedKey() {
        let store = ForegroundInAppBannerStore()
        let value = candidate()
        store.updateContext(isForeground: true, visibleRoomID: nil, isInputActive: true)
        for _ in 0..<300 { store.receive(value) }
        store.updateContext(isForeground: true, visibleRoomID: nil, isInputActive: false)
        store.receive(value)
        XCTAssertNil(store.current)
    }

    func testStoreReplacesNewEventAndOldTimerCannotDismissIt() async {
        let clock = TestClock()
        let store = ForegroundInAppBannerStore(displayDuration: .seconds(3), sleep: { duration in await clock.sleep(duration) })
        store.updateContext(isForeground: true, visibleRoomID: nil, isInputActive: false)
        let first = candidate(eventID: "$one")
        let second = candidate(roomID: "!other:test", eventID: "$two", date: date.addingTimeInterval(1))
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

    func testStoreReplacesSameRoomWithANewerEventAndOldTimerCannotDismissIt() async {
        let clock = TestClock()
        let store = ForegroundInAppBannerStore(displayDuration: .seconds(3), sleep: { duration in await clock.sleep(duration) })
        store.updateContext(isForeground: true, visibleRoomID: nil, isInputActive: false)
        let first = candidate(eventID: "$same-room-one")
        let second = candidate(roomID: first.roomID, eventID: "$same-room-two", date: date.addingTimeInterval(1))
        store.receive(first)
        await clock.waitForSleeps(1)
        store.receive(second)
        await clock.waitForSleeps(2)
        await clock.advance()
        await Task.yield()
        XCTAssertEqual(store.current, second)
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

    func testSameMillisecondEventsInOneRoomStayDistinctByEventID() {
        // Production identity: sync notifications always carry a Matrix event ID.
        let store = ForegroundInAppBannerStore(displayDuration: .seconds(100), sleep: { _ in })
        store.updateContext(isForeground: true, visibleRoomID: nil, isInputActive: false)
        let first = candidate(eventID: "$same-ms-a", date: date.addingTimeInterval(0.123))
        let second = ForegroundBannerCandidate(roomID: first.roomID, eventID: "$same-ms-b",
                                               latestActivity: first.latestActivity, title: "Room", preview: "Other")
        store.receive(first)
        XCTAssertEqual(store.current, first)
        store.receive(second)
        XCTAssertEqual(store.current, second)
        store.receive(first)
        XCTAssertEqual(store.current, second) // A presented event stays consumed.
    }

    func testEventsTenSecondsApartEachShowThenExpireAndBurstResetsTimer() async {
        let clock = TestClock()
        let store = ForegroundInAppBannerStore(displayDuration: .seconds(3), sleep: { duration in await clock.sleep(duration) })
        store.updateContext(isForeground: true, visibleRoomID: nil, isInputActive: false)
        // ~10s apart: every event gets its own banner and its own full timer.
        for index in 0 ..< 3 {
            let value = candidate(eventID: "$gap-\(index)", date: date.addingTimeInterval(Double(index) * 10))
            store.receive(value)
            await clock.waitForSleeps(index + 1)
            XCTAssertEqual(store.current, value)
            await clock.advance()
            await waitForDismissal(store)
            XCTAssertNil(store.current)
        }
        // Within 2s: each event replaces the banner and restarts the timer;
        // earlier timers cannot dismiss the newest banner.
        var latest: ForegroundBannerCandidate?
        for index in 0 ..< 3 {
            let value = candidate(eventID: "$burst-\(index)", date: date.addingTimeInterval(40 + Double(index) * 0.9))
            store.receive(value)
            await clock.waitForSleeps(4 + index)
            XCTAssertEqual(store.current, value)
            latest = value
        }
        let durations = await clock.durations()
        XCTAssertEqual(durations, Array(repeating: .seconds(3), count: 6))
        for _ in 0 ..< 2 {
            await clock.advance()
            await Task.yield()
            XCTAssertEqual(store.current, latest)
        }
        await clock.advance()
        await waitForDismissal(store)
        XCTAssertNil(store.current)
    }

    func testTransientInactivityDoesNotReportBackgroundResume() {
        var resume = ForegroundBannerResumeState()
        XCTAssertFalse(resume.becameActive())
        XCTAssertTrue(resume.shouldPresent(candidate(eventID: "$live"), suppressedEvents: []))
    }

    func testBackgroundResumeSuppressesOnlyDeliveredEvents() {
        var resume = ForegroundBannerResumeState()
        resume.enteredBackground()
        let first = candidate(eventID: "$first", date: date.addingTimeInterval(1))
        XCTAssertFalse(resume.shouldPresent(first, suppressedEvents: []))
        XCTAssertTrue(resume.becameActive())
        let background = candidate(eventID: "$background", date: date.addingTimeInterval(2))
        let delivered = [PushEventReference(roomID: background.roomID, eventID: "$background")]
        XCTAssertFalse(resume.shouldPresent(background, suppressedEvents: delivered))
        XCTAssertFalse(resume.becameActive())
        // Same room and same timestamp, different event: not suppressed.
        let sameMillisecond = candidate(eventID: "$live", date: background.latestActivity)
        XCTAssertTrue(resume.shouldPresent(sameMillisecond, suppressedEvents: delivered))
    }

    func testRoomRefreshCommitGateRejectsReplacedOrCancelledOnlineRefresh() {
        for cancelled in [false, true] {
            let current = UUID()
            let generation = cancelled ? current : UUID()
            var published = 0
            XCTAssertFalse(RoomRefreshCommitGate.commit(onlineGeneration: generation, currentGeneration: current,
                                                       isCancelled: cancelled) { published += 1 })
            XCTAssertEqual(published, 0)
            XCTAssertTrue(RoomRefreshCommitGate.commit(onlineGeneration: current, currentGeneration: current,
                                                      isCancelled: false) { published += 1 })
            // Local refreshes carry no online generation and always publish.
            XCTAssertTrue(RoomRefreshCommitGate.commit(onlineGeneration: nil, currentGeneration: current,
                                                      isCancelled: true) { published += 1 })
            XCTAssertEqual(published, 2)
        }
    }

    private func waitForDismissal(_ store: ForegroundInAppBannerStore) async {
        guard store.current != nil else { return }
        let dismissed = expectation(description: "banner expires")
        let observation = store.$current.dropFirst().sink { if $0 == nil { dismissed.fulfill() } }
        await fulfillment(of: [dismissed], timeout: 1)
        observation.cancel()
    }

    private func candidate(roomID: String = "!room:test", eventID: String = "$one", date: Date? = nil, own: Bool = false) -> ForegroundBannerCandidate {
        ForegroundBannerCandidate(roomID: roomID, eventID: eventID, latestActivity: date ?? self.date,
                                  title: "Room", preview: "Body", isOwn: own)
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
