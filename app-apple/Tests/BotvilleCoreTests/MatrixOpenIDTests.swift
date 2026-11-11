// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

@testable import BotvilleCore
import Foundation
import XCTest

final class MatrixOpenIDTests: XCTestCase {
    private func credential(_ index: Int = 1, lifetime: Double = 3600,
                            type: String = "Bearer", server: String = "matrix.test") -> MatrixOpenIDToken {
        MatrixOpenIDToken(accessToken: "synthetic-openid-\(index)", tokenType: type,
                          matrixServerName: server, expiresInSeconds: lifetime)
    }

    private func transport(_ provider: MatrixOpenIDTokenProvider? = nil,
                           base: String = "https://gateway.test/_botville/gateway") throws -> (HTTPGatewayClient, OpenIDRecorder) {
        let recorder = OpenIDRecorder()
        OpenIDURLProtocol.recorder = recorder
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [OpenIDURLProtocol.self]
        return (try HTTPGatewayClient(baseURL: base, token: "synthetic-site",
            session: URLSession(configuration: configuration), openIDProvider: provider), recorder)
    }

    override func tearDown() { OpenIDURLProtocol.recorder = nil; super.tearDown() }

    func testExactSchemeAndNonMomentsBearerUnchanged() async throws {
        let provider = MatrixOpenIDTokenProvider { self.credential() }
        let (client, recorder) = try transport(provider)
        _ = try await client.momentsRequest()
        _ = try await client.responseData(for: client.request(["capabilities"]))
        let requests = recorder.requests
        XCTAssertEqual(requests[0].url?.path, "/_botville/gateway/v1/moments")
        // Boolean assertions intentionally never print credential values on failure.
        XCTAssertTrue(requests[0].value(forHTTPHeaderField: "Authorization") == "MatrixOpenID synthetic-openid-1")
        XCTAssertFalse(requests[0].value(forHTTPHeaderField: "Authorization")?.contains("Bearer") ?? true)
        XCTAssertTrue(requests[1].value(forHTTPHeaderField: "Authorization") == "Bearer synthetic-site")
    }

    func testPathSegmentsAndQueryWithBasePrefixes() async throws {
        for base in ["https://gateway.test", "https://gateway.test/v1", "https://gateway.test/_botville/gateway", "https://gateway.test/_botville/gateway/v1/"] {
            let (client, recorder) = try transport(MatrixOpenIDTokenProvider { self.credential() }, base: base)
            for (path, expected) in [(["moments"], true), (["moments", "id", "comments"], true), (["momentsx"], false), (["capabilities"], false)] {
                let request = try client.request(path, query: [URLQueryItem(name: "x", value: "moments")])
                XCTAssertEqual(try client.isMomentsRequest(request), expected)
                _ = try await client.responseData(for: request)
                let header = recorder.requests.last?.value(forHTTPHeaderField: "Authorization")
                XCTAssertTrue(header == (expected ? "MatrixOpenID synthetic-openid-1" : "Bearer synthetic-site"))
            }
        }
    }

    func testCacheAndSixtySecondRefreshBoundary() async throws {
        let clock = OpenIDClock()
        let source = OpenIDSource()
        let provider = MatrixOpenIDTokenProvider(now: { clock.now }) { try await source.issue() }
        _ = try await provider.token()
        clock.advance(3539)
        _ = try await provider.token()
        let before = await source.count
        XCTAssertEqual(before, 1)
        clock.advance(1)
        _ = try await provider.token()
        let after = await source.count
        XCTAssertEqual(after, 2)
    }

    func testShortLifetimeUsesHalfAsMargin() async throws {
        let clock = OpenIDClock()
        let source = OpenIDSource(lifetime: 20)
        let provider = MatrixOpenIDTokenProvider(now: { clock.now }) { try await source.issue() }
        _ = try await provider.token()
        clock.advance(9)
        _ = try await provider.token()
        clock.advance(1)
        _ = try await provider.token()
        let count = await source.count
        XCTAssertEqual(count, 2)
    }

    func testRequestLatencyDoesNotExtendLifetime() async throws {
        let clock = OpenIDClock()
        let source = OpenIDSource(lifetime: 20)
        let provider = MatrixOpenIDTokenProvider(now: { clock.now }) {
            clock.advance(6)
            return try await source.issue()
        }
        _ = try await provider.token()
        clock.advance(4)
        _ = try await provider.token()
        let count = await source.count
        XCTAssertEqual(count, 2)
    }

