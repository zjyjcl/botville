// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation
import XCTest
@testable import BotvilleCore

final class PushNotificationContentTests: XCTestCase {
    override func tearDown() {
        URLProtocolStub.handler = nil
        URLProtocolStub.onStopLoading = nil
        super.tearDown()
    }

    func testStubCancellationKeepsStartingRequestCallback() {
        let stopped = expectation(description: "original request cancelled once")
        URLProtocolStub.handler = { _, _ in }
        URLProtocolStub.onStopLoading = { stopped.fulfill() }
        let stub = URLProtocolStub(request: URLRequest(url: URL(string: "https://matrix.test/event")!),
                                   cachedResponse: nil, client: nil)
        stub.startLoading()
        URLProtocolStub.onStopLoading = { XCTFail("late cancellation must not enter a later test's callback") }
        stub.stopLoading()
        stub.stopLoading()
        wait(for: [stopped], timeout: 1)
    }

    func testEventReferenceAcceptsSygnalTopLevelAndKnownWrappersWithoutMixing() {
        let expected = PushEventReference(roomID: "!room:example.test", eventID: "$event:example.test")
        for payload: [AnyHashable: Any] in [
            ["room_id": expected.roomID, "event_id": expected.eventID],
            ["notification": ["room_id": expected.roomID, "event_id": expected.eventID]],
            ["data": ["room_id": expected.roomID, "event_id": expected.eventID]],
            ["data": ["notification": ["room_id": expected.roomID, "event_id": expected.eventID]]],
            ["notification": ["data": ["room_id": expected.roomID, "event_id": expected.eventID]]]
        ] {
            XCTAssertEqual(PushEventReference.parse(payload), expected)
        }
        XCTAssertNil(PushEventReference.parse(["room_id": expected.roomID, "notification": ["event_id": expected.eventID]]))
        XCTAssertNil(PushEventReference.parse(["room_id": "room", "event_id": expected.eventID]))
        XCTAssertNil(PushEventReference.parse(["room_id": expected.roomID, "event_id": "event"]))
        XCTAssertNil(PushEventReference.parse(["room_id": 3, "event_id": expected.eventID]))
        XCTAssertNil(PushEventReference.parse(["room_id": expected.roomID, "event_id": ""]))
        XCTAssertNil(PushEventReference.parse(["room_id": "!room\nexample.test", "event_id": expected.eventID]))
        XCTAssertEqual(PushEventReference.parse([
            "room_id": expected.roomID, "event_id": expected.eventID,
            "notification": ["room_id": "!other:example.test", "event_id": "$other:example.test"]
        ]), expected)
    }

    func testEventParsingShowsMessageSenderAndBoundsText() throws {
        let reference = PushEventReference(roomID: "!room:example.test", eventID: "$event:example.test")
        let message = String(repeating: "文", count: 300)
        let data = try JSONSerialization.data(withJSONObject: [
            "type": "m.room.message", "event_id": reference.eventID, "room_id": reference.roomID,
            "sender": "@alice:example.test", "content": ["msgtype": "m.text", "body": message]
        ])
        let text = try XCTUnwrap(PushNotificationText.parseEvent(data, reference: reference))
        XCTAssertEqual(text.title, "@alice:example.test")
        XCTAssertEqual(text.body.count, 240)
        XCTAssertTrue(text.body.hasSuffix("…"))
    }

    func testEventParsingUsesEncryptedFallbackForEncryptedAndUnrenderableEvents() throws {
        let reference = PushEventReference(roomID: "!room:example.test", eventID: "$event:example.test")
        for (event, expectedTitle): ([String: Any], String) in [
            (["type": "m.room.encrypted", "sender": "@alice:example.test", "content": ["body": "secret"]], "@alice:example.test"),
            (["type": "m.room.message", "content": [:]], reference.roomID),
            (["type": "m.room.member", "content": ["body": "joined"]], reference.roomID)
        ] {
            let text = try XCTUnwrap(PushNotificationText.parseEvent(JSONSerialization.data(withJSONObject: event), reference: reference))
            XCTAssertEqual(text.title, expectedTitle)
            XCTAssertEqual(text.body, "加密消息")
        }
        XCTAssertNil(PushNotificationText.parseEvent(Data("not-json".utf8), reference: reference))
    }

