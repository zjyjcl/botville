// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import XCTest

@testable import BotvilleCore

final class MemoryClientTests: XCTestCase {
    override func tearDown() {
        MemoryURLProtocol.recorder = nil
        super.tearDown()
    }

    private func client(
        statuses: [Int] = [200], bodies: [Data] = [], sleep: HTTPGatewayClient.RetrySleep? = nil
    ) throws -> (HTTPGatewayClient, MemoryRecorder, MemoryTokenSource) {
        let recorder = MemoryRecorder(statuses: statuses, bodies: bodies)
        MemoryURLProtocol.recorder = recorder
        let source = MemoryTokenSource()
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [MemoryURLProtocol.self]
        let provider = MatrixOpenIDTokenProvider { await source.issue() }
        return (
            try HTTPGatewayClient(
                baseURL: "https://gateway.example.invalid", token: "site",
                session: URLSession(configuration: config), openIDProvider: provider,
                retrySleep: sleep ?? { _ in }), recorder, source
        )
    }

    private func memoryData() -> Data {
        Data(
            """
            {"id":"mf_0123456789abcdef","persona_id":"p","layer":"profile","scope":{"kind":"dm","counterpart":"@u:example.invalid","room_id":null},"visibility":"dm_only","trust":"owner","source":{"kind":"owner"},"pinned":false,"text":null,"key":"likes","value":"tea","dirty":false,"redacted":false,"created_at":"2026-11-13T00:00:00Z","updated_at":"2026-11-13T00:00:00Z","expires_at":null}
            """.utf8)
    }
    private func memoryPageData() -> Data {
        Data(
            "{\"items\":[\(String(data: memoryData(), encoding: .utf8)!)],\"page\":{\"next_cursor\":null,\"has_more\":false}}"
                .utf8)
    }
    func testMemoryModelsDecodeRedactedAndUnknownLayer() throws {
        let data = Data(
            """
            {"items":[{"id":"mr_0123456789abcdef","persona_id":"p","layer":"future_layer","scope":{"kind":"group","counterpart":null,"room_id":"!r:example.invalid"},"visibility":"scope_only","trust":"derived","source":{"kind":"summary"},"pinned":false,"text":null,"value":null,"redacted":true,"future_field":{"x":1},"created_at":"2026-11-13T00:00:00Z","updated_at":"2026-11-13T00:00:00Z","expires_at":null}],"page":{"next_cursor":"next","has_more":true}}
            """.utf8)
        let page = try GatewayCoding.decoder.decode(MemoryPage.self, from: data)
        XCTAssertTrue(page.hasMore)
        XCTAssertEqual(page.nextCursor, "next")
        XCTAssertNil(page.items[0].text)
        XCTAssertNil(page.items[0].value)
        XCTAssertTrue(page.items[0].redacted)
        XCTAssertFalse(page.items[0].canMutate)
        XCTAssertFalse(page.items[0].actions.canTogglePin)
    }

    func testDecodedRedactedMemoryOffersDeleteAndExistingPinRemovalOnly() throws {
        var object = try XCTUnwrap(JSONSerialization.jsonObject(with: memoryData()) as? [String: Any])
        object["redacted"] = true
        object["text"] = NSNull()
        for pinned in [false, true] {
            object["pinned"] = pinned
            let memory = try GatewayCoding.decoder.decode(
                Memory.self, from: JSONSerialization.data(withJSONObject: object))
            XCTAssertTrue(memory.actions.canDelete)
            XCTAssertFalse(memory.actions.canPin)
            XCTAssertEqual(memory.actions.canUnpin, pinned)
            XCTAssertEqual(memory.actions.canTogglePin, pinned)
        }
    }

    func testMemoryRequestsUseExactPathsAndRepeatedQuery() throws {
        let client = try HTTPGatewayClient(
            baseURL: "https://gateway.example.invalid", token: "site-token")
        let list = try client.request(
            ["memories"],
            query: [
                URLQueryItem(name: "persona_id", value: "p"),
                URLQueryItem(name: "layer", value: "recent"),
                URLQueryItem(name: "layer", value: "profile"),
            ])
        XCTAssertEqual(list.url?.path, "/v1/memories")
        let layers = URLComponents(url: try XCTUnwrap(list.url), resolvingAgainstBaseURL: false)?
            .queryItems?
            .filter { $0.name == "layer" }.map { $0.value }
        XCTAssertEqual(layers, ["recent", "profile"])
        XCTAssertNil(list.value(forHTTPHeaderField: "Authorization"))
        XCTAssertTrue(try client.isOpenIDRequest(list))
        XCTAssertFalse(
            try client.isOpenIDRequest(
                try client.request(
                    ["memoriesx"], query: [URLQueryItem(name: "path", value: "memories")])))
        XCTAssertEqual(
            try client.request(["memories", "mr_0123456789abcdef"], method: "DELETE").httpMethod,
            "DELETE"
        )
        XCTAssertEqual(
            try client.request(["memories", "mr_0123456789abcdef", "pin"], method: "PUT")
                .httpMethod,
            "PUT")
        let fact = try client.request(
            ["memories", "facts"], method: "PUT", body: Data("{}".utf8),
            idempotencyKey: "test-key-123")
        XCTAssertEqual(fact.value(forHTTPHeaderField: "Idempotency-Key"), "test-key-123")
    }