    func testTokenAlreadyInsideRefreshMarginOnArrivalIsRejected() async throws {
        let clock = OpenIDClock()
        let provider = MatrixOpenIDTokenProvider(now: { clock.now }) {
            clock.advance(11)
            return self.credential(lifetime: 20)
        }
        do { _ = try await provider.token(); XCTFail("Expected expired response") }
        catch { XCTAssertTrue(error as? MatrixOpenIDError == .invalidTokenResponse) }
    }

    func test401RefreshFailureDoesNotRetryHTTPOrFallBack() async throws {
        let source = OpenIDSource()
        let provider = MatrixOpenIDTokenProvider {
            let value = try await source.issue()
            if await source.count == 2 { throw SensitiveSyntheticError() }
            return value
        }
        let (client, recorder) = try transport(provider)
        recorder.statuses = [401, 200]
        do { _ = try await client.momentsRequest(); XCTFail("Expected refresh failure") }
        catch { XCTAssertTrue(error as? MatrixOpenIDError == .requestFailed) }
        XCTAssertEqual(recorder.requests.count, 1)
        _ = try await client.momentsRequest()
        XCTAssertEqual(recorder.requests.count, 2)
        let count = await source.count
        XCTAssertEqual(count, 3)
    }

    func testInvalidLifetimesAreRejectedAndNeverCached() async throws {
        for lifetime in [0, -1, Double.nan, Double.infinity, -Double.infinity] {
            let source = OpenIDSource(lifetime: lifetime)
            let provider = MatrixOpenIDTokenProvider { try await source.issue() }
            for _ in 0..<2 {
                do { _ = try await provider.token(); XCTFail("Expected invalid lifetime") }
                catch { XCTAssertTrue(error as? MatrixOpenIDError == .invalidTokenResponse) }
            }
            let count = await source.count
            XCTAssertEqual(count, 2)
        }
    }

    func testInvalidTokenMetadataAndHeaderCharacters() async throws {
        let values = [credential(type: "Other"), credential(server: "  "),
            MatrixOpenIDToken(accessToken: "", tokenType: "Bearer", matrixServerName: "matrix.test", expiresInSeconds: 100),
            MatrixOpenIDToken(accessToken: "synthetic\r\nheader", tokenType: "Bearer", matrixServerName: "matrix.test", expiresInSeconds: 100)]
        for value in values {
            let (client, recorder) = try transport(MatrixOpenIDTokenProvider { value })
            do { _ = try await client.momentsRequest(); XCTFail("Expected invalid metadata") }
            catch { XCTAssertTrue(error as? MatrixOpenIDError == .invalidTokenResponse) }
            XCTAssertEqual(recorder.requests.count, 0)
        }
    }

    func test401RefreshesOnceAndReusesBodyAndIdempotencyKey() async throws {
        let source = OpenIDSource()
        let (client, recorder) = try transport(MatrixOpenIDTokenProvider { try await source.issue() })
        recorder.statuses = [401, 200]
        let body = Data("{}".utf8)
        _ = try await client.momentsRequest(method: "POST", body: body, idempotencyKey: "synthetic-intent")
        XCTAssertEqual(recorder.requests.count, 2)
        let count = await source.count
        XCTAssertEqual(count, 2)
        for (index, request) in recorder.requests.enumerated() {
            XCTAssertTrue(request.value(forHTTPHeaderField: "Authorization") == "MatrixOpenID synthetic-openid-\(index + 1)")
            XCTAssertTrue(request.httpBody == body)
            XCTAssertEqual(request.value(forHTTPHeaderField: "Idempotency-Key"), "synthetic-intent")
            XCTAssertEqual(request.httpMethod, "POST")
        }
    }

    func testRepeated401StopsAfterOneRetry() async throws {
        let source = OpenIDSource()
        let (client, recorder) = try transport(MatrixOpenIDTokenProvider { try await source.issue() })
        recorder.statuses = [401, 401, 200]
        do { _ = try await client.momentsRequest(); XCTFail("Expected 401") }
        catch { XCTAssertEqual(error as? GatewayClientError, .httpStatus(401)) }
        let count = await source.count
        XCTAssertEqual(count, 2)
        XCTAssertEqual(recorder.requests.count, 2)
    }

