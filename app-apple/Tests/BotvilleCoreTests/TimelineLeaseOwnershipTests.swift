// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

@testable import BotvilleCore
import Foundation
import MatrixRustSDK
import XCTest

final class TimelineLeaseOwnershipTests: XCTestCase {
    func testStaleCloseCannotCloseReopenedSameRoom() {
        var ownership = TimelineLeaseOwnership()
        let oldLease = ownership.open(roomID: "!room:matrix.test")
        let currentLease = ownership.open(roomID: "!room:matrix.test")

        XCTAssertFalse(ownership.close(roomID: "!room:matrix.test", lease: oldLease))
        XCTAssertEqual(ownership.activeLease, currentLease)
    }

    func testCancelledOpenCompletionCannotReleaseNewLease() {
        var ownership = TimelineLeaseOwnership()
        let cancelledOpen = ownership.open(roomID: "!room:matrix.test")
        let replacement = ownership.open(roomID: "!room:matrix.test")

        XCTAssertFalse(ownership.close(roomID: "!room:matrix.test", lease: cancelledOpen))
        XCTAssertEqual(ownership.activeLease, replacement)
    }

    func testTabReturnGetsFreshLeaseForReopen() {
        var ownership = TimelineLeaseOwnership()
        let beforeTabSwitch = ownership.open(roomID: "!room:matrix.test")
        XCTAssertTrue(ownership.close(roomID: "!room:matrix.test", lease: beforeTabSwitch))

        let afterTabReturn = ownership.open(roomID: "!room:matrix.test")
        XCTAssertNotEqual(beforeTabSwitch, afterTabReturn)
        XCTAssertEqual(ownership.activeLease, afterTabReturn)
    }

    @MainActor
    func testTabReturnReopensTimelineAndMarksItRead() async throws {
        let fixture = try await LeaseFixture.make()
        defer { try? FileManager.default.removeItem(at: fixture.directory) }

        let beforeTabSwitch = try await fixture.service.openRoom(fixture.room.id())
        fixture.service.closeRoom(fixture.room.id(), lease: beforeTabSwitch)
        let afterTabReturn = try await fixture.service.openRoom(fixture.room.id())

        XCTAssertNotEqual(beforeTabSwitch, afterTabReturn)
        XCTAssertEqual(fixture.room.timelineCalls, 2)
        for _ in 0 ..< 100 where fixture.timeline.markReadCalls < 1 {
            try await Task.sleep(for: .milliseconds(5))
        }
        // The cancelled off-screen open must not mark the room read; only the
        // visible reopened timeline owns the receipt.
        XCTAssertEqual(fixture.timeline.markReadCalls, 1)
        try await fixture.service.clearLocalSession()
    }

    @MainActor
    func testListenerRegistrationDoesNotPretendFirstSnapshotHasArrived() async throws {
        let fixture = try await LeaseFixture.make()
        defer { try? FileManager.default.removeItem(at: fixture.directory) }
        _ = try await fixture.service.openRoom(fixture.room.id())
        XCTAssertFalse(fixture.service.hasReceivedTimelineSnapshot)
        XCTAssertTrue(fixture.service.messages.isEmpty)
        // A genuinely empty first snapshot ends the waiting state too.
        fixture.timeline.listeners[0].onUpdate(diff: [.reset(values: [])])
        for _ in 0..<100 where !fixture.service.hasReceivedTimelineSnapshot {
            try await Task.sleep(for: .milliseconds(5))
        }
        XCTAssertTrue(fixture.service.hasReceivedTimelineSnapshot)
        XCTAssertTrue(fixture.service.messages.isEmpty)
        try await fixture.service.clearLocalSession()
    }

    func testSnapshotReadinessIgnoresNoOpsButAcceptsNonTextAndEmptyReset() {
        var state = TimelineState()
        state.apply([])
        state.apply([.append(values: []), .remove(index: 9), .popFront, .popBack, .truncate(length: 0)])
        XCTAssertFalse(state.hasReceivedSnapshot)
        state.apply([.pushBack(value: LeaseNonTextItem())])
        XCTAssertTrue(state.hasReceivedSnapshot)
        XCTAssertTrue(state.messages.isEmpty)
        state = TimelineState()
        state.apply([.reset(values: [])])
        XCTAssertTrue(state.hasReceivedSnapshot)
        state = TimelineState()
        state.apply([.clear])
        XCTAssertTrue(state.hasReceivedSnapshot)
    }

