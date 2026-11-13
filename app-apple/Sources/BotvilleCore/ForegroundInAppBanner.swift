// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Combine
import Foundation

/// A display-safe notification assembled from one SDK sync notification event.
/// The Matrix event ID is its identity. It carries no APNs or credential material.
public struct ForegroundBannerCandidate: Equatable, Sendable {
    public let roomID: String
    public let eventID: String
    public let latestActivity: Date
    public let title: String
    public let preview: String
    public let isOwn: Bool

    public init(roomID: String, eventID: String, latestActivity: Date,
                title: String, preview: String, isOwn: Bool = false) {
        self.roomID = roomID
        self.eventID = eventID
        self.latestActivity = latestActivity
        self.title = title
        self.preview = preview
        self.isOwn = isOwn
    }

    public var route: PushNotificationRoute { .room(roomID) }

    /// Same-millisecond events stay distinct; time is never part of identity.
    fileprivate var deduplicationKey: String { roomID + "|" + eventID }
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
    private var mutedRoomIDs = Set<String>()
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

    public func updateContext(isForeground: Bool, visibleRoomID: String?, isInputActive: Bool, mutedRoomIDs: Set<String> = []) {
        self.mutedRoomIDs = mutedRoomIDs
        self.isForeground = isForeground
        self.visibleRoomID = visibleRoomID
        self.isInputActive = isInputActive
        if let current, shouldSuppress(current) { dismiss() }
    }

    public func receive(_ candidate: ForegroundBannerCandidate) {
        let key = candidate.deduplicationKey
        // Decide before taking a presentation key. Input/current-room events
        // are intentionally discarded, not deferred until the user leaves.
        if shouldSuppress(candidate) {
#if DEBUG
            UnreadBannerProbeLog.banner(roomID: candidate.roomID, reason: suppressionReason(candidate))
#endif
            if candidate.isOwn || visibleRoomID == candidate.roomID || isInputActive { consume(key) }
            return
        }
        guard !consumedKeys.contains(key) else {
#if DEBUG
            UnreadBannerProbeLog.banner(roomID: candidate.roomID, reason: .alreadyHandled)
#endif
            return
        }
        consume(key)
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
        mutedRoomIDs.removeAll()
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
        !isForeground || candidate.isOwn || visibleRoomID == candidate.roomID || isInputActive || mutedRoomIDs.contains(candidate.roomID)
    }

#if DEBUG
    private func suppressionReason(_ candidate: ForegroundBannerCandidate) -> UnreadBannerProbeLog.BannerReason {
        if candidate.isOwn { return .ownMessage }
        if visibleRoomID == candidate.roomID { return .visibleRoom }
        if isInputActive { return .inputActive }
        if !isForeground { return .notForeground }
        return .muted
    }
#endif

    private func consume(_ key: String) {
        guard consumedKeys.insert(key).inserted else { return }
        consumedOrder.append(key)
        while consumedOrder.count > 256 {
            consumedKeys.remove(consumedOrder.removeFirst())
        }
    }
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

/// Tracks the background edge only. Foreground messages remain eligible even if
/// a network-driven replacement sync is still draining; on resume only events
/// already delivered as system notifications are suppressed, by room + event ID.
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

/// A replaced or cancelled online refresh must not publish its room snapshot.
/// Local refreshes (no online generation) always publish.
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

#if DEBUG
import os

/// Allowlisted diagnostics only. No message, user, token, or event ID input.
@MainActor
enum UnreadBannerProbeLog {
    /// Duplicate room IDs keep the last snapshot value in SDK/list order.
    static func unreadSnapshot(_ rooms: [MatrixRoom]) -> [String: UInt64] {
        Dictionary(rooms.map { ($0.id, $0.unreadCount) }, uniquingKeysWith: { _, latest in latest })
    }

    enum UnreadSource: String, CaseIterable {
        case readReceiptLocalZero, sdkUnreadZero, sdkInfoUnavailable, refreshCommitZero
    }
    enum RefreshOutcome: String, CaseIterable {
        case observed, committed, superseded, sessionChanged, inactiveSession, gateRejected
    }
    enum BannerReason: String, CaseIterable {
        case notForeground, ownMessage, visibleRoom, inputActive, muted, alreadyHandled
        case staleVisibility, deliveredInBackground, roomUnavailable, initialSync, cancelled, staleSession
    }
    private static let logger = Logger(subsystem: "com.zjyjcl.botville", category: "UnreadBannerProbeLog")
    // Test observer receives exactly the same sanitized line as os.Logger.
    static var observer: ((String) -> Void)?

    static func fingerprint(_ roomID: String) -> String { TimelineTrace.fingerprint(roomID) }

    static func unread(_ source: UnreadSource, roomID: String, before: UInt64?, after: UInt64?,
                       muted: Bool? = nil, refresh: UUID? = nil, online: Bool = false,
                       outcome: RefreshOutcome = .observed,
                       now: TimeInterval = ProcessInfo.processInfo.systemUptime) {
        emit("unread_banner_probe t=\(now) source=\(source.rawValue) room=\(fingerprint(roomID))"
             + " before=\(before.map(String.init) ?? "-") after=\(after.map(String.init) ?? "-") muted=\(muted.map(String.init) ?? "-")"
             + " refresh=\(TimelineTrace.fingerprint(refresh?.uuidString)) online=\(online) outcome=\(outcome.rawValue)")
    }

    static func refresh(_ refresh: UUID, online: Bool, outcome: RefreshOutcome,
                        now: TimeInterval = ProcessInfo.processInfo.systemUptime) {
        emit("unread_banner_probe t=\(now) refresh=\(TimelineTrace.fingerprint(refresh.uuidString))"
             + " online=\(online) outcome=\(outcome.rawValue)")
    }

    static func banner(roomID: String, reason: BannerReason,
                       now: TimeInterval = ProcessInfo.processInfo.systemUptime) {
        emit("unread_banner_probe t=\(now) room=\(fingerprint(roomID)) reason=\(reason.rawValue)")
    }

    private static func emit(_ line: String) {
        observer?(line)
        logger.debug("\(line, privacy: .public)")
    }
}
#endif
