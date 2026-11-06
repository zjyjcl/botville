// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

@testable import BotvilleCore
import XCTest

final class PairingProtocolTests: XCTestCase {
    private let validCode = String(repeating: "A", count: 43)
    private let future = Date(timeIntervalSince1970: 1_900_000_000)

    private func uri(_ query: String) -> String { "botville://pair?\(query)" }
    private var basics: String { "v=1&hs=https://matrix.example.invalid&code=\(validCode)&exp=2000000000" }

    func testParsesStrictValidURI() throws {
        let qr = try PairingQRCode.parse(uri("\(basics)&gw=https://gateway.example.invalid&fp=sha256:\(String(repeating: "A", count: 43))"), now: future)
        XCTAssertEqual(qr.homeserver.host, "matrix.example.invalid")
        XCTAssertEqual(qr.gateway?.host, "gateway.example.invalid")
        XCTAssertEqual(qr.fingerprint?.count, 32)
    }

    func testRejectsRepeatedIncludingUnknownAndMissingRequiredFields() {
        XCTAssertThrowsError(try PairingQRCode.parse(uri("\(basics)&x=1&x=2"), now: future))
        XCTAssertThrowsError(try PairingQRCode.parse(uri("v=1&hs=https://matrix.example.invalid&exp=2000000000"), now: future)) { XCTAssertEqual($0 as? PairingError, .missingParameter) }
        for missing in ["v", "hs", "code", "exp"] {
            let parts = basics.split(separator: "&").filter { !$0.hasPrefix("\(missing)=") }.joined(separator: "&")
            XCTAssertThrowsError(try PairingQRCode.parse(uri(parts), now: future), "missing \(missing)") { XCTAssertEqual($0 as? PairingError, .missingParameter) }
        }
        for malformed in ["\(basics)&x=", "\(basics)&V=1", "\(basics)&%76=1", "\(basics)&&gw=https://gateway.example.invalid", "\(basics)&gw", "\(basics)&gw=https://gateway.example.invalid#fragment", "\(basics)&gw=https://gateway.example.invalid space"] {
            XCTAssertThrowsError(try PairingQRCode.parse(uri(malformed), now: future), malformed)
        }
    }

    func testRawGrammarTrimsOnlyOuterWhitespaceAndPreservesValueCharacters() throws {
        let decorated = " \n\(uri(basics))&gw=https://gateway.example.invalid/path=part+next\t "
        let parsed = try PairingQRCode.parse(decorated, now: future)
        XCTAssertEqual(parsed.gateway?.absoluteString, "https://gateway.example.invalid/path=part+next")
        for invalid in [
            "botville://pairing?\(basics)",
            "botville://Pair?\(basics)",
            "botville://pair?\(basics)&",
            "botville://pair?\(basics)&gw=https://gateway.example.invalid\u{0001}"
        ] {
            XCTAssertThrowsError(try PairingQRCode.parse(invalid, now: future), invalid)
        }
    }

    func testRawPercentAndCaseVariantNamesAreAlwaysRejected() {
        for invalid in [
            "\(basics)&V=",
            "v=1&hs=https%://matrix.example.invalid&code=\(validCode)&exp=2000000000",
            "v=1&hs=https://matrix.example.invalid&code=\(validCode)%&exp=2000000000",
            "v=1&hs=https://matrix.example.invalid&code=\(validCode)&exp=2000000000%"
        ] {
            XCTAssertThrowsError(try PairingQRCode.parse(uri(invalid), now: future), invalid)
        }
    }

