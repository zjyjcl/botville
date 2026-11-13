// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation
import XCTest

@testable import BotvilleCore

#if canImport(WebKit)
    import WebKit
#endif

final class MockContractConsistencyTests: XCTestCase {
    func testBundledExamplesStayIdenticalToGatewayContractExamples() throws {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        for name in ["capability-manifest", "card", "persona-page", "persona-state"] {
            let bundled = root.appendingPathComponent(
                "app-apple/Sources/BotvilleCore/Resources/mock-\(name).json")
            let contract = root.appendingPathComponent("gateway/openapi/examples/\(name).json")
            XCTAssertEqual(
                try Data(contentsOf: bundled), try Data(contentsOf: contract),
                "Keep the App mock and the gateway \(name) example synchronized")
        }
    }

    func testManifestVersionsMatchCurrentContractAndRuntimeManifest() async throws {
        let versions = try ContractTestVersions()
        XCTAssertEqual(versions.openAPIVersion, versions.contractVersion)
        for path in ["gateway/openapi/examples/capability-manifest.json",
                     "app-apple/Sources/BotvilleCore/Resources/mock-capability-manifest.json"] {
            let object = try ContractTestVersions.manifest(at: path)
            XCTAssertEqual(object["contract_version"] as? String, versions.contractVersion)
            XCTAssertEqual(object["manifest_version"] as? String, versions.manifestVersion)
        }
        let runtime = try await MockGatewayClient().capabilities()
        XCTAssertEqual(runtime.contractVersion, versions.contractVersion)
        XCTAssertEqual(runtime.manifestVersion, versions.manifestVersion)
    }

    @MainActor
    func testViewLogsActionCreatesSessionAndLoadsOfflineLogPage() async throws {
        let client = MockGatewayClient()
        let cards = try await client.cards()
        let card = try XCTUnwrap(cards.first)
        let action = try XCTUnwrap(card.actions.first(where: { $0.id == "open" }))
        XCTAssertEqual(action.label, "查看日志")
        XCTAssertEqual(action.effectiveRisk(cardRisk: card.effectiveRisk), .low)
        XCTAssertEqual(GatewayActionSelectionPolicy.decision(for: card, action: action), .perform)

        let manifest = try await client.capabilities()
        let projection = try XCTUnwrap(
            manifest.capabilityProjections(knownNativeRoutes: ["chat.direct": "botville://chat"])
                .first(where: { $0.capability.id == action.capabilityID }))
        XCTAssertTrue(projection.isAvailable)
        let capability = projection.capability
        XCTAssertEqual(capability.entry.type, "card_list")
        XCTAssertEqual(capability.sessionToken?.supported, true)
        let entryURL = try XCTUnwrap(capability.entry.webURL)
        let url = try XCTUnwrap(action.url)
        XCTAssertEqual(ModuleWebOrigin(url: entryURL), ModuleWebOrigin(url: url))
        let grant = try await client.createModuleWebSession(
            capabilityID: capability.id, url: url, allowedOrigins: [], scopes: capability.scopes)
        let session = ModuleWebSession(grant: grant)
        XCTAssertTrue(session.isValid())
        XCTAssertEqual(
            ModuleWebNavigationPolicy(allowedOrigins: session.allowedOrigins).decision(for: url), .allow)
        let code = try XCTUnwrap(session.oneTimeCode)
        let receipt = try await client.exchangeSessionCookie(capabilityID: capability.id, code: code)
        XCTAssertEqual(receipt.statusCode, 204)
        do {
            _ = try await client.exchangeSessionCookie(capabilityID: capability.id, code: code)
            XCTFail("The log session code must be single-use")
        } catch GatewayClientError.invalidResponse {}

        // This follows the app's offline mock path, not a live Set-Cookie exchange.
        let exchange = MockModuleWebCookieExchange()
        try await exchange.exchange(launchURL: session.launchURL, for: session)
        let cookies = await exchange.storedCookies()
        let cookie = try XCTUnwrap(cookies.first)
        XCTAssertTrue(cookie.isHTTPOnly)
        XCTAssertTrue(cookie.isSecure)
        XCTAssertEqual(cookie.sameSite, "Lax")
        let html = client.mockModuleWebHTML(for: session)
        XCTAssertTrue(html.contains("部署日志"))
        XCTAssertFalse(html.contains(code), "The offline page must not render the session code")

        #if canImport(WebKit)
            let configuration = WKWebViewConfiguration()
            configuration.websiteDataStore = .nonPersistent()
            let store = configuration.websiteDataStore.httpCookieStore
            await WebKitModuleWebCookieStore(store: store).store(cookie)
            let stored = await store.allCookies()
            let saved = try XCTUnwrap(stored.first(where: { $0.name == "botville_session" }))
            XCTAssertTrue(saved.isHTTPOnly)
            XCTAssertTrue(saved.isSecure)
            XCTAssertEqual(saved.domain, cookie.origin.host)

            let webView = WKWebView(frame: .zero, configuration: configuration)
            let loaded = expectation(description: "Offline deployment log page rendered")
            let delegate = MockLogPageDelegate(loaded: loaded)
            webView.navigationDelegate = delegate
            webView.loadHTMLString(html, baseURL: url)
            await fulfillment(of: [loaded], timeout: 15)
            XCTAssertNil(delegate.loadError)
            let heading = try await webView.evaluateJavaScript("document.querySelector('h1').textContent")
            XCTAssertEqual(heading as? String, "模拟部署日志")
            withExtendedLifetime(delegate) {}
        #endif
    }
}

