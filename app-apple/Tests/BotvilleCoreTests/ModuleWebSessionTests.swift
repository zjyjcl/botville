// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import XCTest

@testable import BotvilleCore

#if canImport(WebKit)
    import WebKit
#endif

final class ModuleWebSessionTests: XCTestCase {
    func testOriginPolicyNormalizesCaseAndDefaultPortOnly() throws {
        let policy = ModuleWebNavigationPolicy(allowedOrigins: ["HTTPS://MODULE.Example.test:443"])
        XCTAssertEqual(
            policy.decision(for: try XCTUnwrap(URL(string: "https://module.example.test/path"))), .allow)
        XCTAssertEqual(
            policy.decision(for: try XCTUnwrap(URL(string: "https://module.example.test:444/path"))),
            .openExternally)
        XCTAssertEqual(
            policy.decision(for: try XCTUnwrap(URL(string: "https://child.module.example.test/path"))),
            .openExternally)
    }

    func testOriginPolicyRejectsEncodedAndWrongSchemeBypasses() throws {
        let policy = ModuleWebNavigationPolicy(allowedOrigins: ["https://module.example.test"])
        XCTAssertEqual(
            policy.decision(for: try XCTUnwrap(URL(string: "https://module.example.test%2Eevil.test/path"))),
            .openExternally)
        XCTAssertEqual(
            policy.decision(for: try XCTUnwrap(URL(string: "http://module.example.test/path"))),
            .openExternally)
        XCTAssertEqual(
            policy.decision(for: try XCTUnwrap(URL(string: "https://module.example.test.evil.test/path"))),
            .openExternally)
        XCTAssertEqual(
            policy.decision(for: try XCTUnwrap(URL(string: "https://mod%75le.example.test/path"))),
            .openExternally)
        XCTAssertEqual(
            policy.decision(for: try XCTUnwrap(URL(string: "https://module.example.test./path"))),
            .openExternally)
    }

    func testBridgeOnlyAcceptsMainFrameAtAllowedOrigin() throws {
        let policy = ModuleWebBridgePolicy(
            navigationPolicy: ModuleWebNavigationPolicy(allowedOrigins: ["https://module.example.test"]))
        let trusted = try XCTUnwrap(URL(string: "https://module.example.test/a"))
        XCTAssertEqual(
            policy.accepts(eventBody: "botville.session.renew", frameURL: trusted, isMainFrame: true), .renew)
        XCTAssertNil(
            policy.accepts(
                eventBody: ["name": "botville.session.renew"], frameURL: trusted, isMainFrame: false))
        XCTAssertNil(
            policy.accepts(
                eventBody: "botville.session.renew", frameURL: URL(string: "https://evil.test"),
                isMainFrame: true))
    }

    func testMockExchangeSetsSafeCookieOnce() async throws {
        let session = ModuleWebSession(
            capabilityID: "memory.search",
            launchURL: try XCTUnwrap(URL(string: "https://module.example.test/?code=opaque")),
            allowedOrigins: ["https://module.example.test"],
            expiresAt: Date().addingTimeInterval(60)
        )
        let exchange = MockModuleWebCookieExchange()
        try await exchange.exchange(launchURL: session.launchURL, for: session)
        let storedCookies = await exchange.storedCookies()
        let cookie = try XCTUnwrap(storedCookies.first)
        XCTAssertTrue(cookie.isHTTPOnly)
        XCTAssertTrue(cookie.isSecure)
        XCTAssertEqual(cookie.sameSite, "Lax")
        XCTAssertEqual(cookie.origin.host, "module.example.test")
        XCTAssertEqual(cookie.expiresAt, session.expiresAt)

        do {
            try await exchange.exchange(launchURL: session.launchURL, for: session)
            XCTFail("Expected one-time code")
        } catch ModuleWebCookieError.alreadyExchanged {
        }
    }

    func testMockExchangeRejectsExpiredCodeEvenWhenSessionIsStillValid() async throws {
        let session = ModuleWebSession(
            capabilityID: "memory.search",
            launchURL: try XCTUnwrap(URL(string: "https://module.example.test/?code=expired")),
            allowedOrigins: ["https://module.example.test"],
            codeExpiresAt: Date().addingTimeInterval(-1),
            expiresAt: Date().addingTimeInterval(300)
        )
        let exchange = MockModuleWebCookieExchange()

        do {
            try await exchange.exchange(launchURL: session.launchURL, for: session)
            XCTFail("Expected expired code")
        } catch ModuleWebCookieError.expiredCode {
        }
    }

    #if canImport(WebKit)
        @MainActor
        func testMockCookieWritesToNonPersistentWebKitStore() async throws {
            let dataStore = WKWebsiteDataStore.nonPersistent()
            let store = dataStore.httpCookieStore
            let sink = WebKitModuleWebCookieStore(store: store)
            let origin = try XCTUnwrap(ModuleWebOrigin(string: "https://module.example.test"))
            let cookie = ModuleWebCookie(
                name: "botville_session", value: "mock-cookie", origin: origin,
                expiresAt: Date().addingTimeInterval(60), isHTTPOnly: true,
                isSecure: true, sameSite: "Lax")
            await sink.store(cookie)
            let stored = await store.allCookies()
            let result = try XCTUnwrap(stored.first(where: { $0.name == "botville_session" }))
            XCTAssertEqual(result.domain, "module.example.test")
            XCTAssertTrue(result.isSecure)
            XCTAssertTrue(result.isHTTPOnly)
            XCTAssertGreaterThan(result.expiresDate ?? .distantPast, Date())
            withExtendedLifetime(dataStore) {}
        }
    #endif
}
