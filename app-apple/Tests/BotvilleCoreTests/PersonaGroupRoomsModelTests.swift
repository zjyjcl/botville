// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Combine
import Foundation
import XCTest

@testable import BotvilleCore

@MainActor
final class PersonaGroupRoomsModelTests: XCTestCase {
    func testPaginationDedupAndIncludeSwitch() async {
        let client = RoomsScriptClient(pages: [
            .success(page([room("a"), room("b")], "next", true)),
            .success(page([room("b"), room("c")], nil, false)),
            .success(page([room("left", state: "left")], nil, false)),
        ])
        let model = roomsModel(client)
        await model.load()
        await model.loadMore()
        XCTAssertEqual(model.rooms.map(\.roomRef), [ref("a"), ref("b"), ref("c")])
        XCTAssertFalse(model.canLoadMore)
        await model.loadMore()  // Terminal pages must not issue another request.
        await model.setIncludeAll(true)
        XCTAssertTrue(model.includeAll)
        XCTAssertEqual(model.rooms.map(\.roomRef), [ref("left")])
        let calls = await client.calls()
        XCTAssertEqual(calls, ["nil:false", "next:false", "nil:true"])
    }

    func testModeAndLeaveReplaceInPlaceIncludingPending() async {
        let client = RoomsScriptClient(
            pages: [.success(page([room("a")], nil, false))],
            modes: [.success(room("a", mode: "muted")), .success(room("a", mode: "mention_only"))],
            leaves: [
                .success(room("a", state: "left", pending: true)),
                .success(room("a", state: "left", pending: false)),
            ])
        let model = roomsModel(client)
        await model.load()
        await model.setMode(roomRef: ref("a"), mode: .muted)
        XCTAssertEqual(model.rooms.first?.mode, .muted)
        await model.setMode(roomRef: ref("a"), mode: .mentionOnly)
        XCTAssertEqual(model.rooms.first?.mode, .mentionOnly)
        await model.leave(roomRef: ref("a"))
        XCTAssertEqual(model.rooms.first?.state, .left)
        XCTAssertEqual(model.rooms.first?.leavePending, true)
        // A second leave is intentionally unavailable after left.
        await model.leave(roomRef: ref("a"))
        let leaves = await client.leaveCount()
        XCTAssertEqual(leaves, 1)
    }

    func testConflictAndNotFoundReloadAndFailedReloadKeepsError() async {
        let client = RoomsScriptClient(
            pages: [
                .success(page([room("a")], nil, false)),
                .success(page([room("b")], nil, false)),
                .success(page([room("a")], nil, false)),
                .failure(.httpStatus(500)),
            ],
            modes: [
                .failure(.problem(status: 409, code: "room.state_conflict")),
                .failure(.problem(status: 404, code: "room.not_found")),
            ])
        let model = roomsModel(client)
        await model.load()
        await model.setMode(roomRef: ref("a"), mode: .muted)
        XCTAssertEqual(model.rooms.map(\.roomRef), [ref("b")])
        XCTAssertEqual(model.errorMessage, "群状态已变化，已刷新")
        await model.refresh()
        await model.setMode(roomRef: ref("a"), mode: .muted)
        XCTAssertTrue(model.rooms.isEmpty)
        XCTAssertEqual(model.errorMessage, "网关请求失败，请稍后重试。")
    }

    func testConflictRefreshFailureKeepsPreviousRoomsAndRestartsPagination() async {
        let client = RoomsScriptClient(
            pages: [
                .success(page([room("a"), room("b")], "next", true)),
                .failure(.httpStatus(500)),
                .success(page([room("c")], nil, false)),
            ],
            modes: [.failure(.problem(status: 409, code: "room.state_conflict"))])
        let model = roomsModel(client)
        await model.load()
        await model.setMode(roomRef: ref("a"), mode: .muted)
        // The failed refresh must not blank the list that was already on screen.
        XCTAssertEqual(model.rooms.map(\.roomRef), [ref("a"), ref("b")])
        XCTAssertEqual(model.rooms.first?.mode, .mentionOnly)
        XCTAssertEqual(model.errorMessage, "网关请求失败，请稍后重试。")
        XCTAssertFalse(model.isLoading)
        // The old cursor predates the conflict; the next page request starts over.
        await model.loadMore()
        XCTAssertEqual(model.rooms.map(\.roomRef), [ref("c")])
        XCTAssertNil(model.errorMessage)
        let calls = await client.calls()
        XCTAssertEqual(calls, ["nil:false", "nil:false", "nil:false"])
    }

