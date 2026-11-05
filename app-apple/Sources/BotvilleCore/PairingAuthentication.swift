// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation
import MatrixRustSDK

/// Performs the short-lived Matrix credential exchange returned by pairing.
/// The caller owns the token and must never persist or log it.  Successful
/// replies are converted directly into the SDK Session used by MatrixService.
public protocol PairingCredentialAuthenticating: Sendable {
    func register(at homeserver: URL, registrationToken: String, username: String,
                  password: String, deviceDisplayName: String, deadline: Date) async throws -> Session
    func login(at homeserver: URL, loginToken: String,
               deviceDisplayName: String, deadline: Date) async throws -> Session
    func configuredForPairing(fingerprint: Data?, host: String?, port: Int?) -> any PairingCredentialAuthenticating
}

public extension PairingCredentialAuthenticating {
    func configuredForPairing(fingerprint: Data?, host: String?, port: Int? = nil) -> any PairingCredentialAuthenticating { self }
}

/// A one-time pairing exchange may have created a remote session before a
/// local SDK/store failure. The client never retries that exchange itself;
/// the service must decide whether a new pairing is still permitted.
public enum PairingAuthenticationError: LocalizedError, Equatable, Sendable {
    case remoteStateUnknown

    public var errorDescription: String? {
        "配对凭证可能已被服务端使用，但本机未能完成会话保存。请不要重复扫码；请在服务端确认配对状态后再试。"
    }
}

public struct MatrixPairingCredentialAuthentication: PairingCredentialAuthenticating {
    let session: URLSession

    public init() {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpCookieStorage = nil
        configuration.urlCredentialStorage = nil
        session = URLSession(configuration: configuration, delegate: HTTPSRedirectGuard(), delegateQueue: nil)
    }

    public init(session: URLSession) { self.session = session }

    public func configuredForPairing(fingerprint: Data?, host: String?, port: Int? = nil) -> any PairingCredentialAuthenticating {
        guard let fingerprint, let host else { return self }
        return MatrixPairingCredentialAuthentication(session: PairingURLSession.make(fingerprint: fingerprint, host: host, port: port))
    }

    public func register(at homeserver: URL, registrationToken: String, username: String,
                         password: String, deviceDisplayName: String, deadline: Date) async throws -> Session {
        var request = self.request(at: homeserver, path: "_matrix/client/v3/register")
        request.httpBody = try JSONEncoder().encode(RegistrationRequest(
            username: username, password: password, initial_device_display_name: deviceDisplayName,
            auth: RegistrationAuth(token: registrationToken, session: nil)
        ))
        let first = try await responseRetryingRateLimit(for: request, deadline: deadline)
        if first.response.statusCode == 200 { return try session(from: first.data, homeserver: homeserver) }
        guard first.response.statusCode == 401, let initial = try? JSONDecoder().decode(UIAChallenge.self, from: first.data),
              !initial.session.isEmpty, initial.allowsOnlySupportedStages else {
            throw AuthenticationError.serverFailure
        }
        if initial.completed?.contains("m.login.registration_token") == true {
            return try await completeDummyRegistration(at: homeserver, username: username, password: password,
                                                       deviceDisplayName: deviceDisplayName, session: initial.session, deadline: deadline)
        }
        var tokenCompletion = self.request(at: homeserver, path: "_matrix/client/v3/register")
        tokenCompletion.httpBody = try JSONEncoder().encode(RegistrationRequest(
            username: username, password: password, initial_device_display_name: deviceDisplayName,
            auth: RegistrationAuth(token: registrationToken, session: initial.session)
        ))
        let second = try await responseRetryingRateLimit(for: tokenCompletion, deadline: deadline)
        if second.response.statusCode == 200 { return try session(from: second.data, homeserver: homeserver) }
        guard second.response.statusCode == 401, let final = try? JSONDecoder().decode(UIAChallenge.self, from: second.data),
              !final.session.isEmpty, final.requiresDummyAfterRegistrationToken else {
            throw AuthenticationError.serverFailure
        }
        return try await completeDummyRegistration(at: homeserver, username: username, password: password,
                                                   deviceDisplayName: deviceDisplayName, session: final.session, deadline: deadline)
    }

    public func login(at homeserver: URL, loginToken: String,
                      deviceDisplayName: String, deadline: Date) async throws -> Session {
        var request = self.request(at: homeserver, path: "_matrix/client/v3/login")
        request.httpBody = try JSONEncoder().encode(LoginTokenRequest(token: loginToken, initial_device_display_name: deviceDisplayName))
        let result = try await responseRetryingRateLimit(for: request, deadline: deadline)
        guard result.response.statusCode == 200 else { throw PasswordLoginResultClassifier.error(forHTTPStatus: result.response.statusCode) ?? AuthenticationError.serverFailure }
        return try session(from: result.data, homeserver: homeserver)
    }

