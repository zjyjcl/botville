// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation

/// A row's actual frame in the scroll viewport, including partially visible rows.
public struct TimelineMessageFrame: Equatable, Sendable {
    public let id: String
    public let minY: Double
    public let height: Double

    public init(id: String, minY: Double, height: Double) {
        self.id = id
        self.minY = minY
        self.height = height
    }

    public func intersects(viewportHeight: Double) -> Bool {
        minY.isFinite && height.isFinite && height > 0 && viewportHeight.isFinite
            && viewportHeight > 0 && minY < viewportHeight && minY + height > 0
    }
}

/// Two-stage restoration: materialize the original row, then correct its measured
/// offset. Each action requires a fresh layout acknowledgement. A monotonic
/// deadline also releases follow arbitration if no usable geometry ever arrives.
public struct TimelinePrependAnchor: Sendable {
    public enum Action: Equatable, Sendable {
        case locate(String)
        case restore(id: String, offset: Double)
    }

    private enum Phase: Sendable { case layout, located, corrected(Int), settled }
    private struct Capture: Sendable {
        let operation: UUID
        let appearance: UUID
        let originalIDs: [String]
        let visibleFrames: [TimelineMessageFrame]
        var target: TimelineMessageFrame
        var loading = true
        var phase: Phase = .layout
    }

    public static let timeout: TimeInterval = 3
    public private(set) var deadline: TimeInterval?
    private var missingFrameSamples = 0
    public private(set) var layoutGeneration: UInt = 0
    private var capture: Capture?
    private var input: TimelineScrollInput?
    private var awaitingExecution = false

    public init() {}

    public var blocksFollowing: Bool {
        guard let capture else { return false }
        if case .settled = capture.phase { return capture.loading }
        return true
    }

    public var needsLayout: Bool {
        guard let capture, !awaitingExecution else { return false }
        if case .settled = capture.phase { return false }
        return true
    }

    public var operation: UUID? { capture?.operation }
    public var targetID: String? { capture?.target.id }
    public var targetOffset: Double? { capture?.target.minY }

    /// Beginning another page while correction is in flight chains the intended
    /// anchor, rather than capturing the transient top of the inserted batch.
    public mutating func begin(operation: UUID, input: TimelineScrollInput,
                               frames: [TimelineMessageFrame], viewportHeight: Double,
                               now: TimeInterval = ProcessInfo.processInfo.systemUptime) {
        let chained = blocksFollowing && capture?.appearance == input.request?.appearanceID ? capture : nil
        cancel()
        self.input = input
        guard let appearance = input.request?.appearanceID else { return }
        let visible = input.messageIDs.compactMap { id in frames.first { $0.id == id } }
            .filter { $0.intersects(viewportHeight: viewportHeight) }
        guard let target = chained?.target ?? visible.first else { return }
        deadline = now + Self.timeout
        capture = Capture(operation: operation, appearance: appearance,
                          originalIDs: chained?.originalIDs ?? input.messageIDs,
                          visibleFrames: chained?.visibleFrames ?? visible, target: target)
    }

    public mutating func cancel() {
        capture = nil
        deadline = nil
        missingFrameSamples = 0
        awaitingExecution = false
        layoutGeneration &+= 1
    }

    /// Cancelling also forgets the capture: late SDK diffs cannot resurrect it.
    public mutating func expire(now: TimeInterval) {
        if let deadline, now >= deadline { cancel() }
    }

    public mutating func update(_ next: TimelineScrollInput) {
        let previous = input
        input = next
        guard var current = capture else { return }
        guard next.request?.appearanceID == current.appearance else { cancel(); return }
        guard previous?.messageIDs != next.messageIDs else { return }
        let oldIDs = previous?.messageIDs ?? []
        // Also detects prepend+append and late SDK listener delivery after the
        // pagination await has completed. Keep a settled anchor until a gesture,
        // new page or appearance supersedes it.
        let prepended = oldIDs.first.flatMap { next.messageIDs.firstIndex(of: $0) }.map { $0 > 0 } ?? false
        if !next.messageIDs.contains(current.target.id) {
            let survivors = Set(next.messageIDs)
            if let measured = current.visibleFrames.first(where: { survivors.contains($0.id) }) {
                current.target = measured
            } else if let index = current.originalIDs.firstIndex(of: current.target.id),
                      let successor = current.originalIDs.dropFirst(index + 1).first(where: { survivors.contains($0) }) {
                current.target = .init(id: successor, minY: 0, height: 1)
            } else {
                // No surviving reading context: leave the viewport alone.
                cancel()
                return
            }
            current.phase = .layout
        } else if prepended || blocksFollowing {
            current.phase = .layout
        }
        capture = current
        missingFrameSamples = 0
        awaitingExecution = false
        layoutGeneration &+= 1
    }

    public mutating func finished(operation: UUID) {
        guard var current = capture, current.operation == operation else { return }
        missingFrameSamples = 0
        current.loading = false
        current.phase = .layout // Includes spinner/button removal and empty pages.
        awaitingExecution = false
        capture = current
        layoutGeneration &+= 1
    }

    /// The view executes outside the preference callback, after SwiftUI has
    /// registered the rendered lazy targets. Only then request a fresh sample.
    public mutating func didExecute(generation: UInt) {
        guard generation == layoutGeneration, awaitingExecution else { return }
        awaitingExecution = false
        layoutGeneration &+= 1
    }

    /// Called only with geometry emitted by the current rendered message revision
    /// and generation. At most one locate and two pixel corrections per revision.
    public mutating func layout(input measuredInput: TimelineScrollInput, generation: UInt,
                                frames: [TimelineMessageFrame]) -> Action? {
        guard measuredInput == input, generation == layoutGeneration,
              !awaitingExecution, var current = capture else { return nil }
        if case .settled = current.phase { return nil }
        let frame = frames.first { $0.id == current.target.id && $0.minY.isFinite }
        var action: Action?
        if let frame {
            missingFrameSamples = 0
            let delta = frame.minY - current.target.minY
            let corrections: Int
            if case .corrected(let count) = current.phase { corrections = count } else { corrections = 0 }
            if abs(delta) <= 0.5 || corrections >= 2 {
                current.phase = .settled
            } else {
                action = .restore(id: current.target.id, offset: current.target.minY)
                current.phase = .corrected(corrections + 1)
            }
        } else if case .layout = current.phase {
            // The first new-generation preference can contain only the content
            // reader. Give row preferences a turn before moving a visible row
            // to .top and back. The bounded driver retries even without events.
            missingFrameSamples += 1
            guard missingFrameSamples >= 2 else { return nil }
            action = .locate(current.target.id)
            current.phase = .located
        } else {
            // Lazy materialization may take more than one layout. Wait for the
            // target's own frame; data removal is handled by update(_:).
            return nil
        }
        capture = current
        awaitingExecution = action != nil
        layoutGeneration &+= 1
        return action
    }
}
