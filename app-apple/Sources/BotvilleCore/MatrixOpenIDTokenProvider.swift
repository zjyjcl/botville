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
    case providerUnavailable, requestFailed, requestTimedOut, invalidTokenResponse, sessionEnded
}

public actor MatrixOpenIDTokenProvider {
    public typealias Source = @Sendable () async throws -> MatrixOpenIDToken
    public typealias TimeoutSleep = @Sendable (TimeInterval) async throws -> Void
    public static let defaultRequestTimeout: TimeInterval = 30
    private var source: Source?
    private let now: @Sendable () -> Date
    private let requestTimeout: TimeInterval
    private let timeoutSleep: TimeoutSleep
    private var cached: (token: MatrixOpenIDToken, refreshAt: Date)?
    private struct Flight {
        let id: UUID
        let request: Task<Void, Never>
        let timeout: Task<Void, Never>
        var waiters: [UUID: CheckedContinuation<MatrixOpenIDToken, Error>]
    }
    private var flight: Flight?

    /// Refresh when remaining lifetime < max(60s, lifetime * 20%). Cap the
    /// margin at 50% of lifetime so even 1s/30s/60s tokens can be reused for
    /// half their lifetime instead of refreshing on every request. Lifetime
    /// starts before the source call; responses already past this window fail
    /// closed rather than recursively requesting another token.
    private static let minimumRefreshMargin: TimeInterval = 60
    private static let refreshFraction = 0.2
    private static let maximumLifetimeFraction = 0.5

    public init(now: @escaping @Sendable () -> Date = { Date() },
                requestTimeout: TimeInterval = defaultRequestTimeout,
                timeoutSleep: @escaping TimeoutSleep = { try await Task.sleep(for: .seconds($0)) },
                source: @escaping Source) {
        precondition(requestTimeout.isFinite && requestTimeout > 0)
        self.now = now
        self.requestTimeout = requestTimeout
        self.timeoutSleep = timeoutSleep
        self.source = source
    }

    public func token() async throws -> MatrixOpenIDToken {
        try Task.checkCancellation()
        guard let source else { throw MatrixOpenIDError.sessionEnded }
        if let cached, now() <= cached.refreshAt { return cached.token }
        let waiterID = UUID()
        let token: MatrixOpenIDToken = try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                // Covers cancellation before the actor registers the waiter.
                guard !Task.isCancelled else {
                    continuation.resume(throwing: CancellationError())
                    return
                }
                if flight != nil {
                    flight?.waiters[waiterID] = continuation
                } else {
                    startFlight(source: source, waiterID: waiterID, continuation: continuation)
                }
            }
        } onCancel: {
            Task { await self.cancelWaiter(waiterID) }
        }
        try Task.checkCancellation()
        return token
    }

    private func startFlight(source: @escaping Source, waiterID: UUID,
                             continuation: CheckedContinuation<MatrixOpenIDToken, Error>) {
        let id = UUID()
        // TTL includes request latency. Unstructured tasks let the deadline
        // release waiters even if the SDK source ignores cooperative cancellation.
        let startedAt = now()
        let request = Task { [weak self, now] in
            let result: Result<(MatrixOpenIDToken, Date), Error>
            do {
                let token: MatrixOpenIDToken
                do { token = try await source() }
                catch { throw MatrixOpenIDError.requestFailed }
                guard token.isValid else { throw MatrixOpenIDError.invalidTokenResponse }
                let margin = min(max(Self.minimumRefreshMargin, token.expiresInSeconds * Self.refreshFraction),
                                 token.expiresInSeconds * Self.maximumLifetimeFraction)
                let refreshAt = startedAt.addingTimeInterval(token.expiresInSeconds - margin)
                guard refreshAt.timeIntervalSinceReferenceDate.isFinite, now() <= refreshAt else {
                    throw MatrixOpenIDError.invalidTokenResponse
                }
                result = .success((token, refreshAt))
            } catch {
                result = .failure(error as? MatrixOpenIDError == .invalidTokenResponse
                    ? MatrixOpenIDError.invalidTokenResponse : MatrixOpenIDError.requestFailed)
            }
            await self?.finish(id, result: result)
        }
        let timeout = Task { [weak self, timeoutSleep, requestTimeout] in
            do { try await timeoutSleep(requestTimeout) }
            catch { return }
            guard !Task.isCancelled else { return }
            await self?.finish(id, result: .failure(MatrixOpenIDError.requestTimedOut))
        }
        flight = Flight(id: id, request: request, timeout: timeout, waiters: [waiterID: continuation])
    }

    private func cancelWaiter(_ id: UUID) {
        flight?.waiters.removeValue(forKey: id)?.resume(throwing: CancellationError())
        // Even with no waiters, keep this bounded refresh alive to warm the cache.
        // One caller never cancels the SDK request shared by other callers.
    }

    private func finish(_ id: UUID, result: Result<(MatrixOpenIDToken, Date), Error>) {
        guard let active = flight, active.id == id else { return }
        flight = nil // Reject late SDK completions after timeout/shutdown or a newer flight.
        active.request.cancel()
        active.timeout.cancel()
        if case .success(let value) = result { cached = value }
        for waiter in active.waiters.values { waiter.resume(with: result.map { $0.0 }) }
    }

    public func invalidate(_ token: MatrixOpenIDToken) {
        if cached?.token.identity == token.identity { cached = nil }
    }

    /// Permanently closes this account-bound provider, including old clients
    /// that still retain it. A newly activated SDK client gets a new provider.
    public func shutdown() {
        cached = nil
        source = nil
        if let flight { finish(flight.id, result: .failure(MatrixOpenIDError.sessionEnded)) }
    }
}
