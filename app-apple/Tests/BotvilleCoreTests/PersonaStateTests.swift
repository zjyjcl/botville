// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

@testable import BotvilleCore
import Foundation
import XCTest

final class PersonaStateTests: XCTestCase {
    func testProactiveSettingsDecodesCompleteConfiguration() throws {
        let data = Data(#"{"enabled":true,"max_per_day":6,"require_reply":false,"quiet_hours":{"enabled":false,"start":"23:00","end":"07:30","timezone":"Asia/Shanghai"}}"#.utf8)
        let settings = try GatewayCoding.decoder.decode(ProactiveSettings.self, from: data)
        XCTAssertTrue(settings.enabled)
        XCTAssertEqual(settings.maxPerDay, 6)
        XCTAssertEqual(settings.requireReply, false)
        XCTAssertEqual(settings.quietHours?.enabled, false)
        XCTAssertEqual(settings.quietHours?.start, "23:00")
        XCTAssertEqual(settings.quietHours?.end, "07:30")
        XCTAssertEqual(settings.quietHours?.timezone, "Asia/Shanghai")
        let encoded = try JSONEncoder().encode(settings)
        XCTAssertEqual(try JSONSerialization.jsonObject(with: encoded) as? NSDictionary,
                       try JSONSerialization.jsonObject(with: data) as? NSDictionary)
    }

    func testProactiveMissingEnabledDefaultsOffAndIgnoresUnknownFields() throws {
        let data = Data(#"{"max_per_day":0,"require_reply":true,"future_mode":"unknown","quiet_hours":{"start":"23:00","end":"07:30","timezone":"Asia/Shanghai","future":{}}}"#.utf8)
        let settings = try GatewayCoding.decoder.decode(ProactiveSettings.self, from: data)
        XCTAssertFalse(settings.enabled)
        XCTAssertEqual(settings.maxPerDay, 0)
        XCTAssertEqual(settings.requireReply, true)
        XCTAssertEqual(settings.quietHours?.enabled, true)
        let empty = try GatewayCoding.decoder.decode(ProactiveSettings.self, from: Data("{}".utf8))
        XCTAssertFalse(empty.enabled)
        XCTAssertNil(empty.maxPerDay)
        XCTAssertNil(empty.requireReply)
        XCTAssertNil(empty.quietHours)
    }

    func testPersonaProactiveIsOptionalAndDoesNotChangeUnknownMoodHandling() throws {
        let base: [String: Any] = [
            "id": "persona_demo", "owner_id": "@owner:example.invalid", "display_name": "Demo",
            "kind": "custom", "status": "active", "revision": 1, "visibility": "private",
            "created_at": "2026-11-13T00:00:00Z", "updated_at": "2026-11-13T00:00:00Z",
        ]
        for proactive: Any? in [nil, NSNull(), [:], ["enabled": true, "unknown": "future"]] {
            for mood: Any? in [nil, NSNull(), "future", "happy"] {
                var object = base
                object["proactive"] = proactive
                object["mood"] = mood
                object["future_field"] = ["ignored": true]
                let persona = try GatewayCoding.decoder.decode(MemoryPersona.self,
                    from: JSONSerialization.data(withJSONObject: object))
                XCTAssertEqual(persona.mood, mood as? String == "happy" ? .happy : nil)
                if let config = proactive as? [String: Any] {
                    XCTAssertEqual(try XCTUnwrap(persona.proactive).enabled, config["enabled"] as? Bool ?? false)
                } else {
                    XCTAssertNil(persona.proactive)
                }
            }
        }
    }

    func testStateDecodesKnownUnknownAndNullMoodLabels() throws {
        for (label, expected): (Any, PersonaMood?) in [
            ("calm", .calm), ("happy", .happy), ("sad", .sad), ("excited", .excited),
            ("tired", .tired), ("future", nil), (NSNull(), nil),
        ] {
            let mood: Any = label is NSNull ? NSNull() : ["label": label, "source": "owner", "set_at": NSNull()]
            let object: [String: Any] = ["persona_id": "id", "mood": mood,
                "mood_public": true, "updated_at": NSNull()]
            let state = try GatewayCoding.decoder.decode(PersonaState.self,
                from: JSONSerialization.data(withJSONObject: object))
            XCTAssertEqual(state.mood, expected)
        }
        for mood in [#"null"#, #"{"label":null}"#, #"{}"#] {
            let data = Data("{\"persona_id\":\"id\",\"mood\":\(mood),\"mood_public\":false,\"updated_at\":null}".utf8)
            XCTAssertNil(try GatewayCoding.decoder.decode(PersonaState.self, from: data).mood)
        }
    }

    func testMoodLabelsAndSingleFieldPatchesMatchContract() throws {
        XCTAssertEqual(PersonaMood.allCases.map(\.label), ["平静", "愉快", "低落", "兴奋", "疲惫"])
        let mood = try JSONSerialization.jsonObject(with: JSONEncoder().encode(PersonaStatePatch(mood: .happy))) as? NSDictionary
        XCTAssertEqual(mood, ["mood": "happy"] as NSDictionary)
        let visibility = try JSONSerialization.jsonObject(with: JSONEncoder().encode(PersonaStatePatch(moodPublic: false))) as? NSDictionary
        XCTAssertEqual(visibility, ["mood_public": false] as NSDictionary)
    }

    func testStateGETAndPATCHUseMatrixOpenIDAndExpectedBody() async throws {
        let recorder = StateRequestRecorder()
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StateURLProtocol.self]
        StateURLProtocol.recorder = recorder
        defer { StateURLProtocol.recorder = nil }
        let client = try makeClient(session: URLSession(configuration: configuration))
        let state = #"{"persona_id":"id","mood":{"label":"calm","source":"owner","set_at":null},"mood_public":true,"updated_at":null}"#
        recorder.enqueue(status: 200, body: state)
        _ = try await client.personaState(id: "id")
        let get = try XCTUnwrap(recorder.requests.last)
        XCTAssertEqual(get.httpMethod, "GET")
        XCTAssertEqual(get.url?.path, "/v1/personas/id/state")
        XCTAssertNil(get.httpBody)
        XCTAssertTrue(get.value(forHTTPHeaderField: "Authorization")?.hasPrefix("MatrixOpenID ") == true)
        XCTAssertFalse(get.value(forHTTPHeaderField: "Authorization")?.hasPrefix("Bearer ") == true)

        recorder.enqueue(status: 200, body: state)
        _ = try await client.updatePersonaState(id: "id", patch: .init(mood: .tired, moodPublic: false))
        let patch = try XCTUnwrap(recorder.requests.last)
        XCTAssertEqual(patch.httpMethod, "PATCH")
        XCTAssertEqual(patch.url?.path, "/v1/personas/id/state")
        XCTAssertTrue(patch.value(forHTTPHeaderField: "Authorization")?.hasPrefix("MatrixOpenID ") == true)
        XCTAssertFalse(patch.value(forHTTPHeaderField: "Authorization")?.hasPrefix("Bearer ") == true)
        XCTAssertNil(patch.value(forHTTPHeaderField: "Idempotency-Key"))
        let body = try XCTUnwrap(JSONSerialization.jsonObject(with: try XCTUnwrap(patch.httpBody)) as? [String: Any])
        XCTAssertEqual(body as NSDictionary, ["mood": "tired", "mood_public": false] as NSDictionary)
    }

    func testStateOpenIDRoutingRejectsLookalikesAndOtherSubpaths() throws {
        let client = try makeClient()
        for method in ["GET", "PATCH"] {
            let constructed = try client.request(["personas", "id", "state"], method: method)
            XCTAssertNil(constructed.value(forHTTPHeaderField: "Authorization"))
        }
        XCTAssertTrue(try client.isOpenIDRequest(client.request(["personas", "id", "state"], method: "GET")))
        XCTAssertTrue(try client.isOpenIDRequest(client.request(["personas", "id", "state"], method: "PATCH")))
        let stateQuery = URLRequest(url: URL(string: "https://gateway.example.test/v1/personas/id/state?next=state")!)
        XCTAssertTrue(try client.isOpenIDRequest(stateQuery))
        let otherQuery = URLRequest(url: URL(string: "https://gateway.example.test/v1/cards?next=/personas/id/state")!)
        XCTAssertFalse(try client.isOpenIDRequest(otherQuery))
        XCTAssertThrowsError(try client.request(["personas", "", "state"], method: "PATCH"))
        XCTAssertFalse(try client.isOpenIDRequest(client.request(["personasx", "id", "state"], method: "GET")))
        let emptyID = URLRequest(url: URL(string: "https://gateway.example.test/v1/personas//state?x=1")!)
        XCTAssertFalse(try client.isOpenIDRequest(emptyID))
        XCTAssertFalse(try client.isOpenIDRequest(client.request(["personas", "id", "state", "child"], method: "GET")))
        XCTAssertFalse(try client.isOpenIDRequest(client.request(["personas", "id", "other"], method: "PATCH")))
        for method in ["POST", "PUT", "DELETE"] {
            XCTAssertFalse(try client.isOpenIDRequest(client.request(["personas", "id", "state"], method: method)))
        }
        let prefixed = try HTTPGatewayClient(baseURL: URL(string: "https://gateway.example.test/base")!)
        XCTAssertTrue(try prefixed.isOpenIDRequest(prefixed.request(["personas", "id/with?chars", "state"], method: "PATCH")))
    }

    func testUnkeyedStatePATCHDoesNotRetry500() async throws {
        let recorder = StateRequestRecorder()
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StateURLProtocol.self]
        StateURLProtocol.recorder = recorder
        defer { StateURLProtocol.recorder = nil }
        let client = try makeClient(session: URLSession(configuration: configuration), retrySleep: { _ in
            XCTFail("unkeyed state PATCH must not retry")
        })
        recorder.enqueue(status: 500, body: #"{"code":"internal.error"}"#)
        do {
            _ = try await client.updatePersonaState(id: "id", patch: .init(moodPublic: false))
            XCTFail("expected 500")
        } catch {
            XCTAssertEqual(error as? GatewayClientError, .problem(status: 500, code: "internal.error"))
        }
        XCTAssertEqual(recorder.requests.count, 1)
    }

    private func makeClient(session: URLSession? = nil, retrySleep: HTTPGatewayClient.RetrySleep? = nil) throws -> HTTPGatewayClient {
        let provider = MatrixOpenIDTokenProvider {
            MatrixOpenIDToken(accessToken: "test-credential", tokenType: "Bearer",
                              matrixServerName: "example.invalid", expiresInSeconds: 3600)
        }
        return try HTTPGatewayClient(baseURL: URL(string: "https://gateway.example.test")!, token: "site-credential",
                                     session: session, openIDProvider: provider, retrySleep: retrySleep)
    }
}

private final class StateRequestRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var values: [URLRequest] = []
    private var responses: [(Int, Data, [String: String])] = []
    var requests: [URLRequest] { lock.lock(); defer { lock.unlock() }; return values }
    func enqueue(status: Int, body: String) {
        lock.lock()
        responses.append((status, Data(body.utf8), ["Content-Type": "application/problem+json"]))
        lock.unlock()
    }
    func append(_ request: URLRequest) {
        var captured = request
        if captured.httpBody == nil, let stream = captured.httpBodyStream {
            stream.open()
            defer { stream.close() }
            var bytes = [UInt8](repeating: 0, count: 4096)
            var body = Data()
            while stream.hasBytesAvailable {
                let count = stream.read(&bytes, maxLength: bytes.count)
                guard count > 0 else { break }
                body.append(bytes, count: count)
            }
            captured.httpBody = body
        }
        lock.lock()
        values.append(captured)
        lock.unlock()
    }
    func dequeue() -> (Int, Data, [String: String]) {
        lock.lock()
        defer { lock.unlock() }
        return responses.isEmpty ? (500, Data(), ["Content-Type": "application/problem+json"]) : responses.removeFirst()
    }
}

private final class StateURLProtocol: URLProtocol {
    static var recorder: StateRequestRecorder?
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        guard let recorder = Self.recorder else { client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse)); return }
        recorder.append(request)
        let response = recorder.dequeue()
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: response.0,
            httpVersion: "HTTP/1.1", headerFields: response.2)!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: response.1)
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}