    func testLoaderRequestsOnlyEncodedEventGETAndRendersResponse() throws {
        let reference = PushEventReference(roomID: "!room/%+测试:example.test", eventID: "$event/#%+测试:example.test")
        let expectedPath = "/base/_matrix/client/v3/rooms/%21room%2F%25%2B%E6%B5%8B%E8%AF%95%3Aexample.test/event/%24event%2F%23%25%2B%E6%B5%8B%E8%AF%95%3Aexample.test"
        URLProtocolStub.handler = { request, protocolStub in
            XCTAssertEqual(request.httpMethod, "GET")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Accept"), "application/json")
            XCTAssertTrue(request.value(forHTTPHeaderField: "Authorization") == "Bearer test-only-value")
            let response = HTTPURLResponse(url: try XCTUnwrap(request.url), statusCode: 200, httpVersion: nil, headerFields: nil)!
            protocolStub.client?.urlProtocol(protocolStub, didReceive: response, cacheStoragePolicy: .notAllowed)
            if request.url?.path(percentEncoded: true).contains("/_matrix/client/v3/profile/") == true {
                protocolStub.client?.urlProtocol(protocolStub, didLoad: Data("{}".utf8))
            } else {
                XCTAssertEqual(request.url?.path(percentEncoded: true), expectedPath)
                let event: [String: Any] = ["type": "m.room.message", "sender": "@bob:example.test", "content": ["body": "hello"]]
                protocolStub.client?.urlProtocol(protocolStub, didLoad: try JSONSerialization.data(withJSONObject: event))
            }
            protocolStub.client?.urlProtocolDidFinishLoading(protocolStub)
        }
        let text = try load(reference: reference, store: InMemorySharedPushCredentialsStore(credentials(homeserver: "https://example.test/base")))
        XCTAssertEqual(text, PushNotificationText(title: "@bob:example.test", body: "hello"))
    }

    func testLoaderUsesEncodedDisplaynameProfileAfterEvent() throws {
        let reference = PushEventReference(roomID: "!room:example.test", eventID: "$event:example.test")
        var profileRequests = 0
        URLProtocolStub.handler = { request, protocolStub in
            XCTAssertEqual(request.httpMethod, "GET")
            XCTAssertTrue(request.value(forHTTPHeaderField: "Authorization") == "Bearer test-only-value")
            let response = HTTPURLResponse(url: try XCTUnwrap(request.url), statusCode: 200, httpVersion: nil, headerFields: nil)!
            if request.url?.path(percentEncoded: true).contains("/_matrix/client/v3/profile/%40bob%2F%25%2B%E6%B5%8B%E8%AF%95%3Aexample.test") == true {
                profileRequests += 1
                protocolStub.client?.urlProtocol(protocolStub, didReceive: response, cacheStoragePolicy: .notAllowed)
                protocolStub.client?.urlProtocol(protocolStub, didLoad: try JSONSerialization.data(withJSONObject: ["displayname": "  博布  "]))
            } else {
                let event: [String: Any] = ["type": "m.room.message", "sender": "@bob/%+测试:example.test", "content": ["body": "hello"]]
                protocolStub.client?.urlProtocol(protocolStub, didReceive: response, cacheStoragePolicy: .notAllowed)
                protocolStub.client?.urlProtocol(protocolStub, didLoad: try JSONSerialization.data(withJSONObject: event))
            }
            protocolStub.client?.urlProtocolDidFinishLoading(protocolStub)
        }
        let text = try load(reference: reference, store: InMemorySharedPushCredentialsStore(credentials()))
        XCTAssertEqual(profileRequests, 1)
        XCTAssertEqual(text, PushNotificationText(title: "博布", body: "hello"))
        let longDisplayname = String(repeating: "名", count: 121)
        XCTAssertEqual(PushNotificationText(title: "@bob:example.test", body: "hello").replacingTitle(longDisplayname).title?.count, 120)
    }