    func testSharedErrorsAndDisabledEmptyPage() async {
        let client = RoomsScriptClient(pages: [
            .failure(.httpStatus(401)), .failure(.httpStatus(403)),
            .failure(.problem(status: 404, code: "persona.not_found")),
            .failure(.problem(status: 503, code: "room.unavailable")),
            .success(GroupRoomListJSON.disabled),
        ])
        let model = roomsModel(client)
        await model.load()
        XCTAssertEqual(model.errorMessage, "会话已失效，请重新登录。")
        await model.refresh()
        XCTAssertEqual(model.errorMessage, "没有权限查看")
        await model.refresh()
        XCTAssertEqual(model.errorMessage, "不存在或不可见")
        await model.refresh()
        XCTAssertEqual(model.errorMessage, "服务暂时不可用，请稍后重试。")
        await model.refresh()
        XCTAssertFalse(model.groupsEnabled)
        XCTAssertTrue(model.rooms.isEmpty)
    }

    func testDisabledDeploymentKeepsPagesAndAllowsOnlyMuteAndLeave() async {
        let first = GroupRoomList(items: [room("a")], nextCursor: "next", hasMore: true, groupsEnabled: false)
        let second = GroupRoomList(items: [room("b", state: "encrypted_disabled", mode: "muted")],
                                   nextCursor: nil, hasMore: false, groupsEnabled: false)
        let client = RoomsScriptClient(
            pages: [.success(first), .success(second)],
            modes: [.success(room("a", mode: "muted"))],
            leaves: [.success(room("b", state: "left"))])
        let model = roomsModel(client)
        await model.load()
        XCTAssertFalse(model.groupsEnabled)
        XCTAssertNil(model.errorMessage)
        XCTAssertTrue(model.canLoadMore)
        await model.loadMore()
        XCTAssertEqual(model.rooms.count, 2)
        await model.setMode(roomRef: ref("b"), mode: .mentionOnly)
        let blocked = await client.modeRequests()
        XCTAssertTrue(blocked.isEmpty)
        await model.setMode(roomRef: ref("a"), mode: .muted)
        await model.leave(roomRef: ref("b"))
        XCTAssertEqual(model.rooms.first?.mode, .muted)
        XCTAssertEqual(model.rooms.last?.state, .left)
        let sent = await client.modeRequests()
        XCTAssertEqual(sent, [.muted])
    }

    func testSharedPersonaSettingImmediatelyClosesRoomsAndRollbackRestoresIt() async {
        let client = RoomsScriptClient(
            pages: [.success(page([room("a", mode: "muted")], nil, false))],
            modes: [.success(room("a", mode: "mention_only"))],
            updates: [.success(owner(groups(false, .owner))), .failure(.httpStatus(500)),
                      .success(owner(groups(true, .owner)))])
        let model = roomsModel(client)
        await model.load()
        XCTAssertTrue(model.canEnableMentionOnly)
        var changes = 0
        let observation = model.objectWillChange.sink { changes += 1 }
        await model.settings.setEnabled(false)
        XCTAssertGreaterThan(changes, 0) // The mounted rooms view receives shared-setting updates.
        withExtendedLifetime(observation) {}
        XCTAssertFalse(model.groupsEnabled)
        XCTAssertEqual(model.rooms.count, 1)
        await model.setMode(roomRef: ref("a"), mode: .mentionOnly)
        XCTAssertFalse(model.canEnableMentionOnly)
        await model.settings.setEnabled(true) // Failed save rolls back to disabled.
        XCTAssertFalse(model.groupsEnabled)
        await model.setMode(roomRef: ref("a"), mode: .mentionOnly)
        let blocked = await client.modeRequests()
        XCTAssertTrue(blocked.isEmpty)
        await model.settings.setEnabled(true)
        XCTAssertTrue(model.canEnableMentionOnly)
        await model.setMode(roomRef: ref("a"), mode: .mentionOnly)
        let sent = await client.modeRequests()
        XCTAssertEqual(sent, [.mentionOnly])
    }

