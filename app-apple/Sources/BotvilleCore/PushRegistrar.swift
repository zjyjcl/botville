// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation

/// APNs device tokens are an opaque byte sequence.  Matrix expects the stable,
/// lowercase hexadecimal form; keeping this conversion pure makes it safe to
/// test without ever logging the token.
public enum DevicePushToken {
    public static func hexadecimal(_ token: Data) -> String {
        token.map { String(format: "%02x", $0) }.joined()
    }
}

/// The Sygnal endpoint is deliberately narrower than a generic web URL.  It
/// has no credentials or URL parameters and always receives Matrix notify
/// requests at this exact path. `allowHTTP` is injected by the app target so
/// Release policy is testable without relying on compilation conditions.
public enum PushNotifyURLPolicy {
    public static let requiredPath = "/_matrix/push/v1/notify"

    public static func validatedURL(_ value: String, allowHTTP: Bool = false) throws -> URL {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let url = URL(string: trimmed),
              var components = try? EndpointURLPolicy.validatedComponents(url, originalSource: trimmed),
              let scheme = components.scheme?.lowercased(),
              scheme == "https" || (allowHTTP && scheme == "http"),
              components.query == nil, components.fragment == nil,
              components.path == requiredPath,
              components.percentEncodedPath == requiredPath else {
            throw PushPusherDataError.invalidURL
        }
        components.scheme = scheme
        guard let normalized = components.url else { throw PushPusherDataError.invalidURL }
        return normalized
    }

    public static func validate(_ value: String, allowHTTP: Bool = false) throws -> URL {
        try validatedURL(value, allowHTTP: allowHTTP)
    }
}

/// A1.2 requires metadata-only push payloads; unknown formats fail decoding.
public enum PushPusherDataFormat: String, Codable, Equatable, Sendable {
    case eventIDOnly = "event_id_only"
}

/// Matrix pusher `data` object. The Sygnal endpoint is supplied by deployment
/// configuration; it is never inferred from the user's homeserver.
public struct PushPusherData: Codable, Equatable, Sendable {
    public let url: URL
    public let format: PushPusherDataFormat

    /// The pusher endpoint receives device tokens. Production callers use the
    /// HTTPS default; Debug can explicitly opt into local HTTP.
    public init(url: URL, format: PushPusherDataFormat = .eventIDOnly) throws {
        self.url = try Self.validatedAndNormalizedURL(url, allowHTTP: false)
        self.format = format
    }

    public init(url: URL, allowHTTP: Bool, format: PushPusherDataFormat = .eventIDOnly) throws {
        self.url = try Self.validatedAndNormalizedURL(url, allowHTTP: allowHTTP)
        self.format = format
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let source = try container.decode(String.self, forKey: .url)
        guard EndpointURLPolicy.isSafeURLString(source),
              let url = URL(string: source) else {
            throw PushPusherDataError.invalidURL
        }
        self.url = try Self.validatedAndNormalizedURL(url, originalSource: source, allowHTTP: false)
        self.format = try container.decodeIfPresent(PushPusherDataFormat.self, forKey: .format) ?? .eventIDOnly
    }

    private enum CodingKeys: String, CodingKey {
        case url, format
    }

    private static func validatedAndNormalizedURL(_ url: URL, originalSource: String? = nil, allowHTTP: Bool) throws -> URL {
        try PushNotifyURLPolicy.validatedURL(originalSource ?? url.absoluteString, allowHTTP: allowHTTP)
    }
}

public enum PushPusherDataError: LocalizedError, Equatable, Sendable {
    case invalidURL

    public var errorDescription: String? {
        "推送地址须为无凭据、query 或 fragment 的 HTTPS URL（DEBUG 可启用 HTTP），路径必须为 /_matrix/push/v1/notify。"
    }
}

/// Wire model for `POST /_matrix/client/v3/pushers/set`.  A non-nil `kind`
/// creates or updates a pusher; a nil `kind` removes the matching pusher.
public struct PushRegistration: Codable, Equatable, Sendable {
    public let appID: String
    public let pushKey: String
    public let appDisplayName: String
    public let kind: String?
    public let append: Bool
    public let data: PushPusherData?
    public let deviceDisplayName: String?
    public let language: String?

    public init(
        appID: String,
        pushKey: String,
        appDisplayName: String,
        data: PushPusherData,
        kind: String = "http",
        append: Bool = false,
        deviceDisplayName: String? = nil,
        language: String? = nil
    ) {
        self.appID = appID
        self.pushKey = pushKey
        self.appDisplayName = appDisplayName
        self.kind = kind
        self.append = append
        self.data = data
        self.deviceDisplayName = deviceDisplayName
        self.language = language
    }

    public static func deletion(
        appID: String,
        pushKey: String,
        appDisplayName: String,
        deviceDisplayName: String? = nil,
        language: String? = nil
    ) -> PushRegistration {
        PushRegistration(
            appID: appID,
            pushKey: pushKey,
            appDisplayName: appDisplayName,
            kind: nil,
            append: false,
            data: nil,
            deviceDisplayName: deviceDisplayName,
            language: language
        )
    }

