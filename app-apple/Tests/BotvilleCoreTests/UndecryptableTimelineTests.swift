// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

@testable import BotvilleCore
import MatrixRustSDK
import XCTest

final class UndecryptableTimelineTests: XCTestCase {
    func testUndecryptableEventShowsPlaceholderInTimeline() {
        var state = TimelineState()
        state.apply([.reset(values: [item("utd", kind: utd())])])
        XCTAssertEqual(state.messages.map(\.id), ["utd"])
        XCTAssertEqual(state.messages.first?.body, MatrixMessage.undecryptablePlaceholder.body)
        XCTAssertEqual(state.messages.first?.isUndecryptable, true)
    }

    func testPlaceholderTextAndAccessibilityLabelShareOneConstant() {
        let placeholder = MatrixMessage.undecryptablePlaceholder
        XCTAssertEqual(placeholder.body, "无法解密此消息")
        XCTAssertEqual(placeholder.accessibilityLabel, "无法解密此消息，等待密钥后自动显示")
        XCTAssertTrue(placeholder.accessibilityLabel.hasPrefix(placeholder.body))
    }

    func testLateRoomKeyReplacesPlaceholderWithBodyInPlace() {
        var state = TimelineState()
        state.apply([.reset(values: [item("before", kind: text("前一条")), item("utd", kind: utd())])])
        // The placeholder must already occupy that slot before the key arrives.
        XCTAssertEqual(state.messages.map(\.id), ["before", "utd"])
        XCTAssertEqual(state.messages.last?.body, MatrixMessage.undecryptablePlaceholder.body)
        XCTAssertEqual(state.messages.last?.isUndecryptable, true)
        // The SDK retries decryption when the key arrives and replaces the same item.
        state.apply([.set(index: 1, value: item("utd", kind: text("密钥到达后的正文")))])
        XCTAssertEqual(state.messages.map(\.id), ["before", "utd"])
        XCTAssertEqual(state.messages.last?.body, "密钥到达后的正文")
        XCTAssertEqual(state.messages.last?.isUndecryptable, false)
    }

    func testRedactedAndCustomEventsStayHidden() {
        var state = TimelineState()
        state.apply([.reset(values: [item("redacted", kind: .redacted),
                                     item("other", kind: .other(eventType: .callInvite))])])
        XCTAssertTrue(state.messages.isEmpty)
    }

    private func utd() -> MsgLikeKind {
        .unableToDecrypt(msg: .megolmV1AesSha2(sessionId: "session", cause: .unknown))
    }

    private func text(_ body: String) -> MsgLikeKind {
        .message(content: MessageContent(msgType: .text(content: TextMessageContent(body: body, formatted: nil)),
                                         body: body, isEdited: false, mentions: nil))
    }

    private func item(_ id: String, kind: MsgLikeKind) -> TimelineItem {
        UTDTimelineItem(id: id, event: EventTimelineItem(
            isRemote: true,
            eventOrTransactionId: .eventId(eventId: "$\(id)"),
            sender: "@peer:matrix.test", senderProfile: .unavailable,
            forwarder: nil, forwarderProfile: nil, isOwn: false, isEditable: false,
            content: .msgLike(content: MsgLikeContent(kind: kind, inReplyTo: nil, threadRoot: nil, threadSummary: nil)),
            reactions: [], eventTypeRaw: "m.room.encrypted", timestamp: 1,
            localSendState: nil, editSendState: nil, redactionSendState: nil,
            localCreatedAt: nil, readReceipts: [:], origin: nil,
            canBeRepliedTo: false,
            lazyProvider: LazyTimelineItemProvider(noHandle: .init())
        ))
    }
}

private final class UTDTimelineItem: TimelineItem, @unchecked Sendable {
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