    func testMemoryProblemCodesHaveOwnerFacingDescriptions() {
        XCTAssertEqual(
            GatewayClientError.problem(status: 404, code: "memory.not_found").errorDescription,
            "这条记忆已不存在。")
        XCTAssertEqual(
            GatewayClientError.problem(status: 403, code: "persona.not_owned").errorDescription,
            "没有权限查看")
        XCTAssertEqual(
            GatewayClientError.problem(status: 429, code: "rate_limited.transport")
                .errorDescription,
            "操作过于频繁，请稍后再试。")
    }

    func testEveryMemoryEndpointUsesOpenIDAndNeverSiteBearer() async throws {
        let (client, recorder, _) = try self.client(
            statuses: [200, 200, 204, 200, 200, 201],
            bodies: [
                memoryPageData(), memoryData(), Data(), memoryData(), memoryData(), memoryData(),
            ])
        _ = try await client.memories(
            personaID: "p", layers: ["recent", "profile"], scopeKind: "dm",
            counterpart: "@u:example.invalid", roomID: "!r:example.invalid", pinned: false,
            cursor: "cursor", limit: 7)
        _ = try await client.memory(id: "mf_0123456789abcdef")
        try await client.deleteMemory(id: "mf_0123456789abcdef")
        _ = try await client.pinMemory(id: "mf_0123456789abcdef")
        _ = try await client.unpinMemory(id: "mf_0123456789abcdef")
        _ = try await client.writeMemoryFact(
            .init(personaID: "p", counterpart: "@u:example.invalid", key: "likes", value: "tea"),
            idempotencyKey: "request-key")
        let requests = recorder.requests
        XCTAssertEqual(
            requests.map { $0.httpMethod }, ["GET", "GET", "DELETE", "PUT", "DELETE", "PUT"])
        XCTAssertEqual(
            requests.map { $0.url?.path },
            [
                "/v1/memories", "/v1/memories/mf_0123456789abcdef",
                "/v1/memories/mf_0123456789abcdef",
                "/v1/memories/mf_0123456789abcdef/pin", "/v1/memories/mf_0123456789abcdef/pin",
                "/v1/memories/facts",
            ])
        XCTAssertTrue(
            requests.allSatisfy {
                $0.value(forHTTPHeaderField: "Authorization")?.hasPrefix("MatrixOpenID ") == true
            })
        XCTAssertTrue(
            requests.allSatisfy {
                !($0.value(forHTTPHeaderField: "Authorization")?.contains("Bearer") ?? false)
            })
        XCTAssertEqual(requests.last?.value(forHTTPHeaderField: "Idempotency-Key"), "request-key")
        let firstQuery = URLComponents(
            url: try XCTUnwrap(requests.first?.url), resolvingAgainstBaseURL: false)?.queryItems
        XCTAssertEqual(
            firstQuery?.filter { $0.name == "layer" }.map { $0.value }, ["recent", "profile"])
        XCTAssertEqual(firstQuery?.first(where: { $0.name == "scope_kind" })?.value, "dm")
        XCTAssertEqual(firstQuery?.first(where: { $0.name == "limit" })?.value, "7")
        XCTAssertEqual(firstQuery?.first(where: { $0.name == "persona_id" })?.value, "p")
        XCTAssertEqual(
            firstQuery?.first(where: { $0.name == "counterpart" })?.value, "@u:example.invalid")
        XCTAssertEqual(
            firstQuery?.first(where: { $0.name == "room_id" })?.value, "!r:example.invalid")
        XCTAssertEqual(firstQuery?.first(where: { $0.name == "pinned" })?.value, "false")
        XCTAssertEqual(firstQuery?.first(where: { $0.name == "cursor" })?.value, "cursor")
        XCTAssertTrue(
            requests.dropLast().allSatisfy {
                $0.value(forHTTPHeaderField: "Idempotency-Key") == nil
            })
        let fact = try JSONDecoder().decode(
            MemoryFactWrite.self, from: XCTUnwrap(requests.last?.httpBody))
        XCTAssertEqual(
            fact,
            MemoryFactWrite(
                personaID: "p", counterpart: "@u:example.invalid", key: "likes", value: "tea"))
    }

