// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation

/// A short observation window after a follow command. Lazy row heights may grow
/// even after an initially correct landing. Geometry confirms arrival; elapsed
/// time and the correction budget independently bound the work.
public struct TimelineBottomSettlement: Sendable {
    public static let timeout: TimeInterval = 2
    public static let maximumCorrections = 6
    public private(set) var decision: TimelineScrollDecision?
    public private(set) var deadline: TimeInterval?
    public private(set) var corrections = 0
    private var nextCorrection: TimeInterval = 0

    public init() {}

    public mutating func begin(_ decision: TimelineScrollDecision, now: TimeInterval) {
        self.decision = decision
        deadline = now + Self.timeout
        corrections = 0
        nextCorrection = now + 0.1 // Allow the initial command to produce layout.
    }

    public mutating func cancel() {
        decision = nil
        deadline = nil
    }

    public mutating func expire(now: TimeInterval) {
        if let deadline, now >= deadline { cancel() }
    }

    public mutating func correction(input: TimelineScrollInput, frames: [TimelineMessageFrame],
                                    viewportHeight: Double, now: TimeInterval) -> TimelineScrollDecision? {
        expire(now: now)
        guard let decision, input.request == decision.request else { return nil }
        guard viewportHeight.isFinite, viewportHeight > 0, now >= nextCorrection else { return nil }
        // Require the actual last row, not only the stack's estimated maxY.
        if let last = frames.first(where: { $0.id == decision.request.latestMessageID }),
           last.minY.isFinite, last.height.isFinite, last.height > 0,
           last.minY + last.height > 0,
           last.minY + last.height <= viewportHeight + 1 {
            return nil
        }
        guard corrections < Self.maximumCorrections else { cancel(); return nil }
        corrections += 1
        nextCorrection = now + (decision.animated ? 0.25 : 0.12)
        return decision
    }
}

/// Retains only the last usable capture for the same message revision and
/// appearance. An empty preference or invalid viewport at button-tap time must
/// not erase a valid reading position. Never reuse it across data or room changes.
public struct TimelineAnchorGeometryCache: Sendable {
    private var input: TimelineScrollInput?
    private var frames: [TimelineMessageFrame] = []
    private var viewportHeight: Double = 0

    public init() {}

    public mutating func record(input: TimelineScrollInput, frames: [TimelineMessageFrame], viewportHeight: Double) {
        guard frames.contains(where: { input.messageIDs.contains($0.id) && $0.intersects(viewportHeight: viewportHeight) }) else { return }
        self.input = input
        self.frames = frames
        self.viewportHeight = viewportHeight
    }

    public func capture(input: TimelineScrollInput, frames: [TimelineMessageFrame], viewportHeight: Double)
        -> (frames: [TimelineMessageFrame], viewportHeight: Double) {
        if frames.contains(where: { input.messageIDs.contains($0.id) && $0.intersects(viewportHeight: viewportHeight) }) {
            return (frames, viewportHeight)
        }
        if self.input == input { return (self.frames, self.viewportHeight) }
        return ([], 0)
    }
}
