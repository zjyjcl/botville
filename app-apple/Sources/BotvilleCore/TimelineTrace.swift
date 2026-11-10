// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

#if DEBUG
import CryptoKit
import Foundation
import os

/// Debug-only, allowlisted diagnostics. Never accepts message content, display
/// names or error descriptions. Identifiers are SHA-256 prefixes, not raw IDs.
public enum TimelineTrace {
    public enum Event: String, CaseIterable {
        case listenerRegister, listenerReady, firstSnapshot, diffApplied
        case pageBegin, pageEnd, anchorBegin, anchorFinished, anchorLocate
        case anchorRestore, anchorResult, anchorMissing, anchorExecuted, anchorWaiting, lastRow
    }
    public enum Reason: String, CaseIterable {
        case none, success, failed, stale, cancelled, gesture, appearance, superseded
        case loadingTimeout, recoveryTimeout, unmaterialized, locateExhausted
        case correctionLimit, noCapture, noSurvivor, noGeometry, staleGeometry, invalidViewport
    }

    private static let logger = Logger(subsystem: "com.zjyjcl.botville", category: "TimelineTrace")

    static func fingerprint(_ value: String?) -> String {
        guard let value else { return "-" }
        return SHA256.hash(data: Data(value.utf8)).prefix(4).map { String(format: "%02x", $0) }.joined()
    }

    // Optional measurements remain "-" when unknown; missing geometry must not
    // be reported as a zero offset or a successful landing.
    static func format(_ event: Event, reason: Reason = .none, scope: UUID? = nil,
                       context: UUID? = nil, targetID: String? = nil, count: Int? = nil,
                       previousCount: Int? = nil, generation: UInt? = nil,
                       attempt: Int? = nil, elapsed: Double? = nil, reachedStart: Bool? = nil,
                       visible: Bool? = nil, offset: Double? = nil,
                       now: TimeInterval = ProcessInfo.processInfo.systemUptime) -> String {
        func number<T>(_ value: T?) -> String { value.map { String(describing: $0) } ?? "-" }
        return "timeline_trace t=\(now) event=\(event.rawValue) reason=\(reason.rawValue)"
            + " scope=\(fingerprint(scope?.uuidString)) context=\(fingerprint(context?.uuidString)) target=\(fingerprint(targetID))"
            + " count=\(number(count)) before=\(number(previousCount)) gen=\(number(generation)) attempt=\(number(attempt))"
            + " elapsed=\(number(elapsed)) reached_start=\(number(reachedStart)) visible=\(number(visible)) offset=\(number(offset))"
    }

    public static func record(_ event: Event, reason: Reason = .none, scope: UUID? = nil,
                              context: UUID? = nil, targetID: String? = nil, count: Int? = nil,
                              previousCount: Int? = nil, generation: UInt? = nil,
                              attempt: Int? = nil, elapsed: Double? = nil, reachedStart: Bool? = nil,
                              visible: Bool? = nil, offset: Double? = nil) {
        let line = format(event, reason: reason, scope: scope, context: context, targetID: targetID,
                          count: count, previousCount: previousCount, generation: generation,
                          attempt: attempt, elapsed: elapsed, reachedStart: reachedStart,
                          visible: visible, offset: offset)
        logger.debug("\(line, privacy: .public)")
    }
}
#endif
