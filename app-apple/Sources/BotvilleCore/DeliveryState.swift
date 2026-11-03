// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation

/// The user-visible lifecycle of a Matrix local echo.  A retry always acts on
/// the original SDK queue item; it never creates a second Matrix event.
public enum MessageDeliveryState: String, Equatable, Sendable {
    case queued
    case sent
    case failed
    case cancelled
}

public extension MessageDeliveryState {
    var displayName: String {
        switch self {
        case .queued: return "等待发送"
        case .sent: return "已发送"
        case .failed: return "发送失败"
        case .cancelled: return "已取消"
        }
    }

    var isFailed: Bool { self == .failed }

    /// Cancellation is intentionally offered only after a failed send.  This
    /// avoids treating an in-flight send as a local cancellation/redaction.
    var canCancel: Bool { self == .failed }
}

/// Adapter boundary for SDK queue operations. The production adapter
/// works against a timeline local echo, so retries never enqueue a duplicate.
public protocol SendQueueActioning: Sendable {
    /// Returns false when the queued item left the failed state before action.
    func retry() async throws -> Bool
    /// Returns false when the queued item left the failed state before action.
    func cancel() async throws -> Bool
}
