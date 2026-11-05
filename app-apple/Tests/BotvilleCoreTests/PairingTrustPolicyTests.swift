// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

@testable import BotvilleCore
import Foundation
import XCTest

final class PairingTrustPolicyTests: XCTestCase {
    private let pin = Data(repeating: 7, count: 32)

    func testPinFollowsHomeserverHostIncludingOtherPathsAndPorts() throws {
        let homeserver = try XCTUnwrap(URL(string: "https://HS.example.org/base"))
        for address in ["https://hs.example.org/base/_botville/pair/redeem",
                        "https://hs.example.org/_matrix/client/v3/login",
                        "https://hs.example.org:8443/gateway"] {
            XCTAssertEqual(PairingURLSession.fingerprint(pin, homeserver: homeserver,
                                                       requestURL: try XCTUnwrap(URL(string: address))), pin)
        }
    }

    func testOtherGatewayHostUsesSystemTLS() throws {
        let homeserver = try XCTUnwrap(URL(string: "https://hs.example.org"))
        for address in ["https://gw.example.org", "https://hs.example.org.other.test", "http://hs.example.org"] {
            XCTAssertNil(PairingURLSession.fingerprint(pin, homeserver: homeserver,
                                                     requestURL: try XCTUnwrap(URL(string: address))))
        }
    }

    func testRestoredCrossHostGatewayDoesNotReuseOldIncorrectPin() throws {
        let credentials = credentials(gateway: "https://gw.example.org")
        let restored = try JSONDecoder().decode(SessionCredentials.self, from: JSONEncoder().encode(credentials))
        XCTAssertNil(restored.effectiveGatewayCertificateFingerprint)
    }

    func testRestoredSameHostGatewayKeepsPin() throws {
        let credentials = credentials(gateway: "https://HS.example.org:8443/gateway")
        let restored = try JSONDecoder().decode(SessionCredentials.self, from: JSONEncoder().encode(credentials))
        XCTAssertEqual(restored.effectiveGatewayCertificateFingerprint, pin)
    }

    func testMissingGatewayHasNoPin() {
        XCTAssertNil(credentials(gateway: nil).effectiveGatewayCertificateFingerprint)
    }

    private func credentials(gateway: String?) -> SessionCredentials {
        SessionCredentials(accessToken: "fake-access", refreshToken: nil, userID: "@fake:hs.example.org",
                           deviceID: "FAKE", homeserverURL: "https://hs.example.org", oauthData: nil,
                           storeID: UUID(), storeKey: Data(repeating: 1, count: 32), gatewayURL: gateway,
                           gatewayCertificateFingerprint: pin)
    }
}
