// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation
import XCTest

@testable import BotvilleCore

@MainActor
final class PersonaMoodControllerTests: XCTestCase {
    private var errorCases: [(GatewayClientError, String)] {
        [
            (.problem(status: 401, code: "auth.required"), "会话已失效，请重新登录。"),
            (.httpStatus(401), "会话已失效，请重新登录。"),
            (.problem(status: 403, code: "persona.not_owned"), "没有权限查看"),
            (.httpStatus(403), "没有权限查看"),
            (.problem(status: 404, code: "persona.not_found"), "不存在或不可见"),
            (.problem(status: 404, code: "unknown"), "网关请求失败，请稍后重试。"),
            (.httpStatus(404), "网关请求失败，请稍后重试。"),
            (.problem(status: 422, code: "validation.failed"), "操作失败，请稍后重试"),
            (.httpStatus(422), "操作失败，请稍后重试"),
            (.problem(status: 429, code: "rate_limited.transport"), "操作太频繁，请稍后再试"),
            (.httpStatus(429), "操作太频繁，请稍后再试"),
            (.serverAuthMisconfigured, "服务器认证异常，请联系部署者"),
            (.invalidResponse, "操作失败，请稍后重试"),
        ]
    }

    func testPrivateMoodMarkerRequiresAnExistingPrivateMood() {
        for mood in PersonaMood.allCases {
            let state = PersonaState(personaID: "p", mood: mood, moodPublic: false)
            XCTAssertTrue(state.showsPrivateMoodMarker)
            XCTAssertEqual(state.mood, mood)
            XCTAssertFalse(PersonaState(personaID: "p", mood: mood, moodPublic: true).showsPrivateMoodMarker)
        }
        for moodPublic in [false, true] {
            XCTAssertFalse(PersonaState(personaID: "p", mood: nil, moodPublic: moodPublic).showsPrivateMoodMarker)
        }
    }

    func testPrivateMoodMarkerIsOnlyWiredIntoOwnerView() throws {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        let owner = try String(contentsOf: root.appendingPathComponent("Sources/BotvilleApp/MemoryViews.swift"), encoding: .utf8)
        let counterpart = try String(contentsOf: root.appendingPathComponent("Sources/BotvilleApp/AIProfileView.swift"), encoding: .utf8)
        XCTAssertTrue(owner.contains("if state.showsPrivateMoodMarker"))
        XCTAssertTrue(owner.contains("Text(\"仅自己可见\")"))
        XCTAssertFalse(counterpart.contains("showsPrivateMoodMarker"))
        XCTAssertFalse(counterpart.contains("仅自己可见"))
        XCTAssertTrue(counterpart.contains("PersonaMoodBadge(mood: mood)"))
    }

    func testDisabledMoodHidesBlockWithoutError() async {
        let client = MoodClient(load: .failure(.problem(status: 404, code: "mood.disabled")))
        let controller = PersonaMoodController(personaID: "p", client: client)

        await controller.load()

        XCTAssertTrue(controller.isUnavailable)
        XCTAssertNil(controller.errorMessage)
        XCTAssertNil(controller.state)
    }

    func testLoadErrorsUseOwnerFacingMessages() async {
        for (error, message) in errorCases {
            let client = MoodClient(load: .failure(error))
            let controller = PersonaMoodController(personaID: "p", client: client)
            await controller.load()
            XCTAssertEqual(controller.errorMessage, message)
            XCTAssertFalse(controller.isUnavailable)
        }
    }

    func testFailedOptimisticUpdateRestoresPreviousState() async {
        let initial = PersonaState(personaID: "p", mood: .calm, moodPublic: true)
        let client = MoodClient(load: .success(initial), update: .failure(.httpStatus(500)))
        let controller = PersonaMoodController(personaID: "p", client: client)
        await controller.load()

        await controller.setMood(.happy)

        XCTAssertEqual(controller.state, initial)
        XCTAssertEqual(controller.errorMessage, GatewayClientError.httpStatus(500).errorDescription)
        let patches = await client.patches()
        XCTAssertEqual(patches, [PersonaStatePatch(mood: .happy)])
    }

    func testPatchDisabledHidesBlockWithoutError() async {
        let initial = PersonaState(personaID: "p", mood: .calm, moodPublic: true)
        let client = MoodClient(load: .success(initial),
                                update: .failure(.problem(status: 404, code: "mood.disabled")))
        let controller = PersonaMoodController(personaID: "p", client: client)
        await controller.load()
        await controller.setMood(.happy)

        XCTAssertTrue(controller.isUnavailable)
        XCTAssertNil(controller.state)
        XCTAssertNil(controller.errorMessage)
    }

    func testPatchErrorsShowMessagesAndRollback() async {
        let initial = PersonaState(personaID: "p", mood: .calm, moodPublic: true)
        for (error, message) in errorCases {
            let client = MoodClient(load: .success(initial), update: .failure(error))
            let controller = PersonaMoodController(personaID: "p", client: client)
            await controller.load()
            await controller.setMood(.happy)
            XCTAssertEqual(controller.state, initial)
            XCTAssertEqual(controller.errorMessage, message)
        }
    }

