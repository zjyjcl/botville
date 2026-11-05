// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

@testable import BotvilleCore
import Foundation
import XCTest

final class PairingTrustPolicyTests: XCTestCase {
    private let pin = Data(repeating: 7, count: 32)

    func testPinFollowsHomeserverOriginAcrossPaths() throws {
        let homeserver = try XCTUnwrap(URL(string: "https://HS.example.org:8443/base"))
        for address in ["https://hs.example.org:8443/base/_botville/pair/redeem",
                        "https://hs.example.org:8443/_matrix/client/v3/login",
                        "https://hs.example.org:8443/gateway"] {
            XCTAssertEqual(PairingURLSession.fingerprint(pin, homeserver: homeserver,
                                                       requestURL: try XCTUnwrap(URL(string: address))), pin)
        }
    }

    func testSameHostDifferentPortDoesNotReusePin() throws {
        let homeserver = try XCTUnwrap(URL(string: "https://hs.example.org:8443"))
        XCTAssertNil(PairingURLSession.fingerprint(pin, homeserver: homeserver,
                                                   requestURL: try XCTUnwrap(URL(string: "https://hs.example.org:443/gateway"))))
    }

    func testImplicitAndExplicitHTTPSDefaultPortAreTheSameOrigin() throws {
        let implicit = try XCTUnwrap(URL(string: "https://hs.example.org"))
        let explicit = try XCTUnwrap(URL(string: "https://hs.example.org:443/gateway"))
        XCTAssertEqual(PairingURLSession.fingerprint(pin, homeserver: implicit, requestURL: explicit), pin)
    }

    func testDelegateNormalizesDefaultHTTPSPortsBeforePinning() {
        for configuredPort in [nil, 0, 443] {
            let delegate = PairingTLSDelegate(fingerprint: pin, host: "hs.example.org", port: configuredPort)
            for challengePort in [0, 443] {
                // No server trust is attached, so cancellation proves this
                // challenge reached the pin path rather than system trust.
                XCTAssertEqual(challengeResult(delegate: delegate, host: "hs.example.org", port: challengePort), .cancelAuthenticationChallenge)
            }
        }
        let nonDefault = PairingTLSDelegate(fingerprint: pin, host: "hs.example.org", port: 8448)
        XCTAssertEqual(challengeResult(delegate: nonDefault, host: "hs.example.org", port: 443), .performDefaultHandling)
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

    func testRestoredSameOriginGatewayKeepsPin() throws {
        let credentials = credentials(homeserver: "https://hs.example.org:8443", gateway: "https://HS.example.org:8443/gateway")
        let restored = try JSONDecoder().decode(SessionCredentials.self, from: JSONEncoder().encode(credentials))
        XCTAssertEqual(restored.effectiveGatewayCertificateFingerprint, pin)
    }

    func testRestoredSameHostDifferentPortGatewayDoesNotKeepPin() throws {
        let credentials = credentials(homeserver: "https://hs.example.org", gateway: "https://HS.example.org:8443/gateway")
        let restored = try JSONDecoder().decode(SessionCredentials.self, from: JSONEncoder().encode(credentials))
        XCTAssertNil(restored.effectiveGatewayCertificateFingerprint)
    }

    func testMissingGatewayHasNoPin() {
        XCTAssertNil(credentials(gateway: nil).effectiveGatewayCertificateFingerprint)
    }

    private func credentials(homeserver: String = "https://hs.example.org", gateway: String?) -> SessionCredentials {
        SessionCredentials(accessToken: "fake-access", refreshToken: nil, userID: "@fake:hs.example.org",
                           deviceID: "FAKE", homeserverURL: homeserver, oauthData: nil,
                           storeID: UUID(), storeKey: Data(repeating: 1, count: 32), gatewayURL: gateway,
                           gatewayCertificateFingerprint: pin)
    }

    private func challengeResult(delegate: PairingTLSDelegate, host: String, port: Int) -> URLSession.AuthChallengeDisposition {
        let protectionSpace = URLProtectionSpace(host: host, port: port, protocol: "https", realm: nil, authenticationMethod: NSURLAuthenticationMethodServerTrust)
        let challenge = URLAuthenticationChallenge(protectionSpace: protectionSpace, proposedCredential: nil, previousFailureCount: 0, failureResponse: nil, error: nil, sender: TrustPolicyChallengeSender())
        var result: URLSession.AuthChallengeDisposition?
        delegate.urlSession(URLSession.shared, task: URLSession.shared.dataTask(with: URL(string: "https://\(host)/")!), didReceive: challenge) { disposition, _ in result = disposition }
        return result!
    }
}

private final class TrustPolicyChallengeSender: NSObject, URLAuthenticationChallengeSender {
    func use(_ credential: URLCredential, for challenge: URLAuthenticationChallenge) {}
    func continueWithoutCredential(for challenge: URLAuthenticationChallenge) {}
    func cancel(_ challenge: URLAuthenticationChallenge) {}
    func performDefaultHandling(for challenge: URLAuthenticationChallenge) {}
    func rejectProtectionSpaceAndContinue(with challenge: URLAuthenticationChallenge) {}
}
