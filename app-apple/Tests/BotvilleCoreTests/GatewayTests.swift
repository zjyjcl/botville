// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation
import XCTest

@testable import BotvilleCore

final class GatewayTests: XCTestCase {
    func testManifestKeepsUnknownWebAndCardListButResolvesNativeAndDependencies() throws {
        let manifest = try decodeManifest(
            """
            {"manifest_version":"1.0.0","contract_version":"0.5.0","generated_at":"2026-11-03T12:00:00Z",
             "defaults":{"unknown_capability":"hidden"},"capabilities":[
              {"id":"native.unknown","module":"other","version":"1","title":"N","summary":"","status":"enabled","entry":{"type":"native","deep_link":"botville://new"},"scopes":[]},
              {"id":"web.future","module":"future","version":"1","title":"W","summary":"","status":"enabled","entry":{"type":"web","web_url":"https://example.test/"},"scopes":[]},
              {"id":"cards.future","module":"future","version":"1","title":"C","summary":"","status":"enabled","entry":{"type":"card_list","card_types":["future"]},"scopes":[]},
              {"id":"disabled.module","module":"other","version":"1","title":"D","summary":"","status":"disabled","entry":{"type":"web","web_url":"https://example.test/"},"scopes":[]},
              {"id":"depends.missing","module":"other","version":"1","title":"M","summary":"","status":"enabled","entry":{"type":"web","web_url":"https://example.test/"},"scopes":[],"requires":["not.installed"]},
              {"id":"depends.transitive","module":"other","version":"1","title":"T","summary":"","status":"enabled","entry":{"type":"card_list","card_types":[]},"scopes":[],"requires":["depends.missing"]}
             ]}
            """)
        let projections = Dictionary(
            uniqueKeysWithValues: manifest.capabilityProjections(knownNativeRoutes: [:]).map {
                ($0.capability.id, $0)
            })
        XCTAssertFalse(projections["native.unknown"]!.isVisible)
        XCTAssertTrue(projections["web.future"]!.isVisible)
        XCTAssertTrue(projections["cards.future"]!.isVisible)
        XCTAssertFalse(projections["disabled.module"]!.isVisible)
        XCTAssertFalse(projections["depends.missing"]!.isAvailable)
        XCTAssertFalse(projections["depends.transitive"]!.isAvailable)
    }

    func testUnknownNativeDisabledRemainsVisibleButUnavailableAndUnknownEntryIsHidden() throws {
        let manifest = try decodeManifest(
            """
            {"manifest_version":"1","contract_version":"0.5.0","generated_at":"2026-11-03T12:00:00Z","defaults":{"unknown_capability":"disabled"},"capabilities":[
            {"id":"native.unknown","module":"x","version":"1","title":"N","summary":"","status":"enabled","entry":{"type":"native","deep_link":"botville://x"},"scopes":[]},
            {"id":"entry.future","module":"x","version":"1","title":"F","summary":"","status":"enabled","entry":{"type":"spatial"},"scopes":[]}]}
            """)
        let values = Dictionary(
            uniqueKeysWithValues: manifest.capabilityProjections(knownNativeRoutes: [:]).map {
                ($0.capability.id, $0)
            })
        XCTAssertTrue(values["native.unknown"]!.isVisible)
        XCTAssertFalse(
            values["native.unknown"]!.isAvailable, "disabled policy exposes an unavailable native affordance")
        XCTAssertFalse(values["entry.future"]!.isVisible)
    }

    func testCardToleratesUnknownTypeBlockAndOddBlockValues() throws {
        let card = try decodeCard(
            """
            {"id":"c","card_schema_version":"1.1.0","type":"brand_new","title":"x","fallback_text":"read this","priority":"future","risk":"mystery","status":"active","blocks":[
            {"type":"future","value":{"not":"a number"},"items":"also-not-array"},{"type":"text","text":"ok","level":"new-level"}],"actions":[],"source":{"module":"x"}}
            """)
        XCTAssertFalse(card.isKnownType)
        XCTAssertEqual(card.fallbackText, "read this")
        XCTAssertTrue(card.hasUnknownBlocks)
        XCTAssertEqual(card.renderableBlocks.count, 1)
        XCTAssertEqual(card.effectiveRisk, .high)
    }

