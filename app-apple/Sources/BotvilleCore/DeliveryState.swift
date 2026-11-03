// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation

/// The user-visible lifecycle of a Matrix local echo.  A retry always acts on
/// the original SDK queue item; it never creates a second Matrix event.
public enum MessageDeliveryState: String, Equatable, Sendable {
    case queued
    case sent
    /// SDK retries are exhausted and the room queue is disabled. Explicit user
    /// retry resumes that queue; sync/connection recovery must not resume it.
    case paused
    /// The SDK parked this item. It will not send again until the user retries
    /// or cancels this exact transaction.
    case failed
    case cancelled
}

public extension MessageDeliveryState {
    var displayName: String {
        switch self {
        case .queued: return "等待发送"
        case .sent: return "已发送"
        case .paused, .failed: return "发送失败，已暂停"
        case .cancelled: return "已取消"
        }
    }

    var isFailed: Bool { self == .paused || self == .failed }

    /// Recoverable failures resume the room queue. Unrecoverable failures
    /// unpark the original transaction with retrySend.
    var canRetry: Bool { self == .paused || self == .failed }

    /// Only an SDK-parked unrecoverable item can be cancelled. Disabling a
    /// room queue does not interrupt requests, so paused is not cancellable.
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
