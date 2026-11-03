// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

@testable import BotvilleCore
import MatrixRustSDK
import XCTest

final class AuthenticationSessionTests: XCTestCase {
    override func tearDown() {
        DiscoveryURLProtocol.handler = nil
        DiscoveryURLProtocol.paths = []
        super.tearDown()
    }

    func testHomeserverPolicyRequiresHTTPSExceptExplicitLoopbackDevelopment() throws {
        XCTAssertEqual(try HomeserverPolicy().validatedURL(" https://matrix.example.org/base ").absoluteString, "https://matrix.example.org/base")
        XCTAssertThrowsError(try HomeserverPolicy().validatedURL("http://127.0.0.1:8008"))
        let development = HomeserverPolicy(allowLocalHTTP: true)
        XCTAssertEqual(try development.validatedURL("http://localhost:8008").host, "localhost")
        for unsafe in ["http://example.test", "http://[::1]", "http://user:pass@localhost", "http://localhost?token=x", "http://localhost#x", "http://localhost:0", "http://localhost:65536"] {
            XCTAssertThrowsError(try development.validatedURL(unsafe))
        }
    }

    func testRedirectPolicyAcceptsOnlyTheApprovedOrigin() {
        let origin = URL(string: "https://matrix.example.org:8448/base")!
        XCTAssertTrue(HTTPSRedirectPolicy.allowsRedirect(from: origin, to: URL(string: "https://matrix.example.org:8448/next")!))
        XCTAssertFalse(HTTPSRedirectPolicy.allowsRedirect(from: origin, to: URL(string: "http://matrix.example.org:8448/next")!))
        XCTAssertFalse(HTTPSRedirectPolicy.allowsRedirect(from: origin, to: URL(string: "https://elsewhere.example.org:8448/next")!))
        XCTAssertFalse(HTTPSRedirectPolicy.allowsRedirect(from: origin, to: URL(string: "https://matrix.example.org/next")!))
    }

