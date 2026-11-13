// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation
import XCTest

@testable import BotvilleCore

final class GroupRoomTransportTests: XCTestCase {
    override func tearDown() {
        GroupRoomURLProtocol.reset(status: 200, body: Data())
        super.tearDown()
    }

    func testExactOpenIDRoutesAndLookalikeRejections() throws {
        let client = try makeClient()
        let valid: [(String, String)] = [
            ("GET", "personas/p/rooms"), ("get", "personas/p/rooms"),
            ("PATCH", "personas/p/rooms/ref"), ("PATCH", "personas/p"),
            ("PATCH", "personas/p%2Fq/rooms/ref%2Fpart"),
            ("GET", "personas/p"),  // Existing owner-detail route stays authorized.
        ]
        let invalid: [(String, String)] = [
            ("GET", "personas/p/rooms/ref"), ("GET", "personas/p/rooms/"),
            ("GET", "personas//rooms"), ("GET", "personas/p/roomsx"),
            ("GET", "personasx/p/rooms"), ("POST", "personas/p/rooms"),
            ("DELETE", "personas/p/rooms"), ("PATCH", "personas/p/rooms"),
            ("PATCH", "personas/p/rooms/"), ("PATCH", "personas//rooms/ref"),
            ("PATCH", "personas/p/rooms/ref/extra"), ("PATCH", "personas/p/roomsx/ref"),
            ("GET", "personas/p/rooms/ref"), ("POST", "personas/p/rooms/ref"),
            ("DELETE", "personas/p/rooms/ref"), ("PATCH", "personas"),
            ("PATCH", "personas/"), ("PATCH", "personas/p/extra"),
            ("POST", "personas/p"), ("DELETE", "personas/p"),
        ]
        for (method, path) in valid + invalid {
            var request = URLRequest(
                url: try XCTUnwrap(URL(string: "https://gateway.test/prefix/v1/" + path)))
            request.httpMethod = method
            XCTAssertEqual(
                try client.isOpenIDRequest(request), valid.contains { $0 == method && $1 == path },
                "\(method) \(path)")
        }
    }

    func testListCursorAndIncludeArePassedWithoutInterpretation() async throws {
        GroupRoomURLProtocol.reset(
            status: 200,
            body: Data(
                "{\"items\":[],\"next_cursor\":null,\"has_more\":false,\"groups_enabled\":true}"
                    .utf8))
        let client = try makeClient()
        let cursor = "opaque/+?=%# 游标"
        _ = try await client.groupRooms(
            personaID: "@ai:local.test:8448", limit: 17, cursor: cursor, includeAll: true)
        let request = try XCTUnwrap(GroupRoomURLProtocol.requests.first)
        let components = try XCTUnwrap(
            URLComponents(url: try XCTUnwrap(request.url), resolvingAgainstBaseURL: false))
        XCTAssertEqual(
            components.percentEncodedPath, "/prefix/v1/personas/%40ai%3Alocal.test%3A8448/rooms")
        XCTAssertEqual(components.queryItems?.first { $0.name == "cursor" }?.value, cursor)
        XCTAssertEqual(components.queryItems?.first { $0.name == "include" }?.value, "all")
        XCTAssertEqual(components.queryItems?.first { $0.name == "limit" }?.value, "17")
        XCTAssertEqual(
            request.value(forHTTPHeaderField: "Authorization"), "MatrixOpenID <fixture-only>")
        XCTAssertNil(request.value(forHTTPHeaderField: "Idempotency-Key"))
    }

    func testRoomRefIsOneEncodedPathComponentAndPatchBodiesAreExact() async throws {
        let ref = "opaque/ ?#%:引用"
        let client = try makeClient()
        for mode in [GroupRoomMode.muted, .mentionOnly] {
            GroupRoomURLProtocol.reset(status: 200, body: roomBody)
            _ = try await client.updateGroupRoomMode(personaID: "p/q", roomRef: ref, mode: mode)
            let request = try XCTUnwrap(GroupRoomURLProtocol.requests.first)
            let components = try XCTUnwrap(
                URLComponents(url: try XCTUnwrap(request.url), resolvingAgainstBaseURL: false))
            XCTAssertEqual(
                components.percentEncodedPath,
                "/prefix/v1/personas/p%2Fq/rooms/opaque%2F%20%3F%23%25%3A%E5%BC%95%E7%94%A8")
            XCTAssertEqual(try jsonBody(request) as? [String: String], ["mode": mode.rawValue])
            XCTAssertEqual(request.httpMethod, "PATCH")
            XCTAssertNil(request.value(forHTTPHeaderField: "Idempotency-Key"))
        }
        GroupRoomURLProtocol.reset(status: 200, body: roomBody)
        _ = try await client.leaveGroupRoom(personaID: "p/q", roomRef: ref)
        XCTAssertEqual(
            try jsonBody(XCTUnwrap(GroupRoomURLProtocol.requests.first)) as? [String: String],
            ["action": "leave"])
    }