    func testFactsWithoutKeyAndUnkeyedPinDoNotRetry500() async throws {
        for operation in [0, 1, 2, 3] {
            let waits = MemoryWaits()
            let (client, recorder, _) = try self.client(
                statuses: [500], bodies: [MemoryRecorder.internalError],
                sleep: { await waits.record($0) })
            do {
                if operation == 0 {
                    _ = try await client.writeMemoryFact(
                        .init(
                            personaID: "p", counterpart: "@u:example.invalid", key: "likes",
                            value: "tea"),
                        idempotencyKey: nil)
                } else if operation == 1 {
                    _ = try await client.pinMemory(id: "mf_0123456789abcdef")
                } else if operation == 2 {
                    _ = try await client.unpinMemory(id: "mf_0123456789abcdef")
                } else {
                    try await client.deleteMemory(id: "mf_0123456789abcdef")
                }
                XCTFail("unkeyed mutation must not replay")
            } catch {
                XCTAssertEqual(
                    error as? GatewayClientError, .problem(status: 500, code: "internal.error"))
            }
            XCTAssertEqual(recorder.requests.count, 1)
            let values = await waits.values
            XCTAssertEqual(values, [])
        }
    }

    func testMemoryGETAndKeyedFactRetry500ButUnavailableDoesNot() async throws {
        let waits = MemoryWaits()
        let (client, recorder, _) = try self.client(
            statuses: [500, 500, 500, 200],
            bodies: [
                MemoryRecorder.internalError, MemoryRecorder.internalError,
                MemoryRecorder.internalError,
                memoryData(),
            ], sleep: { await waits.record($0) })
        _ = try await client.memory(id: "mf_0123456789abcdef")
        XCTAssertEqual(recorder.requests.count, 4)
        let values = await waits.values
        XCTAssertEqual(values, [1, 2, 4])
        let unavailable = Data("{\"code\":\"memory.unavailable\"}".utf8)
        let (second, secondRecorder, _) = try self.client(statuses: [503], bodies: [unavailable])
        do {
            _ = try await second.memory(id: "mf_0123456789abcdef")
            XCTFail("503 must not replay")
        } catch {
            XCTAssertEqual(
                error as? GatewayClientError, .problem(status: 503, code: "memory.unavailable"))
        }
        XCTAssertEqual(secondRecorder.requests.count, 1)
        let keyedWaits = MemoryWaits()
        let (keyed, keyedRecorder, _) = try self.client(
            statuses: [500, 200], bodies: [MemoryRecorder.internalError, memoryData()],
            sleep: { await keyedWaits.record($0) })
        _ = try await keyed.writeMemoryFact(
            .init(personaID: "p", counterpart: "@u:example.invalid", key: "likes", value: "tea"),
            idempotencyKey: "test-key-123")
        XCTAssertEqual(keyedRecorder.requests.count, 2)
        let keyedValues = await keyedWaits.values
        XCTAssertEqual(keyedValues, [1])
    }

    func testMemoryInvalidTokenRefreshesOnce() async throws {
        let invalid = Data("{\"code\":\"auth.invalid_token\"}".utf8)
        let (client, recorder, source) = try self.client(
            statuses: [401, 200], bodies: [invalid, memoryData()])
        _ = try await client.memory(id: "mf_0123456789abcdef")
        XCTAssertEqual(recorder.requests.count, 2)
        let count = await source.count
        XCTAssertEqual(count, 2)
        let (broken, brokenRecorder, brokenSource) = try self.client(
            statuses: [401, 401], bodies: [invalid, invalid])
        do {
            _ = try await broken.memory(id: "mf_0123456789abcdef")
            XCTFail("only one refresh")
        } catch { XCTAssertEqual(error as? GatewayClientError, .serverAuthMisconfigured) }
        XCTAssertEqual(brokenRecorder.requests.count, 2)
        let secondCount = await brokenSource.count
        XCTAssertEqual(secondCount, 2)
    }

    func testMissingOpenIDProviderNeverFallsBackToBearer() async throws {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [MemoryURLProtocol.self]
        let recorder = MemoryRecorder(statuses: [200], bodies: [memoryData()])
        MemoryURLProtocol.recorder = recorder
        let client = try HTTPGatewayClient(
            baseURL: "https://gateway.example.invalid", token: "site",
            session: URLSession(configuration: config))
        do {
            _ = try await client.memory(id: "mf_0123456789abcdef")
            XCTFail("memory needs OpenID")
        } catch { XCTAssertEqual(error as? MatrixOpenIDError, .providerUnavailable) }
        XCTAssertTrue(recorder.requests.isEmpty)
    }