    func test403AndOtherErrorsNeverRefresh() async throws {
        for status in [403, 404, 429, 500] {
            let source = OpenIDSource()
            let (client, recorder) = try transport(MatrixOpenIDTokenProvider { try await source.issue() })
            recorder.statuses = [status]
            do { _ = try await client.momentsRequest(); XCTFail("Expected HTTP error") }
            catch { XCTAssertEqual(error as? GatewayClientError, .httpStatus(status)) }
            let count = await source.count
            XCTAssertEqual(count, 1)
            XCTAssertEqual(recorder.requests.count, 1)
        }
    }

    func testMissingProviderDoesNotSend() async throws {
        let (client, recorder) = try transport()
        do { _ = try await client.momentsRequest(); XCTFail("Expected missing provider") }
        catch { XCTAssertTrue(error as? MatrixOpenIDError == .providerUnavailable) }
        XCTAssertTrue(recorder.requests.isEmpty)
    }

    func testSourceFailureDoesNotSendOrExposeUnderlyingError() async throws {
        let (client, recorder) = try transport(MatrixOpenIDTokenProvider { throw SensitiveSyntheticError() })
        do { _ = try await client.momentsRequest(); XCTFail("Expected source failure") }
        catch {
            XCTAssertTrue(error as? MatrixOpenIDError == .requestFailed)
            XCTAssertFalse(String(describing: error).contains("synthetic-sensitive"))
            XCTAssertFalse(String(reflecting: error).contains("synthetic-sensitive"))
        }
        XCTAssertTrue(recorder.requests.isEmpty)
    }

    func testRefreshFailureCanRecoverOnNextCall() async throws {
        let source = OpenIDSource(failures: 1)
        let provider = MatrixOpenIDTokenProvider { try await source.issue() }
        do { _ = try await provider.token(); XCTFail("Expected failure") } catch {}
        _ = try await provider.token()
        let count = await source.count
        XCTAssertEqual(count, 2)
    }

    func testConcurrentRequestsShareSingleFlight() async throws {
        let source = OpenIDSource(delay: true)
        let (client, recorder) = try transport(MatrixOpenIDTokenProvider { try await source.issue() })
        try await withThrowingTaskGroup(of: Void.self) { group in
            for _ in 0..<20 { group.addTask { _ = try await client.momentsRequest() } }
            try await group.waitForAll()
        }
        let count = await source.count
        XCTAssertEqual(count, 1)
        XCTAssertEqual(recorder.requests.count, 20)
    }

    func testConcurrent401sShareOneRefreshIncludingLateResponses() async throws {
        let source = OpenIDSource(delay: true)
        let provider = MatrixOpenIDTokenProvider { try await source.issue() }
        let old = try await provider.token()
        let (client, recorder) = try transport(provider)
        // Hold all first responses until every request used the same token.
        recorder.unauthorizedBarrier = 20
        try await withThrowingTaskGroup(of: Void.self) { group in
            for _ in 0..<20 { group.addTask { _ = try await client.momentsRequest() } }
            try await group.waitForAll()
        }
        await provider.invalidate(old)
        _ = try await provider.token()
        let count = await source.count
        XCTAssertEqual(count, 2)
        XCTAssertEqual(recorder.requests.count, 40)
    }

    func testConcurrentFailureReleasesAllWaitersAndAllowsRetry() async throws {
        let source = OpenIDSource(failures: 1, delay: true)
        let provider = MatrixOpenIDTokenProvider { try await source.issue() }
        let failures = await withTaskGroup(of: Bool.self, returning: Int.self) { group in
            for _ in 0..<20 {
                group.addTask {
                    do { _ = try await provider.token(); return false }
                    catch { return error as? MatrixOpenIDError == .requestFailed }
                }
            }
            var total = 0
            for await failed in group { if failed { total += 1 } }
            return total
        }
        XCTAssertEqual(failures, 20)
        _ = try await provider.token()
        let count = await source.count
        XCTAssertEqual(count, 2)
    }

    func testShutdownClearsCacheAndDisablesRetainedGatewayClient() async throws {
        let source = OpenIDSource()
        let provider = MatrixOpenIDTokenProvider { try await source.issue() }
        _ = try await provider.token()
        let (client, recorder) = try transport(provider)
        await provider.shutdown()
        do { _ = try await client.momentsRequest(); XCTFail("Expected ended session") }
        catch { XCTAssertTrue(error as? MatrixOpenIDError == .sessionEnded) }
        XCTAssertTrue(recorder.requests.isEmpty)
        let count = await source.count
        XCTAssertEqual(count, 1)
    }

