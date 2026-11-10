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
    override func getRoom(roomId: String) throws -> Room? { roomId == room.id() ? room : nil }
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
    init() { super.init(noHandle: .init()) }
    required init(unsafeFromHandle handle: UInt64) { fatalError("test fake") }
    override func markAsRead(receiptType: ReceiptType) async throws { markReadCalls += 1 }
    override func addListener(listener: TimelineListener) async -> TaskHandle { LeaseFakeTaskHandle() }
}

private final class LeaseFakeTaskHandle: TaskHandle, @unchecked Sendable {
    init() { super.init(noHandle: .init()) }
    required init(unsafeFromHandle handle: UInt64) { fatalError("test fake") }
    override func cancel() {}
}