    func testCardSchemaMajorAndMinorNegotiationAndActionRiskOverride() throws {
        XCTAssertTrue(GatewayCardSchema.supports("1.99.0"))
        XCTAssertFalse(GatewayCardSchema.supports("2.0.0"))
        let card = try decodeCard(
            """
            {"id":"c","card_schema_version":"2.0.0","type":"approval","title":"x","fallback_text":"fallback","priority":"normal","risk":"low","status":"active","blocks":[],"actions":[{"id":"a","label":"go","kind":"approve","style":"future","risk":"unknown"}],"source":{"module":"x"}}
            """)
        XCTAssertFalse(card.isActionable, "new major only exposes fallback text")
        XCTAssertEqual(card.actions[0].effectiveRisk(cardRisk: card.effectiveRisk), .high)
    }

    func testCardMissingFieldsUsesReadableFallbackAndUnknownStatusOrActionIsNotActionable() throws {
        let missing = try decodeCard("{\"title\":\"来自模块的内容\"}")
        XCTAssertEqual(missing.fallbackText, "来自模块的内容")
        XCTAssertFalse(missing.isActionable)
        let unknown = try decodeCard(
            """
            {"id":"c","card_schema_version":"1.0.0","type":"notification","title":"x","fallback_text":"f","priority":"normal","risk":"low","status":"future","blocks":[],"actions":[{"id":"future","label":"Future","kind":"new_action","style":"primary"}],"source":{"module":"x"}}
            """)
        XCTAssertFalse(unknown.isActionable)
        XCTAssertFalse(unknown.actions[0].isKnownKind)
    }

    func testHTTPClientBuildsCapabilityCardAndActionRequests() async throws {
        let recorder = RequestRecorder()
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [GatewayURLProtocol.self]
        GatewayURLProtocol.recorder = recorder
        defer { GatewayURLProtocol.recorder = nil }
        let client = try HTTPGatewayClient(
            baseURL: URL(string: "https://gateway.example.test/base")!, token: "token",
            session: URLSession(configuration: configuration))
        recorder.enqueue(status: 200, body: manifestJSON)
        _ = try await client.capabilities(locale: "zh-CN", appVersion: "0.1.0")
        let first = try XCTUnwrap(recorder.requests.first)
        XCTAssertEqual(first.url?.path, "/base/v1/capabilities")
        let queryNames = try XCTUnwrap(
            URLComponents(url: try XCTUnwrap(first.url), resolvingAgainstBaseURL: false)?.queryItems?.map(
                \.name
            ).sorted())
        XCTAssertEqual(queryNames, ["app_version", "locale"])
        XCTAssertNil(first.value(forHTTPHeaderField: "X-Card-Schema-Version"))
        XCTAssertEqual(first.value(forHTTPHeaderField: "Authorization"), "Bearer token")

        let card = try decodeCard(cardJSON)
        recorder.enqueue(status: 200, body: cardPageJSON)
        _ = try await client.cards()
        let cardsRequest = try XCTUnwrap(recorder.requests.last)
        XCTAssertEqual(cardsRequest.url?.path, "/base/v1/cards")
        XCTAssertEqual(
            cardsRequest.value(forHTTPHeaderField: "X-Card-Schema-Version"),
            GatewayCardSchema.requestHeaderValue())
        recorder.enqueue(status: 200, body: receiptJSON)
        _ = try await client.perform(card: card, action: card.actions[0])
        let action = try XCTUnwrap(recorder.requests.last)
        XCTAssertEqual(action.httpMethod, "POST")
        XCTAssertEqual(action.url?.path, "/base/v1/cards/c/actions/deny")
        XCTAssertNotNil(action.value(forHTTPHeaderField: "Idempotency-Key"))
        let object = try JSONSerialization.jsonObject(with: try XCTUnwrap(action.httpBody)) as? [String: Any]
        XCTAssertEqual(object?["revision"] as? Int, 7)
        recorder.enqueue(status: 200, body: receiptJSON)
        _ = try await client.perform(card: card, action: card.actions[0])
        XCTAssertEqual(
            recorder.requests.last?.value(forHTTPHeaderField: "Idempotency-Key"),
            action.value(forHTTPHeaderField: "Idempotency-Key"))
    }