#if canImport(WebKit)
    @MainActor
    private final class MockLogPageDelegate: NSObject, WKNavigationDelegate {
        let loaded: XCTestExpectation
        var loadError: Error?

        init(loaded: XCTestExpectation) { self.loaded = loaded }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) { loaded.fulfill() }

        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
            loadError = error
            loaded.fulfill()
        }

        func webView(
            _ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
            withError error: Error
        ) {
            loadError = error
            loaded.fulfill()
        }
    }
#endif

/// Read the checked-in contract sources without importing the gateway runtime.
/// Missing or ambiguous declarations fail the test instead of supplying a default.
struct ContractTestVersions {
    static let root = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().deletingLastPathComponent()

    let contractVersion: String
    let openAPIVersion: String
    let manifestVersion: String

    init() throws {
        let config = try String(contentsOf: Self.root.appendingPathComponent(
            "gateway/src/botville_gateway/config.py"), encoding: .utf8)
        contractVersion = try Self.capture(
            #"(?m)^CONTRACT_VERSION\s*=\s*["']([^"']+)["']\s*(?:#.*)?$"#, in: config)
        let yaml = try String(contentsOf: Self.root.appendingPathComponent(
            "gateway/openapi/gateway.v1.yaml"), encoding: .utf8)
        let info = try Self.capture(#"(?m)^info:[ \t]*\n((?:[ \t]+[^\n]*\n|\n)+)"#, in: yaml)
        openAPIVersion = try Self.capture(
            #"(?m)^  version:[ \t]*["']?([^\s"']+)["']?[ \t]*$"#, in: info)
        manifestVersion = try XCTUnwrap(Self.manifest(
            at: "gateway/openapi/examples/capability-manifest.json")["manifest_version"] as? String)
    }

    static func manifest(at path: String) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(
            with: Data(contentsOf: root.appendingPathComponent(path))) as? [String: Any])
    }

    private static func capture(_ pattern: String, in source: String) throws -> String {
        let matches = try NSRegularExpression(pattern: pattern).matches(
            in: source, range: NSRange(source.startIndex..., in: source))
        XCTAssertEqual(matches.count, 1, "Expected one contract declaration matching \(pattern)")
        let match = try XCTUnwrap(matches.count == 1 ? matches.first : nil)
        let range = try XCTUnwrap(Range(match.range(at: 1), in: source))
        return String(source[range])
    }
}