    func testPageSnapshotRequiresInitialResetAndNeverUsesLaterIncrementalDiffs() {
        let snapshot = TimelinePageSnapshot()
        snapshot.onUpdate(diff: [])
        snapshot.onUpdate(diff: [.pushBack(value: LeaseNonTextItem())])
        XCTAssertNil(snapshot.messageIDs())
        snapshot.onUpdate(diff: [.reset(values: [LeaseNonTextItem()])])
        XCTAssertEqual(snapshot.messageIDs(), [])
        snapshot.onUpdate(diff: [.clear, .reset(values: [])])
        XCTAssertEqual(snapshot.messageIDs(), [])
    }

    @MainActor
    func testPaginationUsesIndependentSnapshotFenceAndCancelsOnlyItsHandle() async throws {
        let fixture = try await LeaseFixture.make()
        defer { try? FileManager.default.removeItem(at: fixture.directory) }
        _ = try await fixture.service.openRoom(fixture.room.id())
        fixture.timeline.pageSnapshot = []
        let expected = try await fixture.service.loadOlderMessages()
        XCTAssertEqual(expected, []) // Explicitly acknowledged no text, no inc diff.
        XCTAssertEqual(fixture.timeline.paginationCalls, 1)
        XCTAssertEqual(fixture.timeline.listeners.count, 2)
        XCTAssertFalse(fixture.timeline.handles[0].cancelled)
        XCTAssertTrue(fixture.timeline.handles[1].cancelled)
        // A fence must not overwrite or reset the primary stream's state.
        XCTAssertFalse(fixture.service.hasReceivedTimelineSnapshot)
        fixture.timeline.listeners[0].onUpdate(diff: [.reset(values: [LeaseNonTextItem()])])
        for _ in 0..<100 where !fixture.service.hasReceivedTimelineSnapshot {
            try await Task.sleep(for: .milliseconds(5))
        }
        XCTAssertTrue(fixture.service.hasReceivedTimelineSnapshot)
        try await fixture.service.clearLocalSession()
    }

    @MainActor
    func testMissingSDKSnapshotDoesNotInventAnEmptyPageAcknowledgement() async throws {
        let fixture = try await LeaseFixture.make()
        defer { try? FileManager.default.removeItem(at: fixture.directory) }
        _ = try await fixture.service.openRoom(fixture.room.id())
        let expected = try await fixture.service.loadOlderMessages()
        XCTAssertNil(expected)
        XCTAssertTrue(fixture.timeline.handles[1].cancelled)
        try await fixture.service.clearLocalSession()
    }

    @MainActor
    func testSynchronousLookupErrorAlsoClearsCurrentLease() async throws {
        let fixture = try await LeaseFixture.make()
        defer { try? FileManager.default.removeItem(at: fixture.directory) }
        let roomID = "!lookup-error:matrix.test"
        do {
            _ = try await fixture.service.openRoom(roomID)
            XCTFail("Expected lookup error")
        } catch MatrixServiceError.operationFailed {}
        XCTAssertNil(fixture.service.activeRoomID)
        XCTAssertFalse(fixture.service.hasReceivedTimelineSnapshot)
        try await fixture.service.clearLocalSession()
    }

    @MainActor
    func testEmptyDiffAndEmptyAppendDoNotEndSnapshotWait() async throws {
        let fixture = try await LeaseFixture.make()
        defer { try? FileManager.default.removeItem(at: fixture.directory) }
        _ = try await fixture.service.openRoom(fixture.room.id())
        fixture.timeline.listeners[0].onUpdate(diff: [])
        fixture.timeline.listeners[0].onUpdate(diff: [.append(values: [])])
        // Let the stream consume both callbacks before checking readiness.
        try await Task.sleep(for: .milliseconds(30))
        XCTAssertFalse(fixture.service.hasReceivedTimelineSnapshot)
        fixture.timeline.listeners[0].onUpdate(diff: [.reset(values: [])])
        for _ in 0..<100 where !fixture.service.hasReceivedTimelineSnapshot {
            try await Task.sleep(for: .milliseconds(5))
        }
        XCTAssertTrue(fixture.service.hasReceivedTimelineSnapshot)
        try await fixture.service.clearLocalSession()
    }

