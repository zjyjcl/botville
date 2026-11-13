// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation
import XCTest

@testable import BotvilleCore

#if canImport(WebKit)
    import WebKit
#endif

final class MockContractConsistencyTests: XCTestCase {
    func testOpenAPIVersionAcceptsVariableInfoIndentationAndQuotedValues() throws {
        XCTAssertEqual(try ContractTestVersions.openAPIVersion(in: """
            openapi: 3.1.0
            info:
                title: Botville
                version: '0.6.0'
                description:
                  version: ignored
            paths: {}
            """), "0.6.0")
        XCTAssertEqual(try ContractTestVersions.openAPIVersion(in: """
            info:
             title: Botville
             version: 0.6.1
            paths: {}
            """), "0.6.1")
        XCTAssertEqual(try ContractTestVersions.openAPIVersion(in: """
            # Contract metadata follows.
            info:
              # This comment is not a mapping child.
              title: Botville
              version: "0.6.2"
            # The next top-level key ends info despite this comment.
            paths: {}
            """), "0.6.2")
    }

    func testOpenAPIVersionRejectsMissingOrAmbiguousDirectValues() {
        XCTAssertThrowsError(try ContractTestVersions.openAPIVersion(in: """
            info:
              title: Botville
            paths: {}
            """))
        XCTAssertThrowsError(try ContractTestVersions.openAPIVersion(in: """
            info:
              version: 0.6.0
              version: 0.6.1
            paths: {}
            """))
        XCTAssertThrowsError(try ContractTestVersions.openAPIVersion(in: """
            info:
              version: 0.6.0
              version:
            paths: {}
            """))
        XCTAssertThrowsError(try ContractTestVersions.openAPIVersion(in: """
            info:
              version: "0.6.0'
            paths: {}
            """))
    }

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
        openAPIVersion = try Self.openAPIVersion(in: yaml)
        manifestVersion = try XCTUnwrap(Self.manifest(
            at: "gateway/openapi/examples/capability-manifest.json")["manifest_version"] as? String)
    }

    static func manifest(at path: String) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(
            with: Data(contentsOf: root.appendingPathComponent(path))) as? [String: Any])
    }

    /// Reads only direct children of the top-level `info:` mapping. Keeping the
    /// exact child indentation avoids matching prose or nested YAML values; a
    /// contract source with zero or multiple versions must fail rather than
    /// silently selecting one.
    static func openAPIVersion(in yaml: String) throws -> String {
        let lines = yaml.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
        let infoIndices = lines.indices.filter { lines[$0].range(of: #"^info:[ \t]*$"#, options: .regularExpression) != nil }
        guard infoIndices.count == 1, let infoIndex = infoIndices.first else {
            throw ContractVersionParseError.ambiguous
        }

        func isBlankOrComment(_ line: String) -> Bool {
            let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
            return trimmed.isEmpty || trimmed.hasPrefix("#")
        }
        func isTopLevelKey(_ line: String) -> Bool {
            guard !isBlankOrComment(line) else { return false }
            return line.range(of: #"^[^ \t#:][^:]*:"#,
                              options: .regularExpression) != nil
        }
        func leadingIndentation(of line: String) -> String {
            String(line.prefix { $0 == " " || $0 == "\t" })
        }
        func isMappingKey(_ line: String) -> Bool {
            line.range(of: #"^[ \t]+[^ \t#][^:]*:"#,
                       options: .regularExpression) != nil
        }

        let block = lines.dropFirst(infoIndex + 1).prefix { !isTopLevelKey($0) }
        guard let firstChild = block.first(where: { !isBlankOrComment($0) && isMappingKey($0) }) else {
            throw ContractVersionParseError.ambiguous
        }
        let indentation = leadingIndentation(of: firstChild)
        guard !indentation.isEmpty else {
            throw ContractVersionParseError.ambiguous
        }
        let directChildren = block.compactMap { line -> String? in
            guard !isBlankOrComment(line), leadingIndentation(of: line) == indentation else { return nil }
            let value = String(line.dropFirst(indentation.count))
            return value.range(of: #"^[^ \t#][^:]*:"#,
                               options: .regularExpression) == nil ? nil : value
        }
        let versionDeclarations = directChildren.filter {
            $0.range(of: #"^version:[ \t]*"#, options: .regularExpression) != nil
        }
        guard versionDeclarations.count == 1, let declaration = versionDeclarations.first else {
            throw ContractVersionParseError.ambiguous
        }
        let pattern = #"^version:[ \t]*(?:"([^"]+)"|'([^']+)'|([^\s#"']+))[ \t]*(?:#.*)?$"#
        let matches = try NSRegularExpression(pattern: pattern).matches(
            in: declaration, range: NSRange(declaration.startIndex..., in: declaration))
        guard matches.count == 1, let match = matches.first else {
            throw ContractVersionParseError.ambiguous
        }
        for capture in 1...3 where match.range(at: capture).location != NSNotFound {
            guard let range = Range(match.range(at: capture), in: declaration) else { continue }
            return String(declaration[range])
        }
        throw ContractVersionParseError.ambiguous
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

private enum ContractVersionParseError: Error { case ambiguous }
