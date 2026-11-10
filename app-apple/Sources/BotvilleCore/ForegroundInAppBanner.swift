// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Combine
import Foundation

/// A display-safe notification assembled from the locally synced Matrix state.
/// It intentionally carries no APNs or credential material.
public struct ForegroundBannerCandidate: Equatable, Sendable {
    public let roomID: String
    public let eventID: String?
    public let latestActivity: Date
    public let title: String
    public let preview: String
    public let isOwn: Bool

    public init(roomID: String, eventID: String? = nil, latestActivity: Date,
                title: String, preview: String, isOwn: Bool = false) {
        self.roomID = roomID
        self.eventID = eventID
        self.latestActivity = latestActivity
        self.title = title
        self.preview = preview
        self.isOwn = isOwn
    }

    public var route: PushNotificationRoute { .room(roomID) }

    fileprivate var deduplicationKey: String {
        roomID + "|" + (eventID ?? String(latestActivity.timeIntervalSince1970))
    }
}

/// Presentation policy shared by the sync bridge and the SwiftUI overlay.
@MainActor
public final class ForegroundInAppBannerStore: ObservableObject {
    @Published public private(set) var current: ForegroundBannerCandidate?

    private let displayDuration: Duration
    private let sleep: @Sendable (Duration) async -> Void
    private var dismissalTask: Task<Void, Never>?
    // Until the root view explicitly reports an active scene, no synced state
    // may become a foreground banner during launch or restoration.
    private var isForeground = false
    private var visibleRoomID: String?
    private var isInputActive = false
    private var consumedKeys = Set<String>()
    private var consumedOrder = [String]()

    public init() {
        displayDuration = .seconds(3)
        sleep = { duration in try? await Task.sleep(for: duration) }
    }

    init(displayDuration: Duration,
         sleep: @escaping @Sendable (Duration) async -> Void) {
        self.displayDuration = displayDuration
        self.sleep = sleep
    }

    deinit { dismissalTask?.cancel() }

    public func updateContext(isForeground: Bool, visibleRoomID: String?, isInputActive: Bool) {
        self.isForeground = isForeground
        self.visibleRoomID = visibleRoomID
        self.isInputActive = isInputActive
        if let current, shouldSuppress(current) { dismiss() }
    }

    public func receive(_ candidate: ForegroundBannerCandidate) {
        let key = candidate.deduplicationKey
        guard !consumedKeys.contains(key) else { return }
        consume(key)
        guard !shouldSuppress(candidate) else { return }
        current = candidate
        dismissalTask?.cancel()
        let expected = candidate
        let duration = displayDuration
        let sleep = sleep
        dismissalTask = Task { [weak self] in
            await sleep(duration)
            guard !Task.isCancelled else { return }
            self?.dismissIfCurrent(expected)
        }
    }

    public func dismiss() {
        dismissalTask?.cancel()
        dismissalTask = nil
        current = nil
    }

    public func reset() {
        dismiss()
        consumedKeys.removeAll()
        consumedOrder.removeAll()
        isForeground = false
        visibleRoomID = nil
        isInputActive = false
    }

    public func takeRoute() -> PushNotificationRoute? {
        guard let current else { return nil }
        dismiss()
        return current.route
    }

    private func dismissIfCurrent(_ expected: ForegroundBannerCandidate) {
        guard current == expected else { return }
        dismiss()
    }

    private func shouldSuppress(_ candidate: ForegroundBannerCandidate) -> Bool {
        !isForeground || candidate.isOwn || visibleRoomID == candidate.roomID || isInputActive
    }

    private func consume(_ key: String) {
        consumedKeys.insert(key)
        consumedOrder.append(key)
        while consumedOrder.count > 256 {
            consumedKeys.remove(consumedOrder.removeFirst())
        }
    }
}

/// A room-list snapshot and its optional displayable latest event. The event is
/// optional because the list can refresh before the SDK has a renderable preview.
struct ForegroundBannerSnapshot {
    let room: MatrixRoom
    let candidate: ForegroundBannerCandidate?
}