    private init(
        appID: String,
        pushKey: String,
        appDisplayName: String,
        kind: String?,
        append: Bool,
        data: PushPusherData?,
        deviceDisplayName: String?,
        language: String?
    ) {
        self.appID = appID
        self.pushKey = pushKey
        self.appDisplayName = appDisplayName
        self.kind = kind
        self.append = append
        self.data = data
        self.deviceDisplayName = deviceDisplayName
        self.language = language
    }

    enum CodingKeys: String, CodingKey {
        case appID = "app_id"
        case pushKey = "pushkey"
        case appDisplayName = "app_display_name"
        case kind, append, data
        case deviceDisplayName = "device_display_name"
        case language = "lang"
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(appID, forKey: .appID)
        try container.encode(pushKey, forKey: .pushKey)
        try container.encode(appDisplayName, forKey: .appDisplayName)
        if let kind { try container.encode(kind, forKey: .kind) }
        else { try container.encodeNil(forKey: .kind) }
        try container.encode(append, forKey: .append)
        try container.encodeIfPresent(data, forKey: .data)
        try container.encodeIfPresent(deviceDisplayName, forKey: .deviceDisplayName)
        try container.encodeIfPresent(language, forKey: .language)
    }
}

public protocol PushRegistrar: Sendable {
    func register(_ registration: PushRegistration) async throws
    func unregister() async throws
}

/// Deliberate C2.1 placeholder: preserves the call boundary without making a
/// network request or requiring any Apple credential.
public struct NoopPushRegistrar: PushRegistrar {
    public init() {}
    public func register(_ registration: PushRegistration) async throws {}
    public func unregister() async throws {}
}

/// A minimally decoded Matrix pusher list response.  Servers are free to add
/// fields, so only the two identifiers needed by diagnostics are modeled.
public struct MatrixPusher: Codable, Equatable, Sendable {
    public let appID: String
    public let pushKey: String

    enum CodingKeys: String, CodingKey { case appID = "app_id", pushKey = "pushkey" }
}

public struct MatrixPushersResponse: Codable, Equatable, Sendable {
    public let pushers: [MatrixPusher]
    public init(pushers: [MatrixPusher]) { self.pushers = pushers }
    public func contains(appID: String, pushKey: String) -> Bool {
        pushers.contains { $0.appID == appID && $0.pushKey == pushKey }
    }
}

public enum MatrixPusherTransportError: Error, LocalizedError, Sendable {
    case invalidResponse
    case serverRejected
    public var errorDescription: String? { "推送注册服务未接受请求。" }
}

/// Small protocol seam for unit tests. Implementations must not log request
/// headers or JSON: both contain sensitive credentials/device identifiers.
public protocol MatrixPusherTransport: Sendable {
    func setPusher(homeserver: URL, accessToken: String, registration: PushRegistration) async throws
    func pushers(homeserver: URL, accessToken: String) async throws -> MatrixPushersResponse
}

public struct URLSessionMatrixPusherTransport: MatrixPusherTransport {
    private let session: URLSession

    public init() { self.session = Self.makeEphemeralSession() }
    public init(session: URLSession) { self.session = session }

    private static func makeEphemeralSession() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.urlCache = nil
        configuration.httpShouldSetCookies = false
        configuration.httpCookieStorage = nil
        configuration.urlCredentialStorage = nil
        return URLSession(configuration: configuration, delegate: NoRedirectDelegate(), delegateQueue: nil)
    }

    public func setPusher(homeserver: URL, accessToken: String, registration: PushRegistration) async throws {
        var request = try request(homeserver: homeserver, accessToken: accessToken, method: "POST", path: "/_matrix/client/v3/pushers/set")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(registration)
        let (_, response) = try await session.data(for: request)
        try requireSuccess(response)
    }

    public func pushers(homeserver: URL, accessToken: String) async throws -> MatrixPushersResponse {
        let request = try request(homeserver: homeserver, accessToken: accessToken, method: "GET", path: "/_matrix/client/v3/pushers")
        let (data, response) = try await session.data(for: request)
        try requireSuccess(response)
        return try JSONDecoder().decode(MatrixPushersResponse.self, from: data)
    }

    private func request(homeserver: URL, accessToken: String, method: String, path: String) throws -> URLRequest {
        guard var components = URLComponents(url: homeserver, resolvingAgainstBaseURL: false) else { throw MatrixPusherTransportError.invalidResponse }
        let basePath = components.path == "/" ? "" : components.path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        components.path = "/" + (basePath.isEmpty ? "" : basePath + "/") + path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        components.query = nil
        components.fragment = nil
        guard let url = components.url else { throw MatrixPusherTransportError.invalidResponse }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.timeoutInterval = 20
        request.setValue("Bearer " + accessToken, forHTTPHeaderField: "Authorization")
        request.cachePolicy = .reloadIgnoringLocalCacheData
        return request
    }

    private func requireSuccess(_ response: URLResponse) throws {
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw MatrixPusherTransportError.serverRejected
        }
    }
}

private final class NoRedirectDelegate: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest) async -> URLRequest? {
        nil
    }
}
