// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation
import MatrixRustSDK

/// Keep virtual/non-text items too: SDK diff indices address the entire timeline.
struct TimelineState {
    private(set) var items: [TimelineItem] = []

    mutating func apply(_ updates: [TimelineDiff]) {
        for update in updates {
            switch update {
            case .append(let values): items.append(contentsOf: values)
            case .clear: items.removeAll()
            case .pushFront(let value): items.insert(value, at: 0)
            case .pushBack(let value): items.append(value)
            case .popFront: if !items.isEmpty { items.removeFirst() }
            case .popBack: _ = items.popLast()
            case .insert(let index, let value):
                if Int(index) <= items.count { items.insert(value, at: Int(index)) }
            case .set(let index, let value):
                if items.indices.contains(Int(index)) { items[Int(index)] = value }
            case .remove(let index):
                if items.indices.contains(Int(index)) { items.remove(at: Int(index)) }
            case .truncate(let length): items = Array(items.prefix(Int(length)))
            case .reset(let values): items = values
            }
        }
    }

    var messages: [MatrixMessage] {
        items.compactMap { item in
            guard let event = item.asEvent(),
                  case .msgLike(let content) = event.content,
                  case .message(let message) = content.kind else { return nil }
            switch message.msgType {
            case .text, .notice, .emote: break
            default: return nil
            }
            let delivery = MessageDeliveryState(event.localSendState)
            return MatrixMessage(
                id: item.uniqueId().id,
                sender: event.sender,
                body: message.body,
                timestamp: Date(timeIntervalSince1970: Double(event.timestamp) / 1_000),
                isOwn: event.isOwn,
                deliveryState: delivery
            )
        }
    }

    /// Produces the exact identifier the SDK expects for retry/cancel.  It is
    /// derived from the current local echo rather than guessed from message
    /// order or display text. Only non-recoverable (parked) own messages are
    /// actionable; a recoverable queue wedge resumes automatically after an
    /// explicit network/reconnection recovery path.
    func failedSendTransactionID(for messageID: String) -> String? {
        guard let item = items.first(where: { $0.uniqueId().id == messageID }),
              let event = item.asEvent(), event.isOwn else {
            return nil
        }
        guard case .sendingFailed(error: _, isRecoverable: false) = event.localSendState else { return nil }
        guard case .transactionId(let transactionID) = event.eventOrTransactionId else {
            return nil
        }
        return transactionID
    }
}

extension MessageDeliveryState {
    init?(_ sdkState: EventSendState?) {
        switch sdkState {
        case .notSentYet(progress: _): self = .queued
        case .sendingFailed(error: _, isRecoverable: true): self = .retrying
        case .sendingFailed(error: _, isRecoverable: false): self = .failed
        case .sent(eventId: _): self = .sent
        case nil: return nil
        }
    }
}

/// An ordered stream bridges SDK callbacks to the main actor without spawning
/// unordered Tasks for individual diffs. No SDK callback mutates UI state.
final class TimelineListenerBridge: TimelineListener, Sendable {
    let continuation: AsyncStream<[TimelineDiff]>.Continuation

    init(_ continuation: AsyncStream<[TimelineDiff]>.Continuation) {
        self.continuation = continuation
    }

    func onUpdate(diff: [TimelineDiff]) {
        continuation.yield(diff)
    }
}