    func testRejectsInsecureBadBinaryExpiredAndNewerVersion() {
        XCTAssertThrowsError(try PairingQRCode.parse(uri("v=1&hs=http://matrix.example.invalid&code=\(validCode)&exp=2000000000"), now: future))
        XCTAssertThrowsError(try PairingQRCode.parse(uri("v=1&hs=HTTPS://matrix.example.invalid&code=\(validCode)&exp=2000000000"), now: future))
        XCTAssertThrowsError(try PairingQRCode.parse(uri("\(basics)&fp=sha256:abc"), now: future)) { XCTAssertEqual($0 as? PairingError, .invalidFingerprint) }
        XCTAssertThrowsError(try PairingQRCode.parse(uri("v=1&hs=https://matrix.example.invalid&code=abc&exp=2000000000"), now: future)) { XCTAssertEqual($0 as? PairingError, .invalidCode) }
        XCTAssertThrowsError(try PairingQRCode.parse(uri("\(basics.replacingOccurrences(of: "v=1", with: "v=2"))"), now: future)) { XCTAssertEqual($0 as? PairingError, .unsupportedVersion) }
        XCTAssertThrowsError(try PairingQRCode.parse(uri(basics), now: Date(timeIntervalSince1970: 2_000_000_001))) { XCTAssertEqual($0 as? PairingError, .expiredLocally) }
        XCTAssertThrowsError(try PairingQRCode.parse(uri("\(basics)&code=\(validCode)=&fp=sha256:\(String(repeating: "A", count: 43))="), now: future))
        XCTAssertThrowsError(try PairingQRCode.parse(uri("\(basics.replacingOccurrences(of: validCode, with: String(repeating: "_", count: 43)))"), now: future)) { XCTAssertEqual($0 as? PairingError, .invalidCode) }
    }

    func testRejectsStrictGatewayURLShapes() {
        for gateway in ["https://user:pass@gateway.example.invalid", "https://gateway.example.invalid:0", "https://gateway.example.invalid?x=1", "https://gateway.example.invalid#x", "https://%67ateway.example.invalid"] {
            XCTAssertThrowsError(try PairingQRCode.parse(uri("\(basics)&gw=\(gateway)"), now: future), gateway)
        }
    }

