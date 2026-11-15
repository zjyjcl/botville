// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation
import XCTest

@testable import BotvilleCore

final class GroupRoomLookupPlanTests: XCTestCase {
    func testOnlyStrictLocalGroupRoomIDsAreSentOnceInStableOrder() {
        let long = "!" + String(repeating: "x", count: 250) + ":local.test"
        let rooms = [
            MatrixRoom(id: "!a:local.test", name: "甲"),
            MatrixRoom(id: "!dm:local.test", name: "私聊", isDirect: true),
            MatrixRoom(id: "!b:LOCAL.test", name: "乙"),
            MatrixRoom(id: "!a:Local.Test", name: "甲二"),  // Server-case duplicate: server rejects.
            MatrixRoom(id: "!A:local.test", name: "大写"),  // Localpart case is significant.
            MatrixRoom(id: "!x:other.test", name: "跨服"),
            MatrixRoom(id: "#alias:local.test", name: "别名"),
            MatrixRoom(id: "!nocolon", name: "缺冒号"),
            MatrixRoom(id: "!:local.test", name: "空"),
            MatrixRoom(id: "!bad space:local.test", name: "空白"),
            MatrixRoom(id: "!bad/slash:local.test", name: "斜杠"),
            MatrixRoom(id: "!p:local.test:8448", name: "端口"),
            MatrixRoom(id: "!c.d_e~f=g-h:local.test", name: "字符"),
            MatrixRoom(id: long, name: "超长"),
        ]
        XCTAssertEqual(
            GroupRoomLookupPlan.roomIDs(from: rooms, localServerName: "local.test"),
            ["!a:local.test", "!b:LOCAL.test", "!A:local.test", "!c.d_e~f=g-h:local.test"])
        XCTAssertEqual(GroupRoomLookupPlan.roomIDs(from: rooms, localServerName: nil), [])
        XCTAssertEqual(GroupRoomLookupPlan.roomIDs(from: rooms, localServerName: ""), [])
        XCTAssertEqual(
            GroupRoomLookupPlan.roomIDs(from: rooms, localServerName: "local.test:8448"),
            ["!p:local.test:8448"])
    }

    func testBatchesNeverExceedFifty() {
        let ids = (0..<120).map { "!r\($0):local.test" }
        XCTAssertEqual(GroupRoomLookupPlan.batches(ids).map(\.count), [50, 50, 20])
        XCTAssertEqual(GroupRoomLookupPlan.batches(ids).flatMap { $0 }, ids)
        XCTAssertEqual(GroupRoomLookupPlan.batches(Array(ids.prefix(50))).map(\.count), [50])
        XCTAssertEqual(GroupRoomLookupPlan.batches([]).count, 0)
        XCTAssertEqual(GroupRoomLookupPlan.maxBatchSize, 50)
    }
}

final class GroupRoomRowTitleTests: XCTestCase {
    private let room = GroupRoom(
        roomRef: "opaque-reference", state: .accepted, mode: .muted,
        joinedAt: nil, updatedAt: Date(timeIntervalSince1970: 0), leavePending: nil)

    func testMappedNameReplacesOrdinalAndBlankOrMissingKeepsOrdinal() {
        func title(_ name: String?) -> String {
            GroupRoomRowViewState(
                room: room, ordinal: 3, name: name, groupsEnabled: true,
                canEnableMentionOnly: true, isSubmitting: false
            ).title
        }
        XCTAssertEqual(title("周末群"), "周末群")
        XCTAssertEqual(title("  周末群 \n"), "周末群")
        XCTAssertEqual(title(nil), "群聊 3")
        XCTAssertEqual(title(""), "群聊 3")
        XCTAssertEqual(title(" \n"), "群聊 3")
    }
}

final class GroupsManageEntryStateTests: XCTestCase {
    private func manifest(_ capabilityJSON: String?) throws -> GatewayCapabilityManifest {
        let list = capabilityJSON.map { "[\($0)]" } ?? "[]"
        let json =
            "{\"manifest_version\":\"1.4.0\",\"contract_version\":\"0.7.0\",\"generated_at\":\"2026-11-15T00:00:00Z\",\"capabilities\":\(list)}"
        return try GatewayCoding.decoder.decode(GatewayCapabilityManifest.self, from: Data(json.utf8))
    }

    private func groups(status: String, reason: String? = nil, roomLookup: Bool? = true) -> String {
        let reasonField = reason.map { ",\"unavailable_reason\":\"\($0)\"" } ?? ""
        let lookupField = roomLookup.map { ",\"room_lookup\":\($0)" } ?? ""
        return
            "{\"id\":\"groups.manage\",\"module\":\"groups\",\"version\":\"0.1.0\",\"title\":\"群聊\",\"summary\":\"管理我的 AI 所在的群：安静模式与退出。\",\"status\":\"\(status)\",\"entry\":{\"type\":\"native\",\"deep_link\":\"botville://personas\"},\"scopes\":[\"groups.read\",\"groups.write\"]\(reasonField)\(lookupField)}"
    }

