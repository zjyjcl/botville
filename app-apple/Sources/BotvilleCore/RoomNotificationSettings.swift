// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Combine
import Foundation
import MatrixRustSDK
#if DEBUG
import OSLog
#endif

@MainActor
public protocol RoomNotificationSettingsProviding {
    func isMuted(_ room: MatrixRoom) async throws -> Bool
    func setMuted(_ muted: Bool, room: MatrixRoom) async throws
    func mutedStates(for rooms: [MatrixRoom]) async throws -> [String: Bool]
}

public extension RoomNotificationSettingsProviding {
    func mutedStates(for rooms: [MatrixRoom]) async throws -> [String: Bool] {
        var result: [String: Bool] = [:]
        for room in rooms {
            try Task.checkCancellation()
            do { result[room.id] = try await isMuted(room) }
            catch is CancellationError { throw CancellationError() }
            catch { continue } // A missing value preserves this room's old state.
        }
        return result
    }
}

@MainActor
final class SDKRoomNotificationSettings: RoomNotificationSettingsProviding {
    let settings: NotificationSettings
    private let resolveEncryption: (MatrixRoom) async throws -> RoomEncryptionState

    init(_ settings: NotificationSettings,
         resolveEncryption: @escaping (MatrixRoom) async throws -> RoomEncryptionState = { _ in .unknown }) {
        self.settings = settings
        self.resolveEncryption = resolveEncryption
    }

    func isMuted(_ room: MatrixRoom) async throws -> Bool {
        // A failed single read must throw, especially after an optimistic write.
        guard try await settings.getRawPushRules() != nil else { throw MatrixServiceError.operationFailed }
        return try await readMuted(room)
    }

    func mutedStates(for rooms: [MatrixRoom]) async throws -> [String: Bool] {
        guard !rooms.isEmpty else { return [:] }
        // One account-data check per batch. Missing rules are unknown, never a
        // confirmed unmuted default. Each mode still comes from the SDK.
        guard try await settings.getRawPushRules() != nil else { throw MatrixServiceError.operationFailed }
        var result: [String: Bool] = [:]
        for room in rooms {
            try Task.checkCancellation()
            do { result[room.id] = try await readMuted(room) }
            catch is CancellationError { throw CancellationError() }
            catch { continue } // Retry on the next delegate/page/notification refresh.
        }
        return result
    }

    func setMuted(_ muted: Bool, room: MatrixRoom) async throws {
        if muted { try await settings.setRoomNotificationMode(roomId: room.id, mode: .mute) }
        else {
            let encrypted = try await isEncrypted(room)
            try Task.checkCancellation()
            try await settings.unmuteRoom(roomId: room.id,
                isEncrypted: encrypted, isOneToOne: room.isDirect)
        }
    }

    private func readMuted(_ room: MatrixRoom) async throws -> Bool {
        let encrypted = try await isEncrypted(room)
        try Task.checkCancellation()
        return try await settings.getRoomNotificationSettings(roomId: room.id,
            isEncrypted: encrypted, isOneToOne: room.isDirect).mode == .mute
    }

    private func isEncrypted(_ room: MatrixRoom) async throws -> Bool {
        let encryption: RoomEncryptionState
        do { encryption = room.encryption == .unknown ? try await resolveEncryption(room) : room.encryption }
        catch is CancellationError { throw CancellationError() }
        catch { throw RoomNotificationSettingsError.encryptionUnknown }
        switch encryption {
        case .encrypted: return true
        case .unencrypted: return false
        case .unknown: throw RoomNotificationSettingsError.encryptionUnknown
        }
    }
}

enum RoomNotificationSettingsError: Error { case encryptionUnknown }

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

    public func refresh(_ room: MatrixRoom) async { await refresh([room]) }

    func refresh(_ rooms: [MatrixRoom]) async {
        guard let provider else { return }
        let rooms = rooms.filter { !state(for: $0.id).isUpdating }
        guard !rooms.isEmpty else { return }
        let generation = session
        var reads: [String: UUID] = [:]
        for room in rooms {
            let revision = UUID()
            reads[room.id] = revision
            revisions[room.id] = revision
        }
        do {
            let values = try await provider.mutedStates(for: rooms)
            guard generation == session, !Task.isCancelled else { return }
            for room in rooms where revisions[room.id] == reads[room.id] {
                guard let muted = values[room.id] else { continue }
                publish(RoomMuteState(isMuted: muted, isLoaded: true), roomID: room.id)
            }
        } catch {
            guard generation == session, !Task.isCancelled else { return }
            for room in rooms where revisions[room.id] == reads[room.id] {
                var state = state(for: room.id)
                state.errorMessage = "无法读取消息免打扰设置，请检查网络后重试。"
                states[room.id] = state
            }
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
            if error is RoomNotificationSettingsError {
                restored.errorMessage = "暂时无法确认房间加密状态，已恢复原设置。请稍后重试。"
            } else {
                restored.errorMessage = "消息免打扰修改失败，已恢复原设置。请检查网络后重试。"
            }
            publish(restored, roomID: room.id)
        }
    }

    /// Offline extension defense must never block primary session cleanup.
    /// A later reset or confirmed readback retries the shared-file write.
    func reset() {
        session = UUID()
        provider = nil
        revisions = [:]
        hashes = []
        states = [:]
        didChange?()
        do { try sharedStore.clear() }
        catch {
#if DEBUG
            Logger(subsystem: "com.zjyjcl.botville", category: "RoomMute").debug("Shared mute cache cleanup unavailable.")
#endif
        }
    }

    private func publish(_ value: RoomMuteState, roomID: String) {
        var state = value
        let hash = SharedRoomMuteStore.hash(roomID)
        if state.isMuted { hashes.insert(hash) } else { hashes.remove(hash) }
        do { try sharedStore.save(hashes) }
        catch SharedRoomMuteStoreError.containerUnavailable {
#if DEBUG
            Logger(subsystem: "com.zjyjcl.botville", category: "RoomMute").debug("Shared mute container unavailable.")
#endif
        }
        catch { state.errorMessage = state.errorMessage ?? "设置已读回，但本机通知防御缓存未能保存；请重新打开 App 重试。" }
        states[roomID] = state
        didChange?()
    }
}
