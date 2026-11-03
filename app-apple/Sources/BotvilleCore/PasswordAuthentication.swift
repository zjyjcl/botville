// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation
import MatrixRustSDK

/// Own the password request so 429 is visible immediately. The pinned SDK's
/// login() uses an internal short_retry that hides Retry-After from Swift.
/// Successful credentials still enter the same encrypted SDK session/store.
public protocol PasswordAuthenticating: Sendable {
    func login(at server: URL, username: String, password: String) async throws -> Session
    func discard(_ session: Session) async
}

public struct MatrixPasswordAuthentication: PasswordAuthenticating {
    private let session: URLSession

    public init() {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpCookieStorage = nil
        configuration.urlCredentialStorage = nil
        session = URLSession(configuration: configuration, delegate: HTTPSRedirectGuard(), delegateQueue: nil)
    }

    public init(session: URLSession) { self.session = session }

    public func login(at server: URL, username: String, password: String) async throws -> Session {
        var request = URLRequest(url: server.appendingPathComponent("_matrix/client/v3/login"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(LoginRequest(identifier: Identifier(user: username), password: password))
        let (data, response) = try await session.data(for: request)
        guard let response = response as? HTTPURLResponse else { throw AuthenticationError.networkFailure }
        if let error = MatrixRateLimit.error(response: response, data: data) { throw error }
        if let error = PasswordLoginResultClassifier.error(forHTTPStatus: response.statusCode) { throw error }
        let result = try JSONDecoder().decode(LoginResponse.self, from: data)
        guard !result.access_token.isEmpty, !result.user_id.isEmpty, !result.device_id.isEmpty else { throw AuthenticationError.serverFailure }
        // Ignore response well_known: credentials never move to an unapproved origin.
        return Session(accessToken: result.access_token, refreshToken: result.refresh_token,
                       userId: result.user_id, deviceId: result.device_id,
                       homeserverUrl: server.absoluteString, oauthData: nil, slidingSyncVersion: .none)
    }

    public func discard(_ credentials: Session) async {
        guard let server = URL(string: credentials.homeserverUrl) else { return }
        var request = URLRequest(url: server.appendingPathComponent("_matrix/client/v3/logout"))
        request.httpMethod = "POST"
        request.timeoutInterval = 5
        request.setValue("Bearer \(credentials.accessToken)", forHTTPHeaderField: "Authorization")
        _ = try? await session.data(for: request)
    }

    private struct Identifier: Encodable { let type = "m.id.user"; let user: String }
    private struct LoginRequest: Encodable {
        let type = "m.login.password"
        let identifier: Identifier
        let password: String
        let initial_device_display_name = "botville C2.1"
    }
    private struct LoginResponse: Decodable {
        let access_token: String
        let refresh_token: String?
        let user_id: String
        let device_id: String
    }
}

enum MatrixRateLimit {
    static func error(response: HTTPURLResponse, data: Data, now: Date = Date()) -> AuthenticationError? {
        let body = try? JSONDecoder().decode(ErrorBody.self, from: data)
        guard response.statusCode == 429 || body?.errcode == "M_LIMIT_EXCEEDED" else { return nil }
        var delay: TimeInterval?
        if let header = response.value(forHTTPHeaderField: "Retry-After") {
            if let seconds = Double(header), seconds.isFinite, seconds >= 0 { delay = seconds }
            else {
                let formatter = DateFormatter()
                formatter.locale = Locale(identifier: "en_US_POSIX")
                formatter.timeZone = TimeZone(secondsFromGMT: 0)
                formatter.dateFormat = "EEE, dd MMM yyyy HH:mm:ss zzz"
                delay = formatter.date(from: header).map { max(0, $0.timeIntervalSince(now)) }
            }
        }
        if delay == nil, let milliseconds = body?.retry_after_ms, milliseconds.isFinite, milliseconds >= 0 {
            delay = milliseconds / 1_000
        }
        return .rateLimited(retryAfter: delay)
    }
    private struct ErrorBody: Decodable { let errcode: String?; let retry_after_ms: Double? }
}

/// Unlike a task group, a timeout does not wait for an uncooperative SDK future.
/// The worker retains ownership until it finishes cleanup; callers cannot start
/// another attempt against its store meanwhile. All post-await commits must
/// check cancellation. No abandoned candidate can activate a session later.
@MainActor
final class AuthenticationAttempt {
    private var continuation: CheckedContinuation<Void, Error>?
    private var worker: Task<Void, Never>?
    private var timer: Task<Void, Never>?
    private var cancellation: Error?

    func run(timeout: TimeInterval, operation: @escaping @MainActor () async throws -> Void) async throws {
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                self.continuation = continuation
                worker = Task {
                    do { try await operation(); finish(.success(())) }
                    catch { finish(.failure(error)) }
                    worker = nil
                }
                if let cancellation {
                    worker?.cancel()
                    finish(.failure(cancellation))
                    return
                }
                timer = Task {
                    do { try await Task.sleep(for: .seconds(timeout)) }
                    catch { return }
                    cancel(error: AuthenticationError.timedOut)
                }
            }
        } onCancel: {
            Task { @MainActor in self.cancel() }
        }
    }

    func cancel(error: Error = CancellationError()) {
        cancellation = error
        worker?.cancel()
        finish(.failure(error))
    }

    private func finish(_ result: Result<Void, Error>) {
        timer?.cancel()
        timer = nil
        let pending = continuation
        continuation = nil
        pending?.resume(with: result)
    }
}
