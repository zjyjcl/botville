// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

@testable import BotvilleCore
import Foundation
import XCTest

/// Opt-in only. The caller must supply a disposable loopback gateway + fake HS,
/// with cache TTL 0, server name localhost and /_botville/gateway prefix.
/// The fake HS accepts the fixture placeholder, rejects <invalid-placeholder-N>,
/// and returns 502 for <unavailable-placeholder>. No real account is used.
final class OpenIDGatewayIntegrationTests: XCTestCase {
    private func gatewayURL() throws -> URL {
        guard let raw = ProcessInfo.processInfo.environment["BOTVILLE_OPENID_LIVE_GATEWAY_URL"] else {
            throw XCTSkip("Disposable OpenID gateway not configured")
        }
        let url = try XCTUnwrap(URL(string: raw))
        guard url.scheme == "http", url.host == "127.0.0.1",
              let port = url.port, (18000..<19000).contains(port),
              url.user == nil, url.password == nil, url.query == nil, url.fragment == nil,
              url.path.isEmpty || url.path == "/" else {
            throw GatewayClientError.invalidBaseURL
        }
        return url
    }

    func testValidIdentityWithAndWithoutPrefixReachesNotFound() async throws {
        let root = try gatewayURL()
        for base in [root, root.appendingPathComponent("_botville/gateway")] {
            let source = IntegrationOpenIDSource(mode: .valid)
            let client = try HTTPGatewayClient(baseURL: base, allowLocalHTTP: true,
                openIDProvider: MatrixOpenIDTokenProvider { try await source.issue() })
            do { _ = try await client.momentsRequest(); XCTFail("CRUD is not implemented yet") }
            catch { XCTAssertEqual(error as? GatewayClientError, .problem(status: 404, code: "not_found")) }
            let count = await source.count
            XCTAssertEqual(count, 1)
        }
    }

    func testInvalidIdentityRefreshesExactlyOnce() async throws {
        let root = try gatewayURL()
        let source = IntegrationOpenIDSource(mode: .invalid)
        let client = try HTTPGatewayClient(baseURL: root, allowLocalHTTP: true,
            openIDProvider: MatrixOpenIDTokenProvider { try await source.issue() })
        do { _ = try await client.momentsRequest(); XCTFail("Expected authentication failure") }
        catch { XCTAssertEqual(error as? GatewayClientError, .serverAuthMisconfigured) }
        let count = await source.count
        XCTAssertEqual(count, 2)
    }

    func testUnavailableHomeserverRetriesThreeTimesWithoutRefresh() async throws {
        let root = try gatewayURL()
        let source = IntegrationOpenIDSource(mode: .unavailable)
        let waits = IntegrationWaits()
        let client = try HTTPGatewayClient(baseURL: root, allowLocalHTTP: true,
            openIDProvider: MatrixOpenIDTokenProvider { try await source.issue() },
            retrySleep: { await waits.record($0) })
        do { _ = try await client.momentsRequest(); XCTFail("Expected unavailable server") }
        catch { XCTAssertEqual(error as? GatewayClientError, .problem(status: 500, code: "internal.error")) }
        let count = await source.count
        let delays = await waits.values
        XCTAssertEqual(count, 1)
        XCTAssertEqual(delays, [1, 2, 4])
    }
}

private actor IntegrationOpenIDSource {
    enum Mode { case valid, invalid, unavailable }
    let mode: Mode
    var count = 0
    init(mode: Mode) { self.mode = mode }
    func issue() throws -> MatrixOpenIDToken {
        count += 1
        let fixture = try OpenIDFixtures.response()
        let value: String
        switch mode {
        case .valid: value = fixture.access_token
        case .invalid: value = "<invalid-placeholder-\(count)>"
        case .unavailable: value = "<unavailable-placeholder>"
        }
        return MatrixOpenIDToken(accessToken: value, tokenType: fixture.token_type,
            matrixServerName: fixture.matrix_server_name, expiresInSeconds: fixture.expires_in)
    }
}

private actor IntegrationWaits {
    var values: [TimeInterval] = []
    func record(_ delay: TimeInterval) { values.append(delay) }
}