    func testEnabledActiveStatesCanSwitchBothDirections() async {
        for state in ["accepted", "encrypted_disabled"] {
            let client = RoomsScriptClient(
                pages: [.success(page([room("a", state: state)], nil, false))],
                modes: [.success(room("a", state: state, mode: "muted")),
                        .success(room("a", state: state, mode: "mention_only"))])
            let model = roomsModel(client)
            await model.load()
            await model.setMode(roomRef: ref("a"), mode: .muted)
            await model.setMode(roomRef: ref("a"), mode: .mentionOnly)
            let sent = await client.modeRequests()
            XCTAssertEqual(sent, [.muted, .mentionOnly])
        }
    }

    func testConflictRefreshesPersonaAndPreventsAnotherMentionOnlyWrite() async {
        let client = RoomsScriptClient(
            pages: [.success(page([room("a", mode: "muted")], nil, false)),
                    .success(page([room("a", mode: "muted")], nil, false))],
            modes: [.failure(.problem(status: 409, code: "room.state_conflict"))],
            personas: [.success(owner(groups(true, .owner))), .success(owner(groups(false, .owner)))])
        let model = roomsModel(client)
        await model.load()
        await model.setMode(roomRef: ref("a"), mode: .mentionOnly)
        XCTAssertFalse(model.groupsEnabled)
        XCTAssertEqual(model.rooms.count, 1)
        XCTAssertEqual(model.errorMessage, "群状态已变化，已刷新")
        await model.setMode(roomRef: ref("a"), mode: .mentionOnly)
        let sent = await client.modeRequests()
        XCTAssertEqual(sent, [.mentionOnly])
        let reads = await client.personaCount()
        XCTAssertEqual(reads, 2)
    }

    func testFailedPersonaRefreshKeepsRoomsButCannotEnableReplies() async {
        let client = RoomsScriptClient(
            pages: [.success(page([room("a", mode: "muted")], nil, false))],
            personas: [.failure(.httpStatus(503))])
        let model = roomsModel(client)
        await model.load()
        XCTAssertEqual(model.rooms.count, 1)
        XCTAssertFalse(model.canEnableMentionOnly)
        XCTAssertEqual(model.errorMessage, "网关请求失败，请稍后重试。")
        await model.setMode(roomRef: ref("a"), mode: .mentionOnly)
        let sent = await client.modeRequests()
        XCTAssertTrue(sent.isEmpty)
    }

    func testSettingsLoadSaveRollbackAndRejectsPublicOrFailedLoad() async {
        let initial = groups(false, .owner)
        let client = RoomsScriptClient(
            personas: [.success(owner(groups(true, .localHumans)))],
            updates: [.failure(.httpStatus(500))])
        let settings = PersonaGroupsSettingsController(
            personaID: "@ai:local", initialGroups: initial, client: client)
        await settings.load()
        XCTAssertTrue(settings.hasLoaded)
        XCTAssertEqual(settings.groups.invitePolicy, .localHumans)
        await settings.setEnabled(false)
        XCTAssertTrue(settings.groups.enabled)  // rolled back
        let updateCount = await client.groupUpdates().count
        XCTAssertEqual(updateCount, 1)

        let rejected = RoomsScriptClient(personas: [.success(publicPersona())])
        let blocked = PersonaGroupsSettingsController(
            personaID: "@ai:local", initialGroups: initial, client: rejected)
        await blocked.load()
        await blocked.setEnabled(true)
        XCTAssertFalse(blocked.hasLoaded)
        let rejectedUpdateCount = await rejected.groupUpdates().count
        XCTAssertEqual(rejectedUpdateCount, 0)
    }

