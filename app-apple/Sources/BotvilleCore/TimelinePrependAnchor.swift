// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation

/// Keep the measured pagination header footprint after its button disappears.
/// At content offset zero, removing it makes the former first-row offset
/// unreachable by scrollTo (negative content offset is clamped). Keep the slot
/// for this appearance, including after restoration expires or is cancelled.
public struct TimelineHeaderFootprint: Sendable {
    public private(set) var height: Double = 0
    public init() {}
    public mutating func record(height: Double, buttonVisible: Bool) {
        guard buttonVisible, height.isFinite, height > 0 else { return }
        self.height = height
    }
}

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

    private enum Phase: Sendable { case layout, located, corrected, settled }
    private struct Capture: Sendable {
        let operation: UUID
        let appearance: UUID
        let originalIDs: [String]
        let pageStartIDs: [String]
        let visibleFrames: [TimelineMessageFrame]
        var target: TimelineMessageFrame
        var loading = true
        // A post-pagination SDK snapshot is the fence, not the arrival of an
        // arbitrary status/append diff. nil means the page is not acknowledged.
        var expectedMessageIDs: [String]?
        var phase: Phase = .layout
    }

    public static let timeout: TimeInterval = 5
    public static let recoveryWindow: TimeInterval = 2
    public private(set) var deadline: TimeInterval?
    // Absolute ceiling from begin; completion can never extend beyond it.
    private var hardDeadline: TimeInterval?
    private var missingFrameSamples = 0
    private var locateAttempts = 0
    private var correctionAttempts = 0
    private var staleGeometrySamples = 0
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
    public var pageRevisionApplied: Bool {
        guard let current = capture, !current.loading,
              let expected = current.expectedMessageIDs, let input else { return false }
        return Self.containsPageRevision(expected, in: input.messageIDs, replacing: current.pageStartIDs)
    }

    /// Subsequent live appends may already be present when the page fence arrives.
    /// Preserve ordering, rather than accepting an unrelated count/status change.
    public static func containsPageRevision(_ expected: [String], in actual: [String],
                                           replacing original: [String] = []) -> Bool {
        // A snapshot can also remove a prior row. Seeing its surviving IDs is
        // insufficient until that removal has reached the live input too.
        let removed = Set(original).subtracting(expected)
        guard removed.isDisjoint(with: actual) else { return false }
        var index = expected.startIndex
        for id in actual where index < expected.endIndex {
            if id == expected[index] { index += 1 }
        }
        return index == expected.endIndex
    }

    /// Beginning another page while correction is in flight chains the intended
    /// anchor, rather than capturing the transient top of the inserted batch.
    public mutating func begin(operation: UUID, input: TimelineScrollInput,
                               frames: [TimelineMessageFrame], viewportHeight: Double,
                               now: TimeInterval = ProcessInfo.processInfo.systemUptime) {
        let chained = blocksFollowing && capture?.appearance == input.request?.appearanceID ? capture : nil
#if DEBUG
        if let capture {
            TimelineTrace.record(.anchorResult, reason: .superseded, scope: capture.operation)
        }
#endif
        cancel()
        self.input = input
        guard let appearance = input.request?.appearanceID else { return }
        let visible = input.messageIDs.compactMap { id in frames.first { $0.id == id } }
            .filter { $0.intersects(viewportHeight: viewportHeight) }
        guard let target = chained?.target ?? visible.first else { return }
        deadline = now + Self.timeout
        hardDeadline = now + Self.timeout + Self.recoveryWindow
        capture = Capture(operation: operation, appearance: appearance,
                          originalIDs: chained?.originalIDs ?? input.messageIDs,
                          pageStartIDs: input.messageIDs,
                          visibleFrames: chained?.visibleFrames ?? visible, target: target)
#if DEBUG
        TimelineTrace.record(.anchorBegin, scope: operation, context: appearance, targetID: target.id,
                             count: input.messageIDs.count, offset: target.minY)
#endif
    }

    public mutating func cancel() {
        capture = nil
        deadline = nil
        hardDeadline = nil
        missingFrameSamples = 0
        locateAttempts = 0
        correctionAttempts = 0
        staleGeometrySamples = 0
        awaitingExecution = false
        layoutGeneration &+= 1
    }

    /// Cancelling also forgets the capture: late SDK diffs cannot resurrect it.
    public mutating func expire(now: TimeInterval) {
        if let deadline, now >= deadline {
#if DEBUG
            if let current = capture {
                let settled: Bool
                if case .settled = current.phase { settled = true } else { settled = false }
                // A settled result has already been logged, including residual
                // error at the correction cap. Expiry is not another failure.
                if !settled {
                    TimelineTrace.record(.anchorResult, reason: current.loading ? .loadingTimeout : .recoveryTimeout,
                                         scope: current.operation, targetID: current.target.id,
                                         generation: layoutGeneration, attempt: locateAttempts)
                }
            }
#endif
            cancel()
        }
    }

    public mutating func update(_ next: TimelineScrollInput) {
        let previous = input
        input = next
        guard var current = capture else { return }
        guard next.request?.appearanceID == current.appearance else {
#if DEBUG
            TimelineTrace.record(.anchorResult, reason: .appearance, scope: current.operation)
#endif
            cancel(); return
        }
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
#if DEBUG
                TimelineTrace.record(.anchorResult, reason: .noSurvivor, scope: current.operation)
#endif
                cancel()
                return
            }
            current.phase = .layout
        } else if prepended || blocksFollowing {
            current.phase = .layout
        }
        capture = current
        missingFrameSamples = 0
        staleGeometrySamples = 0
        awaitingExecution = false
        layoutGeneration &+= 1
    }

    public mutating func finished(operation: UUID,
                                 expectedMessageIDs: [String]? = nil,
                                 now: TimeInterval = ProcessInfo.processInfo.systemUptime) {
        expire(now: now)
        guard var current = capture, current.operation == operation, current.loading else { return }
        // One completion grace period, never renewed by geometry, data or a
        // duplicate completion. A cancelled/expired capture cannot resurrect.
        deadline = min(now + Self.recoveryWindow, hardDeadline ?? now)
        missingFrameSamples = 0
        staleGeometrySamples = 0
        current.loading = false
        current.expectedMessageIDs = expectedMessageIDs
        current.phase = .layout // Includes spinner/button removal and empty pages.
        awaitingExecution = false
        capture = current
        layoutGeneration &+= 1
#if DEBUG
        TimelineTrace.record(.anchorFinished, scope: operation, targetID: current.target.id,
                             generation: layoutGeneration, pageApplied: pageRevisionApplied)
#endif
    }

    /// The view executes outside the preference callback, after SwiftUI has
    /// registered the rendered lazy targets. Only then request a fresh sample.
    public mutating func didExecute(generation: UInt) {
        guard generation == layoutGeneration, awaitingExecution else { return }
#if DEBUG
        TimelineTrace.record(.anchorExecuted, scope: capture?.operation, generation: generation)
#endif
        awaitingExecution = false
        layoutGeneration &+= 1
    }

    /// The driver calls this even when SwiftUI emits no matching preference.
    /// Share the SAME three-locate budget with missing-row recovery, never reset
    /// it on status/data updates. No stale frame is used for pixel correction.
    public mutating func missingGeometry() -> Action? {
        guard needsLayout, pageRevisionApplied, correctionAttempts < 2,
              var current = capture else { return nil }
        staleGeometrySamples += 1
        guard staleGeometrySamples >= 3, locateAttempts < 3 else { return nil }
        staleGeometrySamples = 0
        locateAttempts += 1
        current.phase = .located
        capture = current
        awaitingExecution = true
#if DEBUG
        TimelineTrace.record(.anchorLocate, reason: .staleGeometry, scope: current.operation,
                             targetID: current.target.id, generation: layoutGeneration,
                             attempt: locateAttempts, pageApplied: true)
#endif
        layoutGeneration &+= 1
        return .locate(current.target.id)
    }

    /// Called only with geometry emitted by the current rendered message revision
    /// and generation. Retry lazy registration at most three times per operation.
    public mutating func layout(input measuredInput: TimelineScrollInput, generation: UInt,
                                frames: [TimelineMessageFrame]) -> Action? {
        guard measuredInput == input, generation == layoutGeneration,
              !awaitingExecution, pageRevisionApplied, var current = capture else { return nil }
        staleGeometrySamples = 0
        if case .settled = current.phase { return nil }
        let frame = frames.first { $0.id == current.target.id && $0.minY.isFinite }
        var action: Action?
        if let frame {
            missingFrameSamples = 0
            let delta = frame.minY - current.target.minY
            let corrections = correctionAttempts
            if abs(delta) <= 0.5 || corrections >= 2 {
#if DEBUG
                TimelineTrace.record(.anchorResult, reason: abs(delta) <= 0.5 ? .success : .correctionLimit,
                                     scope: current.operation, targetID: current.target.id,
                                     generation: layoutGeneration, attempt: corrections, offset: delta, pageApplied: true)
#endif
                current.phase = .settled
            } else {
#if DEBUG
                TimelineTrace.record(.anchorRestore, scope: current.operation, targetID: current.target.id,
                                     generation: layoutGeneration, attempt: corrections + 1, offset: delta, pageApplied: true)
#endif
                action = .restore(id: current.target.id, offset: current.target.minY)
                correctionAttempts += 1
                current.phase = .corrected
            }
        } else {
            // The first new-generation preference can contain only the content
            // reader. Give row preferences a turn before moving a visible row
            // to .top and back. The bounded driver retries even without events.
            missingFrameSamples += 1
#if DEBUG
            if missingFrameSamples == 1 {
                TimelineTrace.record(.anchorMissing, reason: locateAttempts < 3 ? .unmaterialized : .locateExhausted,
                                     scope: current.operation, targetID: current.target.id,
                                     generation: layoutGeneration, attempt: locateAttempts)
            }
#endif
            guard missingFrameSamples >= (locateAttempts == 0 ? 2 : 4), locateAttempts < 3,
                  correctionAttempts < 2 else { return nil }
            missingFrameSamples = 0
            locateAttempts += 1
#if DEBUG
            TimelineTrace.record(.anchorLocate, scope: current.operation, targetID: current.target.id,
                                 generation: layoutGeneration, attempt: locateAttempts)
#endif
            action = .locate(current.target.id)
            current.phase = .located
        }
        capture = current
        awaitingExecution = action != nil
        layoutGeneration &+= 1
        return action
    }
}