    func testGroupsPatchOnlySendsWholeGroupsObject() async throws {
        GroupRoomURLProtocol.reset(status: 200, body: ownerBody)
        _ = try await makeClient().updatePersonaGroups(
            id: "@ai:local.test", groups: .init(enabled: true, invitePolicy: .localHumans))
        let request = try XCTUnwrap(GroupRoomURLProtocol.requests.first)
        let body = try XCTUnwrap(try jsonBody(request) as? [String: Any])
        XCTAssertEqual(Set(body.keys), ["groups"])
        let groups = try XCTUnwrap(body["groups"] as? [String: Any])
        XCTAssertEqual(Set(groups.keys), ["enabled", "invite_policy"])
        XCTAssertEqual(groups["enabled"] as? Bool, true)
        XCTAssertEqual(groups["invite_policy"] as? String, "local_humans")
        XCTAssertNil(request.value(forHTTPHeaderField: "If-Match"))
        XCTAssertNil(request.value(forHTTPHeaderField: "Idempotency-Key"))
    }

    func testAllThreePatchesSendOnlyOnceOn500() async throws {
        let client = try makeClient()
        for operation in 0..<3 {
            GroupRoomURLProtocol.reset(
                status: 500, body: Data("{\"code\":\"internal.error\"}".utf8))
            do {
                switch operation {
                case 0: _ = try await client.leaveGroupRoom(personaID: "p", roomRef: "ref")
                case 1:
                    _ = try await client.updateGroupRoomMode(
                        personaID: "p", roomRef: "ref", mode: .muted)
                default: _ = try await client.updatePersonaGroups(id: "p", groups: .init())
                }
                XCTFail("Expected 500")
            } catch {
                XCTAssertEqual(
                    error as? GatewayClientError, .problem(status: 500, code: "internal.error"))
            }
            XCTAssertEqual(GroupRoomURLProtocol.requests.count, 1)
            XCTAssertNil(
                GroupRoomURLProtocol.requests.first?.value(forHTTPHeaderField: "Idempotency-Key"))
        }
    }

    func testListRetainsExistingGETRetryBudget() async throws {
        GroupRoomURLProtocol.reset(status: 500, body: Data("{\"code\":\"internal.error\"}".utf8))
        do {
            _ = try await makeClient().groupRooms(personaID: "p")
            XCTFail("Expected 500")
        } catch {
            XCTAssertEqual(
                error as? GatewayClientError, .problem(status: 500, code: "internal.error"))
        }
        XCTAssertEqual(GroupRoomURLProtocol.requests.count, 4)
    }

    func testPermissionAndEncryptedErrorsDoNotRetryOrRefreshCredentials() async throws {
        for code in ["persona.not_owned", "conversation.e2ee", ""] {
            GroupRoomURLProtocol.reset(
                status: 403, body: code.isEmpty ? Data() : Data("{\"code\":\"\(code)\"}".utf8))
            do {
                _ = try await makeClient().leaveGroupRoom(personaID: "p", roomRef: "ref")
                XCTFail("Expected 403")
            } catch {
                let gatewayError = try XCTUnwrap(error as? GatewayClientError)
                XCTAssertEqual(gatewayError.httpStatus, 403)
                XCTAssertEqual(
                    gatewayError.errorDescription,
                    code == "conversation.e2ee" ? "加密群不支持 AI 发言" : "没有权限查看")
            }
            XCTAssertEqual(GroupRoomURLProtocol.requests.count, 1)
        }
    }

    @MainActor
    func testRoomRefreshUsesProblemCodeRegardlessOfTitle() async throws {
        let cases: [(Int, String?, String, Bool)] = [
            (404, "room.not_found", "Room request rejected", true),
            (404, "room.not_found", "Arbitrary title", true),
            (409, "room.state_conflict", "Room request rejected", true),
            (409, "room.state_conflict", "Something else entirely", true),
            (404, "persona.not_found", "Room not found", false),
            (409, "request.invalid", "Room state conflict", false),
            (409, nil, "Room request rejected", false),
        ]
        for (status, code, title, refreshes) in cases {
            var problem: [String: Any] = ["title": title, "status": status]
            if let code { problem["code"] = code }
            let disabledOwner = Data(String(decoding: ownerBody, as: UTF8.self)
                .replacingOccurrences(of: "\"enabled\":true", with: "\"enabled\":false").utf8)
            GroupRoomURLProtocol.reset(replies: [
                (200, ownerBody), (200, activeRoomListBody),
                (status, try JSONSerialization.data(withJSONObject: problem)),
                (200, disabledOwner), (200, activeRoomListBody),
            ])
            let client = try makeClient()
            let settings = PersonaGroupsSettingsController(personaID: "@ai:local.test",
                initialGroups: .init(enabled: true, invitePolicy: .owner), client: client)
            let model = PersonaGroupRoomsModel(personaID: "@ai:local.test", client: client, settings: settings)
            await model.load()
            await model.setMode(roomRef: String(repeating: "a", count: 64), mode: .muted)
            XCTAssertEqual(GroupRoomURLProtocol.requests.count, refreshes ? 5 : 3)
            XCTAssertEqual(model.errorMessage == "群状态已变化，已刷新", refreshes)
            XCTAssertEqual(model.groupsEnabled, !refreshes)
            XCTAssertEqual(model.rooms.count, 1)
        }
    }

