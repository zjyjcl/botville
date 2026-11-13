// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Combine
import Foundation

public enum GroupRoomState: Sendable, Equatable, Hashable, Codable {
    case accepted, encryptedDisabled, rejectedEncrypted, left
    case unknown(String)
    public init(from decoder: Decoder) throws {
        self = Self.value(try decoder.singleValueContainer().decode(String.self))
    }
    public func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        try c.encode(rawValue)
    }
    public var rawValue: String {
        switch self {
        case .accepted: "accepted"
        case .encryptedDisabled: "encrypted_disabled"
        case .rejectedEncrypted: "rejected_encrypted"
        case .left: "left"
        case .unknown(let value): value
        }
    }
    static func value(_ raw: String) -> Self {
        switch raw {
        case "accepted": .accepted
        case "encrypted_disabled": .encryptedDisabled
        case "rejected_encrypted": .rejectedEncrypted
        case "left": .left
        default: .unknown(raw)
        }
    }
}

public enum GroupRoomMode: Sendable, Equatable, Hashable, Codable {
    case mentionOnly, muted
    case unknown(String)
    public init(from decoder: Decoder) throws {
        self = Self.value(try decoder.singleValueContainer().decode(String.self))
    }
    public func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        try c.encode(rawValue)
    }
    public var rawValue: String {
        switch self {
        case .mentionOnly: "mention_only"
        case .muted: "muted"
        case .unknown(let value): value
        }
    }
    static func value(_ raw: String) -> Self {
        switch raw {
        case "mention_only": .mentionOnly
        case "muted": .muted
        default: .unknown(raw)
        }
    }
}

public struct GroupRoom: Codable, Sendable, Equatable, Identifiable {
    public let roomRef: String
    public let state: GroupRoomState
    public let mode: GroupRoomMode
    public let joinedAt: Date?
    public let updatedAt: Date
    public let leavePending: Bool?
    public var id: String { roomRef }
    enum CodingKeys: String, CodingKey {
        case roomRef = "room_ref"
        case state, mode
        case joinedAt = "joined_at"
        case updatedAt = "updated_at"
        case leavePending = "leave_pending"
    }
}

public struct GroupRoomList: Codable, Sendable, Equatable {
    public let items: [GroupRoom]
    public let nextCursor: String?
    public let hasMore: Bool
    public let groupsEnabled: Bool
    enum CodingKeys: String, CodingKey {
        case items
        case nextCursor = "next_cursor"
        case hasMore = "has_more"
        case groupsEnabled = "groups_enabled"
    }
}

public enum GroupRoomPresentation {
    public static func label(_ room: GroupRoom) -> String {
        if room.state != .left, case .unknown = room.mode { return "状态未知" }
        switch room.state {
        case .accepted: return "已加入"
        case .encryptedDisabled: return "加密群，AI 不参与"
        case .left: return "已退出"
        case .rejectedEncrypted: return "已拒绝（加密）"
        case .unknown: return "状态未知"
        }
    }
    public static func canChangeMode(_ room: GroupRoom) -> Bool {
        switch room.state {
        case .accepted, .encryptedDisabled: return room.mode == .mentionOnly || room.mode == .muted
        default: return false
        }
    }
}

/// Reads can supersede older reads. Writes are serialized so a stale page can
/// never undo a successful mutation or reintroduce a rotated room reference.
@MainActor
public final class PersonaGroupRoomsModel: ObservableObject {
    @Published public private(set) var rooms: [GroupRoom] = []
    @Published private var deploymentGroupsEnabled = true
    public let settings: PersonaGroupsSettingsController
    private var settingsObservation: AnyCancellable?

    public var groupsEnabled: Bool { deploymentGroupsEnabled && settings.groups.enabled }
    public var canEnableMentionOnly: Bool {
        groupsEnabled && settings.hasLoaded && !settings.isLoading && !settings.isSaving
    }
    @Published public private(set) var includeAll = false
    @Published public private(set) var isLoading = false
    @Published public private(set) var isLoadingMore = false
    @Published public private(set) var errorMessage: String?
    @Published private var submitting = Set<String>()
    private let personaID: String
    private let client: any GatewayClient
    private var nextCursor: String?
    private var hasMore = false
    private var generation = 0
    private var seenCursors = Set<String>()
    private var paginationNeedsRefresh = false

    public init(
        personaID: String, client: any GatewayClient, settings: PersonaGroupsSettingsController
    ) {
        self.personaID = personaID
        self.client = client
        self.settings = settings
        // The settings screen and rooms screen share this owner-scoped object.
        // Forward optimistic changes and rollbacks without reloading the page.
        settingsObservation = settings.objectWillChange.sink { [weak self] in
            self?.objectWillChange.send()
        }
    }

