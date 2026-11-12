// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation
import MatrixRustSDK

struct ForegroundSyncEvent: Sendable {
    let candidate: ForegroundBannerCandidate
    let visibilityEpoch: UUID
}

/// The SDK invokes this on its sync worker, before the sync future completes.
/// Capture lifecycle eligibility here, rather than after a MainActor hop, so
/// cold-start/background callbacks cannot turn into live events while queued.
final class ForegroundSyncNotificationBridge: SyncNotificationListener, @unchecked Sendable {
    let continuation: AsyncStream<ForegroundSyncEvent>.Continuation
    private let userID: String
    private let lock = NSLock()
    private var ready = false
    private var active = true
    private var epoch = UUID()
    private var seen = Set<String>()
    private var order = [String]()

    init(userID: String, continuation: AsyncStream<ForegroundSyncEvent>.Continuation) {
        self.userID = userID
        self.continuation = continuation
    }

    func finishInitialSync() { lock.withLock { ready = true } }

    func setActive(_ value: Bool) {
        lock.withLock {
            if active && !value { epoch = UUID() }
            active = value
        }
    }

    func isCurrent(_ event: ForegroundSyncEvent) -> Bool {
        lock.withLock { active && event.visibilityEpoch == epoch }
    }

    func onNotification(notification: NotificationItem, roomId: String) {
        let context = lock.withLock { (ready, active, epoch) }
#if DEBUG
        if case .timeline(let event) = notification.event, event.senderId() == userID {
            recordSuppression(.ownMessage, roomID: roomId)
        }
#endif
        guard let candidate = Self.candidate(notification: notification, roomID: roomId, userID: userID),
              let eventID = candidate.eventID else { return }
        lock.withLock {
            let key = roomId + "|" + eventID
            guard seen.insert(key).inserted else {
#if DEBUG
                recordSuppression(.alreadyHandled, roomID: roomId)
#endif
                return
            }
            order.append(key)
            if order.count > 2048 { seen.remove(order.removeFirst()) }
            guard context.0, context.1, active, context.2 == epoch else {
#if DEBUG
                let reason: UnreadBannerProbeLog.BannerReason = !context.0 ? .initialSync
                    : (!context.1 || !active ? .notForeground : .staleVisibility)
                recordSuppression(reason, roomID: roomId)
#endif
                return
            }
            continuation.yield(ForegroundSyncEvent(candidate: candidate, visibilityEpoch: epoch))
        }
    }

#if DEBUG
    private func recordSuppression(_ reason: UnreadBannerProbeLog.BannerReason, roomID: String) {
        // Capture monotonic time at the SDK callback, before the actor hop.
        let now = ProcessInfo.processInfo.systemUptime
        Task { @MainActor in
            UnreadBannerProbeLog.banner(roomID: roomID, reason: reason, now: now)
        }
    }
#endif

    static func candidate(notification: NotificationItem, roomID: String,
                          userID: String) -> ForegroundBannerCandidate? {
        guard case .timeline(let event) = notification.event,
              event.senderId() != userID,
              case .messageLike(let content) = try? event.content() else { return nil }
        switch content {
        case .roomMessage, .roomEncrypted, .sticker: break
        default: return nil
        }
        let reference = PushEventReference(roomID: roomID, eventID: event.eventId())
        // Parse only in memory; retain the bounded display text, never raw JSON.
        guard let details = PushNotificationText.parseEventDetails(Data(notification.rawEvent.utf8), reference: reference) else { return nil }
        let name = notification.senderInfo.displayName?.trimmingCharacters(in: .whitespacesAndNewlines)
        let title = notification.roomInfo.isDirect || notification.roomInfo.isDm
            ? (name.flatMap { $0.isEmpty ? nil : $0 } ?? notification.roomInfo.displayName)
            : notification.roomInfo.displayName
        let text = details.text.replacingTitle(title)
        return ForegroundBannerCandidate(roomID: roomID, eventID: reference.eventID,
            latestActivity: Date(timeIntervalSince1970: Double(event.timestamp()) / 1_000),
            title: text.title ?? roomID,
            preview: notification.roomInfo.isEncrypted == true ? "加密消息" : text.body)
    }
}