    func test503WithoutCodeAlwaysUsesGenericCopy() async throws {
        for body in [Data(), Data("{\"status\":503,\"title\":\"Room unavailable\"}".utf8),
                     Data("{\"status\":503,\"code\":\"\",\"title\":\"room.unavailable\"}".utf8)] {
            GroupRoomURLProtocol.reset(status: 503, body: body)
            let client = try makeClient()
            do {
                _ = try await client.groupRooms(personaID: "@ai:local.test")
                XCTFail("Expected generic 503")
            } catch let error as GatewayClientError {
                XCTAssertEqual(error, .httpStatus(503))
                XCTAssertEqual(error.errorDescription, "网关请求失败，请稍后重试。")
            }
            XCTAssertEqual(GroupRoomURLProtocol.requests.count, 1)
        }
        XCTAssertEqual(GatewayClientError.httpStatus(503).errorDescription, "网关请求失败，请稍后重试。")
        XCTAssertEqual(GatewayClientError.problem(status: 503, code: "").errorDescription,
                       "网关请求失败，请稍后重试。")
    }

    private var activeRoomListBody: Data {
        let item = String(decoding: roomBody, as: UTF8.self)
            .replacingOccurrences(of: "\"state\":\"left\"", with: "\"state\":\"accepted\"")
        return Data("{\"items\":[\(item)],\"next_cursor\":null,\"has_more\":false,\"groups_enabled\":true}".utf8)
    }

    private func makeClient() throws -> HTTPGatewayClient {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [GroupRoomURLProtocol.self]
        return try HTTPGatewayClient(
            baseURL: "https://gateway.test/prefix", token: "<site-fixture>",
            session: URLSession(configuration: configuration),
            openIDProvider: MatrixOpenIDTokenProvider {
                MatrixOpenIDToken(
                    accessToken: "<fixture-only>", tokenType: "Bearer",
                    matrixServerName: "local.test",
                    expiresInSeconds: 3600)
            }, retrySleep: { _ in })
    }

    private func jsonBody(_ request: URLRequest) throws -> Any {
        if let body = request.httpBody { return try JSONSerialization.jsonObject(with: body) }
        let stream = try XCTUnwrap(request.httpBodyStream)
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 1024)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            if count <= 0 { break }
            data.append(buffer, count: count)
        }
        return try JSONSerialization.jsonObject(with: data)
    }

    private var roomBody: Data {
        Data(
            "{\"room_ref\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\",\"state\":\"left\",\"mode\":\"muted\",\"joined_at\":null,\"updated_at\":\"2026-11-13T00:00:00Z\",\"leave_pending\":true}"
                .utf8)
    }
    private var ownerBody: Data {
        Data(
            "{\"id\":\"@ai:local.test\",\"display_name\":\"AI\",\"owner_id\":\"@me:local.test\",\"kind\":\"custom\",\"status\":\"active\",\"revision\":1,\"visibility\":\"private\",\"created_at\":\"2026-11-13T00:00:00Z\",\"updated_at\":\"2026-11-13T00:00:00Z\",\"groups\":{\"enabled\":true,\"invite_policy\":\"local_humans\"}}"
                .utf8)
    }
}

private final class GroupRoomURLProtocol: URLProtocol, @unchecked Sendable {
    private static let lock = NSLock()
    private static var status = 200
    private static var body = Data()
    private static var recorded: [URLRequest] = []
    private static var replies: [(Int, Data)] = []
    static var requests: [URLRequest] { lock.withLock { recorded } }
    static func reset(status: Int, body: Data) {
        lock.withLock {
            self.status = status
            self.body = body
            recorded = []
            replies = []
        }
    }
    static func reset(replies: [(Int, Data)]) {
        lock.withLock {
            self.replies = replies
            recorded = []
            status = 500
            body = Data()
        }
    }
    override class func canInit(with request: URLRequest) -> Bool {
        request.url?.host == "gateway.test"
    }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let (status, body) = Self.lock.withLock {
            Self.recorded.append(request)
            if !Self.replies.isEmpty { return Self.replies.removeFirst() }
            return (Self.status, Self.body)
        }
        let response = HTTPURLResponse(
            url: request.url!, statusCode: status, httpVersion: nil,
            headerFields: [
                "Content-Type": status >= 400 ? "application/problem+json" : "application/json"
            ])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}