    func testEnabledShowsFullEntryAndUsesRoomLookupFlag() throws {
        let state = GroupsManageEntryState(manifest: try manifest(groups(status: "enabled")))
        XCTAssertEqual(state.visibility, .visible)
        XCTAssertNil(state.notice)
        XCTAssertTrue(state.allowsMentionOnly)
        XCTAssertTrue(state.canOpenSettings)
        XCTAssertTrue(state.supportsRoomLookup)
        for flag in [false, nil] as [Bool?] {
            XCTAssertFalse(
                GroupsManageEntryState(
                    manifest: try manifest(groups(status: "enabled", roomLookup: flag))
                ).supportsRoomLookup)
        }
    }

    func testDegradedOrGroupsDisabledIsDimmedWithQuietAndLeaveOnly() throws {
        for capability in [
            groups(status: "degraded", reason: "groups_disabled"),
            groups(status: "degraded"),
            groups(status: "degraded", reason: "future_reason"),
            groups(status: "enabled", reason: "groups_disabled"),
        ] {
            let state = GroupsManageEntryState(manifest: try manifest(capability))
            XCTAssertEqual(state.visibility, .dimmed, capability)
            XCTAssertEqual(state.notice, "群聊功能已关闭，只能设为安静或退出群聊", capability)
            XCTAssertFalse(state.allowsMentionOnly, capability)
            XCTAssertFalse(state.canOpenSettings, capability)
            XCTAssertTrue(state.supportsRoomLookup, capability)
        }
    }

    func testDisabledAndUnknownStatusesAreHidden() throws {
        for capability in [
            groups(status: "disabled", reason: "module_disabled"),
            groups(status: "disabled", reason: "groups_disabled"),
            groups(status: "beta"),
        ] {
            let state = GroupsManageEntryState(manifest: try manifest(capability))
            XCTAssertEqual(state.visibility, .hidden, capability)
            XCTAssertFalse(state.allowsMentionOnly, capability)
            XCTAssertFalse(state.canOpenSettings, capability)
            XCTAssertFalse(state.supportsRoomLookup, capability)
        }
    }

    /// Gateways before 0.7.0 already serve the rooms API but publish no capability.
    func testMissingCapabilityKeepsLegacyEntryWithoutLookup() throws {
        for state in [
            GroupsManageEntryState(manifest: nil),
            GroupsManageEntryState(manifest: try manifest(nil)),
            GroupsManageEntryState.legacy,
        ] {
            XCTAssertEqual(state.visibility, .visible)
            XCTAssertNil(state.notice)
            XCTAssertTrue(state.allowsMentionOnly)
            XCTAssertTrue(state.canOpenSettings)
            XCTAssertFalse(state.supportsRoomLookup)
        }
    }

    func testBundledMockManifestEnablesGroupsWithLookup() async throws {
        let state = GroupsManageEntryState(manifest: try await MockGatewayClient().capabilities())
        XCTAssertEqual(state.visibility, .visible)
        XCTAssertTrue(state.supportsRoomLookup)
    }
}

final class MockGroupRoomLookupTests: XCTestCase {
    func testMockLookupIsIndexAlignedAndValidatesLikeTheGateway() async throws {
        let client = MockGatewayClient()
        let personas = try await client.personas()
        let personaID = try XCTUnwrap(personas.first?.id)
        let known = try await client.groupRooms(personaID: personaID, includeAll: true).items
        let ref = try XCTUnwrap(known.first?.roomRef)
        await client.setMockRoomLookup(["!known:local.test": ref])
        let result = try await client.lookupGroupRooms(
            personaID: personaID, roomIDs: ["!missing:local.test", "!known:local.test"])
        XCTAssertEqual(result.items.map { $0?.roomRef }, [nil, ref])
        XCTAssertTrue(result.groupsEnabled)
        for bad in [[], (0...50).map { "!r\($0):local.test" }, ["!a:local.test", "!a:LOCAL.test"]] {
            do {
                _ = try await client.lookupGroupRooms(personaID: personaID, roomIDs: bad)
                XCTFail("Expected 400 for \(bad.count) ids")
            } catch let error as GatewayClientError {
                XCTAssertEqual(error, .problem(status: 400, code: "request.invalid"))
            }
        }
        do {
            _ = try await client.lookupGroupRooms(personaID: "@nobody:local", roomIDs: ["!a:local.test"])
            XCTFail("Expected 404")
        } catch let error as GatewayClientError {
            XCTAssertEqual(error, .problem(status: 404, code: "persona.not_found"))
        }
    }
}
