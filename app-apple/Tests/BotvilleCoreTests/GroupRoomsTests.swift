// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import XCTest

@testable import BotvilleCore

final class GroupRoomsTests: XCTestCase {
    private func room(
        _ state: GroupRoomState, mode: GroupRoomMode = .mentionOnly, pending: Bool? = nil
    ) -> GroupRoom {
        GroupRoom(
            roomRef: String(repeating: "a", count: 64), state: state, mode: mode,
            joinedAt: nil, updatedAt: Date(timeIntervalSince1970: 0), leavePending: pending)
    }
    func testUnknownStateAndModeAreForwardCompatible() throws {
        let value = try GatewayCoding.decoder.decode(
            GroupRoom.self,
            from: Data(
                "{\"room_ref\":\"x\",\"state\":\"future\",\"mode\":\"future\",\"joined_at\":null,\"updated_at\":\"2026-11-13T00:00:00Z\"}"
                    .utf8))
        XCTAssertEqual(GroupRoomPresentation.label(value), "状态未知")
        XCTAssertFalse(GroupRoomPresentation.canChangeMode(value))
    }
    func testLeftAlwaysShowsExitedEvenWhenPending() {
        XCTAssertEqual(GroupRoomPresentation.label(room(.left, pending: true)), "已退出")
        XCTAssertEqual(GroupRoomPresentation.label(room(.encryptedDisabled)), "加密群，AI 不参与")
        XCTAssertEqual(GroupRoomPresentation.label(room(.rejectedEncrypted)), "已拒绝（加密）")
    }
    func testRoomRequestsUseSeparateEncodedComponentsAndOpenIDOnlyExactPaths() throws {
        let client = try HTTPGatewayClient(
            baseURL: "https://gateway.example.invalid", token: "site")
        let good = try client.request(
            ["personas", "@ai:example.invalid", "rooms", "a/b?c"], method: "PATCH",
            body: Data("{}".utf8))
        XCTAssertEqual(
            URLComponents(url: try XCTUnwrap(good.url), resolvingAgainstBaseURL: false)?
                .percentEncodedPath,
            "/v1/personas/%40ai%3Aexample.invalid/rooms/a%2Fb%3Fc")
        XCTAssertTrue(try client.isOpenIDRequest(good))
        XCTAssertTrue(try client.isOpenIDRequest(try client.request(["personas", "p", "rooms"])))
        XCTAssertTrue(
            try client.isOpenIDRequest(
                try client.request(["personas", "p"], method: "PATCH", body: Data("{}".utf8))))
        XCTAssertFalse(
            try client.isOpenIDRequest(
                try client.request(
                    ["personas", "p", "rooms", "r", "extra"], method: "PATCH", body: Data("{}".utf8)
                )))
        XCTAssertFalse(try client.isOpenIDRequest(try client.request(["personas", "p", "roomsx"])))
    }
    func testSharedRoomAndEncryptionCopy() {
        XCTAssertEqual(
            GatewayClientError.problem(status: 404, code: "room.not_found").errorDescription,
            "群状态已变化，请刷新后重试。")
        XCTAssertEqual(
            GatewayClientError.problem(status: 403, code: "conversation.e2ee").errorDescription,
            "加密群不支持 AI 发言")
    }
    func testMockSupportsPaginationAllStatesModesAndConfigurableLeavePending() async throws {
        let client = MockGatewayClient()
        let personas = try await client.personas()
        let persona = try XCTUnwrap(personas.first?.id)
        let first = try await client.groupRooms(
            personaID: persona, limit: 1, cursor: nil, includeAll: true)
        XCTAssertTrue(first.hasMore)
        XCTAssertEqual(first.nextCursor, "1")
        let second = try await client.groupRooms(
            personaID: persona, limit: 10, cursor: first.nextCursor, includeAll: true)
        XCTAssertTrue(second.items.contains { $0.state == .encryptedDisabled })
        XCTAssertTrue(second.items.contains { $0.state == .left })
        let third = try await client.groupRooms(
            personaID: persona, limit: 10, cursor: second.nextCursor, includeAll: true)
        XCTAssertTrue(third.items.contains { $0.state == .rejectedEncrypted })
        XCTAssertFalse(third.hasMore)
        await client.setMockLeavePending(true)
        let accepted = try XCTUnwrap(first.items.first)
        let left = try await client.leaveGroupRoom(personaID: persona, roomRef: accepted.roomRef)
        XCTAssertEqual(left.state, .left)
        XCTAssertEqual(left.leavePending, true)
        let changed = try await client.updateGroupRoomMode(
            personaID: persona, roomRef: String(repeating: "b", count: 64), mode: .muted)
        XCTAssertEqual(changed.mode, .muted)
        let normal = try await client.updateGroupRoomMode(
            personaID: persona, roomRef: changed.roomRef, mode: .mentionOnly)
        XCTAssertEqual(normal.mode, .mentionOnly)
        await client.setMockLeavePending(false)
        let confirmed = try await client.leaveGroupRoom(
            personaID: persona, roomRef: accepted.roomRef)
        XCTAssertEqual(confirmed.state, .left)
        XCTAssertEqual(confirmed.leavePending, false)
        let refreshed = try await client.groupRooms(personaID: persona, limit: 2, includeAll: true)
        XCTAssertTrue(refreshed.items.allSatisfy { $0.leavePending == nil })
        let saved = try await client.updatePersonaGroups(
            id: persona, groups: .init(enabled: true, invitePolicy: .localHumans))
        guard case .owner(let detail) = saved else { return XCTFail("Expected owner detail") }
        XCTAssertEqual(detail.groups, .init(enabled: true, invitePolicy: .localHumans))
    }

    func testMockRejectsInvalidCursorAndLimitsWithoutCrashing() async throws {
        let client = MockGatewayClient()
        let personas = try await client.personas()
        let id = try XCTUnwrap(personas.first?.id)
        for (cursor, limit) in [("-1", 1), ("garbled", 1), ("999", 1), ("0", 0), ("0", 201)] {
            do {
                _ = try await client.groupRooms(personaID: id, limit: limit, cursor: cursor)
                XCTFail("Expected invalid request")
            } catch {
                XCTAssertEqual(
                    error as? GatewayClientError, .problem(status: 400, code: "request.invalid"))
            }
        }
    }
}
