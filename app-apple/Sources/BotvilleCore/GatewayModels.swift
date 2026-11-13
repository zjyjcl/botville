// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation

public struct GatewayCapabilityManifest: Codable, Sendable, Equatable {
    public let manifestVersion: String
    public let contractVersion: String
    public let generatedAt: Date
    public let cacheTTLSeconds: Int?
    public let defaults: GatewayManifestDefaults?
    public let capabilities: [GatewayCapability]
    enum CodingKeys: String, CodingKey {
        case manifestVersion = "manifest_version"
        case contractVersion = "contract_version"
        case generatedAt = "generated_at"
        case cacheTTLSeconds = "cache_ttl_seconds"
        case defaults, capabilities
    }
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        manifestVersion = try c.decode(String.self, forKey: .manifestVersion)
        contractVersion = try c.decode(String.self, forKey: .contractVersion)
        generatedAt = try c.decode(Date.self, forKey: .generatedAt)
        cacheTTLSeconds = try? c.decode(Int.self, forKey: .cacheTTLSeconds)
        defaults = try? c.decode(GatewayManifestDefaults.self, forKey: .defaults)
        capabilities = (try? c.decode(LossyArray<GatewayCapability>.self, forKey: .capabilities).values) ?? []
    }
}

public struct GatewayManifestDefaults: Codable, Sendable, Equatable {
    public let unknownCapability: String?
    public let cardFallback: String?
    enum CodingKeys: String, CodingKey {
        case unknownCapability = "unknown_capability"
        case cardFallback = "card_fallback"
    }
}

public struct GatewayCapability: Codable, Sendable, Equatable, Identifiable {
    public let id: String
    public let module: String
    public let version: String
    public let title: String
    public let summary: String
    public let description: String?
    public let status: String
    public let entry: GatewayCapabilityEntry
    public let scopes: [String]
    public let requires: [String]?
    public let sessionToken: GatewayCapabilitySessionConfig?
    public let unavailableReason: String?
    /// Disclosure is opt-in: absent and false both remain hidden in the App.
    public let ownerCanReadDM: Bool?
    enum CodingKeys: String, CodingKey {
        case id, module, version, title, summary, description, status, entry, scopes, requires
        case sessionToken = "session_token"
        case unavailableReason = "unavailable_reason"
        case ownerCanReadDM = "owner_can_read_dm"
    }
    public var isEnabled: Bool { status == "enabled" || status == "degraded" }
    public var normalizedEntryType: GatewayCapabilityEntryType { .init(rawValue: entry.type) }
}

public struct GatewayCapabilityEntry: Codable, Sendable, Equatable {
    public let type: String
    public let deepLink: String?
    public let webURL: URL?
    public let cardTypes: [String]?
    enum CodingKeys: String, CodingKey {
        case type
        case deepLink = "deep_link"
        case webURL = "web_url"
        case cardTypes = "card_types"
    }
}

public enum GatewayCapabilityEntryType: Sendable, Equatable {
    case native, web, cardList, none
    case unknown(String)
    public init(rawValue: String) {
        switch rawValue {
        case "native": self = .native
        case "web": self = .web
        case "card_list": self = .cardList
        case "none": self = .none
        default: self = .unknown(rawValue)
        }
    }
}

public struct GatewayCapabilitySessionConfig: Codable, Sendable, Equatable {
    public let supported: Bool
    public let ttlSeconds: Int?
    enum CodingKeys: String, CodingKey {
        case supported
        case ttlSeconds = "ttl_seconds"
    }
}

public struct GatewayCapabilityProjection: Sendable, Equatable {
    public let capability: GatewayCapability
    public let isVisible: Bool
    public let isAvailable: Bool
    public let nativeRoute: String?
}