    func testLoaderKeepsEventTextWhenProfileFailsOrHasNoDisplayname() throws {
        let reference = standardReference
        for profileResponse: (Int, Any?) in [(401, nil), (302, nil), (200, [:]), (200, ["displayname": " \n "]), (200, ["displayname": 3])] {
            URLProtocolStub.handler = { request, protocolStub in
                let profile = request.url?.path(percentEncoded: true).contains("/_matrix/client/v3/profile/") == true
                let response = HTTPURLResponse(url: try XCTUnwrap(request.url), statusCode: profile ? profileResponse.0 : 200, httpVersion: nil, headerFields: nil)!
                protocolStub.client?.urlProtocol(protocolStub, didReceive: response, cacheStoragePolicy: .notAllowed)
                if profile {
                    if let payload = profileResponse.1 {
                        protocolStub.client?.urlProtocol(protocolStub, didLoad: try JSONSerialization.data(withJSONObject: payload))
                    }
                } else {
                    let event: [String: Any] = ["type": "m.room.message", "sender": "@alice:example.test", "content": ["body": "ready"]]
                    protocolStub.client?.urlProtocol(protocolStub, didLoad: try JSONSerialization.data(withJSONObject: event))
                }
                protocolStub.client?.urlProtocolDidFinishLoading(protocolStub)
            }
            XCTAssertEqual(try load(reference: reference, store: InMemorySharedPushCredentialsStore(credentials())), PushNotificationText(title: "@alice:example.test", body: "ready"))
        }
    }

    func testLoaderKeepsEventTextWhenProfileTimesOutOrReturnsInvalidJSON() throws {
        for failure: (URLProtocolStub) -> Void in [
            { $0.client?.urlProtocol($0, didFailWithError: URLError(.timedOut)) },
            { $0.client?.urlProtocol($0, didLoad: Data("not-json".utf8)); $0.client?.urlProtocolDidFinishLoading($0) }
        ] {
            URLProtocolStub.handler = { request, protocolStub in
                if request.url?.path(percentEncoded: true).contains("/_matrix/client/v3/profile/") == true {
                    failure(protocolStub)
                    return
                }
                let response = HTTPURLResponse(url: try XCTUnwrap(request.url), statusCode: 200, httpVersion: nil, headerFields: nil)!
                let event: [String: Any] = ["type": "m.room.message", "sender": "@alice:example.test", "content": ["body": "ready"]]
                protocolStub.client?.urlProtocol(protocolStub, didReceive: response, cacheStoragePolicy: .notAllowed)
                protocolStub.client?.urlProtocol(protocolStub, didLoad: try JSONSerialization.data(withJSONObject: event))
                protocolStub.client?.urlProtocolDidFinishLoading(protocolStub)
            }
            XCTAssertEqual(try load(reference: standardReference, store: InMemorySharedPushCredentialsStore(credentials())), PushNotificationText(title: "@alice:example.test", body: "ready"))
        }
    }