    func testSuccessfulOptimisticUpdatesUseOneFieldPatches() async {
        let initial = PersonaState(personaID: "p", mood: .calm, moodPublic: true)
        let updatedMood = PersonaState(personaID: "p", mood: .happy, moodPublic: true)
        let updatedVisibility = PersonaState(personaID: "p", mood: .happy, moodPublic: false)
        let client = MoodClient(load: .success(initial), updates: [.success(updatedMood), .success(updatedVisibility)])
        let controller = PersonaMoodController(personaID: "p", client: client)
        await controller.load()

        await controller.setMood(.happy)
        await controller.setMoodPublic(false)

        XCTAssertEqual(controller.state, updatedVisibility)
        XCTAssertTrue(controller.state?.showsPrivateMoodMarker == true)
        let patches = await client.patches()
        XCTAssertEqual(patches, [
            PersonaStatePatch(mood: .happy), PersonaStatePatch(moodPublic: false),
        ])
    }

    func testFailedVisibilityUpdateRestoresPreviousSwitch() async {
        let initial = PersonaState(personaID: "p", mood: .tired, moodPublic: true)
        let client = MoodClient(load: .success(initial), update: .failure(.httpStatus(422)))
        let controller = PersonaMoodController(personaID: "p", client: client)
        await controller.load()
        await controller.setMoodPublic(false)
        XCTAssertEqual(controller.state, initial)
        XCTAssertFalse(controller.state?.showsPrivateMoodMarker == true)
        XCTAssertEqual(controller.errorMessage, "操作失败，请稍后重试")
        let patches = await client.patches()
        XCTAssertEqual(patches, [PersonaStatePatch(moodPublic: false)])
    }

    func testOptimisticMoodIsVisibleBeforeFailedPatchRollsBack() async {
        let initial = PersonaState(personaID: "p", mood: .calm, moodPublic: true)
        let gate = UpdateGate()
        let client = PendingMoodClient(initial: initial, gate: gate)
        let controller = PersonaMoodController(personaID: "p", client: client)
        await controller.load()

        let task = Task { await controller.setMood(.happy) }
        await gate.waitUntilStarted()
        XCTAssertEqual(controller.state?.mood, .happy)

        await gate.finish(with: .httpStatus(500))
        await task.value
        XCTAssertEqual(controller.state, initial)
    }
}

private actor UpdateGate {
    private var start: CheckedContinuation<Void, Never>?
    private var result: CheckedContinuation<GatewayClientError, Never>?
    private var started = false

    func wait() async -> GatewayClientError {
        await withCheckedContinuation { continuation in
            result = continuation
            started = true
            start?.resume()
            start = nil
        }
    }

    func waitUntilStarted() async {
        if started { return }
        await withCheckedContinuation { start = $0 }
    }

    func finish(with error: GatewayClientError) {
        result?.resume(returning: error)
        result = nil
    }
}

private actor PendingMoodClient: GatewayClient {
    let initial: PersonaState
    let gate: UpdateGate

    init(initial: PersonaState, gate: UpdateGate) {
        self.initial = initial
        self.gate = gate
    }

    func personaState(id: String) async throws -> PersonaState { initial }
    func updatePersonaState(id: String, patch: PersonaStatePatch) async throws -> PersonaState {
        let error = await gate.wait()
        throw error
    }
    func capabilities(locale: String?, appVersion: String?) async throws -> GatewayCapabilityManifest { throw GatewayClientError.invalidResponse }
    func cards(capabilityID: String?, cardTypes: [String]?) async throws -> [GatewayCard] { throw GatewayClientError.invalidResponse }
    func perform(card: GatewayCard, action: GatewayCardAction) async throws -> GatewayActionReceipt { throw GatewayClientError.invalidResponse }
    func exchangeSessionCookie(capabilityID: String, code: String) async throws -> GatewayCookieExchange { throw GatewayClientError.invalidResponse }
    func createModuleWebSession(capabilityID: String, url: URL, allowedOrigins: [String]?, scopes: [String]?, ttlSeconds: Int?) async throws -> ModuleWebSessionGrant { throw GatewayClientError.invalidResponse }
}

private actor MoodClient: GatewayClient {
    private let loadResult: Result<PersonaState, GatewayClientError>
    private var updateResults: [Result<PersonaState, GatewayClientError>]
    private var receivedPatches: [PersonaStatePatch] = []

    init(load: Result<PersonaState, GatewayClientError>,
         update: Result<PersonaState, GatewayClientError>? = nil,
         updates: [Result<PersonaState, GatewayClientError>] = []) {
        loadResult = load
        updateResults = updates.isEmpty ? (update.map { [$0] } ?? []) : updates
    }

    func personaState(id: String) async throws -> PersonaState { try loadResult.get() }

    func capabilities(locale: String?, appVersion: String?) async throws -> GatewayCapabilityManifest {
        throw GatewayClientError.invalidResponse
    }

    func cards(capabilityID: String?, cardTypes: [String]?) async throws -> [GatewayCard] {
        throw GatewayClientError.invalidResponse
    }

    func perform(card: GatewayCard, action: GatewayCardAction) async throws -> GatewayActionReceipt {
        throw GatewayClientError.invalidResponse
    }

    func exchangeSessionCookie(capabilityID: String, code: String) async throws -> GatewayCookieExchange {
        throw GatewayClientError.invalidResponse
    }

    func updatePersonaState(id: String, patch: PersonaStatePatch) async throws -> PersonaState {
        receivedPatches.append(patch)
        guard !updateResults.isEmpty else { throw GatewayClientError.invalidResponse }
        return try updateResults.removeFirst().get()
    }

    func patches() -> [PersonaStatePatch] { receivedPatches }

    func createModuleWebSession(
        capabilityID: String, url: URL, allowedOrigins: [String]?, scopes: [String]?, ttlSeconds: Int?
    ) async throws -> ModuleWebSessionGrant {
        throw GatewayClientError.invalidResponse
    }
}
