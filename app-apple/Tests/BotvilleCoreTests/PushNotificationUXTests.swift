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

    func testForegroundPresentationPolicyIncludesAllVisibleChoices() {
        XCTAssertEqual(PushNotificationPresentation.foreground, [.banner, .list, .sound, .badge])
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
}