    func testCookieExchangeHasNoAuthorizationAndBaseURLRejectsUnsafeShapes() async throws {
        let recorder = RequestRecorder()
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [GatewayURLProtocol.self]
        GatewayURLProtocol.recorder = recorder
        defer { GatewayURLProtocol.recorder = nil }
        let client = try HTTPGatewayClient(
            baseURL: URL(string: "https://gateway.example.test")!, token: "secret",
            session: URLSession(configuration: configuration))
        recorder.enqueue(
            status: 204, headers: ["Set-Cookie": "session=opaque; HttpOnly; Secure; SameSite=Lax"])
        let result = try await client.exchangeSessionCookie(
            capabilityID: "memory.search", code: "opaque-code")
        XCTAssertEqual(result.statusCode, 204)
        let request = try XCTUnwrap(recorder.requests.last)
        XCTAssertNil(request.value(forHTTPHeaderField: "Authorization"))
        XCTAssertNil(request.value(forHTTPHeaderField: "Idempotency-Key"))
        XCTAssertEqual(request.url?.path, "/v1/capabilities/memory.search/session-cookies")
        XCTAssertThrowsError(
            try HTTPGatewayClient(baseURL: URL(string: "https://user:pass@gateway.example.test")!))
        XCTAssertThrowsError(
            try HTTPGatewayClient(baseURL: URL(string: "https://gateway.example.test/?q=x")!))
        XCTAssertThrowsError(try HTTPGatewayClient(baseURL: URL(string: "https://gateway.example.test:0")!))
        XCTAssertThrowsError(try HTTPGatewayClient(baseURL: "http://gateway.example.test"))
        XCTAssertNoThrow(try HTTPGatewayClient(baseURL: "http://localhost:8080", allowLocalHTTP: true))
    }

    func testHTTPSessionGrantIntersectsGatewayOriginsWithLocalLaunchOrigin() async throws {
        let recorder = RequestRecorder()
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [GatewayURLProtocol.self]
        GatewayURLProtocol.recorder = recorder
        defer { GatewayURLProtocol.recorder = nil }
        let client = try HTTPGatewayClient(
            baseURL: URL(string: "https://gateway.example.test")!,
            session: URLSession(configuration: configuration))
        let moduleURL = try XCTUnwrap(URL(string: "https://module.example.test/path"))
        let expiresAt = ISO8601DateFormatter().string(from: Date().addingTimeInterval(300))
        func token(origins: [String]) -> String {
            let originJSON = origins.map { "\"\($0)\"" }.joined(separator: ",")
            return "{\"code\":\"once\",\"code_ttl_seconds\":60,\"expires_at\":\"\(expiresAt)\",\"audience\":\"memory.search\",\"url\":\"https://module.example.test/path?code=once\",\"allowed_origins\":[\(originJSON)]}"
        }
        recorder.enqueue(status: 200, body: token(origins: [
            "https://module.example.test", "https://gateway-added.example.test",
        ]))
        let grant = try await client.createModuleWebSession(
            capabilityID: "memory.search", url: moduleURL,
            allowedOrigins: ["https://module.example.test", "https://caller-extra.example.test"])
        XCTAssertEqual(grant.allowedOrigins, ["https://module.example.test"])
        let body = try XCTUnwrap(recorder.requests.last?.httpBody)
        let requestJSON = try XCTUnwrap(JSONSerialization.jsonObject(with: body) as? [String: Any])
        XCTAssertEqual(requestJSON["allowed_origins"] as? [String], ["https://module.example.test"])

        recorder.enqueue(status: 200, body: token(origins: ["https://gateway-added.example.test"]))
        await XCTAssertGatewayError(.invalidResponse) {
            _ = try await client.createModuleWebSession(capabilityID: "memory.search", url: moduleURL)
        }
    }

    func testMockActionTransitionsAndHighRiskPlaceholderDoesNotAuthorize() async throws {
        let mock = MockGatewayClient()
        let initialCards = try await mock.cards()
        let card = try XCTUnwrap(initialCards.first)
        let normal = try XCTUnwrap(card.actions.first(where: { $0.id == "deny" }))
        let receipt = try await mock.perform(card: card, action: normal)
        XCTAssertEqual(receipt.status, "accepted")
        XCTAssertEqual(receipt.card.status, "acted")
        let updatedCards = try await mock.cards()
        XCTAssertEqual(try XCTUnwrap(updatedCards.first).status, "acted")
        let highRisk = try XCTUnwrap(card.actions.first(where: { $0.id == "approve" }))
        do {
            _ = try await mock.perform(card: card, action: highRisk)
            XCTFail("high risk must not submit")
        } catch HighRiskAuthorizationError.unavailable {}
        let cardsAfterHighRisk = try await mock.cards()
        XCTAssertEqual(cardsAfterHighRisk, updatedCards)
        do {
            _ = try await UnavailableHighRiskAuthorizer().authorize(
                cardID: card.id, actionID: "approve", revision: card.revision ?? 1)
            XCTFail("must not synthesize an assertion")
        } catch HighRiskAuthorizationError.unavailable {}
    }

