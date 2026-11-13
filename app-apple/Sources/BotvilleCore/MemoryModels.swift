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
    public let ownerID: String
    public let kind: String
    public let status: String
    public let revision: Int
    public let createdAt: Date
    public let updatedAt: Date
    public let visibility: String
    enum CodingKeys: String, CodingKey {
        case id, kind, status, revision, visibility
        case displayName = "display_name"
        case ownerID = "owner_id"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
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
    @Published public private(set) var errorMessage: String?
    public let client: any GatewayClient
    public var isEmpty: Bool { !isLoading && errorMessage == nil && personas.isEmpty }

    public init(client: any GatewayClient) { self.client = client }

    public func load() async {
        guard !isLoading else { return }
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do { personas = try await client.personas() } catch {
            personas = []
            if let error = error as? GatewayClientError {
                errorMessage = error == .problem(status: 403, code: "persona.not_owned")
                    ? "当前身份无权查看我的 AI，请重新登录后重试。"
                    : error.errorDescription
            }
            if errorMessage == nil { errorMessage = "暂时无法加载 AI，请稍后重试。" }
        }
    }
}
