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
                           base: String = "https://gateway.test/_botville/gateway",
                           sleep: HTTPGatewayClient.RetrySleep? = nil) throws -> (HTTPGatewayClient, OpenIDRecorder) {
        let recorder = OpenIDRecorder()
        OpenIDURLProtocol.recorder = recorder
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [OpenIDURLProtocol.self]
        return (try HTTPGatewayClient(baseURL: base, token: "synthetic-site",
            session: URLSession(configuration: configuration), openIDProvider: provider, retrySleep: sleep ?? { _ in }), recorder)
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
            for (path, expected) in [(["moments"], true), (["moments", "id", "comments"], true), (["momentsx"], false), (["capabilities"], false), (["cards"], false), (["context", "assemble"], false)] {
                let request = try client.request(path, query: [URLQueryItem(name: "x", value: "moments")])
                XCTAssertEqual(try client.isMomentsRequest(request), expected)
                _ = try await client.responseData(for: request)
                let header = recorder.requests.last?.value(forHTTPHeaderField: "Authorization")
                XCTAssertTrue(header == (expected ? "MatrixOpenID synthetic-openid-1" : "Bearer synthetic-site"))
            }
        }
    }

    func testCacheAndTwentyPercentRefreshBoundary() async throws {
        let clock = OpenIDClock()
        let source = OpenIDSource()
        let provider = MatrixOpenIDTokenProvider(now: { clock.now }) { try await source.issue() }
        _ = try await provider.token()
        clock.advance(2880)
        _ = try await provider.token()
        let before = await source.count
        XCTAssertEqual(before, 1)
        clock.advance(0.01)
        _ = try await provider.token()
        let after = await source.count
        XCTAssertEqual(after, 2)
        clock.advance(3600)
        _ = try await provider.token()
        let expired = await source.count
        XCTAssertEqual(expired, 3)
    }

    func testMixedAuthAndServerRetriesHaveIndependentBoundedBudgets() async throws {
        let waits = OpenIDWaits()
        let source = OpenIDSource()
        let (client, recorder) = try transport(MatrixOpenIDTokenProvider { try await source.issue() },
            sleep: { await waits.record($0) })
        recorder.statuses = [500, 401, 500, 500, 500]
        let unavailable = try OpenIDFixtures.data("problem-homeserver-unavailable")
        recorder.bodies = [unavailable, try OpenIDFixtures.data("problem-auth-invalid-token"),
                           unavailable, unavailable, unavailable]
        do { _ = try await client.momentsRequest(); XCTFail("Expected bounded server retries") }
        catch { XCTAssertEqual(error as? GatewayClientError, .problem(status: 500, code: "internal.error")) }
        let count = await source.count
        let delays = await waits.values
        XCTAssertEqual(count, 2)
        XCTAssertEqual(recorder.requests.count, 5)
        XCTAssertEqual(delays, [1, 2, 4])
    }

    func testShortLifetimeUsesHalfAsMargin() async throws {
        let clock = OpenIDClock()
        let source = OpenIDSource(lifetime: 20)
        let provider = MatrixOpenIDTokenProvider(now: { clock.now }) { try await source.issue() }
        _ = try await provider.token()
        clock.advance(10)
        _ = try await provider.token()
        clock.advance(0.01)
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
        clock.advance(4.01)
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
        catch {
            XCTAssertEqual(error as? GatewayClientError, .serverAuthMisconfigured)
            XCTAssertEqual((error as? GatewayClientError)?.httpStatus, 401)
            XCTAssertEqual((error as? GatewayClientError)?.problemCode, "auth.invalid_token")
            XCTAssertEqual((error as? LocalizedError)?.errorDescription, "服务器认证异常，请联系部署者")
            var dumped = ""
            dump(error, to: &dumped)
            for output in [error.localizedDescription, String(describing: error), String(reflecting: error), dumped] {
                XCTAssertFalse(output.contains("synthetic-openid"))
                XCTAssertFalse(output.contains("request_id"))
            }
        }
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

    func testShortTTLsAreReusableAndAlwaysRefreshAfterExpiry() async throws {
        for lifetime in [30.0, 60, 61, 1] {
            let clock = OpenIDClock()
            let source = OpenIDSource(lifetime: lifetime)
            let (client, recorder) = try transport(MatrixOpenIDTokenProvider(now: { clock.now }) { try await source.issue() })
            for _ in 0..<10 { _ = try await client.momentsRequest() }
            clock.advance(lifetime / 2)
            _ = try await client.momentsRequest()
            let atBoundary = await source.count
            XCTAssertEqual(atBoundary, 1)
            clock.advance(0.001)
            _ = try await client.momentsRequest()
            for _ in 0..<10 { _ = try await client.momentsRequest() }
            let refreshed = await source.count
            XCTAssertEqual(refreshed, 2)
            clock.advance(lifetime)
            _ = try await client.momentsRequest()
            let expired = await source.count
            XCTAssertEqual(expired, 3)
            XCTAssertEqual(recorder.requests.count, 23)
        }
    }

    func testMissingAuthAndOtherProblemCodesNeverRetryOrInvalidate() async throws {
        let cases: [(Int, Data, String)] = [
            (401, try OpenIDFixtures.data("problem-auth-missing"), "auth.missing"),
            (403, Data(#"{"code":"persona.not_owned"}"#.utf8), "persona.not_owned"),
            (404, Data(#"{"code":"not_found"}"#.utf8), "not_found"),
            (401, Data(#"{"code":"future.code"}"#.utf8), "future.code"),
            (500, Data(#"{"code":"auth.invalid_token"}"#.utf8), "auth.invalid_token")
        ]
        for (status, body, code) in cases {
            let source = OpenIDSource()
            let provider = MatrixOpenIDTokenProvider { try await source.issue() }
            let (client, recorder) = try transport(provider)
            recorder.statuses = [status]
            recorder.bodies = [body]
            do { _ = try await client.momentsRequest(); XCTFail("Expected problem") }
            catch { XCTAssertEqual(error as? GatewayClientError, .problem(status: status, code: code)) }
            _ = try await provider.token()
            let count = await source.count
            XCTAssertEqual(count, 1)
            XCTAssertEqual(recorder.requests.count, 1)
        }
    }

    func testUnparseableProblemConservativelyPreservesStatusAndToken() async throws {
        for status in [401, 500] {
            for (body, type) in [(Data(), "application/problem+json"),
                                 (Data("not-json".utf8), "application/problem+json"),
                                 (Data("{}".utf8), "application/problem+json"),
                                 (Data(#"{"code":null}"#.utf8), "application/problem+json"),
                                 (try OpenIDFixtures.data("problem-auth-invalid-token"), "application/json"),
                                 (try OpenIDFixtures.data("problem-homeserver-unavailable"), "")] {
                let source = OpenIDSource()
                let provider = MatrixOpenIDTokenProvider { try await source.issue() }
                let (client, recorder) = try transport(provider)
                recorder.statuses = [status]
                recorder.bodies = [body]
                recorder.contentTypes = [type]
                do { _ = try await client.momentsRequest(); XCTFail("Expected original HTTP error") }
                catch { XCTAssertEqual(error as? GatewayClientError, .httpStatus(status)) }
                _ = try await provider.token()
                let count = await source.count
                XCTAssertEqual(count, 1)
                XCTAssertEqual(recorder.requests.count, 1)
            }
        }
    }

    func testServerErrorsRetryOnlyGETOrIdempotencyKeyWithExactBackoff() async throws {
        for (method, key, retries) in [("GET", nil, 3), ("get", nil, 3), ("gEt", nil, 3), ("POST", "placeholder-intent", 3),
                                       ("POST", nil, 0), ("PATCH", nil, 0), ("DELETE", nil, 0), ("HEAD", nil, 0), ("POST", " ", 0)] {
            let waits = OpenIDWaits()
            let source = OpenIDSource()
            let provider = MatrixOpenIDTokenProvider { try await source.issue() }
            let (client, recorder) = try transport(provider, sleep: { await waits.record($0) })
            recorder.statuses = Array(repeating: 500, count: 4)
            recorder.bodies = Array(repeating: try OpenIDFixtures.data("problem-homeserver-unavailable"), count: 4)
            let body = Data("{}".utf8)
            do { _ = try await client.momentsRequest(method: method, body: body, idempotencyKey: key); XCTFail("Expected server problem") }
            catch { XCTAssertEqual(error as? GatewayClientError, .problem(status: 500, code: "internal.error")) }
            _ = try await provider.token()
            let count = await source.count
            let delays = await waits.values
            XCTAssertEqual(count, 1)
            XCTAssertEqual(recorder.requests.count, retries + 1)
            XCTAssertEqual(delays, retries == 3 ? [1, 2, 4] : [])
            for request in recorder.requests {
                XCTAssertEqual(request.httpMethod?.lowercased(), method.lowercased())
                XCTAssertEqual(request.httpBody, body)
                XCTAssertEqual(request.value(forHTTPHeaderField: "Idempotency-Key"), key)
                XCTAssertTrue(request.value(forHTTPHeaderField: "Authorization") == "MatrixOpenID synthetic-openid-1")
            }
        }
    }

    func testGatewayProblemExamplesDriveStatusCodeAndRetryBranches() async throws {
        struct Problem: Decodable { let status: Int; let code: String }
        for (name, status, code, requests, issues) in [
            ("problem-auth-missing", 401, "auth.missing", 1, 1),
            ("problem-auth-invalid-token", 401, "auth.invalid_token", 2, 2),
            ("problem-homeserver-unavailable", 500, "internal.error", 4, 1)
        ] {
            let data = try OpenIDFixtures.data(name)
            let problem = try JSONDecoder().decode(Problem.self, from: data)
            XCTAssertEqual(problem.status, status)
            XCTAssertEqual(problem.code, code)
            let source = OpenIDSource()
            let (client, recorder) = try transport(MatrixOpenIDTokenProvider { try await source.issue() })
            recorder.statuses = Array(repeating: problem.status, count: requests)
            recorder.bodies = Array(repeating: data, count: requests)
            do { _ = try await client.momentsRequest(); XCTFail("Expected contract problem") }
            catch {
                let actual = try XCTUnwrap(error as? GatewayClientError)
                if code == "auth.invalid_token" {
                    XCTAssertEqual(actual, .serverAuthMisconfigured)
                    XCTAssertEqual(actual.httpStatus, problem.status)
                    XCTAssertEqual(actual.problemCode, problem.code)
                } else {
                    XCTAssertEqual(actual, .problem(status: problem.status, code: problem.code))
                    XCTAssertEqual(actual.httpStatus, problem.status)
                    XCTAssertEqual(actual.problemCode, problem.code)
                }
            }
            XCTAssertEqual(recorder.requests.count, requests)
            let count = await source.count
            XCTAssertEqual(count, issues)
        }
    }

    func testNonMomentsInternalErrorNeverRetriesEvenWithIdempotencyKey() async throws {
        for path in [["capabilities"], ["cards"], ["cards", "id", "actions", "open"],
                     ["capabilities", "id", "session-tokens"], ["context", "assemble"], ["momentsx"]] {
            for method in ["GET", "get", "POST"] {
                let waits = OpenIDWaits()
                let (client, recorder) = try transport(sleep: { await waits.record($0) })
                recorder.statuses = [500, 200]
                recorder.bodies = [try OpenIDFixtures.data("problem-homeserver-unavailable")]
                do {
                    _ = try await client.responseData(for: client.request(path, method: method,
                        idempotencyKey: "placeholder-intent"))
                    XCTFail("Expected original server problem")
                } catch {
                    XCTAssertEqual(error as? GatewayClientError, .problem(status: 500, code: "internal.error"))
                }
                XCTAssertEqual(recorder.requests.count, 1)
                let delays = await waits.values
                XCTAssertTrue(delays.isEmpty)
            }
        }
    }

    func testServerBackoffCanRecoverAndCancellationStopsRetry() async throws {
        for cancel in [false, true] {
            let waits = OpenIDWaits()
            let source = OpenIDSource()
            let (client, recorder) = try transport(MatrixOpenIDTokenProvider { try await source.issue() }, sleep: {
                await waits.record($0)
                if cancel { throw CancellationError() }
            })
            recorder.statuses = [500, 200]
            recorder.bodies = [try OpenIDFixtures.data("problem-homeserver-unavailable")]
            do {
                _ = try await client.momentsRequest()
                XCTAssertFalse(cancel)
            } catch { XCTAssertTrue(cancel && error is CancellationError) }
            let count = await source.count
            let delays = await waits.values
            XCTAssertEqual(count, 1)
            XCTAssertEqual(delays, [1])
            XCTAssertEqual(recorder.requests.count, cancel ? 1 : 2)
        }
    }

    func testRefreshFollowedByMissingAuthDoesNotBecomeMisconfigurationError() async throws {
        let source = OpenIDSource()
        let (client, recorder) = try transport(MatrixOpenIDTokenProvider { try await source.issue() })
        recorder.statuses = [401, 401]
        recorder.bodies = [try OpenIDFixtures.data("problem-auth-invalid-token"), try OpenIDFixtures.data("problem-auth-missing")]
        do { _ = try await client.momentsRequest(); XCTFail("Expected missing auth") }
        catch { XCTAssertEqual(error as? GatewayClientError, .problem(status: 401, code: "auth.missing")) }
        let count = await source.count
        XCTAssertEqual(count, 2)
        XCTAssertEqual(recorder.requests.count, 2)
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

    func testSingleCallerCancellationReturnsBeforeSourceCompletes() async throws {
        let gate = OpenIDGate()
        let source = OpenIDSource()
        let provider = MatrixOpenIDTokenProvider {
            _ = try await source.issue()
            return await gate.issue()
        }
        let returned = expectation(description: "Cancelled caller returned without SDK completion")
        let pending = Task {
            defer { returned.fulfill() }
            return try await provider.token()
        }
        await gate.waitUntilStarted()
        pending.cancel()
        await fulfillment(of: [returned], timeout: 2)
        await gate.release()
        do { _ = try await pending.value; XCTFail("Expected caller cancellation") }
        catch { XCTAssertTrue(error is CancellationError) }
        // With all callers cancelled, the bounded flight remains available to
        // subsequent callers and may warm the cache; it is never restarted here.
        _ = try await provider.token()
        let count = await source.count
        XCTAssertEqual(count, 1)
        await provider.shutdown()
    }

    func testOneCancelledCallerDoesNotCancelOtherWaiters() async throws {
        let gate = OpenIDGate()
        let source = OpenIDSource()
        let provider = MatrixOpenIDTokenProvider {
            _ = try await source.issue()
            return await gate.issue()
        }
        let returned = expectation(description: "Only cancelled caller returned")
        let cancelled = Task {
            defer { returned.fulfill() }
            return try await provider.token()
        }
        await gate.waitUntilStarted()
        let survivor = Task { try await provider.token() }
        cancelled.cancel()
        await fulfillment(of: [returned], timeout: 2)
        await gate.release()
        do { _ = try await cancelled.value; XCTFail("Expected caller cancellation") }
        catch { XCTAssertTrue(error is CancellationError) }
        let token = try await survivor.value
        XCTAssertTrue(token.isValid)
        let count = await source.count
        XCTAssertEqual(count, 1)
        await provider.shutdown()
    }

    func testRequestTimeoutConfigurationIsNormalized() async throws {
        let cases: [(TimeInterval, TimeInterval)] = [
            (.nan, 30), (.infinity, 30), (-.infinity, 30), (0, 30), (-1, 30),
            (.greatestFiniteMagnitude, 300), (301, 300), (300, 300), (30, 30), (0.001, 0.001)
        ]
        for (input, expected) in cases {
            let gate = OpenIDGate()
            let durations = OpenIDWaits()
            let provider = MatrixOpenIDTokenProvider(requestTimeout: input, timeoutSleep: {
                await durations.record($0)
                await gate.waitUntilStarted()
            }) { await gate.issue() }
            do { _ = try await provider.token(); XCTFail("Expected normalized deadline") }
            catch { XCTAssertTrue(error as? MatrixOpenIDError == .requestTimedOut) }
            let observed = await durations.values
            XCTAssertEqual(observed, [expected])
            await gate.release()
            await provider.shutdown()
        }
    }

    func testNilTimeoutSleepUsesDefaultImplementation() async throws {
        let gate = OpenIDGate()
        let provider = MatrixOpenIDTokenProvider(requestTimeout: 0.05, timeoutSleep: nil) {
            await gate.issue()
        }
        do { _ = try await provider.token(); XCTFail("Expected real deadline") }
        catch { XCTAssertTrue(error as? MatrixOpenIDError == .requestTimedOut) }
        await gate.release()
        await provider.shutdown()
    }

    func testTimeoutSleepFailureReleasesAllWaitersAndAllowsRetry() async throws {
        let gate = OpenIDGate() // Source ignores cancellation until explicitly released.
        let deadline = OpenIDGate()
        let source = OpenIDSource()
        let sleeps = OpenIDSource()
        let provider = MatrixOpenIDTokenProvider(timeoutSleep: { _ in
            _ = try await sleeps.issue()
            if await sleeps.count == 1 {
                _ = await deadline.issue()
                throw SensitiveSyntheticError()
            }
            try await Task.sleep(for: .seconds(3600))
        }) {
            let value = try await source.issue()
            if await source.count == 1 { return await gate.issue() }
            return value
        }
        let completions = OpenIDWaits()
        let started = expectation(description: "All callers launched")
        started.expectedFulfillmentCount = 20
        let returned = expectation(description: "All callers released by scheduler failure")
        returned.expectedFulfillmentCount = 20
        let callers = (0..<20).map { _ in Task {
            started.fulfill()
            let failed: Bool
            do { _ = try await provider.token(); failed = false }
            catch {
                failed = error as? MatrixOpenIDError == .requestFailed
                    && !String(reflecting: error).contains("synthetic-sensitive")
            }
            await completions.record(1)
            returned.fulfill()
            return failed
        } }
        await fulfillment(of: [started], timeout: 2)
        await gate.waitUntilStarted()
        await deadline.waitUntilStarted()
        // Give launched callers time to join the suspended flight, as in the
        // existing concurrent source-failure test. The source count below also
        // checks that no caller accidentally started another flight.
        try await Task.sleep(for: .milliseconds(100))
        await deadline.release()
        await fulfillment(of: [returned], timeout: 2)
        // On a regression release hung callers before awaiting their results.
        let completed = await completions.values.count
        guard completed == 20 else {
            await provider.shutdown()
            await gate.release()
            for caller in callers { _ = await caller.value }
            return
        }
        let recovered = try await provider.token()
        XCTAssertTrue(recovered.isValid)
        await gate.release()
        for caller in callers { let failed = await caller.value; XCTAssertTrue(failed) }
        let cached = try await provider.token()
        XCTAssertTrue(cached.identity == recovered.identity)
        let count = await source.count
        XCTAssertEqual(count, 2)
        await provider.shutdown()
    }

    func testTimeoutSleepCancellationLeavesSourceFlightAlive() async throws {
        let gate = OpenIDGate()
        let slept = expectation(description: "Injected sleeper cancelled")
        let returned = expectation(description: "Cancellation must not finish the flight")
        returned.isInverted = true
        let provider = MatrixOpenIDTokenProvider(timeoutSleep: { _ in
            slept.fulfill()
            throw CancellationError()
        }) { await gate.issue() }
        let pending = Task {
            defer { returned.fulfill() }
            return try await provider.token()
        }
        await gate.waitUntilStarted()
        await fulfillment(of: [slept], timeout: 2)
        await fulfillment(of: [returned], timeout: 0.05)
        await gate.release()
        let value = try await pending.value
        XCTAssertTrue(value.isValid)
        await provider.shutdown()
    }

    func testInjectedTimeoutReleasesCallerAndNextFlightCanSucceed() async throws {
        let gate = OpenIDGate() // Deliberately ignores SDK Task cancellation.
        let deadline = OpenIDGate()
        let source = OpenIDSource()
        let sleeps = OpenIDSource()
        let observedTimeouts = OpenIDWaits()
        let provider = MatrixOpenIDTokenProvider(requestTimeout: 7, timeoutSleep: { duration in
            await observedTimeouts.record(duration)
            _ = try await sleeps.issue()
            if await sleeps.count == 1 { _ = await deadline.issue() }
            else { try await Task.sleep(for: .seconds(3600)) }
        }) {
            let value = try await source.issue()
            if await source.count == 1 { return await gate.issue() }
            return value
        }
        let returned = expectation(description: "Deadline returned before uncooperative SDK")
        let pending = Task {
            defer { returned.fulfill() }
            return try await provider.token()
        }
        await gate.waitUntilStarted()
        await deadline.waitUntilStarted()
        await deadline.release()
        await fulfillment(of: [returned], timeout: 2)
        do { _ = try await pending.value; XCTFail("Expected timeout") }
        catch { XCTAssertTrue(error as? MatrixOpenIDError == .requestTimedOut) }
        let recovered = try await provider.token()
        XCTAssertTrue(recovered.isValid)
        await gate.release() // Late completion must not replace the new cache.
        let cached = try await provider.token()
        XCTAssertTrue(cached.identity == recovered.identity)
        let count = await source.count
        XCTAssertEqual(count, 2)
        let timeouts = await observedTimeouts.values
        XCTAssertFalse(timeouts.isEmpty)
        XCTAssertTrue(timeouts.allSatisfy { $0 == 7 })
        await provider.shutdown()
    }

    func testAlreadyCancelledCallerDoesNotStartRefresh() async throws {
        let gate = OpenIDGate()
        let source = OpenIDSource()
        let provider = MatrixOpenIDTokenProvider { try await source.issue() }
        let pending = Task {
            _ = await gate.issue()
            return try await provider.token()
        }
        await gate.waitUntilStarted()
        pending.cancel()
        await gate.release()
        do { _ = try await pending.value; XCTFail("Expected caller cancellation") }
        catch { XCTAssertTrue(error is CancellationError) }
        let count = await source.count
        XCTAssertEqual(count, 0)
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
        let returned = expectation(description: "Shutdown releases pending callers")
        let pending = Task {
            defer { returned.fulfill() }
            return try await provider.token()
        }
        await gate.waitUntilStarted()
        await provider.shutdown()
        await fulfillment(of: [returned], timeout: 2)
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
    var bodies: [Data] = []
    var contentTypes: [String] = []
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
        let body = bodies.isEmpty ? nil : bodies.removeFirst()
        let contentType = contentTypes.isEmpty ? "application/problem+json; charset=utf-8" : contentTypes.removeFirst()
        lock.unlock()
        loader.complete(status, body: body, contentType: contentType)
    }
}

private final class OpenIDURLProtocol: URLProtocol {
    static var recorder: OpenIDRecorder?
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() { Self.recorder!.receive(self) }
    func complete(_ status: Int, body: Data? = nil, contentType: String = "application/problem+json; charset=utf-8") {
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status,
            httpVersion: "HTTP/1.1", headerFields: ["Content-Type": contentType])!, cacheStoragePolicy: .notAllowed)
        let payload = body ?? (status == 401 ? try! OpenIDFixtures.data("problem-auth-invalid-token") : Data("{}".utf8))
        client?.urlProtocol(self, didLoad: payload)
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

private actor OpenIDWaits {
    var values: [TimeInterval] = []
    func record(_ delay: TimeInterval) { values.append(delay) }
}
