// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

@testable import BotvilleCore
import Foundation
import XCTest

// Problem fixtures copied byte-for-byte from PR #181 head e86a6a9.
// #181 合入后改用仓库 gateway/openapi/examples/ 并参考
// MockContractConsistencyTests 做逐字节一致性检查。request-token is an
// App-owned Matrix C-S example; all credential/identity values are placeholders.
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