extension GatewayCapabilityManifest {
    public func capabilityProjections(knownNativeRoutes: [String: String]) -> [GatewayCapabilityProjection] {
        let byID = Dictionary(capabilities.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
        func dependenciesAvailable(_ capability: GatewayCapability, visiting: Set<String> = []) -> Bool {
            guard !visiting.contains(capability.id) else { return false }
            return (capability.requires ?? []).allSatisfy { id in
                guard let required = byID[id], required.isEnabled else { return false }
                return dependenciesAvailable(required, visiting: visiting.union([capability.id]))
            }
        }
        return capabilities.map { capability in
            let available = capability.isEnabled && dependenciesAvailable(capability)
            switch capability.normalizedEntryType {
            case .native:
                let expected = knownNativeRoutes[capability.id]
                let route = expected == capability.entry.deepLink ? expected : nil
                let visible = available && (route != nil || defaults?.unknownCapability == "disabled")
                return .init(
                    capability: capability, isVisible: visible, isAvailable: available && route != nil,
                    nativeRoute: route)
            case .web, .cardList:
                return .init(
                    capability: capability, isVisible: available, isAvailable: available, nativeRoute: nil)
            case .none, .unknown:
                return .init(capability: capability, isVisible: false, isAvailable: false, nativeRoute: nil)
            }
        }
    }
}

public enum GatewayCapabilityVisibility {
    public static func visible(
        _ capability: GatewayCapability, knownNativeCapabilityIDs: Set<String>,
        defaults: GatewayManifestDefaults?
    ) -> Bool {
        guard capability.isEnabled else { return false }
        switch capability.normalizedEntryType {
        case .web, .cardList: return true
        case .native:
            return knownNativeCapabilityIDs.contains(capability.id)
                || defaults?.unknownCapability == "disabled"
        case .none, .unknown: return false
        }
    }
}

public struct GatewayCard: Codable, Sendable, Equatable, Identifiable {
    public let id: String
    public let cardSchemaVersion: String
    public let type: String
    public let title: String
    public let subtitle: String?
    public let fallbackText: String
    public let priority: String
    public let risk: String
    public let status: String
    public let blocks: [GatewayCardBlock]
    public let actions: [GatewayCardAction]
    public let source: GatewayCardSource
    public let expiresAt: Date?
    public let revision: Int?
    enum CodingKeys: String, CodingKey {
        case id
        case cardSchemaVersion = "card_schema_version"
        case type, title, subtitle
        case fallbackText = "fallback_text"
        case priority, risk, status, blocks, actions, source
        case expiresAt = "expires_at"
        case revision
    }
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = (try? c.decode(String.self, forKey: .id)) ?? "unknown-card"
        cardSchemaVersion = (try? c.decode(String.self, forKey: .cardSchemaVersion)) ?? "999.0.0"
        type = (try? c.decode(String.self, forKey: .type)) ?? "unknown"
        title = (try? c.decode(String.self, forKey: .title)) ?? ""
        subtitle = try? c.decode(String.self, forKey: .subtitle)
        fallbackText =
            (try? c.decode(String.self, forKey: .fallbackText)).flatMap { $0.isEmpty ? nil : $0 }
            ?? (title.isEmpty ? "此卡片暂无法显示。" : title)
        priority = (try? c.decode(String.self, forKey: .priority)) ?? "normal"
        risk = (try? c.decode(String.self, forKey: .risk)) ?? "high"
        status = (try? c.decode(String.self, forKey: .status)) ?? "unknown"
        blocks = (try? c.decode(LossyArray<GatewayCardBlock>.self, forKey: .blocks).values) ?? []
        actions = (try? c.decode(LossyArray<GatewayCardAction>.self, forKey: .actions).values) ?? []
        source = (try? c.decode(GatewayCardSource.self, forKey: .source)) ?? .unknown
        expiresAt = try? c.decode(Date.self, forKey: .expiresAt)
        revision = try? c.decode(Int.self, forKey: .revision)
    }
    public init(
        id: String, cardSchemaVersion: String, type: String, title: String, subtitle: String?,
        fallbackText: String, priority: String, risk: String, status: String, blocks: [GatewayCardBlock],
        actions: [GatewayCardAction], source: GatewayCardSource, expiresAt: Date?, revision: Int?
    ) {
        self.id = id
        self.cardSchemaVersion = cardSchemaVersion
        self.type = type
        self.title = title
        self.subtitle = subtitle
        self.fallbackText = fallbackText
        self.priority = priority
        self.risk = risk
        self.status = status
        self.blocks = blocks
        self.actions = actions
        self.source = source
        self.expiresAt = expiresAt
        self.revision = revision
    }
    public var effectiveRisk: GatewayRisk { .init(rawValue: risk) }
    public var isKnownType: Bool {
        ["notification", "approval", "moment", "system", "reminder", "result"].contains(type)
    }
    public var isActionable: Bool {
        guard id != "unknown-card", !id.isEmpty, isKnownType, ["active", "read"].contains(status),
            GatewayCardSchema.supports(cardSchemaVersion), expiresAt.map({ $0 > Date() }) ?? true
        else { return false }
        return (type != "approval" && effectiveRisk != .high) || expiresAt != nil
    }
    public var hasUnknownBlocks: Bool { blocks.contains { !$0.isKnownType } }
    public var renderableBlocks: [GatewayCardBlock] { Array(blocks.prefix(50)).filter(\.isKnownType) }
    public func withStatus(_ status: String, revision: Int? = nil) -> GatewayCard {
        .init(
            id: id, cardSchemaVersion: cardSchemaVersion, type: type, title: title, subtitle: subtitle,
            fallbackText: fallbackText, priority: priority, risk: risk, status: status, blocks: blocks,
            actions: actions, source: source, expiresAt: expiresAt, revision: revision ?? self.revision)
    }
}

