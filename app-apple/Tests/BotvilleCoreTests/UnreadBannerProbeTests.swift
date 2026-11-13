// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

#if DEBUG
@testable import BotvilleCore
import XCTest

@MainActor
final class UnreadBannerProbeTests: XCTestCase {
    func testFingerprintIsExactlyEightLowercaseSHA256HexCharacters() {
        XCTAssertEqual(UnreadBannerProbeLog.fingerprint("abc"), "ba7816bf")
        XCTAssertNotNil(UnreadBannerProbeLog.fingerprint("!room:test").range(of: "^[0-9a-f]{8}$", options: .regularExpression))
    }

    func testEverySourceOutcomeAndReasonUsesSanitizedAllowlistedRecord() {
        var lines = [String]()
        UnreadBannerProbeLog.observer = { lines.append($0) }
        defer { UnreadBannerProbeLog.observer = nil }
        let room = "!private-room:test"
        let refresh = UUID()
        for source in UnreadBannerProbeLog.UnreadSource.allCases {
            UnreadBannerProbeLog.unread(source, roomID: room, before: 3, after: 0, muted: true,
                                       refresh: refresh, online: true, now: 42)
            XCTAssertTrue(lines.last!.contains("source=\(source.rawValue)"))
            XCTAssertTrue(lines.last!.contains("before=3 after=0 muted=true"))
        }
        UnreadBannerProbeLog.unread(.sdkInfoUnavailable, roomID: room, before: nil, after: nil, now: 42)
        XCTAssertTrue(lines.last!.contains("source=sdkInfoUnavailable"))
        XCTAssertTrue(lines.last!.contains("before=- after=-"))
        for outcome in UnreadBannerProbeLog.RefreshOutcome.allCases {
            UnreadBannerProbeLog.refresh(refresh, online: true, outcome: outcome, now: 42)
            XCTAssertTrue(lines.last!.contains("online=true outcome=\(outcome.rawValue)"))
        }
        for reason in UnreadBannerProbeLog.BannerReason.allCases {
            UnreadBannerProbeLog.banner(roomID: room, reason: reason, now: 42)
            XCTAssertTrue(lines.last!.contains("reason=\(reason.rawValue)"))
        }
        for line in lines {
            XCTAssertTrue(line.contains("t=42.0"))
            for raw in [room, refresh.uuidString, "private message", "@private-user:test", "$private-event"] {
                XCTAssertFalse(line.contains(raw))
            }
        }
    }

    func testStoreRecordsActualSuppressionAndDeduplicationReasonsWithoutContent() {
        var lines = [String]()
        UnreadBannerProbeLog.observer = { lines.append($0) }
        defer { UnreadBannerProbeLog.observer = nil }
        let room = "!private-room:test"
        let value = ForegroundBannerCandidate(roomID: room, eventID: "$private-event", latestActivity: Date(),
                                             title: "@private-user:test", preview: "private message")
        let store = ForegroundInAppBannerStore()
        store.receive(value)
        XCTAssertTrue(lines.last!.contains("reason=notForeground"))
        store.updateContext(isForeground: true, visibleRoomID: nil, isInputActive: false, mutedRoomIDs: [room])
        store.receive(value)
        XCTAssertTrue(lines.last!.contains("reason=muted"))
        store.updateContext(isForeground: true, visibleRoomID: nil, isInputActive: true)
        store.receive(value)
        XCTAssertTrue(lines.last!.contains("reason=inputActive"))
        store.updateContext(isForeground: true, visibleRoomID: room, isInputActive: false)
        store.receive(value)
        XCTAssertTrue(lines.last!.contains("reason=visibleRoom"))
        store.updateContext(isForeground: true, visibleRoomID: nil, isInputActive: false)
        store.receive(value)
        XCTAssertTrue(lines.last!.contains("reason=alreadyHandled"))
        store.receive(ForegroundBannerCandidate(roomID: room, latestActivity: Date(), title: value.title,
                                               preview: value.preview, isOwn: true))
        XCTAssertTrue(lines.last!.contains("reason=ownMessage"))
        for line in lines {
            for raw in [room, value.eventID!, value.title, value.preview] { XCTAssertFalse(line.contains(raw)) }
        }
    }

    func testSuppressionBatchKeepsLatestPerKeyAndRemainsBounded() {
        var latest = ForegroundSyncSuppressionBatch()
        latest.enqueue(reason: .muted, roomID: "!one:test", now: 1)
        latest.enqueue(reason: .muted, roomID: "!one:test", now: 2)
        XCTAssertEqual(latest.drain(), [.init(reason: .muted, roomID: "!one:test", now: 2)])

        var batch = ForegroundSyncSuppressionBatch()
        for index in 0...ForegroundSyncSuppressionBatch.maximumPending {
            batch.enqueue(reason: .notForeground, roomID: "!\(index):test", now: 3)
        }
        let entries = batch.drain()
        XCTAssertLessThanOrEqual(entries.count, ForegroundSyncSuppressionBatch.maximumPending)
        XCTAssertEqual(entries.last?.roomID, "!\(ForegroundSyncSuppressionBatch.maximumPending):test")
        XCTAssertTrue(batch.drain().isEmpty)
    }

    func testReadReceiptRecordsLocalZeroOnlyOnSuccess() {
        var lines = [String]()
        UnreadBannerProbeLog.observer = { lines.append($0) }
        defer { UnreadBannerProbeLog.observer = nil }
        let room = MatrixRoom(id: "!private-room:test", name: "private message", membership: .joined, unreadCount: 3)
        XCTAssertEqual(MatrixService.roomsAfterReadReceipt([room], roomID: room.id, succeeded: false)[0].unreadCount, 3)
        XCTAssertTrue(lines.isEmpty)
        XCTAssertEqual(MatrixService.roomsAfterReadReceipt([room], roomID: room.id, succeeded: true)[0].unreadCount, 0)
        XCTAssertEqual(lines.count, 1)
        XCTAssertTrue(lines[0].contains("source=readReceiptLocalZero"))
        XCTAssertTrue(lines[0].contains("before=3 after=0"))
        XCTAssertFalse(lines[0].contains(room.id))
        XCTAssertFalse(lines[0].contains(room.name))
    }
}
#endif
