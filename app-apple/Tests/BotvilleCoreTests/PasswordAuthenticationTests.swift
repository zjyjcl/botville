// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

@testable import BotvilleCore
import Foundation
import MatrixRustSDK
import XCTest

@MainActor
final class PasswordAuthenticationTests: XCTestCase {
    private var registeredHosts = Set<String>()

    override func tearDown() {
        registeredHosts.forEach(LoginURLProtocol.registry.remove)
        registeredHosts.removeAll()
        super.tearDown()
    }

    func testPasswordLoginPostsExpectedPayloadAndPinsApprovedOrigin() async throws {
        let server = uniqueServer()
        register(server) { request in
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.url?.path, "/_matrix/client/v3/login")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/json")
            let body = try XCTUnwrap(self.requestBody(request))
            let payload = try JSONSerialization.jsonObject(with: body) as? [String: Any]
            XCTAssertEqual(payload?["type"] as? String, "m.login.password")
            XCTAssertEqual((payload?["identifier"] as? [String: String])?["type"], "m.id.user")
            XCTAssertEqual((payload?["identifier"] as? [String: String])?["user"], "@test:example.org")
            XCTAssertEqual(payload?["password"] as? String, "test-password")
            XCTAssertEqual(payload?["initial_device_display_name"] as? String, "botville C2.1")
            XCTAssertNil(payload?["device_id"])
            XCTAssertNil(payload?["refresh_token"])
            return .json(200, """
            {"access_token":"test-access","refresh_token":"test-refresh","user_id":"@test:example.org","device_id":"TEST","well_known":{"m.homeserver":{"base_url":"https://unapproved.example"}}}
            """)
        }

        let session = try await authentication().login(at: server, username: "@test:example.org", password: "test-password")
        XCTAssertEqual(session.accessToken, "test-access")
        XCTAssertEqual(session.refreshToken, "test-refresh")
        XCTAssertEqual(session.homeserverUrl, server.absoluteString)
    }

    func testPasswordLoginCanReuseAnExistingDeviceID() async throws {
        let server = uniqueServer()
        register(server) { request in
            let body = try XCTUnwrap(self.requestBody(request))
            let payload = try JSONSerialization.jsonObject(with: body) as? [String: Any]
            XCTAssertEqual(payload?["device_id"] as? String, "EXISTING_DEVICE")
            return .json(200, "{\"access_token\":\"test-access\",\"user_id\":\"@test:example.org\",\"device_id\":\"EXISTING_DEVICE\"}")
        }

        let session = try await authentication().login(at: server, username: "@test:example.org", password: "test-password", deviceID: "EXISTING_DEVICE")
        XCTAssertEqual(session.deviceId, "EXISTING_DEVICE")
    }

    func testDiscardPostsLogoutForReturnedSession() async throws {
        let server = uniqueServer()
        register(server) { request in
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.url?.path, "/_matrix/client/v3/logout")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer test-access")
            return .json(200, "{}")
        }
        let credentials = Session(accessToken: "test-access", refreshToken: nil, userId: "@test:example.org", deviceId: "TEST", homeserverUrl: server.absoluteString, oauthData: nil, slidingSyncVersion: .none)
        await authentication().discard(credentials)
    }

    func testHTTP429UsesRetryAfterSeconds() async {
        await assertLoginError(.rateLimited(retryAfter: 12), response: .json(429, "{\"errcode\":\"M_LIMIT_EXCEEDED\"}", headers: ["Retry-After": "12"]))
    }

    func testHTTP429UsesRetryAfterDate() {
        let now = Date(timeIntervalSince1970: 1_700_000_000)
        let date = "Tue, 14 Nov 2023 22:13:32 GMT"
        let response = HTTPURLResponse(url: URL(string: "https://matrix.example.test")!, statusCode: 429, httpVersion: nil, headerFields: ["Retry-After": date])!
        XCTAssertEqual(MatrixRateLimit.error(response: response, data: Data("{}".utf8), now: now), .rateLimited(retryAfter: 12))
    }

    func testLimitExceededUsesBodyDelayOrNoDelay() async {
        await assertLoginError(.rateLimited(retryAfter: 2.5), response: .json(429, "{\"errcode\":\"M_LIMIT_EXCEEDED\",\"retry_after_ms\":2500}"))
        await assertLoginError(.rateLimited(retryAfter: nil), response: .json(429, "{\"errcode\":\"M_LIMIT_EXCEEDED\"}"))
        await assertLoginError(.rateLimited(retryAfter: nil), response: .json(400, "{\"errcode\":\"M_LIMIT_EXCEEDED\"}"))
    }

    func testHTTP401And403AreSafeInvalidCredentialsWithoutServerMessage() async {
        for status in [401, 403] {
            await assertLoginError(.invalidCredentials, response: .json(status, "{\"errcode\":\"M_FORBIDDEN\",\"error\":\"server-controlled private diagnostic\"}"))
        }
        XCTAssertFalse(AuthenticationError.invalidCredentials.localizedDescription.contains("server-controlled"))
    }

    func testSDKLimitExceededClassifiesRetryAndCodeFallbackWithoutServerMessage() {
        let knownDelay = ClientError.MatrixApi(kind: .limitExceeded(retryAfterMs: 2_500), code: "M_LIMIT_EXCEEDED", msg: "server-controlled private diagnostic", details: "secret")
        XCTAssertEqual(PasswordLoginResultClassifier.error(for: knownDelay), .rateLimited(retryAfter: 2.5))
        let missingDelay = ClientError.MatrixApi(kind: .forbidden, code: "M_LIMIT_EXCEEDED", msg: "server-controlled private diagnostic", details: "secret")
        XCTAssertEqual(PasswordLoginResultClassifier.error(for: missingDelay), .rateLimited(retryAfter: nil))
        XCTAssertFalse(PasswordLoginResultClassifier.error(for: knownDelay).localizedDescription.contains("private diagnostic"))
    }

    func testAuthenticationAttemptTimesOutWithoutWaitingForUncooperativeOperation() async {
        let attempt = AuthenticationAttempt()
        let late = LateCompletion()
        do {
            try await attempt.run(timeout: 0.02) {
                await withCheckedContinuation { continuation in late.install(continuation) }
            }
            XCTFail("expected timeout")
        } catch let error as AuthenticationError {
            XCTAssertEqual(error, .timedOut)
        } catch { XCTFail("unexpected error \(error)") }
        late.complete()
        await Task.yield()
    }

    func testAuthenticationAttemptCancellationDoesNotDoubleResumeWhenOperationCompletesLate() async {
        let attempt = AuthenticationAttempt()
        let late = LateCompletion()
        let installed = expectation(description: "uncooperative operation installed its continuation")
        late.onInstall = { installed.fulfill() }
        let task = Task { @MainActor in
            try await attempt.run(timeout: 60) {
                await withCheckedContinuation { continuation in late.install(continuation) }
            }
        }
        await fulfillment(of: [installed], timeout: 1)
        task.cancel()
        do {
            try await task.value
            XCTFail("expected cancellation")
        } catch is CancellationError {
            // Expected: the caller returned while the uncooperative operation remains owned.
        } catch { XCTFail("unexpected error \(error)") }
        late.complete()
        await Task.yield()
    }

    private func authentication() -> MatrixPasswordAuthentication {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [LoginURLProtocol.self]
        return MatrixPasswordAuthentication(session: URLSession(configuration: configuration))
    }

    private func uniqueServer() -> URL {
        URL(string: "https://\(UUID().uuidString).matrix.test")!
    }

    private func assertLoginError(_ expected: AuthenticationError, response: LoginURLProtocol.Response) async {
        let server = uniqueServer()
        register(server) { _ in response }
        do {
            _ = try await authentication().login(at: server, username: "@test:example.org", password: "test-password")
            XCTFail("expected \(expected)")
        } catch let error as AuthenticationError {
            XCTAssertEqual(error, expected)
        } catch { XCTFail("unexpected error \(error)") }
    }

    private func register(_ server: URL, handler: @escaping (URLRequest) throws -> LoginURLProtocol.Response) {
        let host = server.host!
        registeredHosts.insert(host)
        LoginURLProtocol.registry.set(host, handler: handler)
    }

    private func requestBody(_ request: URLRequest) -> Data? {
        if let body = request.httpBody { return body }
        guard let stream = request.httpBodyStream else { return nil }
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 1_024)
        while stream.hasBytesAvailable {
            let read = stream.read(&buffer, maxLength: buffer.count)
            guard read > 0 else { break }
            data.append(contentsOf: buffer.prefix(read))
        }
        return data
    }
}