    func testLoaderDegradesForHTTPNetworkInvalidJSONAndRedirect() throws {
        let reference = standardReference
        for status in [401, 403, 404, 500, 302] {
            URLProtocolStub.handler = { request, protocolStub in
                let response = HTTPURLResponse(url: try XCTUnwrap(request.url), statusCode: status, httpVersion: nil, headerFields: nil)!
                protocolStub.client?.urlProtocol(protocolStub, didReceive: response, cacheStoragePolicy: .notAllowed)
                protocolStub.client?.urlProtocolDidFinishLoading(protocolStub)
            }
            XCTAssertEqual(try load(reference: reference, store: InMemorySharedPushCredentialsStore(credentials())), .unavailable)
        }
        URLProtocolStub.handler = { _, protocolStub in protocolStub.client?.urlProtocol(protocolStub, didFailWithError: URLError(.notConnectedToInternet)) }
        XCTAssertEqual(try load(reference: reference, store: InMemorySharedPushCredentialsStore(credentials())), .unavailable)
        URLProtocolStub.handler = { _, protocolStub in protocolStub.client?.urlProtocol(protocolStub, didFailWithError: URLError(.timedOut)) }
        XCTAssertEqual(try load(reference: reference, store: InMemorySharedPushCredentialsStore(credentials())), .unavailable)
        URLProtocolStub.handler = { request, protocolStub in
            let response = HTTPURLResponse(url: try XCTUnwrap(request.url), statusCode: 200, httpVersion: nil, headerFields: nil)!
            protocolStub.client?.urlProtocol(protocolStub, didReceive: response, cacheStoragePolicy: .notAllowed)
            protocolStub.client?.urlProtocol(protocolStub, didLoad: Data("bad".utf8))
            protocolStub.client?.urlProtocolDidFinishLoading(protocolStub)
        }
        XCTAssertEqual(try load(reference: reference, store: InMemorySharedPushCredentialsStore(credentials())), .unavailable)
    }

    func testLoaderUsesPlaceholderForMissingPayloadCredentialsAndExpiry() throws {
        let store = InMemorySharedPushCredentialsStore()
        XCTAssertEqual(try load(payload: [:], store: store), .placeholder)
        XCTAssertEqual(try load(reference: standardReference, store: store), .placeholder)
        XCTAssertEqual(try load(reference: standardReference, store: ThrowingCredentialsStore()), .placeholder)
        let loader = PushNotificationLoader(credentialsStore: InMemorySharedPushCredentialsStore(credentials()), configuration: stubConfiguration, timeBudget: 8)
        let result = expectation(description: "expired")
        var values = [PushNotificationText]()
        loader.load(userInfo: payload(for: standardReference)) { value in values.append(value); result.fulfill() }
        loader.expire()
        loader.expire()
        wait(for: [result], timeout: 1)
        XCTAssertEqual(values, [.placeholder])
    }

    func testLoaderExpiryBeforeLoadStillDeliversOnePlaceholder() {
        let loader = PushNotificationLoader(credentialsStore: InMemorySharedPushCredentialsStore(credentials()), configuration: stubConfiguration)
        loader.expire()
        let result = expectation(description: "already expired")
        var values = [PushNotificationText]()
        loader.load(userInfo: payload(for: standardReference)) { value in values.append(value); result.fulfill() }
        wait(for: [result], timeout: 1)
        XCTAssertEqual(values, [.placeholder])
    }

    func testLoaderRendersEncryptedAndUnrenderableHTTPResponses() throws {
        for event: [String: Any] in [
            ["type": "m.room.encrypted", "sender": "@alice:example.test", "content": ["body": "not-visible"]],
            ["type": "m.room.message", "content": [:]]
        ] {
            URLProtocolStub.handler = { request, protocolStub in
                let response = HTTPURLResponse(url: try XCTUnwrap(request.url), statusCode: 200, httpVersion: nil, headerFields: nil)!
                protocolStub.client?.urlProtocol(protocolStub, didReceive: response, cacheStoragePolicy: .notAllowed)
                protocolStub.client?.urlProtocol(protocolStub, didLoad: try JSONSerialization.data(withJSONObject: event))
                protocolStub.client?.urlProtocolDidFinishLoading(protocolStub)
            }
            XCTAssertEqual(try load(reference: standardReference, store: InMemorySharedPushCredentialsStore(credentials())).body, "加密消息")
        }
    }

