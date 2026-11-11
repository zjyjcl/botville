// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

@testable import BotvilleCore
import Foundation
import XCTest

// Problems are read from the checked-out gateway contract, never bundled copies.
// The Matrix C-S request-token placeholder remains an App-owned test resource.
enum OpenIDFixtures {
    struct TokenResponse: Decodable {
        let access_token: String
        let token_type: String
        let matrix_server_name: String
        let expires_in: TimeInterval
    }

    static func response() throws -> TokenResponse {
        try JSONDecoder().decode(TokenResponse.self, from: data("request-token"))
    }

    static func data(_ name: String) throws -> Data {
        if name.hasPrefix("problem-") {
            let root = URL(fileURLWithPath: #filePath)
                .deletingLastPathComponent().deletingLastPathComponent()
                .deletingLastPathComponent().deletingLastPathComponent()
            // Missing/unreadable contract files must throw and fail the test.
            return try Data(contentsOf: root.appendingPathComponent("gateway/openapi/examples/\(name).json"))
        }
        let url = try XCTUnwrap(Bundle.module.url(forResource: name, withExtension: "json", subdirectory: "OpenID")
                                ?? Bundle.module.url(forResource: name, withExtension: "json"))
        return try Data(contentsOf: url)
    }
}

final class OpenIDFixtureTests: XCTestCase {
    func testRequestTokenExampleDrivesProvider() async throws {
        let fixture = try OpenIDFixtures.response()
        let provider = MatrixOpenIDTokenProvider {
            MatrixOpenIDToken(accessToken: fixture.access_token, tokenType: fixture.token_type,
                matrixServerName: fixture.matrix_server_name, expiresInSeconds: fixture.expires_in)
        }
        let token = try await provider.token()
        XCTAssertTrue(token.isValid)
        XCTAssertEqual(token.expiresInSeconds, 3600)
    }
}
