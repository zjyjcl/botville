// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation

/// Parameters to send to the homeserver pusher endpoint once A1.2 provides a
/// Sygnal+APNs deployment.  This app does not request push entitlement or APNs
/// tokens in C2.1.
/// Matrix pusher `data` object.  `url` belongs to the Sygnal endpoint and is
/// intentionally a configuration value supplied only after A1.2 is ready.
public struct PushPusherData: Codable, Equatable, Sendable {
    public let url: URL

    /// The pusher endpoint receives device tokens. It must therefore use a
    /// credential-free HTTPS origin before this wire model can be created.
    public init(url: URL) throws {
        self.url = try Self.validatedAndNormalizedURL(url)
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let source = try container.decode(String.self, forKey: .url)
        guard EndpointURLPolicy.isSafeURLString(source),
              let url = URL(string: source) else {
            throw PushPusherDataError.invalidURL
        }
        self.url = try Self.validatedAndNormalizedURL(url, originalSource: source)
    }

    private enum CodingKeys: String, CodingKey {
        case url
    }

    private static func validatedAndNormalizedURL(_ url: URL, originalSource: String? = nil) throws -> URL {
        guard var components = try? EndpointURLPolicy.validatedComponents(url, originalSource: originalSource),
              components.scheme?.lowercased() == "https",
              components.fragment == nil else {
            throw PushPusherDataError.invalidURL
        }
        components.scheme = "https"
        guard let normalized = components.url else { throw PushPusherDataError.invalidURL }
        return normalized
    }
}

public enum PushPusherDataError: LocalizedError, Equatable, Sendable {
    case invalidURL

    public var errorDescription: String? {
        "推送服务地址必须是没有凭据的 HTTPS URL。"
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
