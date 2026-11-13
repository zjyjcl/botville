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
        XCTAssertFalse(page.items[0].canPin)
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
            XCTAssertEqual(memory.canPin, pinned)
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
            "你无权管理该 AI 的记忆。")
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
            personaID: "persona_demo", layers: [], scopeKind: nil, counterpart: nil, roomID: nil,
            pinned: nil, cursor: nil, limit: 1)
        XCTAssertEqual(first.items.count, 1)
        XCTAssertTrue(first.hasMore)
        let group = try await client.memories(
            personaID: "persona_demo", layers: ["summary"], scopeKind: "group", counterpart: nil,
            roomID: "!example:invalid", pinned: nil, cursor: nil, limit: nil)
        let summary = try XCTUnwrap(group.items.first)
        XCTAssertTrue(summary.redacted)
        XCTAssertNil(summary.text)
        XCTAssertFalse(summary.canPin)
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

    func testPersonasFollowFlatContractPagination() async throws {
        let first = Data(
            "{\"items\":[{\"id\":\"one\",\"display_name\":\"第一个 AI\"}],\"has_more\":true,\"next_cursor\":\"next\"}"
                .utf8)
        let last = Data(
            "{\"items\":[{\"id\":\"two\",\"display_name\":\"第二个 AI\"}],\"has_more\":false,\"next_cursor\":null}"
                .utf8)
        let (client, recorder, source) = try self.client(bodies: [first, last])
        let values = try await client.personas()
        XCTAssertEqual(values.map(\.id), ["one", "two"])
        XCTAssertEqual(recorder.requests.map { $0.url?.path }, ["/v1/personas", "/v1/personas"])
        let query = URLComponents(
            url: try XCTUnwrap(recorder.requests.last?.url), resolvingAgainstBaseURL: false)?
            .queryItems
        XCTAssertEqual(query, [URLQueryItem(name: "cursor", value: "next")])
        let issued = await source.count
        XCTAssertEqual(issued, 0)
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
        lock.unlock()
        loader.complete(status, body: body)
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