    func testMockSessionCodeIsSingleUseBoundToCapabilityAndExpires() async throws {
        let clock = TestClock(Date(timeIntervalSince1970: 1_700_000_000))
        let mock = MockGatewayClient(now: { clock.now })
        let moduleURL = try XCTUnwrap(URL(string: "https://gateway.example.invalid/modules/memory/"))
        let first = try await mock.createModuleWebSession(
            capabilityID: "memory.search", url: moduleURL, scopes: ["memory.read"], ttlSeconds: 300)
        let code = try XCTUnwrap(
            URLComponents(url: first.url, resolvingAgainstBaseURL: false)?.queryItems?.first(where: {
                $0.name == "code"
            })?.value)
        let cookie = try await mock.exchangeSessionCookie(capabilityID: "memory.search", code: code)
        XCTAssertEqual(cookie.statusCode, 204)
        await XCTAssertGatewayError(.invalidResponse) {
            _ = try await mock.exchangeSessionCookie(capabilityID: "memory.search", code: code)
        }

        let second = try await mock.createModuleWebSession(
            capabilityID: "memory.search", url: moduleURL, scopes: ["memory.read"], ttlSeconds: 300)
        let secondCode = try XCTUnwrap(
            URLComponents(url: second.url, resolvingAgainstBaseURL: false)?.queryItems?.first(where: {
                $0.name == "code"
            })?.value)
        await XCTAssertGatewayError(.invalidResponse) {
            _ = try await mock.exchangeSessionCookie(capabilityID: "other.module", code: secondCode)
        }
        clock.advance(by: 61)
        await XCTAssertGatewayError(.expiredSessionCode) {
            _ = try await mock.exchangeSessionCookie(capabilityID: "memory.search", code: secondCode)
        }
    }

    func testHTTPActionPathPercentEncodesOpaqueIDsAndHighRiskNeverRequests() async throws {
        let recorder = RequestRecorder()
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [GatewayURLProtocol.self]
        GatewayURLProtocol.recorder = recorder
        defer { GatewayURLProtocol.recorder = nil }
        let client = try HTTPGatewayClient(
            baseURL: URL(string: "https://gateway.example.test")!,
            session: URLSession(configuration: configuration))
        let opaque = try decodeCard(
            """
            {"id":"card/a?b","card_schema_version":"1.0.0","type":"notification","title":"x","fallback_text":"f","priority":"normal","risk":"low","status":"active","blocks":[],"actions":[{"id":"deny/?","label":"Deny","kind":"deny","style":"secondary"}],"source":{"module":"x"},"revision":1}
            """)
        recorder.enqueue(status: 200, body: receiptJSON)
        _ = try await client.perform(card: opaque, action: opaque.actions[0])
        let opaqueURL = try XCTUnwrap(recorder.requests.last?.url)
        XCTAssertEqual(
            try XCTUnwrap(URLComponents(url: opaqueURL, resolvingAgainstBaseURL: false)?.percentEncodedPath),
            "/v1/cards/card%2Fa%3Fb/actions/deny%2F%3F")
        let before = recorder.requests.count
        let highRisk = try decodeCard(
            """
            {"id":"high","expires_at":"2099-01-01T00:00:00Z","card_schema_version":"1.0.0","type":"notification","title":"x","fallback_text":"f","priority":"normal","risk":"unknown","status":"active","blocks":[],"actions":[{"id":"deny","label":"Deny","kind":"deny","style":"secondary"}],"source":{"module":"x"},"revision":1}
            """)
        do {
            _ = try await client.perform(card: highRisk, action: highRisk.actions[0])
            XCTFail("unknown risk must stop before transport")
        } catch HighRiskAuthorizationError.unavailable {}
        XCTAssertEqual(recorder.requests.count, before)
        let biometric = try decodeCard(
            """
            {"id":"biometric","card_schema_version":"1.0.0","type":"notification","title":"x","fallback_text":"f","priority":"normal","risk":"low","status":"active","blocks":[],"actions":[{"id":"deny","label":"Deny","kind":"deny","style":"secondary","confirmation":{"required":true,"biometric":true}}],"source":{"module":"x"},"revision":1}
            """)
        do {
            _ = try await client.perform(card: biometric, action: biometric.actions[0])
            XCTFail("biometric action must stop before transport")
        } catch HighRiskAuthorizationError.unavailable {}
        XCTAssertEqual(recorder.requests.count, before)
    }