    func testSettingsSuccessfulWritesPreserveTheOtherFieldAndFailedLoadCannotWrite() async {
        let client = RoomsScriptClient(
            personas: [.success(owner(groups(false, .owner)))],
            updates: [
                .success(owner(groups(true, .owner))), .success(owner(groups(true, .localHumans))),
            ])
        let model = PersonaGroupsSettingsController(
            personaID: "@ai:local", initialGroups: groups(false, .owner), client: client)
        await model.load()
        await model.setEnabled(true)
        await model.setInvitePolicy(.localHumans)
        let updates = await client.groupUpdates()
        XCTAssertEqual(updates, [groups(true, .owner), groups(true, .localHumans)])
        XCTAssertEqual(model.groups, groups(true, .localHumans))

        let unavailable = RoomsScriptClient(personas: [
            .failure(.problem(status: 503, code: "persona.unavailable"))
        ])
        let blocked = PersonaGroupsSettingsController(
            personaID: "@ai:local", initialGroups: groups(false, .owner), client: unavailable)
        await blocked.load()
        await blocked.setEnabled(true)
        XCTAssertFalse(blocked.hasLoaded)
        XCTAssertEqual(blocked.errorMessage, "服务暂时不可用，请稍后重试。")
        let unexpectedWrites = await unavailable.groupUpdates()
        XCTAssertTrue(unexpectedWrites.isEmpty)
    }

    func testIncludeSwitchDiscardsInFlightLoadMoreAndRepeatedLeaveIsSerialized() async {
        let gate = RoomsGate()
        let client = GatedRoomsClient(gate: gate)
        let model = roomsModel(client)
        await model.load()
        let more = Task { @MainActor in await model.loadMore() }
        await gate.waitForMore()
        await model.setIncludeAll(true)
        await gate.finishMore()
        await more.value
        XCTAssertEqual(model.rooms.map(\.roomRef), [ref("all")])
        await model.setIncludeAll(false)
        let first = Task { @MainActor in await model.leave(roomRef: ref("a")) }
        await gate.waitForLeave()
        XCTAssertTrue(model.isSubmitting(roomRef: ref("a")))
        await model.leave(roomRef: ref("a"))  // Still pending: must return without a second PATCH.
        await gate.finishLeave()
        await first.value
        XCTAssertFalse(model.isSubmitting(roomRef: ref("a")))
        XCTAssertEqual(model.rooms.first?.leavePending, false)
        XCTAssertEqual(model.rooms.first.map(GroupRoomPresentation.label), "已退出")
        let count = await client.leaveCount()
        XCTAssertEqual(count, 1)
    }

}

@MainActor
private func roomsModel(_ client: any GatewayClient) -> PersonaGroupRoomsModel {
    PersonaGroupRoomsModel(personaID: "@ai:local", client: client,
        settings: PersonaGroupsSettingsController(personaID: "@ai:local",
            initialGroups: groups(true, .owner), client: client))
}

private enum GroupRoomListJSON {
    static let disabled = try! JSONDecoder().decode(
        GroupRoomList.self,
        from: Data(
            "{\"items\":[],\"next_cursor\":null,\"has_more\":false,\"groups_enabled\":false}".utf8))
}

