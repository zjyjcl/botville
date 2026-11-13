// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation

public struct Memory: Codable, Sendable, Equatable, Identifiable {
    public let id: String
    public let personaID: String
    public let layer: String
    public let scope: MemoryScope
    public let visibility: String
    public let trust: String
    public let source: MemorySource
    public let pinned: Bool
    public let text: String?
    public let key: String?
    public let value: String?
    public let dirty: Bool?
    public let redacted: Bool
    public let createdAt: Date
    public let updatedAt: Date
    public let expiresAt: Date?
    /// Future layers remain visible as metadata but are not offered mutations.
    public var canMutate: Bool { ["recent", "summary", "profile"].contains(layer) }
    public var canPin: Bool { canMutate && layer != "summary" }
    enum CodingKeys: String, CodingKey {
        case id, layer, scope, visibility, trust, source, pinned, text, key, value, dirty, redacted
        case personaID = "persona_id"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case expiresAt = "expires_at"
    }
}
public struct MemoryScope: Codable, Sendable, Equatable {
    public let kind: String
    public let counterpart: String?
    public let roomID: String?
    enum CodingKeys: String, CodingKey {
        case kind, counterpart
        case roomID = "room_id"
    }
}
public struct MemorySource: Codable, Sendable, Equatable {
    public let kind: String
    public let eventID: String?
    public let sender: String?
    public let role: String?
    public let model: String?
    enum CodingKeys: String, CodingKey {
        case kind, sender, role, model
        case eventID = "event_id"
    }
}
public struct MemoryPage: Codable, Sendable, Equatable {
    public let items: [Memory]
    private let page: Page
    public var hasMore: Bool { page.hasMore }
    public var nextCursor: String? { page.nextCursor }
    public init(items: [Memory], nextCursor: String?, hasMore: Bool) {
        self.items = items
        page = Page(nextCursor: nextCursor, hasMore: hasMore)
    }
    private struct Page: Codable, Sendable, Equatable {
        let nextCursor: String?
        let hasMore: Bool
        enum CodingKeys: String, CodingKey {
            case nextCursor = "next_cursor"
            case hasMore = "has_more"
        }
    }
}
public struct MemoryFactWrite: Codable, Sendable, Equatable {
    public let personaID: String
    public let counterpart: String
    public let key: String
    public let value: String
    public let visibility: String?
    public init(
        personaID: String, counterpart: String, key: String, value: String,
        visibility: String? = nil
    ) {
        self.personaID = personaID
        self.counterpart = counterpart
        self.key = key
        self.value = value
        self.visibility = visibility
    }
    enum CodingKeys: String, CodingKey {
        case counterpart, key, value, visibility
        case personaID = "persona_id"
    }
}
public struct MemoryPersona: Codable, Sendable, Equatable, Identifiable {
    public let id: String
    public let displayName: String
    enum CodingKeys: String, CodingKey {
        case id
        case displayName = "display_name"
    }
}
struct MemoryPersonaPage: Decodable {
    let items: [MemoryPersona]
    let nextCursor: String?
    let hasMore: Bool
    enum CodingKeys: String, CodingKey {
        case items
        case nextCursor = "next_cursor"
        case hasMore = "has_more"
    }
}