    func testRedeemErrorsAndResponseDecoding() throws {
        let qr = try PairingQRCode.parse(uri(basics), now: future)
        let errors: [(String, PairingError)] = [("BV_PAIR_INVALID", .invalid), ("BV_PAIR_EXPIRED", .expired), ("BV_PAIR_REVOKED", .revoked), ("BV_PAIR_USED", .used), ("BV_PAIR_RATE_LIMITED", .rateLimited(retryAfter: 1.5))]
        for (code, expected) in errors {
            let data = "{\"errcode\":\"\(code)\",\"retry_after_ms\":1500}".data(using: .utf8)!
            XCTAssertEqual(HTTPPairingService.serverError(status: 400, data: data), expected)
        }
        XCTAssertEqual(HTTPPairingService.serverError(status: 429, data: #"{"errcode":"OTHER","retry_after_ms":1}"#.data(using: .utf8)!), .serverFailure)
        XCTAssertEqual(HTTPPairingService.serverError(status: 429, data: #"{"errcode":"BV_PAIR_RATE_LIMITED","retry_after_ms":-1}"#.data(using: .utf8)!), .rateLimited(retryAfter: nil))
        let goodString = "{\"v\":\"1\",\"homeserver\":\"https://matrix.example.invalid\",\"gateway\":null,\"auth\":{\"type\":\"login_token\",\"token\":\"test\",\"expires_in_ms\":1000},\"features_url\":\"https://matrix.example.invalid/features\"}"
        let good = goodString.data(using: .utf8)!
        XCTAssertEqual(try HTTPPairingService.decodeResponse(good, pairing: qr, redeemHost: qr.homeserver.host, receivedAt: future).auth.type, .loginToken)
        let mismatch = "{\"v\":\"1\",\"homeserver\":\"https://other.example.invalid\",\"gateway\":null,\"auth\":{\"type\":\"login_token\",\"token\":\"test\",\"expires_in_ms\":1000},\"features_url\":\"https://matrix.example.invalid/features\"}".data(using: .utf8)!
        XCTAssertThrowsError(try HTTPPairingService.decodeResponse(mismatch, pairing: qr, redeemHost: qr.homeserver.host, receivedAt: future)) { XCTAssertEqual($0 as? PairingError, .homeserverMismatch) }
        let registration = goodString.replacingOccurrences(of: "login_token", with: "registration_token").data(using: .utf8)!
        XCTAssertEqual(try HTTPPairingService.decodeResponse(registration, pairing: qr, redeemHost: qr.homeserver.host, receivedAt: future).auth.type, .registrationToken)
        for invalid in [goodString.replacingOccurrences(of: "\"v\":\"1\"", with: "\"v\":1"), goodString.replacingOccurrences(of: "\"v\":\"1\"", with: "\"v\":\"2\""), goodString.replacingOccurrences(of: "login_token", with: "other"), goodString.replacingOccurrences(of: "\"expires_in_ms\":1000", with: "\"expires_in_ms\":0")].compactMap({ $0.data(using: .utf8) }) {
            XCTAssertThrowsError(try HTTPPairingService.decodeResponse(invalid, pairing: qr, redeemHost: qr.homeserver.host, receivedAt: future))
        }
    }

    func testRedeemRequestContainsOnlyWireFieldsAndLocalExpiryStopsBeforeNetwork() async throws {
        let qr = try PairingQRCode.parse(uri("\(basics)&gw=https://gateway.example.invalid"), now: future)
        let request = try HTTPPairingService.redeemRequest(pairing: qr, deviceDisplayName: String(repeating: "🙂", count: 129))
        XCTAssertEqual(request.url?.absoluteString, "https://matrix.example.invalid/_botville/pair/redeem")
        XCTAssertEqual(request.httpMethod, "POST")
        let body = try XCTUnwrap(try JSONSerialization.jsonObject(with: XCTUnwrap(request.httpBody)) as? [String: String])
        XCTAssertEqual(body["device_display_name"]?.count, 128)
        XCTAssertEqual(body["device_display_name"], String(repeating: "🙂", count: 128))
        XCTAssertEqual(body["code"], validCode)
        let emptyNameRequest = try HTTPPairingService.redeemRequest(pairing: qr, deviceDisplayName: "")
        let emptyNameBody = try XCTUnwrap(try JSONSerialization.jsonObject(with: XCTUnwrap(emptyNameRequest.httpBody)) as? [String: String])
        XCTAssertGreaterThanOrEqual(emptyNameBody["device_display_name"]?.count ?? 0, 1)
        let expired = try PairingQRCode.parse(uri("v=1&hs=https://matrix.example.invalid&code=\(validCode)&exp=1"), now: Date(timeIntervalSince1970: 0))
        await XCTAssertThrowsErrorAsync(try await HTTPPairingService().redeem(expired, deviceDisplayName: "unused")) { XCTAssertEqual($0 as? PairingError, .expiredLocally) }
    }

    func testContractURIFixtures() throws {
        for name in fixtureNames(prefix: "uri-valid") { XCTAssertNoThrow(try PairingQRCode.parse(try fixtureText(name), now: Date(timeIntervalSince1970: 1_700_000_000)), name) }
        for name in fixtureNames(prefix: "uri-invalid") { XCTAssertThrowsError(try PairingQRCode.parse(try fixtureText(name), now: Date(timeIntervalSince1970: 1_700_000_000)), name) }
    }

    func testContractRedeemFixturesAndExactHomeserver() throws {
        let pairing = try PairingQRCode.parse("botville://pair?v=1&hs=https://hs.example.org&gw=https://gw.example.org&code=QRPAIREXAMPLECODE0000000000000000000000000A&exp=1791000000", now: Date(timeIntervalSince1970: 1_700_000_000))
        let chatOnlyPairing = try PairingQRCode.parse(try fixtureText("uri-valid-no-gw.txt"), now: Date(timeIntervalSince1970: 1_700_000_000))
        for name in fixtureNames(prefix: "redeem-") {
            let data = try fixtureData(name)
            if name.hasPrefix("redeem-200-") {
                let isChatOnly = name == "redeem-200-no-gateway.json"
                let qr = isChatOnly ? chatOnlyPairing : pairing
                let response = try HTTPPairingService.decodeResponse(data, pairing: qr, redeemHost: qr.homeserver.host, receivedAt: future)
                XCTAssertEqual(response.homeserver.absoluteString, "https://hs.example.org", name)
                if isChatOnly {
                    // chat-only（契约 §5.1.1）：gateway 为 null，features_url 省略（即便出现也须忽略）
                    XCTAssertNil(response.gateway, name)
                    XCTAssertNil(response.featuresURL, name)
                } else {
                    XCTAssertEqual(response.gateway?.absoluteString, "https://gw.example.org", name)
                }
                XCTAssertEqual(response.auth.type, name.contains("login") ? .loginToken : .registrationToken, name)
            } else {
                let expected: PairingError
                if name.contains("invalid") { expected = .invalid }
                else if name.contains("used") { expected = .used }
                else if name.contains("expired") { expected = .expired }
                else if name.contains("revoked") { expected = .revoked }
                else { expected = .rateLimited(retryAfter: 60) }
                XCTAssertEqual(HTTPPairingService.serverError(status: name.contains("429") ? 429 : 400, data: data), expected, name)
            }
        }
        let trailing = try PairingQRCode.parse("botville://pair?v=1&hs=https://hs.example.org/&code=\(validCode)&exp=2000000000", now: future)
        let wrongTrailingSlash = #"{"v":"1","homeserver":"https://hs.example.org","gateway":null,"auth":{"type":"login_token","token":"test","expires_in_ms":1},"features_url":"https://hs.example.org/features"}"#.data(using: .utf8)!
        XCTAssertThrowsError(try HTTPPairingService.decodeResponse(wrongTrailingSlash, pairing: trailing, redeemHost: trailing.homeserver.host, receivedAt: future)) { XCTAssertEqual($0 as? PairingError, .homeserverMismatch) }
    }

    func testContractRedeemRequestFixture() throws {
        let expected = try XCTUnwrap(JSONSerialization.jsonObject(with: fixtureData("redeem-request.json")) as? [String: String])
        let pairing = try PairingQRCode.parse("botville://pair?v=1&hs=https://hs.example.org&code=\(expected["code"]!)&exp=1791000000", now: Date(timeIntervalSince1970: 1_700_000_000))
        let request = try HTTPPairingService.redeemRequest(pairing: pairing, deviceDisplayName: expected["device_display_name"]!)
        let actual = try XCTUnwrap(JSONSerialization.jsonObject(with: XCTUnwrap(request.httpBody)) as? [String: String])
        XCTAssertEqual(actual, expected)
    }

    func testGatewayOnDifferentHostIsAcceptedInResponse() throws {
        let pairing = try PairingQRCode.parse("botville://pair?v=1&hs=https://hs.example.org&gw=https://gw.example.org&fp=sha256:\(String(repeating: "A", count: 43))&code=\(validCode)&exp=2000000000", now: future)
        let response = #"{"v":"1","homeserver":"https://hs.example.org","gateway":"https://gw.example.org","auth":{"type":"login_token","token":"test","expires_in_ms":1},"features_url":"https://gw.example.org/features"}"#.data(using: .utf8)!
        XCTAssertNoThrow(try HTTPPairingService.decodeResponse(response, pairing: pairing, redeemHost: pairing.homeserver.host, receivedAt: future))
    }

    func testChatOnlyResponsesMayOmitFeatures() throws {
        let noGateway = try PairingQRCode.parse("botville://pair?v=1&hs=https://hs.example.org&code=\(validCode)&exp=2000000000", now: future)
        let noGatewayResponse = #"{"v":"1","homeserver":"https://hs.example.org","gateway":null,"auth":{"type":"login_token","token":"test","expires_in_ms":1}}"#.data(using: .utf8)!
        let decoded = try HTTPPairingService.decodeResponse(noGatewayResponse, pairing: noGateway, redeemHost: noGateway.homeserver.host, receivedAt: future)
        XCTAssertNil(decoded.gateway)
        XCTAssertNil(decoded.featuresURL)

        let unsolicitedGateway = #"{"v":"1","homeserver":"https://hs.example.org","gateway":"https://gw.example.org","auth":{"type":"login_token","token":"test","expires_in_ms":1},"features_url":"https://gw.example.org/features"}"#.data(using: .utf8)!
        let stillChatOnly = try HTTPPairingService.decodeResponse(unsolicitedGateway, pairing: noGateway, redeemHost: noGateway.homeserver.host, receivedAt: future)
        XCTAssertNil(stillChatOnly.gateway)
        XCTAssertNil(stillChatOnly.featuresURL)

        let nullFeatures = #"{"v":"1","homeserver":"https://hs.example.org","gateway":null,"auth":{"type":"login_token","token":"test","expires_in_ms":1},"features_url":null}"#.data(using: .utf8)!
        XCTAssertNoThrow(try HTTPPairingService.decodeResponse(nullFeatures, pairing: noGateway, redeemHost: noGateway.homeserver.host, receivedAt: future))

        let withGateway = try PairingQRCode.parse("botville://pair?v=1&hs=https://hs.example.org&gw=https://gw.example.org&code=\(validCode)&exp=2000000000", now: future)
        let responseGatewayNull = #"{"v":"1","homeserver":"https://hs.example.org","gateway":null,"auth":{"type":"login_token","token":"test","expires_in_ms":1}}"#.data(using: .utf8)!
        XCTAssertNoThrow(try HTTPPairingService.decodeResponse(responseGatewayNull, pairing: withGateway, redeemHost: withGateway.homeserver.host, receivedAt: future))
        let missingFeaturesWithGateway = #"{"v":"1","homeserver":"https://hs.example.org","gateway":"https://gw.example.org","auth":{"type":"login_token","token":"test","expires_in_ms":1}}"#.data(using: .utf8)!
        XCTAssertThrowsError(try HTTPPairingService.decodeResponse(missingFeaturesWithGateway, pairing: withGateway, redeemHost: withGateway.homeserver.host, receivedAt: future))
    }

    func testExpiresMustBePositiveAndDebugMockRedeemsParsedRawURI() async throws {
        let pairing = try PairingQRCode.parse(uri(basics), now: future)
        for value in [-1, 0] {
            let response = #"{"v":"1","homeserver":"https://matrix.example.invalid","gateway":null,"auth":{"type":"login_token","token":"test","expires_in_ms":\#(value)},"features_url":null}"#.data(using: .utf8)!
            XCTAssertThrowsError(try HTTPPairingService.decodeResponse(response, pairing: pairing, redeemHost: pairing.homeserver.host, receivedAt: future))
        }
        let oneMillisecond = #"{"v":"1","homeserver":"https://matrix.example.invalid","gateway":null,"auth":{"type":"login_token","token":"test","expires_in_ms":1},"features_url":null}"#.data(using: .utf8)!
        XCTAssertNoThrow(try HTTPPairingService.decodeResponse(oneMillisecond, pairing: pairing, redeemHost: pairing.homeserver.host, receivedAt: future))

        #if DEBUG
        let mockPairing = try PairingQRCode.parse(PairingMockData.exampleURI, now: future)
        let mockResponse = try await MockPairingService().redeem(mockPairing, deviceDisplayName: "Test device")
        XCTAssertEqual(mockResponse.homeserver.absoluteString, "https://matrix.example.invalid")
        XCTAssertEqual(mockResponse.gateway?.absoluteString, "https://gateway.example.invalid")
        #endif
    }

    private func fixtureNames(prefix: String) -> [String] {
        ["uri-valid.txt", "uri-valid-with-fp.txt", "uri-valid-no-gw.txt", "uri-invalid-code-length.txt", "uri-invalid-duplicate-param.txt", "uri-invalid-fp-format.txt", "uri-invalid-http-scheme.txt", "uri-invalid-missing-hs.txt", "uri-invalid-missing-required.txt", "uri-invalid-unknown-param.txt", "uri-invalid-unknown-v.txt", "redeem-200-login-token.json", "redeem-200-no-gateway.json", "redeem-200-registration-token.json", "redeem-400-bv-pair-invalid.json", "redeem-409-bv-pair-used.json", "redeem-410-bv-pair-expired.json", "redeem-410-bv-pair-revoked.json", "redeem-429-bv-pair-rate-limited.json"].filter { $0.hasPrefix(prefix) }
    }

    private func fixtureData(_ name: String) throws -> Data {
        let parts = name.split(separator: ".", maxSplits: 1).map(String.init)
        let url = Bundle.module.url(forResource: parts[0], withExtension: parts[1], subdirectory: "pairing-qr-contract") ?? Bundle.module.url(forResource: parts[0], withExtension: parts[1])
        return try Data(contentsOf: XCTUnwrap(url, "missing fixture \(name)"))
    }

    private func fixtureText(_ name: String) throws -> String { String(decoding: try fixtureData(name), as: UTF8.self).trimmingCharacters(in: .newlines) }
}

private func XCTAssertThrowsErrorAsync<T>(_ expression: @autoclosure () async throws -> T, _ handler: (Error) -> Void) async {
    do { _ = try await expression(); XCTFail("expected error") } catch { handler(error) }
}