private func ref(_ label: String) -> String {
    let character =
        ["a": "a", "b": "b", "c": "c", "left": "d", "all": "e", "stale": "f"][label] ?? "a"
    return String(repeating: character, count: 64)
}
private func room(
    _ label: String, state: String = "accepted", mode: String = "mention_only", pending: Bool? = nil
) -> GroupRoom {
    let pendingField = pending.map { ",\"leave_pending\":\($0)" } ?? ""
    return try! GatewayCoding.decoder.decode(
        GroupRoom.self,
        from: Data(
            "{\"room_ref\":\"\(ref(label))\",\"state\":\"\(state)\",\"mode\":\"\(mode)\",\"joined_at\":null,\"updated_at\":\"2026-11-13T00:00:00Z\"\(pendingField)}"
                .utf8))
}
private func page(_ items: [GroupRoom], _ cursor: String?, _ more: Bool) -> GroupRoomList {
    GroupRoomListJSON.decode(items, cursor, more)
}
extension GroupRoomListJSON {
    fileprivate static func decode(_ items: [GroupRoom], _ cursor: String?, _ more: Bool)
        -> GroupRoomList
    {
        let raw =
            "["
            + items.map { item in
                let pending = item.leavePending.map { ",\"leave_pending\":\($0)" } ?? ""
                return
                    "{\"room_ref\":\"\(item.roomRef)\",\"state\":\"\(item.state.rawValue)\",\"mode\":\"\(item.mode.rawValue)\",\"joined_at\":null,\"updated_at\":\"2026-11-13T00:00:00Z\"\(pending)}"
            }.joined(separator: ",") + "]"
        let cursorJSON = cursor.map { "\"\($0)\"" } ?? "null"
        return try! GatewayCoding.decoder.decode(
            GroupRoomList.self,
            from: Data(
                "{\"items\":\(raw),\"next_cursor\":\(cursorJSON),\"has_more\":\(more),\"groups_enabled\":true}"
                    .utf8))
    }
}
private func groups(_ enabled: Bool, _ policy: PersonaGroupsSettings.InvitePolicy)
    -> PersonaGroupsSettings
{
    try! GatewayCoding.decoder.decode(
        PersonaGroupsSettings.self,
        from: Data("{\"enabled\":\(enabled),\"invite_policy\":\"\(policy.rawValue)\"}".utf8))
}
private func owner(_ settings: PersonaGroupsSettings) -> PersonaDetail {
    let json =
        "{\"id\":\"@ai:local\",\"owner_id\":\"@me:local\",\"display_name\":\"AI\",\"bio\":null,\"kind\":\"custom\",\"status\":\"active\",\"revision\":1,\"created_at\":\"2026-11-13T00:00:00Z\",\"updated_at\":\"2026-11-13T00:00:00Z\",\"visibility\":\"private\",\"groups\":{\"enabled\":\(settings.enabled),\"invite_policy\":\"\(settings.invitePolicy.rawValue)\"}}"
    return try! GatewayCoding.decoder.decode(PersonaDetail.self, from: Data(json.utf8))
}
private func publicPersona() -> PersonaDetail {
    try! JSONDecoder().decode(
        PersonaDetail.self,
        from: Data(
            "{\"id\":\"@ai:local\",\"display_name\":\"AI\",\"bio\":null,\"status\":\"active\",\"mood\":null}"
                .utf8))
}

private actor RoomsScriptClient: GatewayClient {
    private var pages: [Result<GroupRoomList, GatewayClientError>]
    private var modes: [Result<GroupRoom, GatewayClientError>]
    private var leaves: [Result<GroupRoom, GatewayClientError>]
    private var personas: [Result<PersonaDetail, GatewayClientError>]
    private var updates: [Result<PersonaDetail, GatewayClientError>]
    private var roomCalls: [String] = []
    private var sentUpdates: [PersonaGroupsSettings] = []
    private var leavesSent = 0
    private var modesSent: [GroupRoomMode] = []
    private var personasRead = 0
    init(
        pages: [Result<GroupRoomList, GatewayClientError>] = [],
        modes: [Result<GroupRoom, GatewayClientError>] = [],
        leaves: [Result<GroupRoom, GatewayClientError>] = [],
        personas: [Result<PersonaDetail, GatewayClientError>] = [],
        updates: [Result<PersonaDetail, GatewayClientError>] = []
    ) {
        self.pages = pages
        self.modes = modes
        self.leaves = leaves
        self.personas = personas
        self.updates = updates
    }
    func groupRooms(personaID: String, limit: Int?, cursor: String?, includeAll: Bool) async throws
        -> GroupRoomList
    {
        roomCalls.append("\(cursor ?? "nil"):\(includeAll)")
        return try pages.removeFirst().get()
    }
    func updateGroupRoomMode(personaID: String, roomRef: String, mode: GroupRoomMode) async throws
        -> GroupRoom
    {
        modesSent.append(mode)
        return try modes.removeFirst().get()
    }
    func leaveGroupRoom(personaID: String, roomRef: String) async throws -> GroupRoom {
        leavesSent += 1
        return try leaves.removeFirst().get()
    }
    func persona(id: String) async throws -> PersonaDetail {
        personasRead += 1
        return try personas.isEmpty ? owner(groups(true, .owner)) : personas.removeFirst().get()
    }
    func personaCount() -> Int { personasRead }
    func modeRequests() -> [GroupRoomMode] { modesSent }
    func updatePersonaGroups(id: String, groups: PersonaGroupsSettings) async throws
        -> PersonaDetail
    {
        sentUpdates.append(groups)
        return try updates.removeFirst().get()
    }
    func calls() -> [String] { roomCalls }
    func leaveCount() -> Int { leavesSent }
    func groupUpdates() -> [PersonaGroupsSettings] { sentUpdates }
    func capabilities(locale: String?, appVersion: String?) async throws
        -> GatewayCapabilityManifest
    { throw GatewayClientError.invalidResponse }
    func cards(capabilityID: String?, cardTypes: [String]?) async throws -> [GatewayCard] {
        throw GatewayClientError.invalidResponse
    }
    func perform(card: GatewayCard, action: GatewayCardAction) async throws -> GatewayActionReceipt
    { throw GatewayClientError.invalidResponse }
    func exchangeSessionCookie(capabilityID: String, code: String) async throws
        -> GatewayCookieExchange
    { throw GatewayClientError.invalidResponse }
    func createModuleWebSession(
        capabilityID: String, url: URL, allowedOrigins: [String]?, scopes: [String]?,
        ttlSeconds: Int?
    ) async throws -> ModuleWebSessionGrant { throw GatewayClientError.invalidResponse }
}

