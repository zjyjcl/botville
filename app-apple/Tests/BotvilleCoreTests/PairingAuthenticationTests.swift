// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

@testable import BotvilleCore
import Foundation
import XCTest

final class PairingAuthenticationTests: XCTestCase {
    override func tearDown() {
        PairingAuthenticationURLProtocol.handler = nil
        super.tearDown()
    }

    func testRegistrationTokenUsesUIAAndReturnsSessionOnSelectedHomeserver() async throws {
        let client = MatrixPairingCredentialAuthentication(session: fakeSession { request in
            XCTAssertEqual(request.url?.path, "/base/_matrix/client/v3/register")
            let body = try XCTUnwrap(try JSONSerialization.jsonObject(with: XCTUnwrap(self.requestBody(request))) as? [String: Any])
            XCTAssertEqual(body["username"] as? String, "new-user")
            XCTAssertEqual(body["password"] as? String, "chosen-password")
            XCTAssertEqual(body["initial_device_display_name"] as? String, "Test device")
            let auth = try XCTUnwrap(body["auth"] as? [String: Any])
            XCTAssertEqual(auth["type"] as? String, "m.login.registration_token")
            XCTAssertEqual(auth["token"] as? String, "temporary-registration-token")
            return (200, #"{"access_token":"access","user_id":"@new-user:example.test","device_id":"DEVICE"}"#)
        })

        let session = try await client.register(at: URL(string: "https://matrix.example.test/base")!,
                                                registrationToken: "temporary-registration-token", username: "new-user",
                                                password: "chosen-password", deviceDisplayName: "Test device", deadline: .distantFuture)
        XCTAssertEqual(session.homeserverUrl, "https://matrix.example.test/base")
        XCTAssertEqual(session.userId, "@new-user:example.test")
    }

    func testLoginTokenUsesMatrixLoginTokenFlow() async throws {
        let client = MatrixPairingCredentialAuthentication(session: fakeSession { request in
            XCTAssertEqual(request.url?.path, "/_matrix/client/v3/login")
            let body = try XCTUnwrap(try JSONSerialization.jsonObject(with: XCTUnwrap(self.requestBody(request))) as? [String: Any])
            XCTAssertEqual(body["type"] as? String, "m.login.token")
            XCTAssertEqual(body["token"] as? String, "temporary-login-token")
            XCTAssertEqual(body["initial_device_display_name"] as? String, "Test device")
            return (200, #"{"access_token":"access","refresh_token":"refresh","user_id":"@existing:example.test","device_id":"DEVICE"}"#)
        })

        let session = try await client.login(at: URL(string: "https://matrix.example.test")!, loginToken: "temporary-login-token", deviceDisplayName: "Test device", deadline: .distantFuture)
        XCTAssertEqual(session.refreshToken, "refresh")
        XCTAssertEqual(session.homeserverUrl, "https://matrix.example.test")
    }

    func testRegistrationTokenSubmitsServerUIASessionBeforeSuccess() async throws {
        var count = 0
        let client = MatrixPairingCredentialAuthentication(session: fakeSession { request in
            count += 1
            let body = try XCTUnwrap(try JSONSerialization.jsonObject(with: XCTUnwrap(self.requestBody(request))) as? [String: Any])
            let auth = try XCTUnwrap(body["auth"] as? [String: Any])
            XCTAssertEqual(auth["type"] as? String, "m.login.registration_token")
            if count == 1 {
                XCTAssertNil(auth["session"])
                return (401, #"{"session":"uia-1","flows":[{"stages":["m.login.registration_token"]}],"completed":[]}"#)
            }
            XCTAssertEqual(auth["session"] as? String, "uia-1")
            return (200, #"{"access_token":"access","user_id":"@new:example.test","device_id":"DEVICE"}"#)
        })
        _ = try await client.register(at: URL(string: "https://matrix.example.test")!, registrationToken: "one-time", username: "new", password: "password", deviceDisplayName: "Test", deadline: .distantFuture)
        XCTAssertEqual(count, 2)
    }

    func testRegistrationTokenAlreadyCompletedUsesDummyWithoutReplayingToken() async throws {
        var count = 0
        let client = MatrixPairingCredentialAuthentication(session: fakeSession { request in
            count += 1
            let body = try XCTUnwrap(try JSONSerialization.jsonObject(with: XCTUnwrap(self.requestBody(request))) as? [String: Any])
            let auth = try XCTUnwrap(body["auth"] as? [String: Any])
            if count == 1 {
                XCTAssertEqual(auth["type"] as? String, "m.login.registration_token")
                return (401, #"{"session":"uia-2","flows":[{"stages":["m.login.registration_token","m.login.dummy"]}],"completed":["m.login.registration_token"]}"#)
            }
            XCTAssertEqual(auth["type"] as? String, "m.login.dummy")
            XCTAssertEqual(auth["session"] as? String, "uia-2")
            return (200, #"{"access_token":"access","user_id":"@new:example.test","device_id":"DEVICE"}"#)
        })
        _ = try await client.register(at: URL(string: "https://matrix.example.test")!, registrationToken: "one-time", username: "new", password: "password", deviceDisplayName: "Test", deadline: .distantFuture)
        XCTAssertEqual(count, 2)
    }

    func testMatrixRateLimitPastDeadlineExpiresWithoutExposingBody() async {
        let client = MatrixPairingCredentialAuthentication(session: fakeSession { _ in
            (429, #"{"errcode":"M_LIMIT_EXCEEDED","error":"do not surface this","retry_after_ms":1500}"#)
        })
        do {
            _ = try await client.login(at: URL(string: "https://matrix.example.test")!, loginToken: "token", deviceDisplayName: "Test", deadline: Date().addingTimeInterval(0.1))
            XCTFail("expected expiry")
        } catch let error as PairingError {
            XCTAssertEqual(error, .expired)
            XCTAssertFalse(error.localizedDescription.contains("do not surface this"))
        } catch { XCTFail("unexpected error \(error)") }
    }

    func testDummyRateLimitRetriesOnlyDummyWithoutReplayingRegistrationToken() async throws {
        var count = 0
        let client = MatrixPairingCredentialAuthentication(session: fakeSession { request in
            count += 1
            let body = try XCTUnwrap(try JSONSerialization.jsonObject(with: XCTUnwrap(self.requestBody(request))) as? [String: Any])
            let auth = try XCTUnwrap(body["auth"] as? [String: Any])
            switch count {
            case 1:
                XCTAssertEqual(auth["type"] as? String, "m.login.registration_token")
                return (401, #"{"session":"uia-3","flows":[{"stages":["m.login.registration_token","m.login.dummy"]}],"completed":["m.login.registration_token"]}"#)
            case 2:
                XCTAssertEqual(auth["type"] as? String, "m.login.dummy")
                return (429, #"{"errcode":"M_LIMIT_EXCEEDED","retry_after_ms":1}"#)
            default:
                XCTAssertEqual(auth["type"] as? String, "m.login.dummy")
                return (200, #"{"access_token":"access","user_id":"@new:example.test","device_id":"DEVICE"}"#)
            }
        })
        _ = try await client.register(at: URL(string: "https://matrix.example.test")!, registrationToken: "one-time", username: "new", password: "password", deviceDisplayName: "Test", deadline: Date().addingTimeInterval(3))
        XCTAssertEqual(count, 3)
    }

    func testMalformedSuccessfulResponseIsSafeFailure() async {
        let client = MatrixPairingCredentialAuthentication(session: fakeSession { _ in (200, "{}") })
        do {
            _ = try await client.login(at: URL(string: "https://matrix.example.test")!, loginToken: "token", deviceDisplayName: "Test", deadline: .distantFuture)
            XCTFail("expected safe failure")
        } catch let error as AuthenticationError {
            XCTAssertEqual(error, .serverFailure)
        } catch { XCTFail("unexpected error \(error)") }
    }

    func testSessionCredentialsPreserveGatewayPinAndDecodeOldRecord() throws {
        let credentials = SessionCredentials(accessToken: "access", refreshToken: nil, userID: "@u:example.test", deviceID: "D",
                                             homeserverURL: "https://matrix.example.test", oauthData: nil, storeID: UUID(),
                                             storeKey: Data(repeating: 1, count: 32), gatewayURL: "https://gateway.example.test",
                                             gatewayCertificateFingerprint: Data(repeating: 2, count: 32))
        let restored = try JSONDecoder().decode(SessionCredentials.self, from: JSONEncoder().encode(credentials))
        XCTAssertEqual(restored.gatewayURL, "https://gateway.example.test")
        XCTAssertEqual(restored.gatewayCertificateFingerprint, Data(repeating: 2, count: 32))

        let old = """
        {"accessToken":"access","refreshToken":null,"userID":"@u:example.test","deviceID":"D","homeserverURL":"https://matrix.example.test","oauthData":null,"storeID":"\(UUID().uuidString)","storeKey":"AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE=","allowLocalHTTP":false}
        """
        let decodedOld = try JSONDecoder().decode(SessionCredentials.self, from: Data(old.utf8))
        XCTAssertNil(decodedOld.gatewayURL)
        XCTAssertNil(decodedOld.gatewayCertificateFingerprint)
    }

    func testPinnedGatewayRejectsInjectedSession() {
        let session = URLSession(configuration: .ephemeral)
        XCTAssertThrowsError(try HTTPGatewayClient(baseURL: URL(string: "https://gateway.example.test")!, session: session,
                                                   certificateFingerprint: Data(repeating: 3, count: 32)))
    }

    func testConfiguredPairingTransportCarriesHomeserverHostAndPort() throws {
        let host = "matrix.example.test"
        let port = 8448
        let configured = MatrixPairingCredentialAuthentication().configuredForPairing(
            fingerprint: Data(repeating: 4, count: 32), host: host, port: port
        )
        let pairingTransport = try XCTUnwrap(configured as? MatrixPairingCredentialAuthentication)
        defer { pairingTransport.session.invalidateAndCancel() }
        let delegate = try XCTUnwrap(pairingTransport.session.delegate as? PairingTLSDelegate)
        XCTAssertEqual(challengeResult(delegate: delegate, host: host, port: port), .cancelAuthenticationChallenge)
        XCTAssertEqual(challengeResult(delegate: delegate.requestDelegate(), host: host, port: port), .cancelAuthenticationChallenge)
        XCTAssertEqual(challengeResult(delegate: delegate, host: host, port: 443), .performDefaultHandling)
        XCTAssertEqual(challengeResult(delegate: delegate, host: "other.example.test", port: port), .performDefaultHandling)
    }

    private func fakeSession(_ handler: @escaping (URLRequest) throws -> (Int, String)) -> URLSession {
        PairingAuthenticationURLProtocol.handler = { request in
            let response = try handler(request)
            return (response.0, Data(response.1.utf8))
        }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [PairingAuthenticationURLProtocol.self]
        return URLSession(configuration: configuration)
    }

    private func requestBody(_ request: URLRequest) -> Data? {
        if let body = request.httpBody { return body }
        guard let stream = request.httpBodyStream else { return nil }
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 1024)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            guard count > 0 else { break }
            data.append(contentsOf: buffer.prefix(count))
        }
        return data
    }

    private func challengeResult(delegate: PairingTLSDelegate, host: String, port: Int) -> URLSession.AuthChallengeDisposition {
        let protectionSpace = URLProtectionSpace(host: host, port: port, protocol: "https", realm: nil, authenticationMethod: NSURLAuthenticationMethodServerTrust)
        let challenge = URLAuthenticationChallenge(protectionSpace: protectionSpace, proposedCredential: nil, previousFailureCount: 0, failureResponse: nil, error: nil, sender: PairingAuthenticationChallengeSender())
        var result: URLSession.AuthChallengeDisposition?
        delegate.urlSession(URLSession.shared, task: URLSession.shared.dataTask(with: URL(string: "https://\(host)/")!), didReceive: challenge) { disposition, _ in result = disposition }
        return result!
    }
}

private final class PairingAuthenticationChallengeSender: NSObject, URLAuthenticationChallengeSender {
    func use(_ credential: URLCredential, for challenge: URLAuthenticationChallenge) {}
    func continueWithoutCredential(for challenge: URLAuthenticationChallenge) {}
    func cancel(_ challenge: URLAuthenticationChallenge) {}
    func performDefaultHandling(for challenge: URLAuthenticationChallenge) {}
    func rejectProtectionSpaceAndContinue(with challenge: URLAuthenticationChallenge) {}
}

private final class PairingAuthenticationURLProtocol: URLProtocol {
    static var handler: ((URLRequest) throws -> (Int, Data))?

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        do {
            let result = try XCTUnwrap(Self.handler)(request)
            let response = HTTPURLResponse(url: request.url!, statusCode: result.0, httpVersion: "HTTP/1.1", headerFields: nil)!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: result.1)
            client?.urlProtocolDidFinishLoading(self)
        } catch { client?.urlProtocol(self, didFailWithError: error) }
    }
    override func stopLoading() {}
}
