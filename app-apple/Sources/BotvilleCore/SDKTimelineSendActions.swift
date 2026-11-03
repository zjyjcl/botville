// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import MatrixRustSDK

/// Production implementation for a single failed timeline local echo.  It
/// accepts a transaction ID only, never an arbitrary remote event ID. If the
/// queue resumes concurrently, the SDK may cancel an in-flight item by queuing
/// a redaction. False means there was no pending item left to act on.
public struct SDKTimelineSendActions: SendQueueActioning {
    private let timeline: Timeline
    private let itemID: EventOrTransactionId

    public init(timeline: Timeline, transactionID: String) {
        self.timeline = timeline
        itemID = .transactionId(transactionId: transactionID)
    }

    public func retry() async throws -> Bool {
        try await timeline.retrySend(itemId: itemID, target: .event)
    }

    public func cancel() async throws -> Bool {
        try await timeline.abortSend(itemId: itemID, target: .event)
    }
}
