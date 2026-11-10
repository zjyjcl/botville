// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation

/// A short observation window after a follow command. Lazy row heights may grow
/// even after an initially correct landing. Geometry confirms arrival; elapsed
/// time bounds the work. A fast series of missing frames must not consume the
/// entire recovery window before the lazy rows have finished sizing.
public struct TimelineBottomSettlement: Sendable {
    public static let timeout: TimeInterval = 4
    public static let stabilityWindow: TimeInterval = 0.35
    public private(set) var decision: TimelineScrollDecision?
    public private(set) var deadline: TimeInterval?
    public private(set) var corrections = 0
    public private(set) var isConfirmed = false
    private var nextCorrection: TimeInterval = 0
    private var stableSince: TimeInterval?
    private var lastFrame: TimelineMessageFrame?
    private var lastViewportHeight: Double?

    public init() {}

    public mutating func begin(_ decision: TimelineScrollDecision, now: TimeInterval) {
        self.decision = decision
        deadline = now + Self.timeout
        corrections = 0
        isConfirmed = false
        stableSince = nil
        lastFrame = nil
        lastViewportHeight = nil
        nextCorrection = now + 0.1 // Allow the initial command to produce layout.
    }

    public mutating func cancel() {
        decision = nil
        deadline = nil
        isConfirmed = false
        stableSince = nil
    }

    public mutating func expire(now: TimeInterval) {
        if let deadline, now >= deadline { cancel() }
    }

    /// A listener may replace its cached snapshot while an appearance is still
    /// settling. Follow its new last row within the ORIGINAL deadline.
    public mutating func retarget(_ request: TimelineScrollRequest) {
        guard let decision, decision.request.appearanceID == request.appearanceID else { return }
        self.decision = .init(request: request, reason: decision.reason)
        isConfirmed = false
        stableSince = nil
        lastFrame = nil
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
            if let previous = lastFrame, lastViewportHeight == viewportHeight,
               abs(previous.minY - last.minY) <= 0.5, abs(previous.height - last.height) <= 0.5 {
                if let stableSince { isConfirmed = now - stableSince >= Self.stabilityWindow }
            } else {
                stableSince = now
                isConfirmed = false
            }
            lastFrame = last
            lastViewportHeight = viewportHeight
            // Keep observing until the deadline: even a stable early landing
            // can be displaced by a later long-row height change.
            return nil
        }
        isConfirmed = false
        stableSince = nil
        lastFrame = nil
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
