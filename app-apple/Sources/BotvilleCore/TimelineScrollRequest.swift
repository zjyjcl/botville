// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation

/// A completed appearance is a scroll trigger even if its last message is unchanged.
public struct TimelineScrollRequest: Equatable, Sendable {
    public let appearanceID: UUID
    public let latestMessageID: String

    public static func make(
        appearanceID: UUID, completedAppearanceID: UUID?,
        isVisible: Bool, isOpening: Bool, roomID: String, activeRoomID: String?,
        latestMessageID: String?
    ) -> Self? {
        guard isVisible, !isOpening, completedAppearanceID == appearanceID,
              activeRoomID == roomID, let latestMessageID else { return nil }
        return Self(appearanceID: appearanceID, latestMessageID: latestMessageID)
    }
}

/// Measurements are in points. Unknown geometry must not opt a reader into following.
public enum TimelineBottomProximity {
    public static func isNearBottom(distanceFromBottom: Double?, viewportHeight: Double?) -> Bool {
        guard let distanceFromBottom, distanceFromBottom.isFinite,
              let viewportHeight, viewportHeight.isFinite, viewportHeight > 0 else { return false }
        return distanceFromBottom <= max(80, viewportHeight * 0.15)
    }
}

public struct TimelineScrollInput: Equatable, Sendable {
    public let request: TimelineScrollRequest?
    public let messageIDs: [String]
    public let latestMessageIsOwn: Bool

    public init(request: TimelineScrollRequest?, messageIDs: [String], latestMessageIsOwn: Bool) {
        self.request = request
        self.messageIDs = messageIDs
        self.latestMessageIsOwn = latestMessageIsOwn
    }
}

public struct TimelineScrollDecision: Equatable, Sendable {
    public enum Reason: Equatable, Sendable { case appearance, newMessage, ownMessage }
    public let request: TimelineScrollRequest
    public let reason: Reason
    public var animated: Bool { reason != .appearance }
    public func animationDuration(reduceMotion: Bool) -> Double? {
        animated && !reduceMotion ? 0.2 : nil
    }
}

/// Owns follow decisions and prepend restoration arbitration; the view only
/// measures frames and executes the resulting scroll actions.
public struct TimelineScrollState: Sendable {
    public enum Change: Equatable, Sendable { case unchanged, prepend, append, replacement }

    public private(set) var pending: TimelineScrollDecision?
    public private(set) var isNearBottom = false
    public private(set) var bottomSettlement = TimelineBottomSettlement()
    public private(set) var prependAnchor = TimelinePrependAnchor()
    public var followDecision: TimelineScrollDecision? {
        prependAnchor.blocksFollowing ? nil : pending
    }

    public mutating func beginPrepend(operation: UUID, input: TimelineScrollInput,
                                      frames: [TimelineMessageFrame], viewportHeight: Double,
                                      now: TimeInterval = ProcessInfo.processInfo.systemUptime) {
        bottomSettlement.cancel()
        update(input)
        prependAnchor.begin(operation: operation, input: input, frames: frames, viewportHeight: viewportHeight, now: now)
    }

    public mutating func finishPrepend(operation: UUID,
                                      expectedMessageIDs: [String]? = nil,
                                      now: TimeInterval = ProcessInfo.processInfo.systemUptime) {
        prependAnchor.finished(operation: operation, expectedMessageIDs: expectedMessageIDs, now: now)
    }

    public mutating func restorePrepend(for input: TimelineScrollInput, generation: UInt,
                                        frames: [TimelineMessageFrame]) -> TimelinePrependAnchor.Action? {
        prependAnchor.layout(input: input, generation: generation, frames: frames)
    }

    public mutating func didExecutePrepend(generation: UInt) {
        prependAnchor.didExecute(generation: generation)
    }

    public mutating func recoverMissingPrependGeometry() -> TimelinePrependAnchor.Action? {
        prependAnchor.missingGeometry()
    }

    public mutating func cancelPrepend() { prependAnchor.cancel() }

