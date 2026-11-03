// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation

#if canImport(WebKit)
    import WebKit

    /// The injectable sink used by the mock cookie-exchange path. In production the
    /// module page receives `Set-Cookie` directly from its own exchange request;
    /// keeping this adapter documents the only permitted cookie destination.
    public final class WebKitModuleWebCookieStore: @unchecked Sendable, ModuleWebCookieStoring {
        private let store: WKHTTPCookieStore

        public init(store: WKHTTPCookieStore) {
            self.store = store
        }

        public func store(_ cookie: ModuleWebCookie) async {
            var properties: [HTTPCookiePropertyKey: Any] = [
                .name: cookie.name, .value: cookie.value,
                .domain: cookie.origin.host, .path: "/",
                .secure: cookie.isSecure ? "TRUE" : "FALSE",
                .expires: cookie.expiresAt,
            ]
            // Match the server's secure cookie attributes in the offline mock.
            // The live page receives Set-Cookie directly through WebKit.
            properties[HTTPCookiePropertyKey("SameSite")] = cookie.sameSite
            properties[HTTPCookiePropertyKey("HttpOnly")] = cookie.isHTTPOnly ? "TRUE" : "FALSE"
            guard let webCookie = HTTPCookie(properties: properties) else {
                return
            }
            await withCheckedContinuation { continuation in
                DispatchQueue.main.async { [store] in
                    store.setCookie(webCookie) { continuation.resume() }
                }
            }
        }
    }
#endif

/// An origin, rather than an arbitrary URL. Module web sessions intentionally
/// compare these values exactly so a subdomain, a different port, or a lookalike
/// percent-encoded host cannot inherit the embedded browser's privileges.
public struct ModuleWebOrigin: Hashable, Sendable, CustomStringConvertible {
    public let scheme: String
    public let host: String
    public let port: Int?

    public init?(url: URL) {
        guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
            let scheme = components.scheme?.lowercased(),
            scheme == "https",
            let rawHost = components.host, !rawHost.isEmpty,
            components.user == nil,
            components.password == nil
        else {
            return nil
        }

        // URLComponents gives us a host separate from a URL's escaped path. A
        // literal percent sign in a host is never a DNS origin and is rejected
        // rather than decoded into a potentially different host.
        // Test the encoded form too: URLComponents.host may normalise `%65` to
        // `e`, but an encoded host must never become an allow-list match.
        guard !rawHost.contains("%"),
            !(components.percentEncodedHost?.contains("%") ?? false),
            components.port.map({ (1...65_535).contains($0) }) ?? true
        else {
            return nil
        }
        self.scheme = scheme
        self.host = rawHost.lowercased()
        guard !self.host.isEmpty else {
            return nil
        }
        self.port = (components.port == 443) ? nil : components.port
    }

    public init?(string: String) {
        let trimmed = string.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let components = URLComponents(string: trimmed),
            components.path.isEmpty || components.path == "/",
            components.query == nil, components.fragment == nil,
            let url = components.url
        else {
            return nil
        }
        self.init(url: url)
    }

    public var description: String {
        "\(scheme)://\(host)" + (port.map { ":\($0)" } ?? "")
    }
}

public enum ModuleWebNavigationDecision: Equatable, Sendable {
    case allow
    case openExternally
}

public struct ModuleWebNavigationPolicy: Sendable {
    public let allowedOrigins: Set<ModuleWebOrigin>

    public init(allowedOrigins: [String]) {
        self.allowedOrigins = Set(allowedOrigins.compactMap(ModuleWebOrigin.init(string:)))
    }

    public func decision(for url: URL) -> ModuleWebNavigationDecision {
        guard let origin = ModuleWebOrigin(url: url), allowedOrigins.contains(origin) else {
            return .openExternally
        }
        return .allow
    }
}

/// The token's code is deliberately omitted. It exists only in the supplied
/// launch URL and must never be persisted or emitted in diagnostics.
public struct ModuleWebSession: Equatable, Sendable {
    public let capabilityID: String
    public let launchURL: URL
    public let allowedOrigins: [String]
    /// Separate from the browser-cookie expiry: this bounds the one-time code.
    public let codeExpiresAt: Date
    public let expiresAt: Date

    public init(
        capabilityID: String, launchURL: URL, allowedOrigins: [String], codeExpiresAt: Date? = nil,
        expiresAt: Date
    ) {
        self.capabilityID = capabilityID
        self.launchURL = launchURL
        self.allowedOrigins = allowedOrigins
        self.codeExpiresAt = codeExpiresAt ?? expiresAt
        self.expiresAt = expiresAt
    }

