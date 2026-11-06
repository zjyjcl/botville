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
        XCTAssertTrue(detector.candidates(in: [snapshot()]).isEmpty)
        let newer = candidate(eventID: "$two", date: date.addingTimeInterval(1))
        XCTAssertEqual(detector.candidates(in: [snapshot(candidate: newer, activity: newer.latestActivity)]), [newer])
        XCTAssertTrue(detector.candidates(in: [snapshot(candidate: newer, activity: newer.latestActivity)]).isEmpty)
    }

    func testDetectorEmitsUnreadIncreaseAndSkipsOwnOrInvitedRooms() {
        var detector = ForegroundBannerDetector()
        _ = detector.candidates(in: [snapshot()])
        let unreadOnly = candidate(eventID: "$unread", date: date)
        XCTAssertEqual(detector.candidates(in: [snapshot(candidate: unreadOnly, unread: 1)]), [unreadOnly])
        let own = candidate(eventID: "$own", date: date.addingTimeInterval(1), own: true)
        XCTAssertTrue(detector.candidates(in: [snapshot(candidate: own, unread: 1, activity: own.latestActivity)]).isEmpty)
        let invited = snapshot(candidate: candidate(eventID: "$invite", date: date.addingTimeInterval(2)), unread: 2,
                               membership: .invited, activity: date.addingTimeInterval(2))
        XCTAssertTrue(detector.candidates(in: [invited]).isEmpty)
    }

    func testDetectorTreatsNewRoomAndResetAsBaselines() {
        var detector = ForegroundBannerDetector()
        _ = detector.candidates(in: [snapshot()])
        let added = candidate(roomID: "!new:test", eventID: "$new", date: date.addingTimeInterval(1))
        XCTAssertTrue(detector.candidates(in: [snapshot(), snapshot(roomID: "!new:test", candidate: added,
                                                               unread: 1, activity: added.latestActivity)]).isEmpty)
        detector.reset()
        XCTAssertTrue(detector.candidates(in: [snapshot(candidate: added, unread: 1,
                                                         activity: added.latestActivity)]).isEmpty)
    }

    func testDetectorUsesTimestampWhenAnEventIDIsUnavailable() {
        var detector = ForegroundBannerDetector()
        _ = detector.candidates(in: [snapshot()])
        let first = ForegroundBannerCandidate(roomID: "!room:test", latestActivity: date.addingTimeInterval(1),
                                              title: "Room", preview: "Body")
        XCTAssertEqual(detector.candidates(in: [snapshot(candidate: first, unread: 1, activity: first.latestActivity)]), [first])
        XCTAssertTrue(detector.candidates(in: [snapshot(candidate: first, unread: 2, activity: first.latestActivity)]).isEmpty)
    }

    func testDetectorSortsSimultaneousRoomsFromOldestToNewest() {
        var detector = ForegroundBannerDetector()
        _ = detector.candidates(in: [snapshot(roomID: "!a:test"), snapshot(roomID: "!b:test")])
        let first = candidate(roomID: "!a:test", eventID: "$a", date: date.addingTimeInterval(1))
        let second = candidate(roomID: "!b:test", eventID: "$b", date: date.addingTimeInterval(2))
        XCTAssertEqual(detector.candidates(in: [snapshot(roomID: "!b:test", candidate: second, activity: second.latestActivity),
                                              snapshot(roomID: "!a:test", candidate: first, activity: first.latestActivity)]), [first, second])
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