/// Navigation owns visibility, independent of retained RoomView lifecycles.
/// Room-attached sheets retain the room context (and same-room suppression).
public enum ForegroundBannerVisibility {
    public static func roomID(isLoggedIn: Bool, selectedRoomID: String?,
                              isCompact: Bool, showsDetail: Bool,
                              isSceneActive: Bool, isChatTab: Bool,
                              joinedRoomIDs: [String]) -> String? {
        guard isLoggedIn, isSceneActive, isChatTab, let selectedRoomID,
              !isCompact || showsDetail,
              joinedRoomIDs.contains(selectedRoomID) else { return nil }
        return selectedRoomID
    }
}

/// A background edge never invalidates the online baseline. Foreground messages
/// remain eligible even if a network-driven replacement sync is still draining.
struct ForegroundBannerResumeState {
    private var isActive = true
    var isForeground: Bool { isActive }
    mutating func enteredBackground() {
        isActive = false
    }
    @discardableResult
    mutating func becameActive() -> Bool {
        let wasBackground = !isActive
        isActive = true
        return wasBackground
    }
    func shouldPresent(_ candidate: ForegroundBannerCandidate,
                       suppressedEvents: [PushEventReference]) -> Bool {
        isActive && !suppressedEvents.contains {
            $0.roomID == candidate.roomID && $0.eventID == candidate.eventID
        }
    }
}

/// The commit closure includes both UI publication and detector mutation.
/// Keeping them behind one gate prevents rejected snapshots arming the baseline.
enum RoomRefreshCommitGate {
    @discardableResult
    static func commit(onlineGeneration: UUID?, currentGeneration: UUID,
                       isCancelled: Bool, body: () -> Void) -> Bool {
        if let onlineGeneration {
            guard !isCancelled, onlineGeneration == currentGeneration else { return false }
        }
        body()
        return true
    }
}

/// Only a committed online snapshot can establish the notification baseline.
/// Cached rooms remain available to the UI without arming history replay.
struct ForegroundBannerDetector {
    private struct Baseline: Equatable {
        let unreadCount: UInt64
        let latestActivity: Date?
        let candidateKey: String?
    }

    private var baselines = [String: Baseline]()
    private var hasBaseline = false

    mutating func beginCatchUp() {
        hasBaseline = false
    }

    mutating func candidates(in snapshots: [ForegroundBannerSnapshot],
                             isOnlineSync: Bool) -> [ForegroundBannerCandidate] {
        // Local refreshes must neither arm nor advance the online baseline.
        guard isOnlineSync else { return [] }
        var next = [String: Baseline]()
        var result = [ForegroundBannerCandidate]()
        for snapshot in snapshots {
            let room = snapshot.room
            guard room.membership == .joined else { continue }
            let candidate = snapshot.candidate
            let baseline = Baseline(unreadCount: room.unreadCount,
                                    latestActivity: room.latestActivity,
                                    candidateKey: candidate.map { $0.deduplicationKey })
            defer { next[room.id] = baseline }
            guard hasBaseline, let previous = baselines[room.id] else { continue }
            let isNewer = (room.latestActivity ?? .distantPast) > (previous.latestActivity ?? .distantPast)
            let hasUnreadIncrease = room.unreadCount > previous.unreadCount
            guard isNewer || hasUnreadIncrease,
                  let candidate,
                  !candidate.isOwn,
                  candidate.deduplicationKey != previous.candidateKey else { continue }
            result.append(candidate)
        }
        baselines = next
        hasBaseline = true
        return result.sorted { lhs, rhs in
            lhs.latestActivity == rhs.latestActivity ? lhs.roomID < rhs.roomID : lhs.latestActivity < rhs.latestActivity
        }
    }

    mutating func reset() {
        baselines.removeAll()
        hasBaseline = false
    }
}
