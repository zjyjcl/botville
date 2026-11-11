// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Combine
import Foundation
import MatrixRustSDK

@MainActor
public protocol RoomNotificationSettingsProviding {
    func isMuted(_ room: MatrixRoom) async throws -> Bool
    func setMuted(_ muted: Bool, room: MatrixRoom) async throws
}

@MainActor
final class SDKRoomNotificationSettings: RoomNotificationSettingsProviding {
    let settings: NotificationSettings
    init(_ settings: NotificationSettings) { self.settings = settings }

    func isMuted(_ room: MatrixRoom) async throws -> Bool {
        // Missing account-data is unknown, never a confirmed unmuted default.
        guard try await settings.getRawPushRules() != nil else { throw MatrixServiceError.operationFailed }
        return try await settings.getRoomNotificationSettings(roomId: room.id,
            isEncrypted: room.encryption == .encrypted, isOneToOne: room.isDirect).mode == .mute
    }

    func setMuted(_ muted: Bool, room: MatrixRoom) async throws {
        if muted { try await settings.setRoomNotificationMode(roomId: room.id, mode: .mute) }
        else {
            try await settings.unmuteRoom(roomId: room.id,
                isEncrypted: room.encryption == .encrypted, isOneToOne: room.isDirect)
        }
    }
}

final class RoomNotificationSettingsBridge: NotificationSettingsDelegate, Sendable {
    let continuation: AsyncStream<Void>.Continuation
    init(_ continuation: AsyncStream<Void>.Continuation) { self.continuation = continuation }
    func settingsDidChange() { continuation.yield(()) }
}

public struct RoomMuteState: Equatable, Sendable {
    public var isMuted = false
    public var isLoaded = false
    public var isUpdating = false
    public var errorMessage: String?
}

public struct RoomMuteListPresentation: Equatable, Sendable {
    public let symbol: String?
    public let showsUnreadDot: Bool
    public let unreadNumber: UInt64?

    public init(isMuted: Bool, unreadCount: UInt64) {
        symbol = isMuted ? "bell.slash.fill" : nil
        showsUnreadDot = isMuted && unreadCount > 0
        unreadNumber = !isMuted && unreadCount > 0 ? unreadCount : nil
    }
}

/// All async completions are revision/session fenced; stale reads cannot undo a
/// toggle, and a response from a logged-out account cannot repopulate the cache.
@MainActor
public final class RoomMuteController: ObservableObject {
    @Published public private(set) var states: [String: RoomMuteState] = [:]
    private var hashes: Set<String>
    private let sharedStore: any SharedRoomMuteStoring
    private var provider: (any RoomNotificationSettingsProviding)?
    private var revisions: [String: UUID] = [:]
    private var session = UUID()
    var didChange: (() -> Void)?

    public init(sharedStore: any SharedRoomMuteStoring = SharedRoomMuteStore()) {
        self.sharedStore = sharedStore
        hashes = (try? sharedStore.load()) ?? []
    }

    func configure(provider: any RoomNotificationSettingsProviding) { self.provider = provider }

    public func state(for roomID: String) -> RoomMuteState {
        states[roomID] ?? RoomMuteState(isMuted: isMuted(roomID))
    }

    public func isMuted(_ roomID: String) -> Bool {
        states[roomID]?.isMuted ?? hashes.contains(SharedRoomMuteStore.hash(roomID))
    }

    public func refresh(_ room: MatrixRoom) async {
        guard let provider, !state(for: room.id).isUpdating else { return }
        let revision = UUID(), generation = session
        revisions[room.id] = revision
        do {
            let muted = try await provider.isMuted(room)
            guard generation == session, revisions[room.id] == revision else { return }
            publish(RoomMuteState(isMuted: muted, isLoaded: true), roomID: room.id)
        } catch {
            guard generation == session, revisions[room.id] == revision else { return }
            var state = state(for: room.id)
            state.errorMessage = "无法读取消息免打扰设置，请检查网络后重试。"
            states[room.id] = state
            didChange?()
        }
    }

    public func setMuted(_ muted: Bool, room: MatrixRoom) async {
        let previous = state(for: room.id)
        guard let provider, previous.isLoaded, !previous.isUpdating, previous.isMuted != muted else { return }
        let revision = UUID(), generation = session
        revisions[room.id] = revision
        publish(RoomMuteState(isMuted: muted, isLoaded: true, isUpdating: true), roomID: room.id)
        do {
            try await provider.setMuted(muted, room: room)
            let confirmed = try await provider.isMuted(room)
            guard generation == session, revisions[room.id] == revision else { return }
            var state = RoomMuteState(isMuted: confirmed, isLoaded: true)
            if confirmed != muted { state.errorMessage = "服务器尚未确认此设置，请稍后重试。" }
            publish(state, roomID: room.id)
        } catch {
            guard generation == session, revisions[room.id] == revision else { return }
            var restored = previous
            restored.errorMessage = "消息免打扰修改失败，已恢复原设置。请检查网络后重试。"
            publish(restored, roomID: room.id)
        }
    }

    func reset() throws {
        session = UUID()
        provider = nil
        revisions = [:]
        hashes = []
        states = [:]
        didChange?()
        try sharedStore.clear()
    }

    private func publish(_ value: RoomMuteState, roomID: String) {
        var state = value
        let hash = SharedRoomMuteStore.hash(roomID)
        if state.isMuted { hashes.insert(hash) } else { hashes.remove(hash) }
        do { try sharedStore.save(hashes) }
        catch { state.errorMessage = state.errorMessage ?? "设置已读回，但本机通知防御缓存未能保存；请重新打开 App 重试。" }
        states[roomID] = state
        didChange?()
    }
}
