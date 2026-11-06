// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

@testable import BotvilleCore
import XCTest

@MainActor
final class PushNotificationUXTests: XCTestCase {
    func testRouteAcceptsTopLevelAndKnownWrappedRoomIDs() {
        XCTAssertEqual(PushNotificationRoute.parse(["room_id": "!top:example.test"]), .room("!top:example.test"))
        XCTAssertEqual(PushNotificationRoute.parse([
            "notification": ["data": ["room_id": "!nested:example.test"]]
        ]), .room("!nested:example.test"))
    }

    func testRouteFallsBackToRoomListForAbsentOrInvalidRoomID() {
        XCTAssertEqual(PushNotificationRoute.parse([:]), .roomList)
        XCTAssertEqual(PushNotificationRoute.parse(["room_id": "not-a-room"]), .roomList)
        XCTAssertEqual(PushNotificationRoute.parse(["data": ["room_id": "!bad room:example.test"]]), .roomList)
    }

    func testBadgeSumsUnreadCountsAndSaturates() {
        XCTAssertEqual(PushBadge.total(unreadCounts: [1, 2, 9]), 12)
        XCTAssertEqual(PushBadge.total(unreadCounts: []), 0)
        XCTAssertEqual(PushBadge.total(unreadCounts: [UInt64.max, 1]), Int.max)
    }

    func testForegroundPresentationPolicyOnlyUpdatesBadge() {
        XCTAssertEqual(PushNotificationPresentation.foreground, [.badge])
        // Match UserNotifications.UNNotificationPresentationOptions raw bits (SDK 27.2).
        XCTAssertEqual(PushNotificationPresentation.foreground.unNotificationPresentationRawValue,
                       1 << 0)
        XCTAssertEqual(PushNotificationPresentation.banner.unNotificationPresentationRawValue, 1 << 4)
        XCTAssertEqual(PushNotificationPresentation.list.unNotificationPresentationRawValue, 1 << 3)
    }

    func testReadReceiptStateClearsOnlyTheOpenedRoomAfterSuccess() {
        let rooms = [
            MatrixRoom(id: "!opened:example.test", name: "opened", unreadCount: 5),
            MatrixRoom(id: "!other:example.test", name: "other", unreadCount: 3)
        ]
        XCTAssertEqual(MatrixService.roomsAfterReadReceipt(rooms, roomID: "!opened:example.test", succeeded: true)
            .map(\.unreadCount), [0, 3])
    }

    func testReadReceiptFailureKeepsExistingUnreadState() {
        let rooms = [MatrixRoom(id: "!opened:example.test", name: "opened", unreadCount: 5)]
        XCTAssertEqual(MatrixService.roomsAfterReadReceipt(rooms, roomID: "!opened:example.test", succeeded: false), rooms)
    }

    func testForegroundProbeLogLineHasNoSecretPlaceholders() {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        let date = formatter.date(from: "2026-11-06T01:00:00Z")!
        let line = PushForegroundProbeLog.makeLine(
            event: "willPresent",
            detail: "optionsRaw=27",
            at: date,
            formatter: formatter)
        XCTAssertEqual(line, "2026-11-06T01:00:00Z willPresent optionsRaw=27")
        XCTAssertFalse(line.lowercased().contains("token"))
        XCTAssertFalse(line.lowercased().contains("bearer"))
        XCTAssertEqual(PushForegroundProbeLog.fileName, "botville-willpresent-probe.log")
        XCTAssertEqual(PushForegroundProbeLog.appGroupIdentifier, "group.com.zjyjcl.botville")
    }

}