public enum GatewayCardSchema {
    public static let maximum = "1.0"
    public static func supports(_ version: String) -> Bool {
        let parts = version.split(separator: ".", omittingEmptySubsequences: false)
        return parts.count == 3
            && parts.allSatisfy { !$0.isEmpty && $0.allSatisfy { $0 >= "0" && $0 <= "9" } } && parts[0] == "1"
    }
    public static func requestHeaderValue() -> String { maximum }
}

public struct GatewayCardBlock: Codable, Sendable, Equatable, Identifiable {
    public var id: String { "\(type)-\(text ?? label ?? url?.absoluteString ?? "block")" }
    public let type: String
    public let text: String?
    public let items: [GatewayCardBlockItem]?
    public let url: URL?
    public let alt: String?
    public let label: String?
    public let value: Double?
    public let max: Double?
    public let level: String?
    enum CodingKeys: String, CodingKey { case type, text, items, url, alt, label, value, max, level }
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        type = try c.decode(String.self, forKey: .type)
        text = try? c.decode(String.self, forKey: .text)
        items = try? c.decode([GatewayCardBlockItem].self, forKey: .items)
        url = try? c.decode(URL.self, forKey: .url)
        alt = try? c.decode(String.self, forKey: .alt)
        label = try? c.decode(String.self, forKey: .label)
        value = try? c.decode(Double.self, forKey: .value)
        max = try? c.decode(Double.self, forKey: .max)
        level = try? c.decode(String.self, forKey: .level)
    }
    public var isKnownType: Bool {
        ["text", "key_value", "divider", "image", "progress", "list", "code"].contains(type)
    }
}

public struct GatewayCardBlockItem: Codable, Sendable, Equatable {
    public let label: String
    public let value: String?
    public let emphasis: Bool?
}
public struct GatewayCardAction: Codable, Sendable, Equatable, Identifiable {
    public let id: String
    public let label: String
    public let kind: String
    public let style: String
    public let risk: String?
    public let target: String?
    public let capabilityID: String?
    public let url: URL?
    public let payload: [String: JSONValue]?
    public let confirmation: GatewayCardActionConfirmation?
    public let disabledReason: String?
    enum CodingKeys: String, CodingKey {
        case id, label, kind, style, risk, target, url, payload, confirmation
        case capabilityID = "capability_id"
        case disabledReason = "disabled_reason"
    }
    public var isKnownKind: Bool { ["approve", "deny", "open_url", "callback", "dismiss"].contains(kind) }
    public func effectiveRisk(cardRisk: GatewayRisk) -> GatewayRisk {
        risk.map(GatewayRisk.init(rawValue:)) ?? cardRisk
    }
}
public struct GatewayCardActionConfirmation: Codable, Sendable, Equatable {
    public let required: Bool
    public let title: String?
    public let body: String?
    public let biometric: Bool?
}
public enum GatewayRisk: Sendable, Equatable {
    case low, medium, high
    public init(rawValue: String) {
        switch rawValue {
        case "low": self = .low
        case "medium": self = .medium
        default: self = .high
        }
    }
}
public struct GatewayCardSource: Codable, Sendable, Equatable {
    public let module: String
    public let capabilityID: String?
    enum CodingKeys: String, CodingKey {
        case module
        case capabilityID = "capability_id"
    }
    static let unknown = GatewayCardSource(module: "unknown", capabilityID: nil)
    public init(module: String, capabilityID: String?) {
        self.module = module
        self.capabilityID = capabilityID
    }
}
public struct GatewayCardPage: Codable, Sendable, Equatable {
    public let items: [GatewayCard]
    public let nextCursor: String?
    public let hasMore: Bool
    enum CodingKeys: String, CodingKey {
        case items
        case nextCursor = "next_cursor"
        case hasMore = "has_more"
    }
}
public struct GatewayActionReceipt: Codable, Sendable, Equatable {
    public let cardID: String
    public let actionID: String
    public let status: String
    public let auditID: String?
    public let card: GatewayCard
    enum CodingKeys: String, CodingKey {
        case cardID = "card_id"
        case actionID = "action_id"
        case status
        case auditID = "audit_id"
        case card
    }
}