    func testDiscoveryRequestsVersionsThenLoginAndFindsPassword() async throws {
        let session = fakeDiscoverySession { request in
            switch request.url!.path {
            case "/base/_matrix/client/versions": return (200, #"{"versions":["v1.1"]}"#)
            case "/base/_matrix/client/v3/login": return (200, #"{"flows":[{"type":"m.login.password"}]}"#)
            default: XCTFail("unexpected endpoint \(request.url!.path)"); return (404, "{}")
            }
        }
        let capabilities = try await MatrixLoginCapabilityDiscovery(session: session).discover(at: URL(string: "https://matrix.example.org/base")!)
        XCTAssertTrue(capabilities.supportsPassword)
        XCTAssertEqual(DiscoveryURLProtocol.paths, ["/base/_matrix/client/versions", "/base/_matrix/client/v3/login"])
    }

    func testDiscoveryReportsUnsupportedPasswordFlow() async throws {
        let session = fakeDiscoverySession { request in
            request.url!.path.hasSuffix("versions") ? (200, #"{"versions":["v1.1"]}"#) : (200, #"{"flows":[{"type":"m.login.sso"}]}"#)
        }
        let capabilities = try await MatrixLoginCapabilityDiscovery(session: session).discover(at: URL(string: "https://matrix.example.org")!)
        XCTAssertFalse(capabilities.supportsPassword)
    }

    func testDiscoveryClassifiesInvalidJSONServerAndNetworkFailures() async {
        await assertDiscoveryError(.discoveryFailed) { _ in (200, "not-json") }
        await assertDiscoveryError(.serverFailure) { _ in (503, "{}") }
        DiscoveryURLProtocol.handler = { _ in throw URLError(.cannotConnectToHost) }
        let session = URLSession(configuration: fakeConfiguration())
        do {
            _ = try await MatrixLoginCapabilityDiscovery(session: session).discover(at: URL(string: "https://matrix.example.org")!)
            XCTFail("expected network failure")
        } catch let error as AuthenticationError {
            XCTAssertEqual(error, .networkFailure)
        } catch { XCTFail("unexpected error \(error)") }
    }

    func testPasswordLoginErrorsAreSafeAndSpecific() {
        XCTAssertEqual(PasswordLoginResultClassifier.error(forHTTPStatus: 401), .invalidCredentials)
        XCTAssertEqual(PasswordLoginResultClassifier.error(forHTTPStatus: 403), .invalidCredentials)
        XCTAssertEqual(PasswordLoginResultClassifier.error(forHTTPStatus: 404), .unsupportedLoginMethod)
        XCTAssertEqual(PasswordLoginResultClassifier.error(forHTTPStatus: 502), .serverFailure)
        XCTAssertNil(PasswordLoginResultClassifier.error(forHTTPStatus: 200))
    }

    func testSDKBuildErrorsRemainDistinctFromPasswordErrors() {
        XCTAssertEqual(PasswordLoginResultClassifier.error(for: ClientBuildError.ServerUnreachable(message: "redacted")), .networkFailure)
        XCTAssertEqual(PasswordLoginResultClassifier.error(for: ClientBuildError.WellKnownDeserializationError(message: "redacted")), .discoveryFailed)
        XCTAssertEqual(PasswordLoginResultClassifier.error(for: ClientBuildError.Sdk(message: "connection timeout")), .serverFailure)
        XCTAssertFalse(PasswordLoginResultClassifier.isExpiredSession(ClientBuildError.ServerUnreachable(message: "redacted")))
    }

    func testSDKTokenErrorClassificationOnlyExpiresUnknownToken() {
        let expired = ClientError.MatrixApi(kind: .unknownToken(softLogout: false), code: "M_UNKNOWN_TOKEN", msg: "token value must not be exposed", details: "secret")
        let forbidden = ClientError.MatrixApi(kind: .forbidden, code: "M_FORBIDDEN", msg: "password must not be exposed", details: nil)
        XCTAssertEqual(PasswordLoginResultClassifier.error(for: expired), .invalidCredentials)
        XCTAssertTrue(PasswordLoginResultClassifier.isExpiredSession(expired))
        XCTAssertEqual(PasswordLoginResultClassifier.error(for: forbidden), .invalidCredentials)
        XCTAssertFalse(PasswordLoginResultClassifier.isExpiredSession(forbidden))
        XCTAssertEqual(PasswordLoginResultClassifier.error(for: URLError(.timedOut)), .networkFailure)
        XCTAssertEqual(PasswordLoginResultClassifier.error(for: AuthenticationError.unsupportedLoginMethod), .unsupportedLoginMethod)
        XCTAssertEqual(PasswordLoginResultClassifier.error(for: ClientError.Generic(msg: "HTTP client connection timeout", details: nil)), .serverFailure)
    }

    func testNetworkClassificationUsesURLErrorAndSDKVariantsRatherThanDiagnostics() {
        XCTAssertEqual(PasswordLoginResultClassifier.error(for: URLError(.cannotConnectToHost)), .networkFailure)
        XCTAssertEqual(PasswordLoginResultClassifier.error(for: URLError(.badServerResponse)), .serverFailure)
        XCTAssertEqual(PasswordLoginResultClassifier.error(for: URLError(.cannotParseResponse)), .serverFailure)
        XCTAssertEqual(PasswordLoginResultClassifier.error(for: ClientError.MatrixApi(kind: .connectionFailed, code: "M_UNKNOWN", msg: "business failure", details: nil)), .networkFailure)
        XCTAssertEqual(PasswordLoginResultClassifier.error(for: ClientError.MatrixApi(kind: .connectionTimeout, code: "M_UNKNOWN", msg: "business failure", details: nil)), .networkFailure)
        XCTAssertEqual(PasswordLoginResultClassifier.error(for: ClientError.Generic(msg: "network timeout", details: "connection refused")), .serverFailure)
    }

    func testDiscoveryUsesTheTypedURLFailureClassification() async {
        DiscoveryURLProtocol.handler = { _ in throw URLError(.cannotParseResponse) }
        let session = URLSession(configuration: fakeConfiguration())
        do {
            _ = try await MatrixLoginCapabilityDiscovery(session: session).discover(at: URL(string: "https://matrix.example.org")!)
            XCTFail("expected response failure")
        } catch let error as AuthenticationError {
            XCTAssertEqual(error, .serverFailure)
        } catch { XCTFail("unexpected error \(error)") }
    }

    func testInMemorySessionStoreRoundTripAndDelete() throws {
        let store = InMemorySessionStore()
        let credentials = testCredentials()
        XCTAssertNil(try store.load())
        try store.save(credentials)
        XCTAssertEqual(try store.load(), credentials)
        try store.delete()
        XCTAssertNil(try store.load())
    }

    func testStoreKeyIsFresh32ByteRandomMaterial() throws {
        let first = try SessionCredentials.makeStoreKey()
        let second = try SessionCredentials.makeStoreKey()
        XCTAssertEqual(first.count, 32)
        XCTAssertEqual(second.count, 32)
        XCTAssertNotEqual(first, second)
    }

    func testSessionStoreDirectoryCreatesAndRemovesOnlyItsSessionDirectory() throws {
        let temporary = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent(".build/authentication-tests/\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: temporary) }
        let directory = SessionStoreDirectory(baseURL: temporary)
        let id = UUID()
        let created = try directory.create(for: id)
        XCTAssertTrue(FileManager.default.fileExists(atPath: created.path))
        let attributes = try FileManager.default.attributesOfItem(atPath: created.path)
        XCTAssertEqual(((attributes[.posixPermissions] as? NSNumber)?.intValue ?? 0) & 0o777, 0o700)
        try directory.remove(for: id)
        XCTAssertFalse(FileManager.default.fileExists(atPath: created.path))
    }

    private func testCredentials() -> SessionCredentials {
        SessionCredentials(accessToken: "test-access-token", refreshToken: nil, userID: "@test:example.org", deviceID: "DEVICE", homeserverURL: "https://example.org", oauthData: nil, storeID: UUID(), storeKey: Data(repeating: 7, count: 32))
    }

    private func fakeDiscoverySession(_ handler: @escaping (URLRequest) throws -> (Int, String)) -> URLSession {
        DiscoveryURLProtocol.paths = []
        DiscoveryURLProtocol.handler = { request in
            let response = try handler(request)
            return (response.0, Data(response.1.utf8))
        }
        return URLSession(configuration: fakeConfiguration())
    }

    private func fakeConfiguration() -> URLSessionConfiguration {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [DiscoveryURLProtocol.self]
        return configuration
    }

    private func assertDiscoveryError(_ expected: AuthenticationError, handler: @escaping (URLRequest) throws -> (Int, String)) async {
        let session = fakeDiscoverySession(handler)
        do {
            _ = try await MatrixLoginCapabilityDiscovery(session: session).discover(at: URL(string: "https://matrix.example.org")!)
            XCTFail("expected \(expected)")
        } catch let error as AuthenticationError {
            XCTAssertEqual(error, expected)
        } catch { XCTFail("unexpected error \(error)") }
    }
}

private final class DiscoveryURLProtocol: URLProtocol {
    static var paths: [String] = []
    static var handler: ((URLRequest) throws -> (Int, Data))?

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        do {
            Self.paths.append(request.url?.path ?? "")
            guard let handler = Self.handler else { throw URLError(.badServerResponse) }
            let result = try handler(request)
            let response = HTTPURLResponse(url: request.url!, statusCode: result.0, httpVersion: nil, headerFields: nil)!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: result.1)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }
    override func stopLoading() {}
}
