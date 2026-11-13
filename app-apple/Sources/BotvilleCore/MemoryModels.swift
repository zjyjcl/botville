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
/// Contract 0.5.0 persona configuration. Missing enabled never opts into sending.
/// Other optional fields preserve absence so the server can apply its defaults.
public struct ProactiveSettings: Codable, Sendable, Equatable {
    public let enabled: Bool
    public let maxPerDay: Int?
    public let requireReply: Bool?
    public let quietHours: SilentHours?

    private enum CodingKeys: String, CodingKey {
        case enabled
        case maxPerDay = "max_per_day"
        case requireReply = "require_reply"
        case quietHours = "quiet_hours"
    }

    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        enabled = try values.decodeIfPresent(Bool.self, forKey: .enabled) ?? false
        maxPerDay = try values.decodeIfPresent(Int.self, forKey: .maxPerDay)
        requireReply = try values.decodeIfPresent(Bool.self, forKey: .requireReply)
        quietHours = try values.decodeIfPresent(SilentHours.self, forKey: .quietHours)
    }
}

public struct SilentHours: Codable, Sendable, Equatable {
    public let enabled: Bool
    public let start: String
    public let end: String
    public let timezone: String

    private enum CodingKeys: String, CodingKey { case enabled, start, end, timezone }

    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        enabled = try values.decodeIfPresent(Bool.self, forKey: .enabled) ?? true
        start = try values.decode(String.self, forKey: .start)
        end = try values.decode(String.self, forKey: .end)
        timezone = try values.decode(String.self, forKey: .timezone)
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
    /// Owner detail responses add this field; list summaries intentionally omit it.
    /// Unknown response values are treated as absent for forward compatibility.
    public let mood: PersonaMood?
    /// Full Persona configuration only; summaries and detail views omit it.
    public let proactive: ProactiveSettings?
    enum CodingKeys: String, CodingKey {
        case id, bio, kind, status, revision, visibility, mood, proactive
        case displayName = "display_name"
        case ownerID = "owner_id"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
    }

    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        id = try values.decode(String.self, forKey: .id)
        displayName = try values.decode(String.self, forKey: .displayName)
        bio = try values.decodeIfPresent(String.self, forKey: .bio)
        ownerID = try values.decode(String.self, forKey: .ownerID)
        kind = try values.decode(String.self, forKey: .kind)
        status = try values.decode(String.self, forKey: .status)
        revision = try values.decode(Int.self, forKey: .revision)
        createdAt = try values.decode(Date.self, forKey: .createdAt)
        updatedAt = try values.decode(Date.self, forKey: .updatedAt)
        visibility = try values.decode(String.self, forKey: .visibility)
        mood = try values.decodeIfPresent(String.self, forKey: .mood).flatMap(PersonaMood.init(rawValue:))
        proactive = try values.decodeIfPresent(ProactiveSettings.self, forKey: .proactive)
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
        try values.encodeIfPresent(mood, forKey: .mood)
        try values.encodeIfPresent(proactive, forKey: .proactive)
    }
}

public enum PersonaMood: String, Codable, Sendable, CaseIterable {
    case calm, happy, sad, excited, tired

    public var label: String {
        switch self {
        case .calm: "平静"
        case .happy: "愉快"
        case .sad: "低落"
        case .excited: "兴奋"
        case .tired: "疲惫"
        }
    }

    public var symbolName: String {
        switch self {
        case .calm: "leaf"
        case .happy: "face.smiling"
        case .sad: "cloud.rain"
        case .excited: "sparkles"
        case .tired: "bed.double"
        }
    }
}

/// Presentation consumed by both mood views; only owner state carries visibility.
public struct PersonaMoodPresentation: Sendable, Equatable {
    public let mood: PersonaMood?
    public let privateMarkerText: String?

    public init(ownerState: PersonaState) {
        mood = ownerState.mood
        privateMarkerText = ownerState.showsPrivateMoodMarker ? "仅自己可见" : nil
    }

    public init(counterpartMood: PersonaMood?) {
        mood = counterpartMood
        privateMarkerText = nil
    }
}

/// Owner-only `/personas/{id}/state` response. The response mood is an object,
/// whereas public persona details expose the label as a string.
public struct PersonaState: Decodable, Sendable, Equatable {
    public let personaID: String
    public let mood: PersonaMood?
    public let moodPublic: Bool
    public let updatedAt: Date?

    /// Owner-only decoration; private moods remain visible to their owner.
    public var showsPrivateMoodMarker: Bool { mood != nil && !moodPublic }

    private enum CodingKeys: String, CodingKey {
        case personaID = "persona_id"
        case mood
        case moodPublic = "mood_public"
        case updatedAt = "updated_at"
    }
    private enum MoodKeys: String, CodingKey { case label }

    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        personaID = try values.decode(String.self, forKey: .personaID)
        moodPublic = try values.decode(Bool.self, forKey: .moodPublic)
        updatedAt = try values.decodeIfPresent(Date.self, forKey: .updatedAt)
        guard values.contains(.mood), !(try values.decodeNil(forKey: .mood)) else {
            mood = nil
            return
        }
        let moodValues = try values.nestedContainer(keyedBy: MoodKeys.self, forKey: .mood)
        mood = try moodValues.decodeIfPresent(String.self, forKey: .label).flatMap(PersonaMood.init(rawValue:))
    }

    public init(personaID: String, mood: PersonaMood?, moodPublic: Bool, updatedAt: Date? = nil) {
        self.personaID = personaID
        self.mood = mood
        self.moodPublic = moodPublic
        self.updatedAt = updatedAt
    }
}

public struct PersonaStatePatch: Encodable, Sendable, Equatable {
    public let mood: PersonaMood?
    public let moodPublic: Bool?

    public init(mood: PersonaMood? = nil, moodPublic: Bool? = nil) {
        self.mood = mood
        self.moodPublic = moodPublic
    }

    enum CodingKeys: String, CodingKey { case mood; case moodPublic = "mood_public" }
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

/// Contract 0.5.0 uses anyOf without a discriminator. An owner_id field selects
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
    public var mood: PersonaMood? {
        switch self { case .owner(let value): value.mood; case .publicView(let value): value.mood }
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
