// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation

public protocol GatewayClient: ModuleWebSessionProviding {
    func capabilities(locale: String?, appVersion: String?) async throws -> GatewayCapabilityManifest
    func cards(capabilityID: String?, cardTypes: [String]?) async throws -> [GatewayCard]
    func perform(card: GatewayCard, action: GatewayCardAction) async throws -> GatewayActionReceipt
    func exchangeSessionCookie(capabilityID: String, code: String) async throws -> GatewayCookieExchange
}

public enum GatewayClientError: Error, Equatable, LocalizedError {
    case invalidBaseURL, insecureBaseURL, invalidResponse, invalidAction, expiredSessionCode
    case httpStatus(Int)
    case problem(status: Int, code: String)
    /// Two invalid-token responses despite one refresh; no credentials/body retained.
    case serverAuthMisconfigured

    public var httpStatus: Int? {
        switch self {
        case .httpStatus(let status), .problem(let status, _): return status
        case .serverAuthMisconfigured: return 401
        default: return nil
        }
    }

    public var problemCode: String? {
        switch self {
        case .problem(_, let code): return code
        case .serverAuthMisconfigured: return "auth.invalid_token"
        default: return nil
        }
    }

    public var errorDescription: String? {
        switch self {
        case .serverAuthMisconfigured: return "服务器认证异常，请联系部署者"
        case .problem, .httpStatus: return "网关请求失败，请稍后重试。"
        default: return nil
        }
    }
}

private struct ActionIntent: Hashable {
    let cardID: String
    let actionID: String
    let revision: Int?
}

private actor ActionIntentKeys {
    private var keys: [ActionIntent: String] = [:]

    func key(for intent: ActionIntent) -> String {
        if let key = keys[intent] { return key }
        let key = UUID().uuidString
        keys[intent] = key
        return key
    }
}

private func validateAction(card: GatewayCard, action: GatewayCardAction) async throws {
    guard card.isActionable, action.isKnownKind, action.disabledReason == nil,
        card.actions.contains(action), card.type != "approval" || card.revision != nil
    else {
        throw GatewayClientError.invalidAction
    }
    if action.effectiveRisk(cardRisk: card.effectiveRisk) == .high || action.confirmation?.biometric == true {
        _ = try await UnavailableHighRiskAuthorizer().authorize(
            cardID: card.id, actionID: action.id, revision: card.revision ?? 1
        )
    }
}

/// Transport skeleton for the v1 contract. No credentials are read from the
/// environment, persisted, or logged. Site and end-user credentials are
/// injected separately; moments always requires the OpenID provider.
public final class HTTPGatewayClient: GatewayClient, @unchecked Sendable {
    public let baseURL: URL
    private let token: String?
    private let openIDProvider: MatrixOpenIDTokenProvider?
    private let session: URLSession
    private let intentKeys = ActionIntentKeys()
    public typealias RetrySleep = @Sendable (TimeInterval) async throws -> Void
    private let retrySleep: RetrySleep

