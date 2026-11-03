// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

@testable import BotvilleCore
import Foundation
import MatrixRustSDK
import XCTest

@MainActor
final class SendQueueRecoveryTests: XCTestCase {
    func testRecoverableFailurePausesUntilExplicitRoomRetry() async throws {
        let fixture = try await makeFixture()
        defer { try? FileManager.default.removeItem(at: fixture.directory) }
        let itemID = "local-paused"
        let transactionID = "txn-paused"

        fixture.timeline.emit(.append(values: [timelineItem(
            id: itemID,
            transactionID: transactionID,
            state: .sendingFailed(error: .genericApiError(msg: "offline"), isRecoverable: true)
        )]))
        try await waitUntil { fixture.service.messages.first?.deliveryState == .paused }

        let paused = try XCTUnwrap(fixture.service.messages.first)
        XCTAssertEqual(paused.deliveryState, .paused)
        XCTAssertTrue(paused.deliveryState?.canRetry == true)
        XCTAssertFalse(paused.deliveryState?.canCancel == true)

        await fixture.service.applicationDidBecomeActive()
        await fixture.service.networkChanged(available: false)
        await fixture.service.networkChanged(available: true)
        try await Task.sleep(for: .milliseconds(20))
        XCTAssertEqual(fixture.room.queueEnableValues, [])

        try await fixture.service.retryMessage(itemID)
        XCTAssertEqual(fixture.room.queueEnableValues, [true])
        XCTAssertEqual(fixture.timeline.sendCalls, 0)
        XCTAssertEqual(fixture.timeline.retryArguments, [])

        do {
            try await fixture.service.cancelMessage(itemID)
            XCTFail("A recoverable paused send cannot be aborted")
        } catch MatrixServiceError.sendNoLongerFailed {
            // Expected: cancel applies only to SDK-parked sends.
        }
        XCTAssertEqual(fixture.timeline.abortArguments, [])

        fixture.timeline.emit(.set(index: 0, value: timelineItem(
            id: itemID,
            transactionID: transactionID,
            state: .sent(eventId: "$sent")
        )))
        try await waitUntil { fixture.service.messages.first?.deliveryState == .sent }

        do {
            try await fixture.service.retryMessage(itemID)
            XCTFail("A sent local echo cannot be retried")
        } catch MatrixServiceError.sendNoLongerFailed {
            // Expected: current timeline state governs retry eligibility.
        }
        XCTAssertEqual(fixture.room.queueEnableValues, [true])
    }

    func testUnrecoverableFailureUsesExactTimelineTransactionForRetryAndCancel() async throws {
        let fixture = try await makeFixture()
        defer { try? FileManager.default.removeItem(at: fixture.directory) }
        let itemID = "local-parked"
        let transactionID = "txn-parked"

        fixture.timeline.emit(.append(values: [timelineItem(
            id: itemID,
            transactionID: transactionID,
            state: .sendingFailed(error: .genericApiError(msg: "rejected"), isRecoverable: false)
        )]))
        try await waitUntil { fixture.service.messages.first?.deliveryState == .failed }

        try await fixture.service.retryMessage(itemID)
        try await fixture.service.cancelMessage(itemID)

        XCTAssertEqual(fixture.timeline.retryArguments, [.transactionId(transactionId: transactionID)])
        XCTAssertEqual(fixture.timeline.abortArguments, [.transactionId(transactionId: transactionID)])
        XCTAssertEqual(fixture.room.queueEnableValues, [])
        XCTAssertEqual(fixture.timeline.sendCalls, 0)
    }

    private func makeFixture() async throws -> Fixture {
        let directory = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent(".build/send-queue-tests/\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let credentials = SessionCredentials(
            accessToken: "test-access", refreshToken: nil,
            userID: "@queue-test:matrix.test", deviceID: "QUEUE",
            homeserverURL: "https://matrix.test", oauthData: nil,
            storeID: UUID(), storeKey: Data(repeating: 1, count: 32)
        )
        let timeline = FakeTimeline()
        let room = FakeRoom(id: "!queue:matrix.test", timeline: timeline)
        let client = FakeClient(room: room)
        let service = MatrixService(
            sessionStore: InMemorySessionStore(credentials),
            storeDirectory: SessionStoreDirectory(baseURL: directory),
            discovery: FakeDiscovery(), monitorNetwork: false
        )
        service.clientFactory = { _, _, _ in client }
        await service.restoreSession()
        try await waitUntil { service.connectionStatus == .connected }
        try await service.openRoom(room.id())
        return Fixture(service: service, room: room, timeline: timeline, directory: directory)
    }

    private func timelineItem(id: String, transactionID: String, state: EventSendState) -> TimelineItem {
        FakeTimelineItem(id: id, event: EventTimelineItem(
            isRemote: false,
            eventOrTransactionId: .transactionId(transactionId: transactionID),
            sender: "@queue-test:matrix.test", senderProfile: .unavailable,
            forwarder: nil, forwarderProfile: nil, isOwn: true, isEditable: true,
            content: .msgLike(content: MsgLikeContent(
                kind: .message(content: MessageContent(
                    msgType: .text(content: TextMessageContent(body: "queued", formatted: nil)),
                    body: "queued", isEdited: false, mentions: nil
                )),
                inReplyTo: nil, threadRoot: nil, threadSummary: nil
            )),
            reactions: [], eventTypeRaw: "m.room.message", timestamp: 1,
            localSendState: state, editSendState: nil, redactionSendState: nil,
            localCreatedAt: nil, readReceipts: [:], origin: nil,
            canBeRepliedTo: true,
            lazyProvider: LazyTimelineItemProvider(noHandle: .init())
        ))
    }

