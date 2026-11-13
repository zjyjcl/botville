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
    static var requests: [URLRequest] { lock.withLock { recorded } }
    static func reset(status: Int, body: Data) {
        lock.withLock {
            self.status = status
            self.body = body
            recorded = []
        }
    }
    override class func canInit(with request: URLRequest) -> Bool {
        request.url?.host == "gateway.test"
    }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let (status, body) = Self.lock.withLock {
            Self.recorded.append(request)
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
