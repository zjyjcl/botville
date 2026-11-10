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
}

/// Owns decisions, not SwiftUI positions. Prepend leaves the top visible ID binding
/// alone; scrollPosition(id:) keeps that item visible as the data changes.
public struct TimelineScrollState: Sendable {
    public enum Change: Equatable, Sendable { case unchanged, prepend, append, replacement }

    public private(set) var pending: TimelineScrollDecision?
    public private(set) var isNearBottom = false
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

    public mutating func update(_ next: TimelineScrollInput) {
        let previous = input
        input = next
        guard let request = next.request else {
            pending = nil
            return
        }
        guard scrolledAppearanceID == request.appearanceID, previous?.messageIDs.isEmpty == false else {
            pending = .init(request: request, reason: .appearance)
            return
        }
        let change = Self.change(from: previous?.messageIDs ?? [], to: next.messageIDs)
        guard change == .append else {
            // A prepend must not replay a pending bottom scroll; the bound visible
            // ID preserves the reader's position without an animated restoration.
            if change != .unchanged { pending = nil }
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
        guard measuredInput == input, pending == nil else { return }
        isNearBottom = TimelineBottomProximity.isNearBottom(
            distanceFromBottom: distanceFromBottom, viewportHeight: viewportHeight)
    }

    public mutating func didScroll(_ decision: TimelineScrollDecision) {
        guard pending == decision else { return }
        scrolledAppearanceID = decision.request.appearanceID
        isNearBottom = true
        pending = nil
    }

    public mutating func userBeganDragging() {
        // A gesture wins over a queued animated follow. Opening still lands at latest.
        if pending?.animated == true { pending = nil }
        isNearBottom = false
    }
}