    func testShutdownDuringRefreshCannotRepopulateCache() async throws {
        let gate = OpenIDGate()
        let provider = MatrixOpenIDTokenProvider { await gate.issue() }
        let pending = Task { try await provider.token() }
        await gate.waitUntilStarted()
        await provider.shutdown()
        await gate.release()
        do { _ = try await pending.value; XCTFail("Expected ended session") }
        catch { XCTAssertTrue(error as? MatrixOpenIDError == .sessionEnded) }
        do { _ = try await provider.token(); XCTFail("Expected closed provider") }
        catch { XCTAssertTrue(error as? MatrixOpenIDError == .sessionEnded) }
    }

    func testDescriptionReflectionAndDumpAreRedacted() {
        let token = credential()
        var dumped = ""
        dump(token, to: &dumped)
        for output in [String(describing: token), String(reflecting: token), dumped] {
            XCTAssertFalse(output.contains("synthetic-openid"))
            XCTAssertFalse(output.contains("matrix.test"))
            XCTAssertTrue(output.contains("redacted"))
        }
        for error in [MatrixOpenIDError.providerUnavailable, .requestFailed, .invalidTokenResponse, .sessionEnded] {
            XCTAssertFalse(String(reflecting: error).contains("synthetic-openid"))
        }
    }
}

private struct SensitiveSyntheticError: Error, CustomStringConvertible {
    var description: String { "synthetic-sensitive" }
}

private actor OpenIDSource {
    var count = 0
    let lifetime: Double
    var failures: Int
    let delay: Bool
    init(lifetime: Double = 3600, failures: Int = 0, delay: Bool = false) {
        self.lifetime = lifetime; self.failures = failures; self.delay = delay
    }
    func issue() async throws -> MatrixOpenIDToken {
        count += 1
        let index = count
        let failing = failures > 0
        if failing { failures -= 1 }
        if delay { try await Task.sleep(for: .milliseconds(100)) }
        if failing { throw SensitiveSyntheticError() }
        return MatrixOpenIDToken(accessToken: "synthetic-openid-\(index)", tokenType: "Bearer",
            matrixServerName: "matrix.test", expiresInSeconds: lifetime)
    }
}

private actor OpenIDGate {
    var continuation: CheckedContinuation<MatrixOpenIDToken, Never>?
    func issue() async -> MatrixOpenIDToken { await withCheckedContinuation { continuation = $0 } }
    func waitUntilStarted() async { while continuation == nil { await Task.yield() } }
    func release() {
        continuation?.resume(returning: MatrixOpenIDToken(accessToken: "synthetic-openid", tokenType: "Bearer",
            matrixServerName: "matrix.test", expiresInSeconds: 3600))
        continuation = nil
    }
}

private final class OpenIDClock: @unchecked Sendable {
    let lock = NSLock()
    private var value = Date(timeIntervalSince1970: 1000)
    var now: Date { lock.lock(); defer { lock.unlock() }; return value }
    func advance(_ seconds: Double) { lock.lock(); defer { lock.unlock() }; value.addTimeInterval(seconds) }
}

private final class OpenIDRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var captured: [URLRequest] = []
    var statuses: [Int] = []
    var unauthorizedBarrier = 0
    private var held: [OpenIDURLProtocol] = []
    var requests: [URLRequest] { lock.lock(); defer { lock.unlock() }; return captured }
    func receive(_ loader: OpenIDURLProtocol) {
        var request = loader.request
        if request.httpBody == nil, let stream = request.httpBodyStream {
            stream.open()
            defer { stream.close() }
            var data = Data()
            var buffer = [UInt8](repeating: 0, count: 1024)
            while stream.hasBytesAvailable {
                let count = stream.read(&buffer, maxLength: buffer.count)
                guard count > 0 else { break }
                data.append(buffer, count: count)
            }
            request.httpBody = data
        }
        lock.lock()
        captured.append(request)
        if unauthorizedBarrier > 0 {
            held.append(loader)
            if held.count == unauthorizedBarrier {
                let ready = held
                held = []; unauthorizedBarrier = 0
                lock.unlock()
                ready.forEach { $0.complete(401) }
            } else { lock.unlock() }
            return
        }
        let status = statuses.isEmpty ? 200 : statuses.removeFirst()
        lock.unlock()
        loader.complete(status)
    }
}

private final class OpenIDURLProtocol: URLProtocol {
    static var recorder: OpenIDRecorder?
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() { Self.recorder!.receive(self) }
    func complete(_ status: Int) {
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status,
            httpVersion: "HTTP/1.1", headerFields: nil)!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data("{}".utf8))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}
