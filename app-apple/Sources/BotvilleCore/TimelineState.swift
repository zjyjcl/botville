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
            let delivery: String?
            switch event.localSendState {
            case .notSentYet: delivery = "等待发送"
            case .sendingFailed: delivery = "发送失败（消息保留在本次会话中）"
            case .sent: delivery = "已发送"
            case nil: delivery = nil
            }
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