    private func waitUntil(_ condition: @escaping () -> Bool) async throws {
        for _ in 0 ..< 100 {
            if condition() { return }
            try await Task.sleep(for: .milliseconds(5))
        }
        XCTFail("Timed out waiting for timeline update")
    }
}

private struct Fixture {
    let service: MatrixService
    let room: FakeRoom
    let timeline: FakeTimeline
    let directory: URL
}

private struct FakeDiscovery: LoginCapabilityDiscovering {
    func discover(at homeserver: URL) async throws -> LoginCapabilities { LoginCapabilities(supportsPassword: true) }
}

private final class FakeClient: Client, @unchecked Sendable {
    private let room: FakeRoom

    init(room: FakeRoom) {
        self.room = room
        super.init(noHandle: .init())
    }

    required init(unsafeFromHandle handle: UInt64) {
        room = FakeRoom(id: "!unexpected:matrix.test", timeline: FakeTimeline())
        super.init(unsafeFromHandle: handle)
    }

    override func restoreSession(session: Session) async throws {}
    override func setDelegate(delegate: ClientDelegate?) throws -> TaskHandle? { nil }
    override func rooms() -> [Room] { [room] }
    override func getRoom(roomId: String) throws -> Room? { roomId == room.id() ? room : nil }
    override func enableAllSendQueues(enable: Bool) async {}
    override func syncOnceV2(settings: SyncSettingsV2) async throws -> SyncResponseV2 {
        SyncResponseV2(nextBatch: "test", rooms: SyncResponseRoomsV2(invited: [], joined: [], left: [], knocked: []))
    }
}

private final class FakeRoom: Room, @unchecked Sendable {
    private let roomID: String
    private let fakeTimeline: FakeTimeline
    private(set) var queueEnableValues: [Bool] = []

    init(id: String, timeline: FakeTimeline) {
        roomID = id
        fakeTimeline = timeline
        super.init(noHandle: .init())
    }

    required init(unsafeFromHandle handle: UInt64) {
        roomID = "!unexpected:matrix.test"
        fakeTimeline = FakeTimeline()
        super.init(unsafeFromHandle: handle)
    }

    override func id() -> String { roomID }
    override func displayName() -> String? { "Queue test" }
    override func membership() -> Membership { .joined }
    override func roomInfo() async throws -> RoomInfo { throw ClientError.Generic(msg: "unused", details: nil) }
    override func latestEvent() async -> LatestEventValue { .none }
    override func isDirect() async -> Bool { false }
    override func timeline() async throws -> Timeline { fakeTimeline }
    override func enableSendQueue(enable: Bool) { queueEnableValues.append(enable) }
}

private final class FakeTimeline: Timeline, @unchecked Sendable {
    private var listener: TimelineListener?
    private(set) var sendCalls = 0
    private(set) var retryArguments: [EventOrTransactionId] = []
    private(set) var abortArguments: [EventOrTransactionId] = []

    init() { super.init(noHandle: .init()) }

    required init(unsafeFromHandle handle: UInt64) { super.init(unsafeFromHandle: handle) }

    override func addListener(listener: TimelineListener) async -> TaskHandle {
        self.listener = listener
        return FakeTaskHandle()
    }

    override func send(msg: RoomMessageEventContentWithoutRelation) async throws -> SendHandle {
        sendCalls += 1
        throw ClientError.Generic(msg: "send should not be called", details: nil)
    }

    override func retrySend(itemId: EventOrTransactionId, target: SendTarget) async throws -> Bool {
        retryArguments.append(itemId)
        return true
    }

    override func abortSend(itemId: EventOrTransactionId, target: SendTarget) async throws -> Bool {
        abortArguments.append(itemId)
        return true
    }

    func emit(_ diff: TimelineDiff) { listener?.onUpdate(diff: [diff]) }
}

private final class FakeTaskHandle: TaskHandle, @unchecked Sendable {
    init() { super.init(noHandle: .init()) }
    required init(unsafeFromHandle handle: UInt64) { super.init(unsafeFromHandle: handle) }
    override func cancel() {}
}

private final class FakeTimelineItem: TimelineItem, @unchecked Sendable {
    private let itemID: String
    private let itemEvent: EventTimelineItem

    init(id: String, event: EventTimelineItem) {
        itemID = id
        itemEvent = event
        super.init(noHandle: .init())
    }

    required init(unsafeFromHandle handle: UInt64) {
        fatalError("Test fake must be constructed with an event")
    }

    override func asEvent() -> EventTimelineItem? { itemEvent }
    override func uniqueId() -> TimelineUniqueId { TimelineUniqueId(id: itemID) }
}
