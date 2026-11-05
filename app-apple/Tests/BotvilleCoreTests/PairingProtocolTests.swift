// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

@testable import BotvilleCore
import XCTest

final class PairingProtocolTests: XCTestCase {
    private let validCode = String(repeating: "A", count: 43)
    private let validFingerprint = String(repeating: "A", count: 43)
    private let future = Date(timeIntervalSince1970: 1_900_000_000)

    private func uri(_ query: String) -> String { "botville://pair?\(query)" }
    private var basics: String { "v=1&hs=https%3A%2F%2Fmatrix.example.invalid&code=\(validCode)&exp=2000000000" }

    func testParsesStrictValidURIAndIgnoresOneUnknownParameter() throws {
        let qr = try PairingQRCode.parse(uri("\(basics)&gw=https%3A%2F%2Fgateway.example.invalid&fp=sha256%3A\(validFingerprint)&future=ignored"), now: future)
        XCTAssertEqual(qr.homeserver.host, "matrix.example.invalid")
        XCTAssertEqual(qr.gateway?.host, "gateway.example.invalid")
        XCTAssertEqual(qr.fingerprint?.count, 32)
    }

    func testRejectsRepeatedIncludingUnknownAndMissingRequiredFields() {
        XCTAssertThrowsError(try PairingQRCode.parse(uri("\(basics)&x=1&x=2"), now: future)) { XCTAssertEqual($0 as? PairingError, .duplicateParameter) }
        XCTAssertThrowsError(try PairingQRCode.parse(uri("v=1&hs=https%3A%2F%2Fmatrix.example.invalid&exp=2000000000"), now: future)) { XCTAssertEqual($0 as? PairingError, .missingParameter) }
        for missing in ["v", "hs", "code", "exp"] {
            let parts = basics.split(separator: "&").filter { !$0.hasPrefix("\(missing)=") }.joined(separator: "&")
            XCTAssertThrowsError(try PairingQRCode.parse(uri(parts), now: future), "missing \(missing)") { XCTAssertEqual($0 as? PairingError, .missingParameter) }
        }
        XCTAssertThrowsError(try PairingQRCode.parse(uri("\(basics)&%76=1"), now: future)) { XCTAssertEqual($0 as? PairingError, .duplicateParameter) }
    }

    func testRejectsInsecureBadBinaryExpiredAndNewerVersion() {
        XCTAssertThrowsError(try PairingQRCode.parse(uri("v=1&hs=http%3A%2F%2Fmatrix.example.invalid&code=\(validCode)&exp=2000000000"), now: future))
        XCTAssertThrowsError(try PairingQRCode.parse(uri("\(basics)&fp=sha256%3Aabc"), now: future)) { XCTAssertEqual($0 as? PairingError, .invalidFingerprint) }
        XCTAssertThrowsError(try PairingQRCode.parse(uri("v=1&hs=https%3A%2F%2Fmatrix.example.invalid&code=abc&exp=2000000000"), now: future)) { XCTAssertEqual($0 as? PairingError, .invalidCode) }
        XCTAssertThrowsError(try PairingQRCode.parse(uri("\(basics.replacingOccurrences(of: "v=1", with: "v=2"))"), now: future)) { XCTAssertEqual($0 as? PairingError, .unsupportedVersion) }
        XCTAssertThrowsError(try PairingQRCode.parse(uri(basics), now: Date(timeIntervalSince1970: 2_000_000_001))) { XCTAssertEqual($0 as? PairingError, .expiredLocally) }
        XCTAssertThrowsError(try PairingQRCode.parse(uri("\(basics)&code=\(validCode)=&fp=sha256%3A\(validFingerprint)="), now: future))
        XCTAssertThrowsError(try PairingQRCode.parse(uri("\(basics.replacingOccurrences(of: validCode, with: String(repeating: "_", count: 43)))"), now: future)) { XCTAssertEqual($0 as? PairingError, .invalidCode) }
    }

    func testRejectsStrictGatewayURLShapes() {
        for gateway in ["https%3A%2F%2Fuser%3Apass%40gateway.example.invalid", "https%3A%2F%2Fgateway.example.invalid%3A0", "https%3A%2F%2Fgateway.example.invalid%3Fx%3D1", "https%3A%2F%2Fgateway.example.invalid%23x", "https%3A%2F%2F%2567ateway.example.invalid"] {
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
        let goodString = "{\"v\":1,\"homeserver\":\"https://matrix.example.invalid\",\"gateway\":null,\"auth\":{\"type\":\"login_token\",\"token\":\"test\",\"expires_in_ms\":1000},\"features_url\":\"https://matrix.example.invalid/features\"}"
        let good = goodString.data(using: .utf8)!
        XCTAssertEqual(try HTTPPairingService.decodeResponse(good, pairing: qr, redeemHost: qr.homeserver.host, receivedAt: future).auth.type, .loginToken)
        let mismatch = "{\"v\":1,\"homeserver\":\"https://other.example.invalid\",\"gateway\":null,\"auth\":{\"type\":\"login_token\",\"token\":\"test\",\"expires_in_ms\":1000},\"features_url\":\"https://matrix.example.invalid/features\"}".data(using: .utf8)!
        XCTAssertThrowsError(try HTTPPairingService.decodeResponse(mismatch, pairing: qr, redeemHost: qr.homeserver.host, receivedAt: future)) { XCTAssertEqual($0 as? PairingError, .homeserverMismatch) }
        let registration = goodString.replacingOccurrences(of: "login_token", with: "registration_token").data(using: .utf8)!
        XCTAssertEqual(try HTTPPairingService.decodeResponse(registration, pairing: qr, redeemHost: qr.homeserver.host, receivedAt: future).auth.type, .registrationToken)
        for invalid in [goodString.replacingOccurrences(of: "\"v\":1", with: "\"v\":2"), goodString.replacingOccurrences(of: "login_token", with: "other"), goodString.replacingOccurrences(of: "https://matrix.example.invalid/features", with: "http://bad.invalid")].compactMap({ $0.data(using: .utf8) }) {
            XCTAssertThrowsError(try HTTPPairingService.decodeResponse(invalid, pairing: qr, redeemHost: qr.homeserver.host, receivedAt: future))
        }
    }

    func testRedeemRequestContainsOnlyWireFieldsAndLocalExpiryStopsBeforeNetwork() async throws {
        let qr = try PairingQRCode.parse(uri("\(basics)&gw=https%3A%2F%2Fgateway.example.invalid"), now: future)
        let request = try HTTPPairingService.redeemRequest(pairing: qr, deviceDisplayName: "My device")
        XCTAssertEqual(request.url?.absoluteString, "https://gateway.example.invalid/_botville/pair/redeem")
        XCTAssertEqual(request.httpMethod, "POST")
        let body = try XCTUnwrap(try JSONSerialization.jsonObject(with: XCTUnwrap(request.httpBody)) as? [String: String])
        XCTAssertEqual(body["device_display_name"], "My device")
        XCTAssertEqual(body["code"], validCode)
        let expired = try PairingQRCode.parse(uri("v=1&hs=https%3A%2F%2Fmatrix.example.invalid&code=\(validCode)&exp=1"), now: Date(timeIntervalSince1970: 0))
        await XCTAssertThrowsErrorAsync(try await HTTPPairingService().redeem(expired, deviceDisplayName: "unused")) { XCTAssertEqual($0 as? PairingError, .expiredLocally) }
    }
}

private func XCTAssertThrowsErrorAsync<T>(_ expression: @autoclosure () async throws -> T, _ handler: (Error) -> Void) async {
    do { _ = try await expression(); XCTFail("expected error") } catch { handler(error) }
}