    func testMockMemorySupportsPreviewPaginationFiltersAndSummaryGuard() async throws {
        let client = MockGatewayClient()
        let first = try await client.memories(
            personaID: "@ai_demo:example.invalid", layers: [], scopeKind: nil, counterpart: nil, roomID: nil,
            pinned: nil, cursor: nil, limit: 1)
        XCTAssertEqual(first.items.count, 1)
        XCTAssertTrue(first.hasMore)
        let group = try await client.memories(
            personaID: "@ai_demo:example.invalid", layers: ["summary"], scopeKind: "group", counterpart: nil,
            roomID: "!example:invalid", pinned: nil, cursor: nil, limit: nil)
        let summary = try XCTUnwrap(group.items.first)
        XCTAssertTrue(summary.redacted)
        XCTAssertNil(summary.text)
        XCTAssertFalse(summary.actions.canTogglePin)
        do {
            _ = try await client.pinMemory(id: summary.id)
            XCTFail("summary cannot pin")
        } catch {
            XCTAssertEqual(
                error as? GatewayClientError, .problem(status: 422, code: "memory.not_pinnable"))
        }
    }
    func testMemoryRouteSelectionHandlesPrefixesEncodedSegmentsAndQueries() throws {
        for base in [
            "https://gateway.example.invalid", "https://gateway.example.invalid/prefix/v1",
            "https://gateway.example.invalid/%E4%B8%AD",
        ] {
            let client = try HTTPGatewayClient(baseURL: base)
            for segments in [
                ["memories"], ["memories", "mr_0123456789abcdef"], ["memories", "facts"],
                ["moments"],
            ] {
                XCTAssertTrue(try client.isOpenIDRequest(try client.request(segments)))
            }
            for segments in [["memoriesx"], ["memories/other"], ["other", "memories"], ["cards"]] {
                XCTAssertFalse(
                    try client.isOpenIDRequest(
                        try client.request(
                            segments, query: [URLQueryItem(name: "route", value: "/v1/memories")])))
            }
            XCTAssertThrowsError(
                try client.isOpenIDRequest(
                    URLRequest(url: URL(string: "https://other.invalid/v1/memories")!)))
        }
    }