    @MainActor
    func testUnavailableRoomEarlyThrowClearsCurrentLease() async throws {
        let fixture = try await LeaseFixture.make()
        defer { try? FileManager.default.removeItem(at: fixture.directory) }
        let missing = "!missing:matrix.test"
        let lease = fixture.service.beginRoomOpen(missing)
        do {
            _ = try await fixture.service.openRoom(missing, lease: lease)
            XCTFail("Expected unavailable room")
        } catch MatrixServiceError.roomUnavailable {}
        XCTAssertNil(fixture.service.activeRoomID)
        XCTAssertFalse(fixture.service.hasReceivedTimelineSnapshot)
        XCTAssertFalse(fixture.service.canLoadOlderMessages)
        try await fixture.service.clearLocalSession()
    }

    @MainActor
    func testNotLoggedInEarlyThrowClearsCurrentLease() async throws {
        let fixture = try await LeaseFixture.make()
        defer { try? FileManager.default.removeItem(at: fixture.directory) }
        try await fixture.service.clearLocalSession()
        let lease = fixture.service.beginRoomOpen(fixture.room.id())
        do {
            _ = try await fixture.service.openRoom(fixture.room.id(), lease: lease)
            XCTFail("Expected logged out error")
        } catch MatrixServiceError.notLoggedIn {}
        XCTAssertNil(fixture.service.activeRoomID)
        XCTAssertFalse(fixture.service.hasReceivedTimelineSnapshot)
    }

    @MainActor
    func testStaleOpenEarlyThrowPreservesNewSnapshotAndLease() async throws {
        let fixture = try await LeaseFixture.make()
        defer { try? FileManager.default.removeItem(at: fixture.directory) }
        let old = fixture.service.beginRoomOpen(fixture.room.id())
        let current = try await fixture.service.openRoom(fixture.room.id())
        fixture.timeline.listeners[0].onUpdate(diff: [.reset(values: [])])
        for _ in 0..<100 where !fixture.service.hasReceivedTimelineSnapshot {
            try await Task.sleep(for: .milliseconds(5))
        }
        do {
            _ = try await fixture.service.openRoom(fixture.room.id(), lease: old)
            XCTFail("Expected stale lease cancellation")
        } catch is CancellationError {}
        XCTAssertEqual(fixture.service.activeRoomID, fixture.room.id())
        XCTAssertTrue(fixture.service.hasReceivedTimelineSnapshot)
        fixture.service.closeRoom(fixture.room.id(), lease: current)
        XCTAssertNil(fixture.service.activeRoomID)
        try await fixture.service.clearLocalSession()
    }

    @MainActor
    func testOldListenerAndStaleCloseCannotChangeReopenedSnapshotReadiness() async throws {
        let fixture = try await LeaseFixture.make()
        defer { try? FileManager.default.removeItem(at: fixture.directory) }
        let old = try await fixture.service.openRoom(fixture.room.id())
        fixture.service.closeRoom(fixture.room.id(), lease: old)
        let current = try await fixture.service.openRoom(fixture.room.id())
        fixture.timeline.listeners[0].onUpdate(diff: [.reset(values: [])])
        await Task.yield()
        XCTAssertFalse(fixture.service.hasReceivedTimelineSnapshot)
        fixture.timeline.listeners[1].onUpdate(diff: [.reset(values: [])])
        for _ in 0..<100 where !fixture.service.hasReceivedTimelineSnapshot {
            try await Task.sleep(for: .milliseconds(5))
        }
        XCTAssertTrue(fixture.service.hasReceivedTimelineSnapshot)
        fixture.service.closeRoom(fixture.room.id(), lease: old)
        XCTAssertTrue(fixture.service.hasReceivedTimelineSnapshot)
        XCTAssertEqual(fixture.service.activeRoomID, fixture.room.id())
        fixture.service.closeRoom(fixture.room.id(), lease: current)
        XCTAssertFalse(fixture.service.hasReceivedTimelineSnapshot)
        try await fixture.service.clearLocalSession()
    }
}

private struct LeaseFixture {
    let service: MatrixService
    let room: LeaseFakeRoom
    let timeline: LeaseFakeTimeline
    let directory: URL