    private func request(at homeserver: URL, path: String) -> URLRequest {
        var request = URLRequest(url: homeserver.appendingPathComponent(path))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        return request
    }

    private func completeDummyRegistration(at homeserver: URL, username: String, password: String,
                                           deviceDisplayName: String, session: String, deadline: Date) async throws -> Session {
        var request = self.request(at: homeserver, path: "_matrix/client/v3/register")
        request.httpBody = try JSONEncoder().encode(RegistrationRequest(
            username: username, password: password, initial_device_display_name: deviceDisplayName,
            auth: DummyAuth(session: session)
        ))
        let result = try await responseRetryingRateLimit(for: request, deadline: deadline)
        guard result.response.statusCode == 200 else { throw PasswordLoginResultClassifier.error(forHTTPStatus: result.response.statusCode) ?? AuthenticationError.serverFailure }
        return try self.session(from: result.data, homeserver: homeserver)
    }

    private func response(for request: URLRequest) async throws -> (data: Data, response: HTTPURLResponse) {
        do {
            let (data, response) = try await PairingURLSession.data(for: request, using: session)
            guard let response = response as? HTTPURLResponse else { throw AuthenticationError.networkFailure }
            if let error = MatrixRateLimit.error(response: response, data: data) { throw error }
            return (data, response)
        } catch let error as PairingError { throw error }
        catch let error as AuthenticationError { throw error }
        catch let error as URLError { throw PasswordLoginResultClassifier.error(for: error) }
        catch { throw AuthenticationError.networkFailure }
    }

    /// Retry only the exact request that received 429.  In particular, a
    /// dummy UIA continuation never replays the already completed token stage.
    private func responseRetryingRateLimit(for request: URLRequest, deadline: Date) async throws -> (data: Data, response: HTTPURLResponse) {
        while true {
            guard Date() < deadline else { throw PairingError.expired }
            do { return try await response(for: request) }
            catch AuthenticationError.rateLimited(let delay) {
                let wait = max(1, delay ?? 5)
                guard Date().addingTimeInterval(wait) < deadline else { throw PairingError.expired }
                try await Task.sleep(for: .seconds(wait))
            }
        }
    }

    private func session(from data: Data, homeserver: URL) throws -> Session {
        let result: SessionResponse
        do { result = try JSONDecoder().decode(SessionResponse.self, from: data) }
        catch { throw AuthenticationError.serverFailure }
        guard !result.access_token.isEmpty, !result.user_id.isEmpty, !result.device_id.isEmpty else {
            throw AuthenticationError.serverFailure
        }
        // Do not accept a server-provided well_known redirect: the QR code
        // selected the origin, and SDK restoration must retain that origin.
        return Session(accessToken: result.access_token, refreshToken: result.refresh_token,
                       userId: result.user_id, deviceId: result.device_id,
                       homeserverUrl: homeserver.absoluteString, oauthData: nil,
                       slidingSyncVersion: .none)
    }

    private struct RegistrationRequest<Auth: Encodable>: Encodable {
        let username: String
        let password: String
        let initial_device_display_name: String
        let auth: Auth
    }
    private struct RegistrationAuth: Encodable {
        let type = "m.login.registration_token"
        let token: String
        let session: String?
        enum CodingKeys: String, CodingKey { case type, token, session }
        func encode(to encoder: Encoder) throws {
            var container = encoder.container(keyedBy: CodingKeys.self)
            try container.encode(type, forKey: .type)
            try container.encode(token, forKey: .token)
            try container.encodeIfPresent(session, forKey: .session)
        }
    }
    private struct DummyAuth: Encodable {
        let type = "m.login.dummy"
        let session: String
    }
    private struct LoginTokenRequest: Encodable {
        let type = "m.login.token"
        let token: String
        let initial_device_display_name: String
    }
    private struct SessionResponse: Decodable {
        let access_token: String
        let refresh_token: String?
        let user_id: String
        let device_id: String
    }
    /// We only support the token-plus-dummy UIA flow. Any CAPTCHA, e-mail or
    /// unknown stage is rejected instead of guessing how to continue it.
    private struct UIAChallenge: Decodable {
        let flows: [Flow]
        let session: String
        let completed: [String]?
        struct Flow: Decodable { let stages: [String] }
        var allowsOnlySupportedStages: Bool {
            !flows.isEmpty && flows.allSatisfy { $0.stages == ["m.login.registration_token"] || $0.stages == ["m.login.registration_token", "m.login.dummy"] }
        }
        var requiresDummyAfterRegistrationToken: Bool {
            allowsOnlySupportedStages && completed?.contains("m.login.registration_token") == true &&
                flows.contains { $0.stages == ["m.login.registration_token", "m.login.dummy"] }
        }
    }
}
