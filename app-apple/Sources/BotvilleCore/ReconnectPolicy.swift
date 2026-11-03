// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation

public enum ConnectionStatus: Equatable, Sendable {
    case idle
    case syncing
    case offline
    case reconnecting(after: TimeInterval)
    case connected

    public var displayName: String {
        switch self {
        case .idle: return "未同步"
        case .syncing: return "正在同步"
        case .offline: return "离线"
        case .reconnecting(let delay):
            return "将在 \(Int(delay.rounded(.up))) 秒后重连"
        case .connected: return "已连接"
        }
    }
}

/// Bounded exponential retry timing for failed sync attempts.  A successful
/// sync calls `reset`, so a later outage starts at the shortest delay again.
public struct ReconnectPolicy: Sendable {
    public let initialDelay: TimeInterval
    public let maximumDelay: TimeInterval
    private(set) public var consecutiveFailures = 0

    public init(initialDelay: TimeInterval = 1, maximumDelay: TimeInterval = 30) {
        precondition(initialDelay > 0 && maximumDelay >= initialDelay)
        self.initialDelay = initialDelay
        self.maximumDelay = maximumDelay
    }

    /// Registers one failed attempt and returns the delay before the next one.
    public mutating func nextDelay() -> TimeInterval {
        let exponent = min(consecutiveFailures, 62)
        let delay = min(initialDelay * pow(2, Double(exponent)), maximumDelay)
        consecutiveFailures += 1
        return delay
    }

    public mutating func reset() {
        consecutiveFailures = 0
    }
}