    public var canLoadMore: Bool { hasMore && nextCursor != nil }
    public var isUpdating: Bool { !submitting.isEmpty }
    public func isSubmitting(roomRef: String) -> Bool { submitting.contains(roomRef) }
    public func load() async { await refresh() }

    public func refresh() async {
        guard !isUpdating else { return }
        _ = await reload()
    }

    public func setIncludeAll(_ value: Bool) async {
        guard includeAll != value, !isUpdating else { return }
        includeAll = value
        _ = await reload()
    }

    /// `preservingRoomsOnFailure` keeps the last list visible until a fresh first
    /// page arrives (409 conflict refresh). On failure the stale rows stay, and the
    /// next loadMore restarts from the first page instead of an old cursor.
    /// The room list and persona settings refresh independently; each flag reports
    /// only its own request so a settings failure never hides a fresh room page.
    /// `settingsError` is the settings error captured by this reload, so the
    /// combined conflict message never needs its own fallback text.
    struct ReloadOutcome: Equatable {
        var roomsRefreshed = false
        var settingsError: String?
        var settingsRefreshed: Bool { settingsError == nil }
    }

    static let conflictRefreshedMessage = "群状态已变化，已刷新"

    @discardableResult
    private func reload(preservingRoomsOnFailure: Bool = false) async -> ReloadOutcome {
        generation += 1
        let requestGeneration = generation
        isLoading = true
        isLoadingMore = false
        if !preservingRoomsOnFailure {
            rooms = []
            nextCursor = nil
            hasMore = false
            seenCursors = []
            paginationNeedsRefresh = false
        }
        errorMessage = nil
        defer { if requestGeneration == generation { isLoading = false } }
        await settings.refresh()
        guard !Task.isCancelled, requestGeneration == generation else { return ReloadOutcome() }
        var outcome = ReloadOutcome(settingsError: settings.errorMessage)
        errorMessage = settings.errorMessage
        do {
            let page = try await client.groupRooms(
                personaID: personaID, limit: 50,
                cursor: nil, includeAll: includeAll)
            guard !Task.isCancelled, requestGeneration == generation else { return ReloadOutcome() }
            if preservingRoomsOnFailure {
                seenCursors = []
                paginationNeedsRefresh = false
            }
            outcome.roomsRefreshed = apply(page, replacing: true)
            return outcome
        } catch {
            guard !Task.isCancelled, requestGeneration == generation else { return ReloadOutcome() }
            if preservingRoomsOnFailure { paginationNeedsRefresh = true }
            errorMessage = message(for: error)
            return outcome
        }
    }

    public func loadMore() async {
        guard !isLoading, !isLoadingMore, !isUpdating, canLoadMore,
            let cursor = nextCursor
        else { return }
        // A mutation moves the server's keyset ordering. Start over rather than
        // continue from a cursor issued before that change.
        if paginationNeedsRefresh {
            await refresh()
            return
        }
        guard seenCursors.insert(cursor).inserted else {
            hasMore = false
            errorMessage = "分页信息异常，请刷新后重试。"
            return
        }
        let requestGeneration = generation
        isLoadingMore = true
        errorMessage = nil
        defer { if requestGeneration == generation { isLoadingMore = false } }
        do {
            let page = try await client.groupRooms(
                personaID: personaID, limit: 50,
                cursor: cursor, includeAll: includeAll)
            guard !Task.isCancelled, requestGeneration == generation else { return }
            apply(page, replacing: false)
        } catch {
            guard !Task.isCancelled, requestGeneration == generation else { return }
            seenCursors.remove(cursor)
            errorMessage = message(for: error)
        }
    }

    public func setMode(roomRef: String, mode: GroupRoomMode) async {
        guard mode == .muted || mode == .mentionOnly else { return }
        // Enforce the same one-way control as the UI, including stale callbacks.
        guard mode != .mentionOnly || canEnableMentionOnly else { return }
        await mutate(roomRef) {
            try await self.client.updateGroupRoomMode(
                personaID: self.personaID, roomRef: roomRef, mode: mode)
        }
    }

    public func leave(roomRef: String) async {
        await mutate(roomRef) {
            try await self.client.leaveGroupRoom(personaID: self.personaID, roomRef: roomRef)
        }
    }

