// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

@testable import BotvilleCore
import MatrixRustSDK
import XCTest

final class DeliveryStateTests: XCTestCase {
    func testProductionActionsRetryExactTransactionItem() async throws {
        let timeline = RecordingTimeline(retryResult: false, cancelResult: true)
        let actions = SDKTimelineSendActions(timeline: timeline, transactionID: "tx-1")

        let result = try await actions.retry()
        XCTAssertFalse(result)
        XCTAssertEqual(timeline.retryArguments?.id, .transactionId(transactionId: "tx-1"))
        XCTAssertEqual(timeline.retryArguments?.target, .event)
    }

    func testProductionActionsCancelExactTransactionItem() async throws {
        let timeline = RecordingTimeline(retryResult: true, cancelResult: false)
        let actions = SDKTimelineSendActions(timeline: timeline, transactionID: "tx-race")

        let result = try await actions.cancel()
        XCTAssertFalse(result)
        XCTAssertEqual(timeline.cancelArguments?.id, .transactionId(transactionId: "tx-race"))
        XCTAssertEqual(timeline.cancelArguments?.target, .event)
    }

    func testPausedFailureOffersRoomRetryAndOnlyParkedFailureOffersCancel() {
        XCTAssertEqual(MessageDeliveryState.queued.displayName, "等待发送")
        XCTAssertFalse(MessageDeliveryState.queued.isFailed)
        XCTAssertFalse(MessageDeliveryState.queued.canCancel)
        XCTAssertEqual(MessageDeliveryState.paused.displayName, "发送失败，已暂停")
        XCTAssertTrue(MessageDeliveryState.paused.isFailed)
        XCTAssertTrue(MessageDeliveryState.paused.canRetry)
        XCTAssertFalse(MessageDeliveryState.paused.canCancel)
        XCTAssertTrue(MessageDeliveryState.failed.isFailed)
        XCTAssertTrue(MessageDeliveryState.failed.canRetry)
        XCTAssertTrue(MessageDeliveryState.failed.canCancel)
        XCTAssertFalse(MessageDeliveryState.sent.canCancel)
    }

    func testSDKLocalSendStateProjectsQueuedFailedAndSent() {
        let projection = [
            EventSendState.notSentYet(progress: nil),
            .sendingFailed(error: .genericApiError(msg: "recoverable"), isRecoverable: true),
            .sendingFailed(error: .genericApiError(msg: "test"), isRecoverable: false),
            .notSentYet(progress: nil),
            .sent(eventId: "$event")
        ].compactMap(MessageDeliveryState.init)
        XCTAssertEqual(projection, [.queued, .paused, .failed, .queued, .sent])
    }

    func testReconnectBackoffCapsAndResets() {
        var policy = ReconnectPolicy(initialDelay: 1, maximumDelay: 30)
        XCTAssertEqual(policy.nextDelay(), 1)
        XCTAssertEqual(policy.nextDelay(), 2)
        XCTAssertEqual(policy.nextDelay(), 4)
        XCTAssertEqual(policy.nextDelay(), 8)
        XCTAssertEqual(policy.nextDelay(), 16)
        XCTAssertEqual(policy.nextDelay(), 30)
        XCTAssertEqual(policy.nextDelay(), 30)
        policy.reset()
        XCTAssertEqual(policy.consecutiveFailures, 0)
        XCTAssertEqual(policy.nextDelay(), 1)
    }

    func testConnectionStatusNamesExposeOfflineAndReconnectState() {
        XCTAssertEqual(ConnectionStatus.offline.displayName, "离线")
        XCTAssertEqual(ConnectionStatus.reconnecting(after: 1.2).displayName, "将在 2 秒后重连")
        XCTAssertEqual(ConnectionStatus.connected.displayName, "已连接")
    }

    func testNoopPushRegistrarMakesNoConfigurationAssumptions() async throws {
        let registration = PushRegistration(
            appID: "com.zjyjcl.botville.apns",
            pushKey: "test-key",
            appDisplayName: "botville",
            data: try PushPusherData(url: XCTUnwrap(URL(string: "https://push.example.org/_matrix/push/v1/notify")))
        )
        let registrar = NoopPushRegistrar()
        try await registrar.register(registration)
        try await registrar.unregister()
    }
}

private final class RecordingTimeline: Timeline, @unchecked Sendable {
    struct Arguments: Equatable {
        let id: EventOrTransactionId
        let target: SendTarget
    }

    private let retryResult: Bool
    private let cancelResult: Bool
    private(set) var retryArguments: Arguments?
    private(set) var cancelArguments: Arguments?

    init(retryResult: Bool, cancelResult: Bool) {
        self.retryResult = retryResult
        self.cancelResult = cancelResult
        super.init(noHandle: .init())
    }

    required init(unsafeFromHandle handle: UInt64) {
        retryResult = false
        cancelResult = false
        super.init(unsafeFromHandle: handle)
    }

    override func retrySend(itemId: EventOrTransactionId, target: SendTarget) async throws -> Bool {
        retryArguments = Arguments(id: itemId, target: target)
        return retryResult
    }

    override func abortSend(itemId: EventOrTransactionId, target: SendTarget) async throws -> Bool {
        cancelArguments = Arguments(id: itemId, target: target)
        return cancelResult
    }
}
