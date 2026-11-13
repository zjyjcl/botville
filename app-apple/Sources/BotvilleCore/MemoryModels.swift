// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation
import Combine

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
    public var actions: MemoryPresentationPolicy.Actions {
        MemoryPresentationPolicy.actions(layer: layer, redacted: redacted, pinned: pinned)
    }
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
    public let bio: String?
    public let ownerID: String
    public let kind: String
    public let status: String
    public let revision: Int
    public let createdAt: Date
    public let updatedAt: Date
    public let visibility: String
    enum CodingKeys: String, CodingKey {
        case id, bio, kind, status, revision, visibility
        case displayName = "display_name"
        case ownerID = "owner_id"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
    }

    public func aiProfile(localServerName: String?) -> AIProfile {
        AIProfile(userID: id, displayName: displayName, avatarData: nil,
                  signature: bio, localServerName: localServerName)
    }

    public func encode(to encoder: Encoder) throws {
        var values = encoder.container(keyedBy: CodingKeys.self)
        try values.encode(id, forKey: .id)
        try values.encode(displayName, forKey: .displayName)
        try values.encode(bio, forKey: .bio)
        try values.encode(ownerID, forKey: .ownerID)
        try values.encode(kind, forKey: .kind)
        try values.encode(status, forKey: .status)
        try values.encode(revision, forKey: .revision)
        try values.encode(createdAt, forKey: .createdAt)
        try values.encode(updatedAt, forKey: .updatedAt)
        try values.encode(visibility, forKey: .visibility)
    }
}

public enum PersonaMood: String, Sendable {
    case neutral, happy, sad, excited, calm, anxious
}

public struct PersonaPublicView: Decodable, Sendable, Equatable, Identifiable {
    public let id: String
    public let displayName: String
    public let bio: String?
    public let status: String
    public let mood: PersonaMood?

    private enum CodingKeys: String, CodingKey {
        case id, bio, status, mood
        case displayName = "display_name"
    }

    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        id = try values.decode(String.self, forKey: .id)
        displayName = try values.decode(String.self, forKey: .displayName)
        bio = try values.decodeIfPresent(String.self, forKey: .bio)
        status = try values.decode(String.self, forKey: .status)
        mood = try values.decodeIfPresent(String.self, forKey: .mood).flatMap(PersonaMood.init(rawValue:))
    }
}

/// Contract 0.3.0 uses anyOf without a discriminator. An owner_id field selects
/// the full summary; malformed owner data must not silently become a public view.
public enum PersonaDetail: Decodable, Sendable, Equatable {
    case owner(MemoryPersona)
    case publicView(PersonaPublicView)

    private enum CodingKeys: String, CodingKey { case ownerID = "owner_id" }

    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        if values.contains(.ownerID) {
            self = .owner(try MemoryPersona(from: decoder))
        } else {
            self = .publicView(try PersonaPublicView(from: decoder))
        }
    }

    public var id: String {
        switch self { case .owner(let value): value.id; case .publicView(let value): value.id }
    }
    public var displayName: String {
        switch self { case .owner(let value): value.displayName; case .publicView(let value): value.displayName }
    }
    public var bio: String? {
        switch self { case .owner(let value): value.bio; case .publicView(let value): value.bio }
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

/// Shared with the native list so empty and permission-error states are testable.
@MainActor
public final class PersonaListModel: ObservableObject {
    @Published public private(set) var personas: [MemoryPersona] = []
    @Published public private(set) var isLoading = false
    @Published public private(set) var hasLoaded = false
    @Published public private(set) var errorMessage: String?
    public let client: any GatewayClient
    public var isEmpty: Bool { hasLoaded && !isLoading && errorMessage == nil && personas.isEmpty }
    public var showsLoading: Bool { isLoading || (!hasLoaded && errorMessage == nil) }

    public init(client: any GatewayClient) { self.client = client }

    public func load() async {
        guard !isLoading else { return }
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            personas = try await client.personas()
            hasLoaded = true
        } catch {
            personas = []
            if let error = error as? GatewayClientError {
                errorMessage = error.errorDescription
            }
            if errorMessage == nil { errorMessage = "暂时无法加载 AI，请稍后重试。" }
        }
    }
}