public struct GatewaySessionToken: Codable, Sendable, Equatable {
    public let code: String
    public let codeTTLSeconds: Int
    public let expiresAt: Date
    public let audience: String
    public let url: URL
    public let allowedOrigins: [String]
    enum CodingKeys: String, CodingKey {
        case code
        case codeTTLSeconds = "code_ttl_seconds"
        case expiresAt = "expires_at"
        case audience, url
        case allowedOrigins = "allowed_origins"
    }
}
public struct ModuleWebSessionGrant: Sendable, Equatable {
    public let capabilityID: String
    public let url: URL
    public let allowedOrigins: [String]
    public let codeTTLSeconds: Int
    public let expiresAt: Date
}
public protocol ModuleWebSessionProviding: Sendable {
    func createModuleWebSession(
        capabilityID: String, url: URL, allowedOrigins: [String]?, scopes: [String]?, ttlSeconds: Int?
    ) async throws -> ModuleWebSessionGrant
}
public struct GatewayCookieExchange: Sendable, Equatable {
    public let statusCode: Int
}

public protocol HighRiskAuthorizer: Sendable {
    func authorize(cardID: String, actionID: String, revision: Int) async throws -> GatewayHighRiskAssertion
}
public struct GatewayHighRiskAssertion: Sendable, Equatable {
    public let challenge: String
    public let assertion: String
}
public struct GatewayActionChallenge: Codable, Sendable, Equatable {
    public let challenge: String
    public let expiresAt: Date
    public let cardID: String
    public let actionID: String
    public let revision: Int
    public let algorithm: String
    enum CodingKeys: String, CodingKey {
        case challenge
        case expiresAt = "expires_at"
        case cardID = "card_id"
        case actionID = "action_id"
        case revision, algorithm
    }
}
public protocol DeviceAssertionSigning: Sendable {
    func sign(cardID: String, actionID: String, revision: Int, challenge: String, algorithm: String)
        async throws -> String
}
public enum HighRiskAuthorizationError: LocalizedError {
    case unavailable
    public var errorDescription: String? { "需在后续版本授权" }
}
public struct UnavailableHighRiskAuthorizer: HighRiskAuthorizer {
    public init() {}
    public func authorize(cardID: String, actionID: String, revision: Int) async throws
        -> GatewayHighRiskAssertion
    { throw HighRiskAuthorizationError.unavailable }
}

public enum JSONValue: Codable, Sendable, Equatable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case object([String: JSONValue])
    case array([JSONValue])
    case null
    public init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() {
            self = .null
        } else if let value = try? c.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? c.decode(Double.self) {
            self = .number(value)
        } else if let value = try? c.decode(String.self) {
            self = .string(value)
        } else if let value = try? c.decode([String: JSONValue].self) {
            self = .object(value)
        } else {
            self = .array(try c.decode([JSONValue].self))
        }
    }
    public func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .string(let value): try c.encode(value)
        case .number(let value): try c.encode(value)
        case .bool(let value): try c.encode(value)
        case .object(let value): try c.encode(value)
        case .array(let value): try c.encode(value)
        case .null: try c.encodeNil()
        }
    }
}
private struct LossyArray<Element: Decodable>: Decodable {
    let values: [Element]
    init(from decoder: Decoder) throws {
        var c = try decoder.unkeyedContainer()
        var result: [Element] = []
        while !c.isAtEnd {
            if let value = try? c.decode(Element.self) {
                result.append(value)
            } else {
                _ = try? c.decode(JSONValue.self)
            }
        }
        values = result
    }
}

enum GatewayDate {
    static func decode(_ decoder: Decoder) throws -> Date {
        let container = try decoder.singleValueContainer()
        let value = try container.decode(String.self)
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = fractional.date(from: value) { return date }
        let standard = ISO8601DateFormatter()
        standard.formatOptions = [.withInternetDateTime]
        if let date = standard.date(from: value) { return date }
        throw DecodingError.dataCorruptedError(
            in: container, debugDescription: "Expected an ISO 8601 date-time.")
    }
}