    private func mutate(_ ref: String, action: () async throws -> GroupRoom) async {
        guard !isUpdating, !isLoading, !isLoadingMore,
            let room = rooms.first(where: { $0.roomRef == ref }),
            GroupRoomPresentation.canChangeMode(room)
        else { return }
        submitting.insert(ref)
        errorMessage = nil
        defer { submitting.remove(ref) }
        do {
            let updated = try await action()
            guard !Task.isCancelled else { return }
            guard updated.roomRef == ref else { throw GatewayClientError.invalidResponse }
            replace(updated)
            paginationNeedsRefresh = true
        } catch let error as GatewayClientError {
            guard !Task.isCancelled else { return }
            if error.problemCode == "room.state_conflict" {
                // Keep the old rows if the refresh fails; 409 does not invalidate references.
                reportConflictRefresh(await reload(preservingRoomsOnFailure: true))
            } else if error.problemCode == "room.not_found" {
                // 404 may mean the HMAC key rotated, so every cached room_ref is suspect.
                reportConflictRefresh(await reload())
            } else {
                errorMessage = message(for: error)
            }
        } catch {
            guard !Task.isCancelled else { return }
            errorMessage = message(for: error)
        }
    }

    /// Rooms refreshed: always say so. Settings failed too: append that error, since
    /// mention_only stays unavailable until settings load. Rooms failed: keep reload's error.
    private func reportConflictRefresh(_ outcome: ReloadOutcome) {
        guard outcome.roomsRefreshed else { return }
        if let detail = outcome.settingsError {
            errorMessage = "\(Self.conflictRefreshedMessage)；群设置未能刷新：\(detail)"
        } else {
            errorMessage = Self.conflictRefreshedMessage
        }
    }

    @discardableResult
    private func apply(_ page: GroupRoomList, replacing: Bool) -> Bool {
        deploymentGroupsEnabled = page.groupsEnabled
        if replacing { rooms = [] }
        for item in page.items { replace(item) }
        nextCursor = page.nextCursor
        hasMore = page.hasMore
        if hasMore && (nextCursor == nil || seenCursors.contains(nextCursor!)) {
            hasMore = false
            errorMessage = "分页信息异常，请刷新后重试。"
            return false
        }
        return true
    }

    private func replace(_ room: GroupRoom) {
        if let index = rooms.firstIndex(where: { $0.roomRef == room.roomRef }) {
            rooms[index] = room
        } else {
            rooms.append(room)
        }
    }

    private func message(for error: Error) -> String {
        (error as? GatewayClientError)?.errorDescription ?? "操作失败，请稍后重试。"
    }
}

@MainActor
public final class PersonaGroupsSettingsController: ObservableObject {
    @Published public private(set) var groups: PersonaGroupsSettings
    @Published public private(set) var isLoading = false
    @Published public private(set) var hasLoaded = false
    @Published public private(set) var isSaving = false
    @Published public private(set) var errorMessage: String?
    private let personaID: String
    private let client: any GatewayClient

    public init(personaID: String, initialGroups: PersonaGroupsSettings, client: any GatewayClient)
    {
        self.personaID = personaID
        groups = initialGroups
        self.client = client
    }

    public func load() async { await refresh() }
    public func refresh() async {
        guard !isLoading, !isSaving else { return }
        isLoading = true
        hasLoaded = false
        errorMessage = nil
        defer { isLoading = false }
        do {
            let detail = try await client.persona(id: personaID)
            guard !Task.isCancelled else { return }
            groups = try validatedGroups(detail)
            hasLoaded = true
        } catch {
            guard !Task.isCancelled else { return }
            errorMessage = message(for: error)
        }
    }

    public func setEnabled(_ value: Bool) async {
        await save(.init(enabled: value, invitePolicy: groups.invitePolicy))
    }
    public func setInvitePolicy(_ value: PersonaGroupsSettings.InvitePolicy) async {
        await save(.init(enabled: groups.enabled, invitePolicy: value))
    }

    private func save(_ value: PersonaGroupsSettings) async {
        guard hasLoaded, !isLoading, !isSaving else { return }
        let old = groups
        groups = value
        isSaving = true
        errorMessage = nil
        defer { isSaving = false }
        do {
            let detail = try await client.updatePersonaGroups(id: personaID, groups: value)
            guard !Task.isCancelled else {
                groups = old
                return
            }
            groups = try validatedGroups(detail)
        } catch {
            groups = old
            guard !Task.isCancelled else { return }
            errorMessage = message(for: error)
        }
    }

    private func validatedGroups(_ detail: PersonaDetail) throws -> PersonaGroupsSettings {
        guard case .owner(let persona) = detail, persona.id == personaID,
            let response = persona.groups
        else { throw GatewayClientError.invalidResponse }
        return response
    }
    private func message(for error: Error) -> String {
        (error as? GatewayClientError)?.errorDescription ?? "暂时无法保存或加载群设置，请稍后重试。"
    }
}