    public var navigationPolicy: ModuleWebNavigationPolicy {
        ModuleWebNavigationPolicy(allowedOrigins: allowedOrigins)
    }

    public func isValid(at date: Date = Date()) -> Bool {
        date < expiresAt
    }

    public var oneTimeCode: String? {
        URLComponents(url: launchURL, resolvingAgainstBaseURL: false)?
            .queryItems?
            .first(where: { $0.name == "code" })?
            .value
    }
}

// `ModuleWebSessionProviding` and `ModuleWebSessionGrant` are defined by the
// gateway client layer. The browser turns its credential-free grant into this
// presentation type, retaining only the URL that already contains the opaque
// one-time code.
extension ModuleWebSession {
    public init(grant: ModuleWebSessionGrant) {
        self.init(
            capabilityID: grant.capabilityID,
            launchURL: grant.url,
            allowedOrigins: grant.allowedOrigins,
            codeExpiresAt: Date().addingTimeInterval(TimeInterval(min(grant.codeTTLSeconds, 60))),
            expiresAt: grant.expiresAt
        )
    }
}

public enum ModuleWebBridgeEvent: Equatable, Sendable {
    case renew

    public static func parse(_ body: Any) -> ModuleWebBridgeEvent? {
        if let text = body as? String, text == "botville.session.renew" { return .renew }
        guard let object = body as? [String: Any],
            object["name"] as? String == "botville.session.renew"
        else { return nil }
        return .renew
    }
}

public struct ModuleWebBridgePolicy: Sendable {
    public let navigationPolicy: ModuleWebNavigationPolicy

    public init(navigationPolicy: ModuleWebNavigationPolicy) {
        self.navigationPolicy = navigationPolicy
    }

    /// Bridge events are accepted only from the main document at an allowed
    /// HTTPS origin; frames and untrusted navigations cannot trigger renewal.
    public func accepts(eventBody: Any, frameURL: URL?, isMainFrame: Bool) -> ModuleWebBridgeEvent? {
        guard isMainFrame, let frameURL,
            navigationPolicy.decision(for: frameURL) == .allow
        else { return nil }
        return ModuleWebBridgeEvent.parse(eventBody)
    }
}

public enum ModuleWebCookieError: Error, Equatable, Sendable { case expiredCode, alreadyExchanged }

public struct ModuleWebCookie: Equatable, Sendable {
    public let name: String
    public let value: String
    public let origin: ModuleWebOrigin
    public let expiresAt: Date
    public let isHTTPOnly: Bool
    public let isSecure: Bool
    public let sameSite: String
}

public protocol ModuleWebCookieStoring: Sendable {
    func store(_ cookie: ModuleWebCookie) async
}

/// Implemented only by the bundled mock client. It lets the app render a local
/// module document while still exercising the same short-session cookie path.
public protocol MockModuleWebSessionProviding: ModuleWebSessionProviding {
    func mockModuleWebHTML(for session: ModuleWebSession) -> String
}

/// A deterministic mock of the page-side `session-cookies` request. Production
/// pages perform this HTTP exchange themselves; this is solely for exercising
/// the cookie-store boundary without exposing credentials to application logs.
public actor MockModuleWebCookieExchange: ModuleWebCookieStoring {
    private var exchangedCodes: Set<String> = []
    private var cookies: [ModuleWebCookie] = []

    public init() {}

    public func exchange(code: String, for session: ModuleWebSession, now: Date = Date()) throws {
        guard now < session.codeExpiresAt, session.isValid(at: now) else {
            throw ModuleWebCookieError.expiredCode
        }
        guard exchangedCodes.insert(code).inserted else { throw ModuleWebCookieError.alreadyExchanged }
        guard let origin = ModuleWebOrigin(url: session.launchURL) else {
            throw ModuleWebCookieError.expiredCode
        }
        cookies.append(
            ModuleWebCookie(
                name: "botville_session", value: "mock-cookie", origin: origin,
                expiresAt: session.expiresAt, isHTTPOnly: true, isSecure: true, sameSite: "Lax"
            ))
    }

    public func exchange(launchURL: URL, for session: ModuleWebSession, now: Date = Date()) throws {
        guard let code = session.oneTimeCode,
            !code.isEmpty
        else { throw ModuleWebCookieError.expiredCode }
        try exchange(code: code, for: session, now: now)
    }

    public func storedCookies() -> [ModuleWebCookie] { cookies }
    public func store(_ cookie: ModuleWebCookie) { cookies.append(cookie) }
}