    public init(baseURL: URL, allowLocalHTTP: Bool = false, token: String? = nil, session: URLSession? = nil,
                certificateFingerprint: Data? = nil, openIDProvider: MatrixOpenIDTokenProvider? = nil,
                retrySleep: @escaping RetrySleep = { try await Task.sleep(for: .seconds($0)) })
        throws
    {
        do {
            self.baseURL = try HomeserverPolicy(allowLocalHTTP: allowLocalHTTP).validatedURL(
                baseURL.absoluteString)
        } catch { throw GatewayClientError.invalidBaseURL }
        guard let components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false),
            !(components.percentEncodedHost?.contains("%") ?? true)
        else {
            throw GatewayClientError.invalidBaseURL
        }
        // A caller-provided session could silently omit the TLS delegate. Pin
        // configuration therefore owns its transport; test injection remains
        // available only for unpinned clients.
        guard certificateFingerprint == nil || session == nil else { throw GatewayClientError.invalidBaseURL }
        self.token = token
        self.openIDProvider = openIDProvider
        self.retrySleep = retrySleep
        if let session {
            self.session = session
        } else if let certificateFingerprint {
            self.session = PairingURLSession.make(fingerprint: certificateFingerprint, host: self.baseURL.host, port: self.baseURL.port)
        } else {
            let configuration = URLSessionConfiguration.ephemeral
            configuration.httpCookieStorage = nil
            configuration.httpShouldSetCookies = false
            configuration.urlCache = nil
            configuration.timeoutIntervalForRequest = 30
            self.session = URLSession(
                configuration: configuration, delegate: HTTPSRedirectGuard(), delegateQueue: nil)
        }
    }

    public convenience init(
        baseURL: String, allowLocalHTTP: Bool = false, token: String? = nil, session: URLSession? = nil,
        certificateFingerprint: Data? = nil, openIDProvider: MatrixOpenIDTokenProvider? = nil,
        retrySleep: @escaping RetrySleep = { try await Task.sleep(for: .seconds($0)) }
    ) throws {
        guard let url = URL(string: baseURL.trimmingCharacters(in: .whitespacesAndNewlines)) else {
            throw GatewayClientError.invalidBaseURL
        }
        try self.init(baseURL: url, allowLocalHTTP: allowLocalHTTP, token: token, session: session,
                      certificateFingerprint: certificateFingerprint, openIDProvider: openIDProvider, retrySleep: retrySleep)
    }

    public func capabilities(locale: String? = nil, appVersion: String? = nil) async throws
        -> GatewayCapabilityManifest
    {
        let query = [
            locale.map { URLQueryItem(name: "locale", value: $0) },
            appVersion.map { URLQueryItem(name: "app_version", value: $0) },
        ].compactMap { $0 }
        return try await decode(request(["capabilities"], query: query), as: GatewayCapabilityManifest.self)
    }

    public func cards(capabilityID: String? = nil, cardTypes: [String]? = nil) async throws -> [GatewayCard] {
        var result: [GatewayCard] = []
        var cursor: String?
        var seenCursors: Set<String> = []
        repeat {
            var query = capabilityID.map { [URLQueryItem(name: "capability_id", value: $0)] } ?? []
            if let cursor { query.append(URLQueryItem(name: "cursor", value: cursor)) }
            let page = try await decode(
                request(["cards"], query: query, negotiateCards: true), as: GatewayCardPage.self)
            result.append(contentsOf: page.items)
            guard page.hasMore else { break }
            guard let next = page.nextCursor, seenCursors.insert(next).inserted, seenCursors.count <= 100
            else {
                throw GatewayClientError.invalidResponse
            }
            cursor = next
        } while cursor != nil
        guard let cardTypes, !cardTypes.isEmpty else { return result }
        // OpenAPI's `type` is a single enum, so multiple requested types are
        // filtered locally instead of inventing a repeated query parameter.
        return result.filter { cardTypes.contains($0.type) }
    }

    public func perform(card: GatewayCard, action: GatewayCardAction) async throws -> GatewayActionReceipt {
        try await validateAction(card: card, action: action)
        struct Body: Encodable {
            let payload: [String: JSONValue]?
            let revision: Int?
        }
        let body = try JSONEncoder().encode(Body(payload: action.payload, revision: card.revision))
        let intent = ActionIntent(cardID: card.id, actionID: action.id, revision: card.revision)
        let key = await intentKeys.key(for: intent)
        return try await decode(
            request(
                ["cards", card.id, "actions", action.id], method: "POST", body: body, idempotencyKey: key),
            as: GatewayActionReceipt.self)
    }

    public func createModuleWebSession(
        capabilityID: String, url: URL, allowedOrigins: [String]? = nil, scopes: [String]? = nil,
        ttlSeconds: Int? = nil
    ) async throws -> ModuleWebSessionGrant {
        guard let localAllowedOrigins = ModuleWebOrigin.localAllowlist(
            for: url, callerAllowedOrigins: allowedOrigins),
            (60...3600).contains(ttlSeconds ?? 300)
        else {
            throw GatewayClientError.invalidResponse
        }
        struct Body: Encodable {
            let url: URL
            let allowed_origins: [String]?
            let scopes: [String]?
            let ttl_seconds: Int?
        }
        let startedAt = Date()
        let body = try JSONEncoder().encode(
            Body(url: url, allowed_origins: localAllowedOrigins, scopes: scopes, ttl_seconds: ttlSeconds))
        let token = try await decode(
            request(["capabilities", capabilityID, "session-tokens"], method: "POST", body: body),
            as: GatewaySessionToken.self)
        let codeItems =
            URLComponents(url: token.url, resolvingAgainstBaseURL: false)?.queryItems?.filter {
                $0.name == "code"
            } ?? []
        let remainingCodeTTL = token.codeTTLSeconds - Int(ceil(Date().timeIntervalSince(startedAt)))
        guard let effectiveAllowedOrigins = ModuleWebOrigin.intersectGrant(token.allowedOrigins, with: url),
            token.audience == capabilityID, (1...60).contains(token.codeTTLSeconds), remainingCodeTTL > 0,
            token.expiresAt > Date(), token.expiresAt.timeIntervalSince(startedAt) <= 3601,
            ModuleWebOrigin(url: token.url) == ModuleWebOrigin(url: url),
            ModuleWebNavigationPolicy(allowedOrigins: effectiveAllowedOrigins).decision(for: token.url)
                == .allow,
            !token.code.isEmpty, codeItems.count == 1, codeItems.first?.value == token.code
        else {
            throw GatewayClientError.invalidResponse
        }
        // The gateway supplies the URL verbatim. Never append/reconstruct its code.
        return ModuleWebSessionGrant(
            capabilityID: capabilityID, url: token.url, allowedOrigins: effectiveAllowedOrigins,
            codeTTLSeconds: remainingCodeTTL, expiresAt: token.expiresAt)
    }

    public func exchangeSessionCookie(capabilityID: String, code: String) async throws
        -> GatewayCookieExchange
    {
        let request = try request(
            ["capabilities", capabilityID, "session-cookies"], method: "POST",
            body: JSONEncoder().encode(["code": code]), authorize: false)
        let (_, response) = try await PairingURLSession.data(for: request, using: session)
        guard let response = response as? HTTPURLResponse else { throw GatewayClientError.invalidResponse }
        guard response.statusCode == 204 else { throw GatewayClientError.httpStatus(response.statusCode) }
        return GatewayCookieExchange(statusCode: 204)
    }

    func request(
        _ segments: [String], method: String = "GET", query: [URLQueryItem] = [], body: Data? = nil,
        authorize: Bool = true, negotiateCards: Bool = false, idempotencyKey: String? = nil
    ) throws -> URLRequest {
        guard var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else {
            throw GatewayClientError.invalidBaseURL
        }
        var path = components.percentEncodedPath
        while path.hasSuffix("/") { path.removeLast() }
        if !path.hasSuffix("/v1") { path += "/v1" }
        let unreserved = CharacterSet(
            charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~")
        for segment in segments {
            guard !segment.isEmpty,
                var encoded = segment.addingPercentEncoding(withAllowedCharacters: unreserved)
            else {
                throw GatewayClientError.invalidResponse
            }
            if segment == "." || segment == ".." {
                encoded = encoded.replacingOccurrences(of: ".", with: "%2E")
            }
            path += "/" + encoded
        }
        components.percentEncodedPath = path
        components.queryItems = query.isEmpty ? nil : query
        guard let url = components.url else { throw GatewayClientError.invalidResponse }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.httpBody = body
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if negotiateCards {
            request.setValue(
                GatewayCardSchema.requestHeaderValue(), forHTTPHeaderField: "X-Card-Schema-Version")
        }
        if body != nil { request.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        if let idempotencyKey { request.setValue(idempotencyKey, forHTTPHeaderField: "Idempotency-Key") }
        if authorize, segments.first != "moments", let token { request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        return request
    }

    /// Minimal raw-data entry point. Resource models and UI are intentionally
    /// deferred; path components are encoded separately, never parsed as URLs.
    public func momentsRequest(
        subpath: [String] = [], method: String = "GET", query: [URLQueryItem] = [],
        body: Data? = nil, idempotencyKey: String? = nil
    ) async throws -> Data {
        try await responseData(for: request(["moments"] + subpath, method: method,
            query: query, body: body, idempotencyKey: idempotencyKey))
    }

    /// Match the first API path segment relative to this gateway's base prefix.
    /// Query strings and similar names (momentsx) never select credentials.
    func isMomentsRequest(_ request: URLRequest) throws -> Bool {
        guard let root = try self.request(["moments"]).url?.path,
              let path = request.url?.path else { throw GatewayClientError.invalidResponse }
        return path == root || path.hasPrefix(root + "/")
    }

    func responseData(for request: URLRequest) async throws -> Data {
        var outgoing = request
        var usedToken: MatrixOpenIDToken?
        if try isMomentsRequest(request) {
            guard let openIDProvider else { throw MatrixOpenIDError.providerUnavailable }
            let token = try await openIDProvider.token()
            usedToken = token
            outgoing.setValue("MatrixOpenID \(token.accessToken)", forHTTPHeaderField: "Authorization")
        }
        var refreshedOnce = false
        var serverRetries = 0
        // Only GET or an explicitly keyed write can be safely replayed after 500.
        let canRetryServerError = request.httpMethod == "GET"
            || !(request.value(forHTTPHeaderField: "Idempotency-Key")?
                .trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ?? true)
        while true {
            try Task.checkCancellation()
            let (data, rawResponse) = try await PairingURLSession.data(for: outgoing, using: session)
            guard let response = rawResponse as? HTTPURLResponse else { throw GatewayClientError.invalidResponse }
            guard !(200..<300).contains(response.statusCode) else { return data }
            let code = Self.problemCode(data: data, response: response)
            if response.statusCode == 401, code == "auth.invalid_token",
               let current = usedToken, let openIDProvider {
                // Identity-scoped invalidation preserves a newer concurrent refresh.
                await openIDProvider.invalidate(current)
                guard !refreshedOnce else { throw GatewayClientError.serverAuthMisconfigured }
                refreshedOnce = true
                let refreshed = try await openIDProvider.token()
                usedToken = refreshed
                // Reuse URL, method, body and Idempotency-Key exactly.
                outgoing.setValue("MatrixOpenID \(refreshed.accessToken)", forHTTPHeaderField: "Authorization")
                continue
            }
            if response.statusCode == 500, code == "internal.error",
               canRetryServerError, serverRetries < 3 {
                let delay = TimeInterval(1 << serverRetries) // 1s, 2s, 4s; total budget per operation.
                serverRetries += 1
                try await retrySleep(delay)
                continue // Preserve the current token, even across backoff.
            }
            if let code { throw GatewayClientError.problem(status: response.statusCode, code: code) }
            throw GatewayClientError.httpStatus(response.statusCode)
        }
    }

    private static func problemCode(data: Data, response: HTTPURLResponse) -> String? {
        // Fail closed: absent/wrong media type, malformed JSON or absent code never
        // triggers refresh/retry. Preserve the original HTTP status, discard body.
        let mediaType = response.value(forHTTPHeaderField: "Content-Type")?
            .split(separator: ";", maxSplits: 1).first?
            .trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard mediaType == "application/problem+json",
              let problem = try? JSONDecoder().decode(ProblemCode.self, from: data),
              !problem.code.isEmpty else { return nil }
        return problem.code
    }

    private struct ProblemCode: Decodable { let code: String }

    private func decode<T: Decodable>(_ request: URLRequest, as: T.Type) async throws -> T {
        try GatewayCoding.decoder.decode(T.self, from: await responseData(for: request))
    }
}

/// Stateful offline gateway. Its resources are derived from contract examples;
/// every credential-looking value is synthetic and remains process-local.
public actor MockGatewayClient: GatewayClient, MockModuleWebSessionProviding {
    private let manifest: Result<GatewayCapabilityManifest, Error>
    private var mockCards: [GatewayCard] = []
    private var receipts: [ActionIntent: GatewayActionReceipt] = [:]
    private var issuedCodes: [String: IssuedCode] = [:]
    private let now: @Sendable () -> Date

    private struct IssuedCode {
        let capabilityID: String
        let codeExpiresAt: Date
        let expiresAt: Date
    }

    public init(now: @escaping @Sendable () -> Date = { Date() }) {
        self.now = now
        do {
            manifest = .success(
                try GatewayCoding.decoder.decode(
                    GatewayCapabilityManifest.self, from: Self.resource("mock-capability-manifest")))
        } catch { manifest = .failure(GatewayClientError.invalidResponse) }
        do {
            guard var object = try JSONSerialization.jsonObject(with: Self.resource("mock-card"))
                as? [String: Any]
            else { throw GatewayClientError.invalidResponse }
            object["expires_at"] = ISO8601DateFormatter().string(from: now().addingTimeInterval(3600))
            object["created_at"] = ISO8601DateFormatter().string(from: now())
            mockCards = [
                try GatewayCoding.decoder.decode(
                    GatewayCard.self, from: JSONSerialization.data(withJSONObject: object))
            ]
        } catch { mockCards = [] }
    }

    public func capabilities(locale: String? = nil, appVersion: String? = nil) async throws
        -> GatewayCapabilityManifest
    {
        try manifest.get()
    }

    public func cards(capabilityID: String? = nil, cardTypes: [String]? = nil) async throws -> [GatewayCard] {
        guard !mockCards.isEmpty else { throw GatewayClientError.invalidResponse }
        return mockCards.filter {
            (capabilityID == nil || $0.source.capabilityID == capabilityID)
                && (cardTypes == nil || cardTypes!.isEmpty || cardTypes!.contains($0.type))
        }
    }

    public func perform(card: GatewayCard, action: GatewayCardAction) async throws -> GatewayActionReceipt {
        let intent = ActionIntent(cardID: card.id, actionID: action.id, revision: card.revision)
        if let receipt = receipts[intent] { return receipt }
        try await validateAction(card: card, action: action)
        guard let index = mockCards.firstIndex(where: { $0.id == card.id }),
            mockCards[index].revision == card.revision, mockCards[index].isActionable
        else {
            throw GatewayClientError.invalidAction
        }
        let updated = card.withStatus(
            action.kind == "dismiss" ? "dismissed" : "acted", revision: (card.revision ?? 1) + 1)
        mockCards[index] = updated
        let receipt = GatewayActionReceipt(
            cardID: card.id, actionID: action.id, status: "accepted",
            auditID: "audit_mock_\(UUID().uuidString)", card: updated)
        receipts[intent] = receipt
        return receipt
    }

    public func createModuleWebSession(
        capabilityID: String, url: URL, allowedOrigins: [String]? = nil, scopes: [String]? = nil,
        ttlSeconds: Int? = nil
    ) async throws -> ModuleWebSessionGrant {
        guard let capability = try manifest.get().capabilities.first(where: { $0.id == capabilityID }),
            capability.isEnabled, let entryURL = capability.entry.webURL,
            let origin = ModuleWebOrigin(url: entryURL), ModuleWebOrigin(url: url) == origin,
            (60...3600).contains(ttlSeconds ?? 300),
            Set(scopes ?? []).isSubset(of: Set(capability.scopes))
        else { throw GatewayClientError.invalidResponse }
        guard let origins = ModuleWebOrigin.localAllowlist(for: url, callerAllowedOrigins: allowedOrigins),
            ModuleWebNavigationPolicy(allowedOrigins: origins).decision(for: url) == .allow,
            var components = URLComponents(url: url, resolvingAgainstBaseURL: false)
        else { throw GatewayClientError.invalidResponse }
        let issuedAt = now()
        let code = "mock-once-\(UUID().uuidString)"
        let expiresAt = issuedAt.addingTimeInterval(TimeInterval(ttlSeconds ?? 300))
        issuedCodes = issuedCodes.filter { $0.value.codeExpiresAt > issuedAt }
        issuedCodes[code] = IssuedCode(
            capabilityID: capabilityID, codeExpiresAt: issuedAt.addingTimeInterval(60), expiresAt: expiresAt)
        components.queryItems =
            (components.queryItems ?? []).filter { $0.name != "code" } + [
                URLQueryItem(name: "code", value: code)
            ]
        guard let launchURL = components.url else { throw GatewayClientError.invalidResponse }
        return ModuleWebSessionGrant(
            capabilityID: capabilityID, url: launchURL, allowedOrigins: origins, codeTTLSeconds: 60,
            expiresAt: expiresAt)
    }

    public func exchangeSessionCookie(capabilityID: String, code: String) async throws
        -> GatewayCookieExchange
    {
        guard let issued = issuedCodes[code], issued.capabilityID == capabilityID else {
            throw GatewayClientError.invalidResponse
        }
        issuedCodes.removeValue(forKey: code)
        guard now() < issued.codeExpiresAt, now() < issued.expiresAt else {
            throw GatewayClientError.expiredSessionCode
        }
        return GatewayCookieExchange(statusCode: 204)
    }

    public nonisolated func mockModuleWebHTML(for session: ModuleWebSession) -> String {
        let content: (title: String, body: String) = session.capabilityID == "notify.approvals"
            ? ("模拟部署日志", "<pre>2026-11-04 10:31:42 dry-run check passed\n2026-11-04 10:31:43 awaiting approval</pre>")
            : ("记忆检索", "<p>示例模块已打开。短时会话通过 HttpOnly cookie 交给浏览器，页面不读取凭据。</p>")
        return """
        <!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>body{font:17px -apple-system;padding:28px;color:#173149;background:#f4f8fb}button{padding:12px;border:0;border-radius:10px;background:#236dba;color:white}p{line-height:1.7}</style>
        </head><body><h1>\(content.title)</h1>\(content.body)
        <button onclick="window.webkit.messageHandlers['botville.session.renew'].postMessage({})">续期会话</button>
        <p><a href="https://example.invalid/outside">测试站外链接提示</a></p></body></html>
        """
    }

    private static func resource(_ name: String) throws -> Data {
        guard let url = Bundle.module.url(forResource: name, withExtension: "json") else {
            throw GatewayClientError.invalidResponse
        }
        return try Data(contentsOf: url)
    }
}

enum GatewayCoding {
    static var decoder: JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .custom { try GatewayDate.decode($0) }
        return decoder
    }
}