    @MainActor static func make() async throws -> Self {
        let directory = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent(".build/timeline-lease-tests/\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let timeline = LeaseFakeTimeline()
        let room = LeaseFakeRoom(id: "!lease:matrix.test", timeline: timeline)
        let client = LeaseFakeClient(room: room)
        let credentials = SessionCredentials(accessToken: "fixture", refreshToken: nil,
                                             userID: "@lease:matrix.test", deviceID: "LEASE",
                                             homeserverURL: "https://matrix.test", oauthData: nil,
                                             storeID: UUID(), storeKey: Data(repeating: 2, count: 32))
        let service = MatrixService(sessionStore: InMemorySessionStore(credentials),
                                    sharedPushCredentialsStore: InMemorySharedPushCredentialsStore(),
                                    sharedPushCredentialsCleanupStateStore: LeaseCleanupState(),
                                    storeDirectory: SessionStoreDirectory(baseURL: directory),
                                    discovery: LeaseDiscovery(), monitorNetwork: false)
        service.clientFactory = { _, _, _ in client }
        await service.restoreSession()
        return Self(service: service, room: room, timeline: timeline, directory: directory)
    }
}

private final class LeaseCleanupState: SharedPushCredentialsCleanupStateStore, @unchecked Sendable {
    var isCleanupRequired = false
    func setCleanupRequired(_ required: Bool) { isCleanupRequired = required }
}

private struct LeaseDiscovery: LoginCapabilityDiscovering {
    func discover(at homeserver: URL) async throws -> LoginCapabilities {
        LoginCapabilities(supportsPassword: true)
    }
}

private final class LeaseFakeClient: Client, @unchecked Sendable {
    private let room: LeaseFakeRoom

    init(room: LeaseFakeRoom) { self.room = room; super.init(noHandle: .init()) }
    required init(unsafeFromHandle handle: UInt64) { fatalError("test fake") }
    override func restoreSession(session: Session) async throws {}
    override func setDelegate(delegate: ClientDelegate?) throws -> TaskHandle? { nil }
    override func registerNotificationHandler(listener: SyncNotificationListener) async {}
    override func rooms() -> [Room] { [room] }
    override func getRoom(roomId: String) throws -> Room? {
        if roomId == "!lookup-error:matrix.test" { throw ClientError.Generic(msg: "lookup failed", details: nil) }
        return roomId == room.id() ? room : nil
    }
    override func enableAllSendQueues(enable: Bool) async {}
    override func syncOnceV2(settings: SyncSettingsV2) async throws -> SyncResponseV2 {
        SyncResponseV2(nextBatch: "lease", rooms: SyncResponseRoomsV2(invited: [], joined: [], left: [], knocked: []))
    }
}

private final class LeaseFakeRoom: Room, @unchecked Sendable {
    private let roomID: String
    private let fakeTimeline: LeaseFakeTimeline
    private(set) var timelineCalls = 0

    init(id: String, timeline: LeaseFakeTimeline) {
        roomID = id; fakeTimeline = timeline; super.init(noHandle: .init())
    }
    required init(unsafeFromHandle handle: UInt64) { fatalError("test fake") }
    override func id() -> String { roomID }
    override func displayName() -> String? { "Lease test" }
    override func membership() -> Membership { .joined }
    override func roomInfo() async throws -> RoomInfo { throw ClientError.Generic(msg: "unused", details: nil) }
    override func latestEvent() async -> LatestEventValue { .none }
    override func isDirect() async -> Bool { false }
    override func latestEncryptionState() async throws -> EncryptionState { .unknown }
    override func timeline() async throws -> Timeline { timelineCalls += 1; return fakeTimeline }
}

private final class LeaseFakeTimeline: Timeline, @unchecked Sendable {
    private(set) var markReadCalls = 0
    private(set) var listeners: [TimelineListener] = []
    private(set) var handles: [LeaseFakeTaskHandle] = []
    private(set) var paginationCalls = 0
    var pageSnapshot: [TimelineItem]?
    init() { super.init(noHandle: .init()) }
    required init(unsafeFromHandle handle: UInt64) { fatalError("test fake") }
    override func markAsRead(receiptType: ReceiptType) async throws { markReadCalls += 1 }
    override func paginateBackwards(numEvents: UInt16) async throws -> Bool {
        paginationCalls += 1
        return true
    }
    override func addListener(listener: TimelineListener) async -> TaskHandle {
        listeners.append(listener)
        if listeners.count > 1, let pageSnapshot { listener.onUpdate(diff: [.reset(values: pageSnapshot)]) }
        let handle = LeaseFakeTaskHandle()
        handles.append(handle)
        return handle
    }
}

private final class LeaseFakeTaskHandle: TaskHandle, @unchecked Sendable {
    private(set) var cancelled = false
    init() { super.init(noHandle: .init()) }
    required init(unsafeFromHandle handle: UInt64) { fatalError("test fake") }
    override func cancel() { cancelled = true }
}

private final class LeaseNonTextItem: TimelineItem, @unchecked Sendable {
    init() { super.init(noHandle: .init()) }
    required init(unsafeFromHandle handle: UInt64) { fatalError("test fake") }
    override func asEvent() -> EventTimelineItem? { nil }
}