private final class LateCompletion: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<Void, Never>?
    var onInstall: (() -> Void)?
    func install(_ continuation: CheckedContinuation<Void, Never>) {
        let callback = lock.withLock { () -> (() -> Void)? in
            self.continuation = continuation
            return onInstall
        }
        callback?()
    }
    func complete() { lock.withLock { let pending = continuation; continuation = nil; pending?.resume() } }
}

private final class LoginURLProtocol: URLProtocol {
    struct Response {
        let status: Int
        let data: Data
        let headers: [String: String]
        static func json(_ status: Int, _ body: String, headers: [String: String] = [:]) -> Self { .init(status: status, data: Data(body.utf8), headers: headers) }
    }

    final class Registry: @unchecked Sendable {
        private let lock = NSLock()
        private var handlers: [String: (URLRequest) throws -> Response] = [:]
        func set(_ host: String, handler: @escaping (URLRequest) throws -> Response) { lock.withLock { handlers[host] = handler } }
        func handler(for host: String) -> ((URLRequest) throws -> Response)? { lock.withLock { handlers[host] } }
        func remove(_ host: String) { _ = lock.withLock { handlers.removeValue(forKey: host) } }
    }
    static let registry = Registry()

    override class func canInit(with request: URLRequest) -> Bool { request.url?.host?.hasSuffix(".matrix.test") == true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        do {
            guard let url = request.url, let host = url.host, let handler = Self.registry.handler(for: host) else { throw URLError(.badServerResponse) }
            let result = try handler(request)
            let response = HTTPURLResponse(url: url, statusCode: result.status, httpVersion: nil, headerFields: result.headers)!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: result.data)
            client?.urlProtocolDidFinishLoading(self)
        } catch { client?.urlProtocol(self, didFailWithError: error) }
    }
    override func stopLoading() {}
}
