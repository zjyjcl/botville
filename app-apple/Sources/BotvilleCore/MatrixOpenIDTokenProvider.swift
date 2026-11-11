// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation

/// Process-local only: deliberately not Codable. Reflection and diagnostics
/// must not expose the bearer credential or homeserver identity.
public struct MatrixOpenIDToken: Sendable, CustomStringConvertible, CustomDebugStringConvertible, CustomReflectable {
    let accessToken: String
    let identity = UUID()
    private let tokenType: String
    private let matrixServerName: String
    let expiresInSeconds: TimeInterval

    public init(accessToken: String, tokenType: String, matrixServerName: String, expiresInSeconds: TimeInterval) {
        self.accessToken = accessToken
        self.tokenType = tokenType
        self.matrixServerName = matrixServerName
        self.expiresInSeconds = expiresInSeconds
    }

    public var description: String { "MatrixOpenIDToken(<redacted>)" }
    public var debugDescription: String { description }
    public var customMirror: Mirror { Mirror(self, children: ["credential": "<redacted>"]) }

    var isValid: Bool {
        tokenType.caseInsensitiveCompare("Bearer") == .orderedSame
            && !matrixServerName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && !accessToken.isEmpty
            && accessToken.unicodeScalars.allSatisfy { $0.value > 32 && $0.value < 127 }
            && expiresInSeconds.isFinite && expiresInSeconds > 0
    }
}

/// Errors never retain SDK errors, response bodies or credentials.
public enum MatrixOpenIDError: Error, Equatable {
    case providerUnavailable, requestFailed, invalidTokenResponse, sessionEnded
}

public actor MatrixOpenIDTokenProvider {
    public typealias Source = @Sendable () async throws -> MatrixOpenIDToken
    private var source: Source?
    private let now: @Sendable () -> Date
    private var cached: (token: MatrixOpenIDToken, refreshAt: Date)?
    private var flight: (id: UUID, task: Task<(MatrixOpenIDToken, Date), Error>)?
    private var generation = UUID()

    /// Refresh at min(60s, lifetime / 2) before expiry: protects long-lived
    /// tokens from network latency without consuming all of a short lifetime.
    private static let maximumRefreshMargin: TimeInterval = 60

    public init(now: @escaping @Sendable () -> Date = { Date() }, source: @escaping Source) {
        self.now = now
        self.source = source
    }

    public func token() async throws -> MatrixOpenIDToken {
        guard let source else { throw MatrixOpenIDError.sessionEnded }
        if let cached, now() < cached.refreshAt { return cached.token }
        let currentGeneration = generation
        let active: (id: UUID, task: Task<(MatrixOpenIDToken, Date), Error>)
        if let flight {
            active = flight
        } else {
            let id = UUID()
            // Count lifetime from the request start, conservatively including
            // time spent requesting the token rather than extending its TTL.
            let startedAt = now()
            let task = Task {
                let token: MatrixOpenIDToken
                do { token = try await source() }
                catch { throw MatrixOpenIDError.requestFailed }
                guard token.isValid else { throw MatrixOpenIDError.invalidTokenResponse }
                let margin = min(Self.maximumRefreshMargin, token.expiresInSeconds / 2)
                let refreshAt = startedAt.addingTimeInterval(token.expiresInSeconds - margin)
                guard refreshAt.timeIntervalSinceReferenceDate.isFinite, self.now() < refreshAt else {
                    throw MatrixOpenIDError.invalidTokenResponse
                }
                return (token, refreshAt)
            }
            active = (id, task)
            flight = active
        }
        do {
            let (token, refreshAt) = try await active.task.value
            guard generation == currentGeneration else { throw MatrixOpenIDError.sessionEnded }
            if flight?.id == active.id {
                // Only the first resumed waiter publishes the result. Late
                // waiters cannot overwrite a newer flight or undo invalidation.
                cached = (token, refreshAt)
                flight = nil
            }
            return token
        } catch {
            if flight?.id == active.id { flight = nil }
            if generation != currentGeneration { throw MatrixOpenIDError.sessionEnded }
            throw error
        }
    }

    public func invalidate(_ token: MatrixOpenIDToken) {
        if cached?.token.identity == token.identity { cached = nil }
    }

    /// Permanently closes this account-bound provider, including old clients
    /// that still retain it. A newly activated SDK client gets a new provider.
    public func shutdown() {
        generation = UUID()
        cached = nil
        source = nil
        flight?.task.cancel()
        flight = nil
    }
}