    func testRedirectDelegateAlwaysCancelsRedirect() {
        let loader = PushNotificationLoader(credentialsStore: InMemorySharedPushCredentialsStore(credentials()), configuration: stubConfiguration)
        let session = URLSession(configuration: .ephemeral)
        let task = session.dataTask(with: URL(string: "https://example.test")!)
        let response = HTTPURLResponse(url: URL(string: "https://example.test")!, statusCode: 302, httpVersion: nil, headerFields: ["Location": "https://elsewhere.test"])!
        let result = expectation(description: "redirect cancelled")
        loader.urlSession(session, task: task, willPerformHTTPRedirection: response, newRequest: URLRequest(url: URL(string: "https://elsewhere.test")!)) { request in
            XCTAssertNil(request)
            result.fulfill()
        }
        wait(for: [result], timeout: 1)
        session.invalidateAndCancel()
    }

    func testLateResponseAfterExpiryDoesNotDeliverTwice() throws {
        let started = expectation(description: "request started")
        let cancelled = expectation(description: "request cancelled")
        var deferred: URLProtocolStub?
        URLProtocolStub.handler = { _, protocolStub in deferred = protocolStub; started.fulfill() }
        URLProtocolStub.onStopLoading = { cancelled.fulfill() }
        let loader = PushNotificationLoader(credentialsStore: InMemorySharedPushCredentialsStore(credentials()), configuration: stubConfiguration, timeBudget: 8)
        let result = expectation(description: "expiry")
        let noSecondResult = expectation(description: "no second result")
        noSecondResult.isInverted = true
        var values = [PushNotificationText]()
        loader.load(userInfo: payload(for: standardReference)) { value in
            values.append(value)
            values.count == 1 ? result.fulfill() : noSecondResult.fulfill()
        }
        wait(for: [started], timeout: 1)
        loader.expire()
        wait(for: [result], timeout: 1)
        wait(for: [cancelled], timeout: 1)
        let stub = try XCTUnwrap(deferred)
        let url = try XCTUnwrap(stub.request.url)
        let response = HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: nil)!
        stub.client?.urlProtocol(stub, didReceive: response, cacheStoragePolicy: .notAllowed)
        stub.client?.urlProtocol(stub, didLoad: try JSONSerialization.data(withJSONObject: ["type": "m.room.message", "content": ["body": "late"]]))
        stub.client?.urlProtocolDidFinishLoading(stub)
        wait(for: [noSecondResult], timeout: 0.1)
        XCTAssertEqual(values, [.placeholder])
    }

    func testSuccessfulResponseThenExpiryDoesNotReplaceContent() throws {
        URLProtocolStub.handler = { request, protocolStub in
            let response = HTTPURLResponse(url: try XCTUnwrap(request.url), statusCode: 200, httpVersion: nil, headerFields: nil)!
            protocolStub.client?.urlProtocol(protocolStub, didReceive: response, cacheStoragePolicy: .notAllowed)
            protocolStub.client?.urlProtocol(protocolStub, didLoad: try JSONSerialization.data(withJSONObject: ["type": "m.room.message", "content": ["body": "ready"]]))
            protocolStub.client?.urlProtocolDidFinishLoading(protocolStub)
        }
        let loader = PushNotificationLoader(credentialsStore: InMemorySharedPushCredentialsStore(credentials()), configuration: stubConfiguration)
        let result = expectation(description: "content")
        var values = [PushNotificationText]()
        loader.load(userInfo: payload(for: standardReference)) { values.append($0); result.fulfill() }
        wait(for: [result], timeout: 1)
        loader.expire()
        XCTAssertEqual(values, [PushNotificationText(title: standardReference.roomID, body: "ready")])
    }

    func testProfileDeadlineKeepsLoadedEventTextAndDeliversOnce() throws {
        let profileStarted = expectation(description: "profile started")
        let noSecondResult = expectation(description: "no second result")
        noSecondResult.isInverted = true
        var deferredProfile: URLProtocolStub?
        URLProtocolStub.handler = { request, protocolStub in
            if request.url?.path(percentEncoded: true).contains("/_matrix/client/v3/profile/") == true {
                deferredProfile = protocolStub
                profileStarted.fulfill()
                return
            }
            let response = HTTPURLResponse(url: try XCTUnwrap(request.url), statusCode: 200, httpVersion: nil, headerFields: nil)!
            let event: [String: Any] = ["type": "m.room.message", "sender": "@alice:example.test", "content": ["body": "ready"]]
            protocolStub.client?.urlProtocol(protocolStub, didReceive: response, cacheStoragePolicy: .notAllowed)
            protocolStub.client?.urlProtocol(protocolStub, didLoad: try JSONSerialization.data(withJSONObject: event))
            protocolStub.client?.urlProtocolDidFinishLoading(protocolStub)
        }
        let loader = PushNotificationLoader(credentialsStore: InMemorySharedPushCredentialsStore(credentials()), configuration: stubConfiguration, timeBudget: 8)
        let result = expectation(description: "event fallback")
        var values = [PushNotificationText]()
        loader.load(userInfo: payload(for: standardReference)) { value in
            values.append(value)
            values.count == 1 ? result.fulfill() : noSecondResult.fulfill()
        }
        wait(for: [profileStarted], timeout: 1)
        loader.expire()
        wait(for: [result], timeout: 1)
        let profile = try XCTUnwrap(deferredProfile)
        let response = HTTPURLResponse(url: try XCTUnwrap(profile.request.url), statusCode: 200, httpVersion: nil, headerFields: nil)!
        profile.client?.urlProtocol(profile, didReceive: response, cacheStoragePolicy: .notAllowed)
        profile.client?.urlProtocol(profile, didLoad: try JSONSerialization.data(withJSONObject: ["displayname": "Alice"]))
        profile.client?.urlProtocolDidFinishLoading(profile)
        wait(for: [noSecondResult], timeout: 0.1)
        XCTAssertEqual(values, [PushNotificationText(title: "@alice:example.test", body: "ready")])
    }

    func testNaturalProfileDeadlineKeepsLoadedEventTextWithinRemainingBudget() throws {
        let profileStarted = expectation(description: "profile started")
        var profileTimeout: TimeInterval?
        URLProtocolStub.handler = { request, protocolStub in
            if request.url?.path(percentEncoded: true).contains("/_matrix/client/v3/profile/") == true {
                profileTimeout = request.timeoutInterval
                profileStarted.fulfill()
                return
            }
            let response = HTTPURLResponse(url: try XCTUnwrap(request.url), statusCode: 200, httpVersion: nil, headerFields: nil)!
            let event: [String: Any] = ["type": "m.room.message", "sender": "@alice:example.test", "content": ["body": "ready"]]
            protocolStub.client?.urlProtocol(protocolStub, didReceive: response, cacheStoragePolicy: .notAllowed)
            protocolStub.client?.urlProtocol(protocolStub, didLoad: try JSONSerialization.data(withJSONObject: event))
            protocolStub.client?.urlProtocolDidFinishLoading(protocolStub)
        }
        let loader = PushNotificationLoader(credentialsStore: InMemorySharedPushCredentialsStore(credentials()), configuration: stubConfiguration, requestTimeout: 8, timeBudget: 0.05)
        let result = expectation(description: "natural event fallback")
        var received: PushNotificationText?
        loader.load(userInfo: payload(for: standardReference)) { received = $0; result.fulfill() }
        wait(for: [profileStarted, result], timeout: 1)
        XCTAssertLessThanOrEqual(try XCTUnwrap(profileTimeout), 0.05)
        XCTAssertEqual(received, PushNotificationText(title: "@alice:example.test", body: "ready"))
    }

    func testLoaderRejectsInvalidHomeserverAndBoundsTimeout() throws {
        for homeserver in ["ftp://example.test", "https://user@example.test", "https://example.test?x=y"] {
            XCTAssertEqual(try load(reference: standardReference, store: InMemorySharedPushCredentialsStore(credentials(homeserver: homeserver))), .unavailable)
        }
        URLProtocolStub.handler = { _, _ in }
        // Only the budget may expire within the one-second assertion window.
        // Equal 10 ms request/budget timers race to return unavailable/placeholder.
        let loader = PushNotificationLoader(credentialsStore: InMemorySharedPushCredentialsStore(credentials()), configuration: stubConfiguration, requestTimeout: 8, timeBudget: 0.01)
        let result = expectation(description: "budget")
        loader.load(userInfo: payload(for: standardReference)) { XCTAssertEqual($0, .placeholder); result.fulfill() }
        wait(for: [result], timeout: 1)
    }

    func testLoaderClampsRequestTimeoutAndMapsTimedOutToUnavailable() throws {
        for (configured, expected) in [(0.0, 0.01), (60.0, 8.0)] {
            let requestSeen = expectation(description: "request carries clamped timeout")
            URLProtocolStub.handler = { request, protocolStub in
                XCTAssertEqual(request.timeoutInterval, expected)
                requestSeen.fulfill()
                // Trigger the error path directly, without waiting for a real
                // URLSession timeout to compete with the delivery deadline.
                protocolStub.client?.urlProtocol(protocolStub, didFailWithError: URLError(.timedOut))
            }
            let loader = PushNotificationLoader(credentialsStore: InMemorySharedPushCredentialsStore(credentials()),
                                                configuration: stubConfiguration, requestTimeout: configured, timeBudget: 10)
            let result = expectation(description: "request timeout is unavailable")
            loader.load(userInfo: payload(for: standardReference)) {
                XCTAssertEqual($0, .unavailable)
                result.fulfill()
            }
            wait(for: [requestSeen, result], timeout: 1)
        }
    }

    private var standardReference: PushEventReference { PushEventReference(roomID: "!room:example.test", eventID: "$event:example.test") }
    private var stubConfiguration: URLSessionConfiguration { let c = URLSessionConfiguration.ephemeral; c.protocolClasses = [URLProtocolStub.self]; return c }
    private func payload(for reference: PushEventReference) -> [AnyHashable: Any] { ["room_id": reference.roomID, "event_id": reference.eventID] }
    private func credentials(homeserver: String = "https://example.test") -> SharedPushCredentials { SharedPushCredentials(homeserverURL: homeserver, userID: "@test:example.test", accessToken: "test-only-value", deviceID: "DEVICE") }
    private func load(reference: PushEventReference, store: any SharedPushCredentialsStore) throws -> PushNotificationText { try load(payload: payload(for: reference), store: store) }
    private func load(payload: [AnyHashable: Any], store: any SharedPushCredentialsStore) throws -> PushNotificationText {
        let loader = PushNotificationLoader(credentialsStore: store, configuration: stubConfiguration)
        let result = expectation(description: "load")
        var received: PushNotificationText?
        loader.load(userInfo: payload) { received = $0; result.fulfill() }
        wait(for: [result], timeout: 1)
        return try XCTUnwrap(received)
    }
}

private final class URLProtocolStub: URLProtocol {
    @TestLocked static var handler: ((URLRequest, URLProtocolStub) throws -> Void)? = nil
    @TestLocked static var onStopLoading: (() -> Void)? = nil
    @TestLocked private var stopHandler: (() -> Void)? = nil
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        stopHandler = Self.onStopLoading
        let handler = Self.handler
        do { try handler?(request, self) }
        catch { client?.urlProtocol(self, didFailWithError: error) }
    }
    override func stopLoading() {
        let callback = $stopHandler.withValue { value in
            let callback = value
            value = nil
            return callback
        }
        callback?()
    }
}

private struct ThrowingCredentialsStore: SharedPushCredentialsStore {
    func load() throws -> SharedPushCredentials? { throw SharedPushCredentialsStoreError.unavailable }
    func save(_ credentials: SharedPushCredentials) throws {}
    func delete() throws {}
}
