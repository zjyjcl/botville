// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation
import XCTest

@testable import BotvilleCore

@MainActor
final class PersonaMoodControllerTests: XCTestCase {
    private var errorCases: [(Error, String)] {
        [
            (GatewayClientError.problem(status: 401, code: "auth.required"), "会话已失效，请重新登录。"),
            (GatewayClientError.httpStatus(401), "会话已失效，请重新登录。"),
            (GatewayClientError.problem(status: 403, code: "persona.not_owned"), "没有权限查看"),
            (GatewayClientError.httpStatus(403), "没有权限查看"),
            (GatewayClientError.problem(status: 404, code: "persona.not_found"), "不存在或不可见"),
            (GatewayClientError.problem(status: 404, code: "unknown"), "网关请求失败，请稍后重试。"),
            (GatewayClientError.httpStatus(404), "网关请求失败，请稍后重试。"),
            (GatewayClientError.problem(status: 422, code: "validation.failed"), "操作失败，请稍后重试"),
            (GatewayClientError.httpStatus(422), "操作失败，请稍后重试"),
            (GatewayClientError.problem(status: 429, code: "rate_limited.transport"), "操作太频繁，请稍后再试。"),
            (GatewayClientError.httpStatus(429), "操作太频繁，请稍后再试。"),
            (GatewayClientError.serverAuthMisconfigured, "服务器认证异常，请联系部署者"),
            (GatewayClientError.problem(status: 503, code: "mood.unavailable"), "心情暂时不可用"),
            (GatewayClientError.httpStatus(503), "网关请求失败，请稍后重试。"),
            (URLError(.notConnectedToInternet), "操作失败，请稍后重试"),
            (GatewayClientError.invalidResponse, "操作失败，请稍后重试"),
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

    func testPrivateMoodMarkerPresentationForOwnerAndCounterpart() {
        for mood in PersonaMood.allCases {
            for moodPublic in [false, true] {
                let state = PersonaState(personaID: "p", mood: mood, moodPublic: moodPublic)
                let owner = PersonaMoodPresentation(ownerState: state)
                XCTAssertEqual(owner.mood, mood)
                XCTAssertEqual(owner.privateMarkerText,
                               state.showsPrivateMoodMarker ? "仅自己可见" : nil)
                let counterpart = PersonaMoodPresentation(counterpartMood: mood)
                XCTAssertEqual(counterpart.mood, mood)
                XCTAssertNil(counterpart.privateMarkerText)
            }
        }
        for moodPublic in [false, true] {
            let state = PersonaState(personaID: "p", mood: nil, moodPublic: moodPublic)
            XCTAssertNil(PersonaMoodPresentation(ownerState: state).privateMarkerText)
        }
        XCTAssertNil(PersonaMoodPresentation(counterpartMood: nil).privateMarkerText)
    }

    func testMoodUnavailableUsesSharedDescription() {
        XCTAssertEqual(GatewayClientError.problem(status: 503, code: "mood.unavailable").errorDescription,
                       "心情暂时不可用")
    }

    func testDisabledMoodHidesBlockWithoutError() async {
        let client = MoodClient(load: .failure(GatewayClientError.problem(status: 404, code: "mood.disabled")))
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
        let client = MoodClient(load: .success(initial), update: .failure(GatewayClientError.httpStatus(500)))
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
                                update: .failure(GatewayClientError.problem(status: 404, code: "mood.disabled")))
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
        let client = MoodClient(load: .success(initial), update: .failure(GatewayClientError.httpStatus(422)))
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
    private let loadResult: Result<PersonaState, Error>
    private var updateResults: [Result<PersonaState, Error>]
    private var receivedPatches: [PersonaStatePatch] = []

    init(load: Result<PersonaState, Error>,
         update: Result<PersonaState, Error>? = nil,
         updates: [Result<PersonaState, Error>] = []) {
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
