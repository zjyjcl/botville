// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation

public protocol GatewayClient: ModuleWebSessionProviding {
    func capabilities(locale: String?, appVersion: String?) async throws -> GatewayCapabilityManifest
    func cards(capabilityID: String?, cardTypes: [String]?) async throws -> [GatewayCard]
    func perform(card: GatewayCard, action: GatewayCardAction) async throws -> GatewayActionReceipt
    func exchangeSessionCookie(capabilityID: String, code: String) async throws -> GatewayCookieExchange
    func personas() async throws -> [MemoryPersona]
    func persona(id: String) async throws -> PersonaDetail
    func personaState(id: String) async throws -> PersonaState
    func updatePersonaState(id: String, patch: PersonaStatePatch) async throws -> PersonaState
    func memories(
        personaID: String, layers: [String], scopeKind: String?, counterpart: String?,
        roomID: String?, pinned: Bool?, cursor: String?, limit: Int?
    ) async throws -> MemoryPage
    func memory(id: String) async throws -> Memory
    func deleteMemory(id: String) async throws
    func pinMemory(id: String) async throws -> Memory
    func unpinMemory(id: String) async throws -> Memory
    func writeMemoryFact(_ fact: MemoryFactWrite, idempotencyKey: String?) async throws -> Memory
}

public extension GatewayClient {
    func personas() async throws -> [MemoryPersona] { throw GatewayClientError.invalidResponse }
    func persona(id: String) async throws -> PersonaDetail { throw GatewayClientError.invalidResponse }
    func personaState(id: String) async throws -> PersonaState { throw GatewayClientError.invalidResponse }
    func updatePersonaState(id: String, patch: PersonaStatePatch) async throws -> PersonaState {
        throw GatewayClientError.invalidResponse
    }
    func memories(
        personaID: String, layers: [String] = [], scopeKind: String? = nil,
        counterpart: String? = nil, roomID: String? = nil, pinned: Bool? = nil,
        cursor: String? = nil, limit: Int? = nil
    ) async throws -> MemoryPage { throw GatewayClientError.invalidResponse }
    func memory(id: String) async throws -> Memory { throw GatewayClientError.invalidResponse }
    func deleteMemory(id: String) async throws { throw GatewayClientError.invalidResponse }
    func pinMemory(id: String) async throws -> Memory { throw GatewayClientError.invalidResponse }
    func unpinMemory(id: String) async throws -> Memory { throw GatewayClientError.invalidResponse }
    func writeMemoryFact(_ fact: MemoryFactWrite, idempotencyKey: String? = nil) async throws
        -> Memory
    { throw GatewayClientError.invalidResponse }
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
        if httpStatus == 403 { return "没有权限查看" }
        if httpStatus == 401, self != .serverAuthMisconfigured { return "会话已失效，请重新登录。" }
        switch self {
        case .serverAuthMisconfigured: return "服务器认证异常，请联系部署者"
        case .problem(_, let code):
            switch code {
            case "memory.disabled": return "记忆管理暂未启用。"
            case "memory.not_found": return "这条记忆已不存在。"
            case "memory.not_pinnable": return "这条记忆不能固定。"
            case "memory.pin_limit": return "固定记忆数量已达上限。"
            case "mood.unavailable": return "心情暂时不可用"
            case "memory.unavailable": return "记忆服务暂时不可用，请稍后重试。"
            case "persona.not_owned": return "你无权管理该 AI 的记忆。"
            case "persona.not_found": return "不存在或不可见"
            case "rate_limited.transport": return "操作过于频繁，请稍后再试。"
            default: return "网关请求失败，请稍后重试。"
            }
        case .httpStatus: return "网关请求失败，请稍后重试。"
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
/// injected separately; moments and memories require the OpenID provider.
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
                retrySleep: RetrySleep? = nil)
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
        // Keep the async default inside this module, as with timeoutSleep.
        self.retrySleep = retrySleep ?? { try await Task.sleep(for: .seconds($0)) }
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
        retrySleep: RetrySleep? = nil
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

    public func personas() async throws -> [MemoryPersona] {
        var result: [MemoryPersona] = [], cursor: String?, seen: Set<String> = []
        repeat {
            let query = cursor.map { [URLQueryItem(name: "cursor", value: $0)] } ?? []
            let page = try await decode(
                request(["personas"], query: query), as: MemoryPersonaPage.self)
            result.append(contentsOf: page.items)
            guard page.hasMore else { break }
            guard let next = page.nextCursor, seen.insert(next).inserted, seen.count <= 100 else {
                throw GatewayClientError.invalidResponse
            }
            cursor = next
        } while cursor != nil
        return result
    }