    // Pinned #218 schema/example: 391a248ee004fda5b57761bc607c662453af7f40.
    private func personaPageData(id: String = "@ai_demo:example.invalid", next: String? = nil,
                                 empty: Bool = false) throws -> Data {
        let resources = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Sources/BotvilleCore/Resources/mock-persona-page.json")
        var page = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: resources)) as? [String: Any])
        var items = try XCTUnwrap(page["items"] as? [[String: Any]])
        items[0]["id"] = id
        page["items"] = empty ? [] : items
        page["next_cursor"] = next.map { $0 as Any } ?? NSNull()
        page["has_more"] = next != nil
        return try JSONSerialization.data(withJSONObject: page)
    }

    func testPersonasFollowFlatContractPaginationWithOpenIDOnly() async throws {
        let first = try personaPageData(id: "@ai_one:example.invalid", next: "next")
        let last = try personaPageData(id: "@ai_two:example.invalid")
        let (client, recorder, source) = try self.client(bodies: [first, last])
        let values = try await client.personas()
        XCTAssertEqual(values.map(\.id), ["@ai_one:example.invalid", "@ai_two:example.invalid"])
        XCTAssertEqual(recorder.requests.map { $0.url?.path }, ["/v1/personas", "/v1/personas"])
        let query = URLComponents(
            url: try XCTUnwrap(recorder.requests.last?.url), resolvingAgainstBaseURL: false)?.queryItems
        XCTAssertEqual(query, [URLQueryItem(name: "cursor", value: "next")])
        XCTAssertTrue(recorder.requests.allSatisfy {
            $0.value(forHTTPHeaderField: "Authorization")?.hasPrefix("MatrixOpenID ") == true
                && !($0.value(forHTTPHeaderField: "Authorization")?.contains("Bearer") ?? true)
        })
        let issued = await source.count
        XCTAssertEqual(issued, 1)
    }

    func testPersonasRouteSelectionIsExactAndGETOnly() throws {
        for base in ["https://gateway.example.invalid", "https://gateway.example.invalid/prefix/v1/"] {
            let client = try HTTPGatewayClient(baseURL: base, token: "synthetic-site")
            let list = try client.request(["personas"], query: [URLQueryItem(name: "cursor", value: "next")])
            XCTAssertTrue(try client.isOpenIDRequest(list))
            XCTAssertNil(list.value(forHTTPHeaderField: "Authorization"))
            for segments in [["personasx"], ["personas", "id", "other"], ["personas/id"], ["personas?x"], ["other", "personas"], ["cards"]] {
                let request = try client.request(segments, query: [URLQueryItem(name: "path", value: "/v1/personas")])
                XCTAssertFalse(try client.isOpenIDRequest(request))
                XCTAssertTrue(request.value(forHTTPHeaderField: "Authorization")?.hasPrefix("Bearer ") == true)
            }
            for method in ["POST", "PUT", "DELETE", "HEAD"] {
                let request = try client.request(["personas"], method: method)
                XCTAssertFalse(try client.isOpenIDRequest(request))
                XCTAssertTrue(request.value(forHTTPHeaderField: "Authorization")?.hasPrefix("Bearer ") == true)
            }
            XCTAssertThrowsError(try client.isOpenIDRequest(URLRequest(url: URL(string: "https://other.invalid/v1/personas")!)))
        }
    }

    func testPersonasRefreshOnceAndReuseGETServerRetry() async throws {
        let invalid = Data("{\"code\":\"auth.invalid_token\"}".utf8)
        let waits = MemoryWaits()
        let (client, recorder, source) = try self.client(statuses: [401, 500, 500, 500, 200],
            bodies: [invalid, MemoryRecorder.internalError, MemoryRecorder.internalError,
                     MemoryRecorder.internalError, try personaPageData()], sleep: { await waits.record($0) })
        _ = try await client.personas()
        XCTAssertEqual(recorder.requests.count, 5)
        let issued = await source.count
        let delays = await waits.values
        XCTAssertEqual(issued, 2)
        XCTAssertEqual(delays, [1, 2, 4])
        XCTAssertTrue(recorder.requests.allSatisfy { $0.httpMethod == "GET" && $0.url == recorder.requests.first?.url })
        XCTAssertTrue(recorder.requests.allSatisfy { $0.value(forHTTPHeaderField: "Authorization")?.hasPrefix("MatrixOpenID ") == true })
        let (broken, failed, _) = try self.client(statuses: [401, 401], bodies: [invalid, invalid])
        do { _ = try await broken.personas(); XCTFail("only one auth refresh") }
        catch { XCTAssertEqual(error as? GatewayClientError, .serverAuthMisconfigured) }
        XCTAssertEqual(failed.requests.count, 2)
    }

    func testPersonasMissingProviderDoesNotFallBackToSiteBearer() async throws {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [MemoryURLProtocol.self]
        let recorder = MemoryRecorder(statuses: [200], bodies: [try personaPageData()])
        MemoryURLProtocol.recorder = recorder
        let client = try HTTPGatewayClient(baseURL: "https://gateway.example.invalid", token: "synthetic-site",
            session: URLSession(configuration: config))
        do { _ = try await client.personas(); XCTFail("identity required") }
        catch { XCTAssertEqual(error as? MatrixOpenIDError, .providerUnavailable) }
        XCTAssertTrue(recorder.requests.isEmpty)
    }

    @MainActor
    func testPersonaListDistinguishesForbiddenFromEmptyAndClearsStaleRows() async throws {
        let denied = Data("{\"code\":\"persona.not_owned\"}".utf8)
        let (client, recorder, _) = try self.client(statuses: [200, 403, 200],
            bodies: [try personaPageData(), denied, try personaPageData(empty: true)])
        let model = PersonaListModel(client: client)
        await model.load()
        XCTAssertEqual(model.personas.count, 1)
        XCTAssertFalse(model.isEmpty)
        await model.load()
        XCTAssertEqual(model.errorMessage, "没有权限查看")
        XCTAssertTrue(model.personas.isEmpty)
        XCTAssertFalse(model.isEmpty, "permission denial must remain an error, not a successful empty list")
        XCTAssertEqual(recorder.requests.count, 2, "403 must not refresh/retry")
        await model.load()
        XCTAssertNil(model.errorMessage)
        XCTAssertTrue(model.isEmpty)
        XCTAssertFalse(model.isLoading)
    }

    func testMockPersonaMatchesTenFieldSummaryAndMemoryIDs() async throws {
        let client = MockGatewayClient()
        let personas = try await client.personas()
        let persona = try XCTUnwrap(personas.first)
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        let encoded = try encoder.encode(persona)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: encoded) as? [String: Any])
        XCTAssertEqual(Set(object.keys), ["id", "owner_id", "display_name", "bio", "kind", "status", "revision", "created_at", "updated_at", "visibility"])
        XCTAssertEqual(persona.kind, "custom")
        XCTAssertEqual(persona.status, "active")
        XCTAssertEqual(persona.revision, 1)
        XCTAssertEqual(persona.visibility, "private")
        let page = try await client.memories(personaID: persona.id)
        XCTAssertFalse(page.items.isEmpty)
        XCTAssertTrue(page.items.allSatisfy { $0.personaID == persona.id })
    }

    private func personaObject(bio: Any = NSNull()) throws -> [String: Any] {
        let page = try XCTUnwrap(JSONSerialization.jsonObject(with: personaPageData()) as? [String: Any])
        var item = try XCTUnwrap((page["items"] as? [[String: Any]])?.first)
        item["bio"] = bio
        return item
    }

    func testPersonaBioNullBlankAndValueMapToSignature() throws {
        for (bio, expected): (Any, String?) in [(NSNull(), nil), (" \n\t ", nil), ("  你好\n世界  ", "你好\n世界")] {
            let data = try JSONSerialization.data(withJSONObject: personaObject(bio: bio))
            let summary = try GatewayCoding.decoder.decode(MemoryPersona.self, from: data)
            XCTAssertEqual(summary.aiProfile(localServerName: "example.invalid").signature, expected)
            let detail = try GatewayCoding.decoder.decode(PersonaDetail.self, from: data)
            guard case .owner(let owner) = detail else { return XCTFail("expected owner summary") }
            XCTAssertEqual(owner, summary)
        }
    }

    func testPublicDetailDecodesAbsentNullKnownAndUnknownMoods() throws {
        for (mood, expected): (Any?, PersonaMood?) in [
            (nil, nil), (NSNull(), nil), ("future-mood", nil),
            ("calm", .calm), ("happy", .happy), ("sad", .sad),
            ("excited", .excited), ("tired", .tired),
            ("neutral", nil), ("anxious", nil),
        ] {
            var object: [String: Any] = ["id": "@ai_demo:example.invalid", "display_name": "公开名字",
                                       "bio": NSNull(), "status": "active"]
            object["mood"] = mood
            let detail = try GatewayCoding.decoder.decode(PersonaDetail.self,
                from: JSONSerialization.data(withJSONObject: object))
            guard case .publicView(let view) = detail else { return XCTFail("expected public view") }
            XCTAssertEqual(view.mood, expected)
            XCTAssertNil(view.bio)
            XCTAssertEqual(view.displayName, "公开名字")
        }
    }

    func testMalformedOwnerDoesNotFallBackToPublicView() throws {
        var object = try personaObject()
        object.removeValue(forKey: "revision")
        XCTAssertThrowsError(try GatewayCoding.decoder.decode(PersonaDetail.self,
            from: JSONSerialization.data(withJSONObject: object)))
        object["owner_id"] = NSNull()
        XCTAssertThrowsError(try GatewayCoding.decoder.decode(PersonaDetail.self,
            from: JSONSerialization.data(withJSONObject: object)))
    }

    func testDetailGETMapsBothViewsToProfileAndKeepsAvatar() async throws {
        let publicView: [String: Any] = ["id": "@ai_demo:example.invalid", "display_name": "公开名字",
            "bio": "  公开签名  ", "status": "active", "mood": "future"]
        for object in [try personaObject(bio: "公开签名"), publicView] {
            let (client, recorder, source) = try self.client(bodies: [JSONSerialization.data(withJSONObject: object)])
            let initial = AIProfile(userID: "@ai_demo:example.invalid", displayName: "Matrix 名字",
                avatarData: Data([1, 2]), signature: nil, localServerName: "example.invalid")
            let result = try await initial.fetchingDetails(using: client)
            XCTAssertEqual(result.signature, "公开签名")
            XCTAssertEqual(result.displayName, object["display_name"] as? String)
            XCTAssertEqual(result.avatarData, initial.avatarData)
            XCTAssertEqual(recorder.requests.count, 1)
            let request = try XCTUnwrap(recorder.requests.first)
            XCTAssertEqual(request.httpMethod, "GET")
            XCTAssertEqual(URLComponents(url: try XCTUnwrap(request.url), resolvingAgainstBaseURL: false)?.percentEncodedPath,
                           "/v1/personas/%40ai_demo%3Aexample.invalid")
            XCTAssertTrue(request.value(forHTTPHeaderField: "Authorization")?.hasPrefix("MatrixOpenID ") == true)
            let count = await source.count
            XCTAssertEqual(count, 1)
        }
    }

    func testProfileMoodLabelUsesOnlyDisclosedKnownMood() async throws {
        for mood in ["calm", "happy", "sad", "excited", "tired", "future", "neutral", "anxious", nil] {
            let object: [String: Any] = ["id": "@ai_demo:example.invalid", "display_name": "Demo",
                "bio": NSNull(), "status": "active", "mood": mood.map { $0 as Any } ?? NSNull()]
            let (client, _, _) = try self.client(bodies: [JSONSerialization.data(withJSONObject: object)])
            let initial = AIProfile(userID: "@ai_demo:example.invalid", displayName: "Demo",
                avatarData: nil, signature: nil, localServerName: "example.invalid")
            let displayed = try await initial.fetchingDetails(using: client)
            XCTAssertEqual(displayed.mood, mood.flatMap(PersonaMood.init(rawValue:)))
        }
    }

    func testDetail404DisplaysMissingAndCrossServerNeverRequests() async throws {
        let (client, recorder, source) = try self.client(statuses: [404],
            bodies: [Data(#"{"code":"persona.not_found"}"#.utf8)])
        let local = AIProfile(userID: "@ai_demo:example.invalid", displayName: "Demo",
            avatarData: nil, signature: nil, localServerName: "example.invalid")
        do { _ = try await local.fetchingDetails(using: client); XCTFail("missing") }
        catch { XCTAssertEqual((error as? GatewayClientError)?.errorDescription, "不存在或不可见") }
        for server in ["elsewhere.invalid", "sub.example.invalid", "example.invalid:8448", "", nil] {
            let crossServer = AIProfile(userID: local.userID, displayName: "Demo",
                avatarData: Data([1]), signature: "stale", localServerName: server, mood: .happy)
            let absent = try await crossServer.fetchingDetails(using: client)
            XCTAssertNil(absent.signature)
            XCTAssertNil(absent.mood)
            XCTAssertEqual(absent.displayName, crossServer.displayName)
            XCTAssertEqual(absent.avatarData, crossServer.avatarData)
        }
        XCTAssertEqual(recorder.requests.count, 1, "cross-server and unknown identities never issue details")
        let count = await source.count
        XCTAssertEqual(count, 1)
    }

    func testPersonaDetailRoutingAcceptsOnlyExactGETResource() throws {
        for base in ["https://gateway.example.invalid", "https://gateway.example.invalid/prefix/v1/",
                     "https://gateway.example.invalid/%E4%B8%AD"] {
            let client = try HTTPGatewayClient(baseURL: base, token: "synthetic-site")
            for id in ["@ai_demo:example.invalid", "中文 /?%#", ".", ".."] {
                let request = try client.request(["personas", id], query: [URLQueryItem(name: "next", value: "/other/path")])
                XCTAssertTrue(try client.isOpenIDRequest(request))
                XCTAssertNil(request.value(forHTTPHeaderField: "Authorization"))
                let path = try XCTUnwrap(URLComponents(url: try XCTUnwrap(request.url), resolvingAgainstBaseURL: false)?.percentEncodedPath)
                XCTAssertEqual(String(try XCTUnwrap(path.split(separator: "/").last)).removingPercentEncoding, id)
            }
            XCTAssertThrowsError(try client.request(["personas", ""]))
            let root = try XCTUnwrap(client.request(["personas"]).url?.absoluteString)
            for suffix in ["/", "//id", "/id/", "/id/other", "x/id", "%2Fid"] {
                let request = URLRequest(url: try XCTUnwrap(URL(string: root + suffix + "?path=/personas/id")))
                XCTAssertFalse(try client.isOpenIDRequest(request), suffix)
            }
            for method in ["POST", "PATCH", "PUT", "DELETE", "HEAD"] {
                let request = try client.request(["personas", "id"], method: method)
                XCTAssertFalse(try client.isOpenIDRequest(request))
                XCTAssertTrue(request.value(forHTTPHeaderField: "Authorization")?.hasPrefix("Bearer ") == true)
            }
        }
    }

    @MainActor
    func testPersonaList401400And403WithoutProblemAreErrorsNotEmpty() async throws {
        for (status, body, message) in [
            (401, Data(#"{"code":"auth.unauthorized"}"#.utf8), "会话已失效，请重新登录。"),
            (400, Data(#"{"code":"request.invalid"}"#.utf8), "网关请求失败，请稍后重试。"),
            (403, Data(), "没有权限查看"),
        ] {
            let (client, recorder, _) = try self.client(statuses: [status, 200],
                bodies: [body, try personaPageData(empty: true)])
            let model = PersonaListModel(client: client)
            XCTAssertFalse(model.isEmpty)
            XCTAssertFalse(model.hasLoaded)
            XCTAssertTrue(model.showsLoading)
            await model.load()
            XCTAssertEqual(model.errorMessage, message)
            XCTAssertFalse(model.isEmpty)
            XCTAssertFalse(model.hasLoaded)
            XCTAssertFalse(model.showsLoading)
            XCTAssertEqual(recorder.requests.count, 1)
            await model.load()
            XCTAssertNil(model.errorMessage)
            XCTAssertTrue(model.hasLoaded)
            XCTAssertTrue(model.isEmpty)
        }
    }

    @MainActor
    func testPersonaListShowsLoadingUntilFirstSuccessAndDuringReload() async throws {
        let (client, recorder, _) = try self.client(bodies: [personaPageData(empty: true), personaPageData(empty: true)])
        let model = PersonaListModel(client: client)
        for round in 1...2 {
            let started = expectation(description: "request started")
            recorder.holdNextResponse(started: { started.fulfill() })
            let task = Task { await model.load() }
            await fulfillment(of: [started], timeout: 3)
            XCTAssertTrue(model.isLoading)
            XCTAssertTrue(model.showsLoading)
            XCTAssertFalse(model.isEmpty)
            await model.load() // A second load must not race the in-flight request.
            XCTAssertEqual(recorder.requests.count, round)
            recorder.releaseResponse()
            await task.value
            XCTAssertTrue(model.hasLoaded)
            XCTAssertFalse(model.showsLoading)
            XCTAssertTrue(model.isEmpty)
        }
    }

    func testContractMemoryExamplesDecode() throws {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let examples = root.appendingPathComponent("gateway/openapi/examples")
        _ = try GatewayCoding.decoder.decode(
            Memory.self, from: Data(contentsOf: examples.appendingPathComponent("memory.json")))
        let page = try GatewayCoding.decoder.decode(
            MemoryPage.self,
            from: Data(contentsOf: examples.appendingPathComponent("memory-page.json")))
        XCTAssertFalse(page.hasMore)
        let fact = try JSONDecoder().decode(
            MemoryFactWrite.self,
            from: Data(contentsOf: examples.appendingPathComponent("memory-fact-write.json")))
        XCTAssertEqual(fact.visibility, "dm_only")
    }

}

private actor MemoryTokenSource {
    var count = 0
    func issue() -> MatrixOpenIDToken {
        count += 1
        return MatrixOpenIDToken(
            accessToken: "memory-token-\(count)", tokenType: "Bearer",
            matrixServerName: "matrix.example.invalid", expiresInSeconds: 3600)
    }
}
private actor MemoryWaits {
    var values: [TimeInterval] = []
    func record(_ value: TimeInterval) { values.append(value) }
}
private final class MemoryRecorder: @unchecked Sendable {
    static let internalError = Data("{\"code\":\"internal.error\"}".utf8)
    private let lock = NSLock()
    private var captured: [URLRequest] = []
    private var statuses: [Int]
    private var bodies: [Data]
    init(statuses: [Int], bodies: [Data]) {
        self.statuses = statuses
        self.bodies = bodies
    }
    var requests: [URLRequest] {
        lock.lock()
        defer { lock.unlock() }
        return captured
    }
    private var started: (() -> Void)?
    private var held: (MemoryURLProtocol, Int, Data)?
    func holdNextResponse(started: @escaping () -> Void) {
        lock.lock(); defer { lock.unlock() }
        self.started = started
    }
    func releaseResponse() {
        lock.lock()
        let response = held
        held = nil
        lock.unlock()
        if let (loader, status, body) = response { loader.complete(status, body: body) }
    }
    func receive(_ loader: MemoryURLProtocol) {
        lock.lock()
        var request = loader.request
        if request.httpBody == nil, let stream = request.httpBodyStream {
            stream.open()
            defer { stream.close() }
            var body = Data()
            var buffer = [UInt8](repeating: 0, count: 1024)
            while stream.hasBytesAvailable {
                let count = stream.read(&buffer, maxLength: buffer.count)
                if count <= 0 { break }
                body.append(buffer, count: count)
            }
            request.httpBody = body
        }
        captured.append(request)
        let status = statuses.isEmpty ? 200 : statuses.removeFirst()
        let body = bodies.isEmpty ? Data("{}".utf8) : bodies.removeFirst()
        if let started {
            self.started = nil
            held = (loader, status, body)
            lock.unlock()
            started()
        } else {
            lock.unlock()
            loader.complete(status, body: body)
        }
    }
}
private final class MemoryURLProtocol: URLProtocol {
    static var recorder: MemoryRecorder?
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() { Self.recorder?.receive(self) }
    override func stopLoading() {}
    func complete(_ status: Int, body: Data) {
        client?.urlProtocol(
            self,
            didReceive: HTTPURLResponse(
                url: request.url!, statusCode: status, httpVersion: "HTTP/1.1",
                headerFields: [
                    "Content-Type": status >= 400 ? "application/problem+json" : "application/json"
                ])!,
            cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }
}
