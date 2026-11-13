// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation
import XCTest

@testable import BotvilleCore

final class GroupRoomContractConsistencyTests: XCTestCase {
    func testBundledGroupRoomExamplesStayByteIdenticalToContract() throws {
        let root = repositoryRoot
        for name in ["group-room-list", "update-group-room-request"] {
            let bundled = root.appendingPathComponent(
                "app-apple/Sources/BotvilleCore/Resources/mock-\(name).json")
            let contract = root.appendingPathComponent("gateway/openapi/examples/\(name).json")
            XCTAssertEqual(
                try Data(contentsOf: bundled), try Data(contentsOf: contract),
                "Keep the App mock and gateway \(name) example byte-identical")
        }
    }

    func testGroupRoomListDecodesContractExample() throws {
        let data = try Data(
            contentsOf: repositoryRoot.appendingPathComponent(
                "gateway/openapi/examples/group-room-list.json"))
        let page = try GatewayCoding.decoder.decode(GroupRoomList.self, from: data)
        XCTAssertEqual(page.items.count, 1)
        XCTAssertEqual(page.items[0].state, .accepted)
        XCTAssertEqual(page.items[0].mode, .muted)
        XCTAssertNil(page.nextCursor)
        XCTAssertFalse(page.hasMore)
        XCTAssertTrue(page.groupsEnabled)
    }

    func testGroupsSettingsDefaultsAndOwnerGroupsAbsence() throws {
        let defaults = try JSONDecoder().decode(PersonaGroupsSettings.self, from: Data("{}".utf8))
        XCTAssertFalse(defaults.enabled)
        XCTAssertEqual(defaults.invitePolicy, .owner)

        let ownerWithoutGroups = try GatewayCoding.decoder.decode(
            MemoryPersona.self, from: ownerJSON(groups: nil))
        XCTAssertNil(ownerWithoutGroups.groups)
        let ownerWithGroups = try GatewayCoding.decoder.decode(
            MemoryPersona.self,
            from: ownerJSON(groups: "{\"enabled\":true,\"invite_policy\":\"local_humans\"}"))
        XCTAssertEqual(ownerWithGroups.groups, .init(enabled: true, invitePolicy: .localHumans))
    }

    func testPublicPersonaDetailDoesNotExposeGroups() throws {
        let data = Data(
            "{\"id\":\"@ai:example.test\",\"display_name\":\"AI\",\"bio\":null,\"status\":\"active\",\"groups\":{\"enabled\":true,\"invite_policy\":\"local_humans\"}}"
                .utf8)
        let detail = try GatewayCoding.decoder.decode(PersonaDetail.self, from: data)
        guard case .publicView(let publicView) = detail else {
            return XCTFail("A response without owner_id must stay a public view")
        }
        XCTAssertEqual(publicView.id, "@ai:example.test")
        XCTAssertEqual(publicView.mood, nil)
    }

    func testUnknownRoomStateAndModeRemainNonActionable() throws {
        let room = try GatewayCoding.decoder.decode(
            GroupRoom.self,
            from: Data(
                "{\"room_ref\":\"opaque\",\"state\":\"future_state\",\"mode\":\"future_mode\",\"joined_at\":null,\"updated_at\":\"2026-11-13T00:00:00Z\"}"
                    .utf8))
        XCTAssertEqual(room.state, .unknown("future_state"))
        XCTAssertEqual(room.mode, .unknown("future_mode"))
        XCTAssertEqual(GroupRoomPresentation.label(room), "状态未知")
        XCTAssertFalse(GroupRoomPresentation.canChangeMode(room))
    }

    func testLeftPresentationDoesNotDependOnLeavePending() {
        for pending in [false, true] {
            let room = GroupRoom(
                roomRef: "opaque", state: .left, mode: .muted,
                joinedAt: nil, updatedAt: .distantPast, leavePending: pending)
            XCTAssertEqual(GroupRoomPresentation.label(room), "已退出")
        }
    }

    private var repositoryRoot: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
    }

    private func ownerJSON(groups: String?) -> Data {
        let groupField = groups.map { ",\"groups\":\($0)" } ?? ""
        return Data(
            "{\"id\":\"@ai:example.test\",\"display_name\":\"AI\",\"bio\":null,\"owner_id\":\"@me:example.test\",\"kind\":\"custom\",\"status\":\"active\",\"revision\":1,\"created_at\":\"2026-11-13T00:00:00Z\",\"updated_at\":\"2026-11-13T00:00:00Z\",\"visibility\":\"private\"\(groupField)}"
                .utf8)
    }
}