private actor RoomsGate {
    private var more: CheckedContinuation<Void, Never>?
    private var leave: CheckedContinuation<Void, Never>?
    private var moreStarted: CheckedContinuation<Void, Never>?
    private var leaveStarted: CheckedContinuation<Void, Never>?
    private var moreDidStart = false
    private var leaveDidStart = false
    func awaitMore() async {
        moreDidStart = true
        moreStarted?.resume()
        moreStarted = nil
        await withCheckedContinuation { more = $0 }
    }
    func awaitLeave() async {
        leaveDidStart = true
        leaveStarted?.resume()
        leaveStarted = nil
        await withCheckedContinuation { leave = $0 }
    }
    func waitForMore() async {
        if moreDidStart { return }
        await withCheckedContinuation { moreStarted = $0 }
    }
    func waitForLeave() async {
        if leaveDidStart { return }
        await withCheckedContinuation { leaveStarted = $0 }
    }
    func finishMore() {
        more?.resume()
        more = nil
    }
    func finishLeave() {
        leave?.resume()
        leave = nil
    }
}

private actor GatedRoomsClient: GatewayClient {
    let gate: RoomsGate
    private var leaves = 0
    init(gate: RoomsGate) { self.gate = gate }
    func persona(id: String) async throws -> PersonaDetail { owner(groups(true, .owner)) }
    func groupRooms(personaID: String, limit: Int?, cursor: String?, includeAll: Bool) async throws
        -> GroupRoomList
    {
        if includeAll { return page([room("all", state: "left")], nil, false) }
        if cursor == "next" {
            await gate.awaitMore()
            return page([room("stale")], nil, false)
        }
        return page([room("a")], "next", true)
    }
    func leaveGroupRoom(personaID: String, roomRef: String) async throws -> GroupRoom {
        leaves += 1
        await gate.awaitLeave()
        return room("a", state: "left", pending: false)
    }
    func leaveCount() -> Int { leaves }
    func capabilities(locale: String?, appVersion: String?) async throws
        -> GatewayCapabilityManifest
    { throw GatewayClientError.invalidResponse }
    func cards(capabilityID: String?, cardTypes: [String]?) async throws -> [GatewayCard] {
        throw GatewayClientError.invalidResponse
    }
    func perform(card: GatewayCard, action: GatewayCardAction) async throws -> GatewayActionReceipt
    { throw GatewayClientError.invalidResponse }
    func exchangeSessionCookie(capabilityID: String, code: String) async throws
        -> GatewayCookieExchange
    { throw GatewayClientError.invalidResponse }
    func createModuleWebSession(
        capabilityID: String, url: URL, allowedOrigins: [String]?, scopes: [String]?,
        ttlSeconds: Int?
    ) async throws -> ModuleWebSessionGrant { throw GatewayClientError.invalidResponse }
}