    private func decodeManifest(_ text: String) throws -> GatewayCapabilityManifest {
        try JSONDecoder.gateway.decode(GatewayCapabilityManifest.self, from: Data(text.utf8))
    }
    private func decodeCard(_ text: String) throws -> GatewayCard {
        try JSONDecoder.gateway.decode(GatewayCard.self, from: Data(text.utf8))
    }
}

extension JSONDecoder {
    fileprivate static var gateway: JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return decoder
    }
}

private final class RequestRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var pending: [(Int, Data, [String: String])] = []
    private var values: [URLRequest] = []
    var requests: [URLRequest] {
        lock.lock()
        defer { lock.unlock() }
        return values
    }
    func enqueue(status: Int, body: String = "", headers: [String: String] = [:]) {
        lock.lock()
        pending.append((status, Data(body.utf8), headers))
        lock.unlock()
    }
    func dequeue() -> (Int, Data, [String: String]) {
        lock.lock()
        defer { lock.unlock() }
        return pending.removeFirst()
    }
    func append(_ request: URLRequest) {
        var captured = request
        if captured.httpBody == nil, let stream = captured.httpBodyStream {
            stream.open()
            defer { stream.close() }
            var data = Data()
            var buffer = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable {
                let count = stream.read(&buffer, maxLength: buffer.count)
                guard count > 0 else { break }
                data.append(buffer, count: count)
            }
            captured.httpBody = data
        }
        lock.lock()
        values.append(captured)
        lock.unlock()
    }
}

private final class GatewayURLProtocol: URLProtocol {
    static var recorder: RequestRecorder?
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        guard let recorder = Self.recorder else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }
        recorder.append(request)
        let response = recorder.dequeue()
        client?.urlProtocol(
            self,
            didReceive: HTTPURLResponse(
                url: request.url!, statusCode: response.0, httpVersion: "HTTP/1.1", headerFields: response.2)!,
            cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: response.1)
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

private final class TestClock: @unchecked Sendable {
    private let lock = NSLock()
    private var value: Date
    init(_ value: Date) { self.value = value }
    var now: Date {
        lock.lock()
        defer { lock.unlock() }
        return value
    }
    func advance(by interval: TimeInterval) {
        lock.lock()
        value = value.addingTimeInterval(interval)
        lock.unlock()
    }
}

private func XCTAssertGatewayError(
    _ expected: GatewayClientError, operation: @escaping () async throws -> Void
) async {
    do {
        try await operation()
        XCTFail("Expected \(expected)")
    } catch let error as GatewayClientError { XCTAssertEqual(error, expected) } catch {
        XCTFail("Unexpected error: \(error)")
    }
}

private let manifestJSON = """
    {"manifest_version":"1","contract_version":"0.5.0","generated_at":"2026-11-03T12:00:00Z","capabilities":[]}
    """
private let cardJSON = """
    {"id":"c","card_schema_version":"1.0.0","type":"approval","title":"x","fallback_text":"f","priority":"normal","risk":"low","status":"active","blocks":[],"actions":[{"id":"deny","label":"Deny","kind":"deny","style":"secondary"},{"id":"approve","label":"Approve","kind":"approve","style":"primary","risk":"high"}],"source":{"module":"x"},"expires_at":"2099-01-01T00:00:00Z","revision":7}
    """
private let cardPageJSON = "{\"items\":[" + cardJSON + "],\"next_cursor\":null,\"has_more\":false}"
private let receiptJSON =
    "{\"card_id\":\"c\",\"action_id\":\"deny\",\"status\":\"accepted\",\"audit_id\":\"a\",\"card\":"
    + cardJSON + "}"