    public func persona(id: String) async throws -> PersonaDetail {
        try await decode(request(["personas", id]), as: PersonaDetail.self)
    }

    public func personaState(id: String) async throws -> PersonaState {
        try await decode(request(["personas", id, "state"]), as: PersonaState.self)
    }

    public func updatePersonaState(id: String, patch: PersonaStatePatch) async throws -> PersonaState {
        guard patch.mood != nil || patch.moodPublic != nil else { throw GatewayClientError.invalidResponse }
        return try await decode(
            request(["personas", id, "state"], method: "PATCH", body: JSONEncoder().encode(patch)),
            as: PersonaState.self)
    }

    public func memories(
        personaID: String, layers: [String] = [], scopeKind: String? = nil,
        counterpart: String? = nil, roomID: String? = nil, pinned: Bool? = nil,
        cursor: String? = nil, limit: Int? = nil
    ) async throws -> MemoryPage {
        var query = [URLQueryItem(name: "persona_id", value: personaID)]
        query += layers.map { URLQueryItem(name: "layer", value: $0) }
        query += [
            scopeKind.map { URLQueryItem(name: "scope_kind", value: $0) },
            counterpart.map { URLQueryItem(name: "counterpart", value: $0) },
            roomID.map { URLQueryItem(name: "room_id", value: $0) },
            pinned.map { URLQueryItem(name: "pinned", value: $0 ? "true" : "false") },
            cursor.map { URLQueryItem(name: "cursor", value: $0) },
            limit.map { URLQueryItem(name: "limit", value: String($0)) },
        ].compactMap { $0 }
        return try await decode(request(["memories"], query: query), as: MemoryPage.self)
    }
    public func memory(id: String) async throws -> Memory {
        try await decode(request(["memories", id]), as: Memory.self)
    }
    public func deleteMemory(id: String) async throws {
        _ = try await responseData(for: request(["memories", id], method: "DELETE"))
    }
    public func pinMemory(id: String) async throws -> Memory {
        try await decode(request(["memories", id, "pin"], method: "PUT"), as: Memory.self)
    }
    public func unpinMemory(id: String) async throws -> Memory {
        try await decode(request(["memories", id, "pin"], method: "DELETE"), as: Memory.self)
    }
    public func writeMemoryFact(_ fact: MemoryFactWrite, idempotencyKey: String? = nil) async throws
        -> Memory
    {
        try await decode(
            request(
                ["memories", "facts"], method: "PUT", body: JSONEncoder().encode(fact),
                idempotencyKey: idempotencyKey), as: Memory.self)
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
        if authorize, !Self.isOpenIDPath(segments, method: method), let token {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        return request
    }

    private static func isOpenIDPath(_ segments: [String], method: String) -> Bool {
        guard let first = segments.first else { return false }
        // Contract 0.5.0 authenticates GET collection/detail and owner state reads/writes.
        return first == "moments" || first == "memories"
            || (first == "personas" && method.caseInsensitiveCompare("GET") == .orderedSame
                && (segments.count == 1 || (segments.count == 2 && !segments[1].isEmpty)))
            || (first == "personas" && (method.caseInsensitiveCompare("GET") == .orderedSame
                || method.caseInsensitiveCompare("PATCH") == .orderedSame)
                && segments.count == 3 && !segments[1].isEmpty && segments[2] == "state")
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
    /// Split the encoded path before decoding segments, so an opaque ID containing
    /// a slash stays one ID. Queries and lookalike resource names cannot select credentials.
    func isOpenIDRequest(_ request: URLRequest) throws -> Bool {
        guard let url = request.url,
            let candidate = URLComponents(url: url, resolvingAgainstBaseURL: false),
            let base = URLComponents(url: baseURL, resolvingAgainstBaseURL: false),
            candidate.scheme == base.scheme, candidate.host == base.host,
            candidate.port == base.port,
            let momentsURL = try self.request(["moments"]).url,
            let memoriesURL = try self.request(["memories"]).url,
            let personasURL = try self.request(["personas"]).url,
            let personas = URLComponents(url: personasURL, resolvingAgainstBaseURL: false)?
                .percentEncodedPath,
            let moments = URLComponents(url: momentsURL, resolvingAgainstBaseURL: false)?
                .percentEncodedPath,
            let memories = URLComponents(url: memoriesURL, resolvingAgainstBaseURL: false)?
                .percentEncodedPath
        else { throw GatewayClientError.invalidResponse }
        let path = candidate.percentEncodedPath
        let personaSegments: [String]
        if path == personas {
            personaSegments = ["personas"]
        } else if path.hasPrefix(personas + "/") {
            let suffix = path.dropFirst(personas.count + 1)
            personaSegments = ["personas"] + suffix.split(separator: "/", omittingEmptySubsequences: false)
                .map { String($0).removingPercentEncoding ?? "" }
        } else {
            personaSegments = []
        }
        return path == moments || path.hasPrefix(moments + "/") || path == memories
            || path.hasPrefix(memories + "/")
            || Self.isOpenIDPath(personaSegments, method: request.httpMethod ?? "GET")
    }

    func responseData(for request: URLRequest) async throws -> Data {
        var outgoing = request
        var usedToken: MatrixOpenIDToken?
        let isOpenID = try isOpenIDRequest(request)
        if isOpenID {
            guard let openIDProvider else { throw MatrixOpenIDError.providerUnavailable }
            let token = try await openIDProvider.token()
            usedToken = token
            outgoing.setValue("MatrixOpenID \(token.accessToken)", forHTTPHeaderField: "Authorization")
        }
        var refreshedOnce = false
        var serverRetries = 0
        // Only GET or an explicitly keyed write can be safely replayed after 500.
        let canRetryServerError = request.httpMethod?.caseInsensitiveCompare("GET") == .orderedSame
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
            if isOpenID, response.statusCode == 500, code == "internal.error",
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
    private var mockMemories: [Memory] = []
    private var mockPersonaState: PersonaState?

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
        let date = now()
        let owner = MemorySource(kind: "owner", eventID: nil, sender: nil, role: nil, model: nil)
        mockMemories = [
            Memory(
                id: "mf_0123456789abcdef", personaID: "@ai_demo:example.invalid", layer: "profile",
                scope: MemoryScope(kind: "dm", counterpart: "@human:example.invalid", roomID: nil),
                visibility: "dm_only", trust: "owner", source: owner, pinned: true, text: nil,
                key: "likes", value: "示例值", dirty: false, redacted: false, createdAt: date,
                updatedAt: date, expiresAt: nil),
            Memory(
                id: "mr_0123456789abcdef", personaID: "@ai_demo:example.invalid", layer: "recent",
                scope: MemoryScope(kind: "dm", counterpart: "@human:example.invalid", roomID: nil),
                visibility: "dm_only", trust: "trusted", source: owner, pinned: false,
                text: "示例近期记忆", key: nil, value: nil, dirty: false, redacted: false,
                createdAt: date, updatedAt: date, expiresAt: nil),
            Memory(
                id: "ms_0123456789abcdef", personaID: "@ai_demo:example.invalid", layer: "summary",
                scope: MemoryScope(kind: "group", counterpart: nil, roomID: "!example:invalid"),
                visibility: "scope_only", trust: "derived",
                source: MemorySource(
                    kind: "summary", eventID: nil, sender: nil, role: nil, model: nil),
                pinned: false, text: nil, key: nil, value: nil, dirty: false, redacted: true,
                createdAt: date, updatedAt: date, expiresAt: nil),
        ]
        mockPersonaState = try? GatewayCoding.decoder.decode(
            PersonaState.self, from: Self.resource("mock-persona-state"))
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

    public func personas() async throws -> [MemoryPersona] {
        // Contract 0.5.0 list summary; detailed state is loaded separately.
        try GatewayCoding.decoder.decode(MemoryPersonaPage.self,
            from: Self.resource("mock-persona-page")).items
    }
    public func persona(id: String) async throws -> PersonaDetail {
        guard let persona = try await personas().first(where: { $0.id == id }) else {
            throw GatewayClientError.problem(status: 404, code: "persona.not_found")
        }
        return .owner(persona)
    }
    public func personaState(id: String) async throws -> PersonaState {
        guard let state = mockPersonaState, state.personaID == id else {
            throw GatewayClientError.problem(status: 404, code: "persona.not_found")
        }
        return state
    }
    public func updatePersonaState(id: String, patch: PersonaStatePatch) async throws -> PersonaState {
        guard patch.mood != nil || patch.moodPublic != nil, let current = mockPersonaState,
              current.personaID == id else {
            throw GatewayClientError.problem(status: 404, code: "persona.not_found")
        }
        let updated = PersonaState(
            personaID: id, mood: patch.mood ?? current.mood,
            moodPublic: patch.moodPublic ?? current.moodPublic, updatedAt: now())
        mockPersonaState = updated
        return updated
    }
    public func memories(
        personaID: String, layers: [String] = [], scopeKind: String? = nil,
        counterpart: String? = nil, roomID: String? = nil, pinned: Bool? = nil,
        cursor: String? = nil, limit: Int? = nil
    ) async throws -> MemoryPage {
        var values = mockMemories.filter { $0.personaID == personaID }
        if !layers.isEmpty { values = values.filter { layers.contains($0.layer) } }
        if let scopeKind { values = values.filter { $0.scope.kind == scopeKind } }
        if let counterpart { values = values.filter { $0.scope.counterpart == counterpart } }
        if let roomID { values = values.filter { $0.scope.roomID == roomID } }
        if let pinned { values = values.filter { $0.pinned == pinned } }
        let start = Int(cursor ?? "0") ?? 0
        let count = min(max(limit ?? 20, 1), 100)
        guard start >= 0, start <= values.count else { throw GatewayClientError.invalidResponse }
        let end = min(start + count, values.count)
        return MemoryPage(
            items: Array(values[start..<end]), nextCursor: end < values.count ? String(end) : nil,
            hasMore: end < values.count)
    }
    public func memory(id: String) async throws -> Memory {
        guard let value = mockMemories.first(where: { $0.id == id }) else {
            throw GatewayClientError.problem(status: 404, code: "memory.not_found")
        }
        return value
    }
    public func deleteMemory(id: String) async throws {
        guard let index = mockMemories.firstIndex(where: { $0.id == id }) else {
            throw GatewayClientError.problem(status: 404, code: "memory.not_found")
        }
        mockMemories.remove(at: index)
    }
    public func pinMemory(id: String) async throws -> Memory { try setPin(id: id, pinned: true) }
    public func unpinMemory(id: String) async throws -> Memory { try setPin(id: id, pinned: false) }
    private func setPin(id: String, pinned: Bool) throws -> Memory {
        guard let index = mockMemories.firstIndex(where: { $0.id == id }) else {
            throw GatewayClientError.problem(status: 404, code: "memory.not_found")
        }
        let old = mockMemories[index]
        guard !pinned || old.actions.canTogglePin else {
            throw GatewayClientError.problem(status: 422, code: "memory.not_pinnable")
        }
        let value = Memory(
            id: old.id, personaID: old.personaID, layer: old.layer, scope: old.scope,
            visibility: old.visibility, trust: old.trust, source: old.source, pinned: pinned,
            text: old.text, key: old.key, value: old.value, dirty: old.dirty,
            redacted: old.redacted, createdAt: old.createdAt, updatedAt: now(),
            expiresAt: old.expiresAt)
        mockMemories[index] = value
        return value
    }
    public func writeMemoryFact(_ fact: MemoryFactWrite, idempotencyKey: String? = nil) async throws
        -> Memory
    {
        let date = now()
        let value = Memory(
            id: "mf_"
                + String(UUID().uuidString.replacingOccurrences(of: "-", with: "").prefix(16))
                .lowercased(), personaID: fact.personaID, layer: "profile",
            scope: MemoryScope(kind: "dm", counterpart: fact.counterpart, roomID: nil),
            visibility: fact.visibility ?? "dm_only", trust: "owner",
            source: MemorySource(kind: "owner", eventID: nil, sender: nil, role: nil, model: nil),
            pinned: false, text: nil, key: fact.key, value: fact.value, dirty: false,
            redacted: false, createdAt: date, updatedAt: date, expiresAt: nil)
        if let index = mockMemories.firstIndex(where: {
            $0.personaID == fact.personaID && $0.scope.counterpart == fact.counterpart
                && $0.key == fact.key
        }) {
            mockMemories[index] = value
        } else {
            mockMemories.append(value)
        }
        return value
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