    public mutating func expireScrollWork(now: TimeInterval) {
        prependAnchor.expire(now: now)
        bottomSettlement.expire(now: now)
    }

    public mutating func correctBottom(for input: TimelineScrollInput, frames: [TimelineMessageFrame],
                                      viewportHeight: Double, now: TimeInterval) -> TimelineScrollDecision? {
        guard hasInput(input), pending == nil, !prependAnchor.blocksFollowing else { return nil }
        return bottomSettlement.correction(input: input, frames: frames, viewportHeight: viewportHeight, now: now)
    }

    public mutating func cancelScrollWork() {
        prependAnchor.cancel()
        bottomSettlement.cancel()
        pending = nil
    }
    private var input: TimelineScrollInput?
    private var scrolledAppearanceID: UUID?

    public init() {}

    public static func change(from old: [String], to new: [String]) -> Change {
        guard old != new else { return .unchanged }
        if new.count > old.count, new.suffix(old.count).elementsEqual(old) { return .prepend }
        if let last = old.last, let index = new.firstIndex(of: last), index < new.count - 1 {
            return .append // Includes a batch with both older and newer messages.
        }
        return .replacement
    }

    public func hasInput(_ expected: TimelineScrollInput) -> Bool { input == expected }

    public mutating func update(_ next: TimelineScrollInput) {
        let previous = input
        let settlingAppearance = bottomSettlement.decision?.reason == .appearance
            && bottomSettlement.decision?.request.appearanceID == next.request?.appearanceID
        let change = Self.change(from: previous?.messageIDs ?? [], to: next.messageIDs)
        input = next
        prependAnchor.update(next)
        if previous != next, settlingAppearance, let request = next.request,
           change == .replacement, !next.latestMessageIsOwn,
           next.messageIDs.count >= (previous?.messageIDs.count ?? 0),
           !prependAnchor.blocksFollowing {
            bottomSettlement.retarget(request)
            return
        }
        if previous != next { bottomSettlement.cancel() }
        guard let request = next.request else {
            pending = nil
            return
        }
        guard scrolledAppearanceID == request.appearanceID, previous?.messageIDs.isEmpty == false else {
            pending = .init(request: request, reason: .appearance)
            return
        }
        guard change == .append else {
            // Keep an append follow deferred until active restoration completes.
            if change != .unchanged && !prependAnchor.blocksFollowing { pending = nil }
            return
        }
        if next.latestMessageIsOwn {
            pending = .init(request: request, reason: .ownMessage)
        } else if isNearBottom || pending != nil {
            pending = .init(request: request, reason: .newMessage)
        } else {
            pending = nil
        }
    }

    /// Reject geometry from another appearance or message revision. In particular,
    /// post-append layout must not overwrite proximity BEFORE its append decision.
    public mutating func recordGeometry(for measuredInput: TimelineScrollInput,
                                        distanceFromBottom: Double, viewportHeight: Double) {
        guard measuredInput == input, pending == nil, !prependAnchor.blocksFollowing else { return }
        isNearBottom = TimelineBottomProximity.isNearBottom(
            distanceFromBottom: distanceFromBottom, viewportHeight: viewportHeight)
    }

    public mutating func didScroll(_ decision: TimelineScrollDecision,
                                   now: TimeInterval = ProcessInfo.processInfo.systemUptime) {
        guard pending == decision, !prependAnchor.blocksFollowing else { return }
        prependAnchor.cancel()
        scrolledAppearanceID = decision.request.appearanceID
        isNearBottom = true
        pending = nil
        bottomSettlement.begin(decision, now: now)
    }

    public mutating func userBeganDragging() {
        prependAnchor.cancel()
        bottomSettlement.cancel()
        // A gesture wins even during an appearance correction. Do not replay it
        // on the next delivery-status update of the same message revision.
        if let pending { scrolledAppearanceID = pending.request.appearanceID }
        pending = nil
        isNearBottom = false
    }
}
