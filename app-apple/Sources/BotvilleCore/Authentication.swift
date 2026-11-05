// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation
import MatrixRustSDK
import Network

/// A homeserver origin policy. Production clients require HTTPS. The explicit
/// development HTTP escape hatch is limited to names and addresses that are
/// meaningful on a local network or a compose-style private network.
public struct HomeserverPolicy: Sendable {
    public let allowLocalHTTP: Bool

    public init(allowLocalHTTP: Bool = false) {
        self.allowLocalHTTP = allowLocalHTTP
    }

    public func validatedURL(_ value: String) throws -> URL {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let url = URL(string: trimmed),
              let components = try? EndpointURLPolicy.validatedComponents(url),
              let scheme = components.scheme?.lowercased(),
              let host = components.host?.lowercased(),
              components.query == nil, components.fragment == nil,
              scheme == "https" || (allowLocalHTTP && scheme == "http" && Self.isLocalNetworkHost(host)) else {
            throw AuthenticationError.invalidHomeserver
        }
        return url
    }

    /// Keep the development exception narrow. In particular, a dotted public
    /// DNS name cannot quietly become a cleartext homeserver just because a
    /// Debug caller supplied `allowLocalHTTP`.
    private static func isLocalNetworkHost(_ host: String) -> Bool {
        if host == "localhost" { return true }
        if isPrivateIPv4(host) || isLocalIPv6(host) { return true }

        if host.hasSuffix(".local") && isWellFormedDNSHost(host) { return true }

        // Docker Compose service names are deliberately single-label. These
        // names can resolve through a device DNS search domain and therefore
        // are not guaranteed to remain on the local network; this DEBUG-only
        // exception requires explicit opt-in. Reject labels containing URL
        // delimiters and labels with a public DNS suffix.
        return isWellFormedDNSLabel(host)
            && !host.lowercased().hasPrefix("0x")
            && host.unicodeScalars.contains(where: isASCIIAlpha)
    }

    private static func isPrivateIPv4(_ host: String) -> Bool {
        guard let address = IPv4Address(host),
              String(describing: address) == host,
              let raw = IPv4Address(host)?.rawValue,
              raw.count == 4 else { return false }
        switch (raw[0], raw[1]) {
        case (10, _), (127, _), (169, 254), (192, 168), (172, 16...31):
            return true
        default:
            return false
        }
    }

    private static func isLocalIPv6(_ host: String) -> Bool {
        let literal: String
        if host.hasPrefix("[") && host.hasSuffix("]") {
            literal = String(host.dropFirst().dropLast())
        } else {
            literal = host
        }
        guard let raw = IPv6Address(literal)?.rawValue, raw.count == 16 else { return false }
        let loopback = raw.dropLast().allSatisfy { $0 == 0 } && raw.last == 1
        let linkLocal = raw[0] == 0xfe && (raw[1] & 0xc0) == 0x80 // fe80::/10
        let uniqueLocal = (raw[0] & 0xfe) == 0xfc // fc00::/7
        return loopback || linkLocal || uniqueLocal
    }

    private static func isWellFormedDNSHost(_ host: String) -> Bool {
        let labels = host.split(separator: ".", omittingEmptySubsequences: false)
        return labels.count > 1 && labels.allSatisfy { isWellFormedDNSLabel(String($0)) }
    }

    private static func isWellFormedDNSLabel(_ label: String) -> Bool {
        guard (1...63).contains(label.utf8.count),
              let first = label.unicodeScalars.first,
              let last = label.unicodeScalars.last,
              isASCIIAlphaNumeric(first), isASCIIAlphaNumeric(last) else { return false }
        return label.unicodeScalars.allSatisfy {
            isASCIIAlphaNumeric($0) || $0 == "-"
        }
    }

    private static func isASCIIAlpha(_ scalar: Unicode.Scalar) -> Bool {
        (65...90).contains(scalar.value) || (97...122).contains(scalar.value)
    }

    private static func isASCIIAlphaNumeric(_ scalar: Unicode.Scalar) -> Bool {
        isASCIIAlpha(scalar) || (48...57).contains(scalar.value)
    }
}

/// Validates the URL authority in its percent-encoded form. `URLComponents`
/// exposes a decoded host, so checking only `host`, `user`, and `password`
/// would allow encoded delimiters to create a differently interpreted origin.
enum EndpointURLPolicy {
    static func validatedComponents(_ url: URL, originalSource: String? = nil) throws -> URLComponents {
        let source = originalSource ?? url.absoluteString
        guard isSafeURLString(source),
              source.removingPercentEncoding.map({ isSafeURLString($0) }) ?? false,
              let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
              let host = components.host, !host.isEmpty,
              let encodedHost = components.percentEncodedHost, !encodedHost.isEmpty,
              !encodedHost.contains("%"),
              components.user == nil, components.password == nil,
              components.fragment == nil else {
            throw URLAuthorityError.invalid
        }

        guard !containsUnsafeHostCharacter(host),
              !containsUnsafeHostCharacter(encodedHost) else {
            throw URLAuthorityError.invalid
        }

        let explicitPort = try explicitPort(in: source)
        guard components.port == explicitPort,
              components.port.map({ (1...65_535).contains($0) }) ?? true else {
            throw URLAuthorityError.invalid
        }
        return components
    }

    static func isSafeURLString(_ value: String) -> Bool {
        value.rangeOfCharacter(from: CharacterSet.whitespacesAndNewlines.union(.controlCharacters)) == nil
    }

    private static func containsUnsafeHostCharacter(_ host: String) -> Bool {
        host.unicodeScalars.contains { scalar in
            CharacterSet.whitespacesAndNewlines.contains(scalar)
                || scalar.value < 0x20 || scalar.value == 0x7F
        } || host.contains("\\") || host.contains("/") || host.contains("@") || host.contains("%")
    }

    /// Returns nil when the authority has no explicit port.  Parsing the raw
    /// authority prevents an overflowing or empty port from being hidden as a
    /// nil `URLComponents.port` value.
    private static func explicitPort(in source: String) throws -> Int? {
        guard let schemeBoundary = source.range(of: "://") else { throw URLAuthorityError.invalid }
        let afterScheme = source[schemeBoundary.upperBound...]
        let authorityEnd = afterScheme.firstIndex(where: { $0 == "/" || $0 == "?" || $0 == "#" }) ?? afterScheme.endIndex
        let authority = String(afterScheme[..<authorityEnd])
        guard !authority.isEmpty, !authority.contains("@") else { throw URLAuthorityError.invalid }

        let portText: Substring?
        if authority.first == "[" {
            guard let closingBracket = authority.firstIndex(of: "]") else { throw URLAuthorityError.invalid }
            let suffix = authority[authority.index(after: closingBracket)...]
            guard !suffix.contains("[") && !suffix.contains("]") else { throw URLAuthorityError.invalid }
            if suffix.isEmpty {
                portText = nil
            } else {
                guard suffix.first == ":" else { throw URLAuthorityError.invalid }
                portText = suffix.dropFirst()
            }
        } else {
            let colonCount = authority.reduce(into: 0) { if $1 == ":" { $0 += 1 } }
            guard colonCount <= 1 else { throw URLAuthorityError.invalid }
            if let colon = authority.lastIndex(of: ":") {
                guard colon != authority.startIndex else { throw URLAuthorityError.invalid }
                portText = authority[authority.index(after: colon)...]
            } else {
                portText = nil
            }
        }

        guard let portText else { return nil }
        guard !portText.isEmpty,
              portText.allSatisfy({ $0.isASCII && $0.isNumber }),
              let port = Int(portText),
              (1...65_535).contains(port) else {
            throw URLAuthorityError.invalid
        }
        return port
    }

    private enum URLAuthorityError: Error { case invalid }
}

public enum AuthenticationError: LocalizedError, Equatable, Sendable {
    case invalidHomeserver
    case discoveryFailed
    case networkFailure
    case invalidCredentials
    case unsupportedLoginMethod
    case serverFailure
    case rateLimited(retryAfter: TimeInterval?)
    case timedOut

    public var errorDescription: String? {
        switch self {
        case .invalidHomeserver: return "请输入完整的 HTTPS homeserver 地址；开发 HTTP 仅允许局域网或 compose 本地地址。"
        case .discoveryFailed: return "服务器能力探测失败，请确认这是兼容的 Matrix homeserver。"
        case .networkFailure: return "无法连接服务器，请检查网络后重试。"
        case .invalidCredentials: return "用户名或密码错误。"
        case .unsupportedLoginMethod: return "该服务器不支持密码登录，请使用支持的登录方式。"
        case .timedOut: return "登录或恢复会话超时，请检查网络后重试。"
        case .rateLimited(let delay):
            if let delay { return "服务器请求过于频繁，请等待至少 \(Int(min(delay.rounded(.up), 86_400))) 秒后重试；可取消本次操作。" }
            return "服务器请求过于频繁，未提供等待时长，请稍后重试；可取消本次操作。"
        case .serverFailure: return "服务器暂时无法处理登录请求，请稍后重试。"
        }
    }
}

public struct LoginFlow: Decodable, Equatable, Sendable {
    public let type: String
}

public struct LoginCapabilities: Equatable, Sendable {
    public let supportsPassword: Bool
}

public protocol LoginCapabilityDiscovering: Sendable {
    func discover(at homeserver: URL) async throws -> LoginCapabilities
}

/// A small, credential-free discovery client. It never sends a password and
/// deliberately maps transport/server details to safe user-facing categories.
public struct MatrixLoginCapabilityDiscovery: LoginCapabilityDiscovering {
    private let session: URLSession

    public init() {
        self.session = Self.secureSession()
    }

    public init(session: URLSession) {
        self.session = session
    }

    private static func secureSession() -> URLSession {
        URLSession(configuration: .ephemeral, delegate: HTTPSRedirectGuard(), delegateQueue: nil)
    }

    public func discover(at homeserver: URL) async throws -> LoginCapabilities {
        let versions = endpoint("_matrix/client/versions", at: homeserver)
        let login = endpoint("_matrix/client/v3/login", at: homeserver)
        do {
            let (versionData, versionResponse) = try await session.data(from: versions)
            guard let response = versionResponse as? HTTPURLResponse else { throw AuthenticationError.networkFailure }
            guard (200..<300).contains(response.statusCode) else { throw discoveryError(response: response, data: versionData) }
            guard validVersions(versionData) else { throw AuthenticationError.discoveryFailed }
            let (loginData, loginResponse) = try await session.data(from: login)
            guard let response = loginResponse as? HTTPURLResponse else { throw AuthenticationError.networkFailure }
            guard (200..<300).contains(response.statusCode) else { throw discoveryError(response: response, data: loginData) }
            let document = try JSONDecoder().decode(LoginDocument.self, from: loginData)
            return LoginCapabilities(supportsPassword: document.flows.contains { $0.type == "m.login.password" })
        } catch let error as AuthenticationError {
            throw error
        } catch let error as URLError {
            throw PasswordLoginResultClassifier.error(for: error)
        } catch is DecodingError {
            throw AuthenticationError.discoveryFailed
        } catch {
            throw AuthenticationError.discoveryFailed
        }
    }

    private func endpoint(_ path: String, at homeserver: URL) -> URL {
        homeserver.appendingPathComponent(path)
    }

    private func validVersions(_ data: Data) -> Bool {
        (try? JSONDecoder().decode(VersionsDocument.self, from: data).versions.isEmpty) == false
    }

    private func discoveryError(response: HTTPURLResponse, data: Data) -> AuthenticationError {
        MatrixRateLimit.error(response: response, data: data)
            ?? ((500..<600).contains(response.statusCode) ? .serverFailure : .discoveryFailed)
    }

    private struct VersionsDocument: Decodable { let versions: [String] }
    private struct LoginDocument: Decodable { let flows: [LoginFlow] }
}

/// Classifies a password login result without exposing a server body.
public enum PasswordLoginResultClassifier {
    public static func error(forHTTPStatus status: Int) -> AuthenticationError? {
        switch status {
        case 200..<300: return nil
        case 429: return .rateLimited(retryAfter: nil)
        case 401, 403: return .invalidCredentials
        case 404, 405: return .unsupportedLoginMethod
        case 500..<600: return .serverFailure
        default: return .serverFailure
        }
    }

    /// Converts SDK errors into display-safe authentication errors.  Matrix
    /// response messages and details can contain server-controlled text, so
    /// they are intentionally never surfaced here.
    public static func error(for error: Error) -> AuthenticationError {
        if let error = error as? AuthenticationError { return error }
        if let error = error as? URLError { return Self.error(forTransportURLCode: error.code) }
        switch error {
        case ClientBuildError.ServerUnreachable: return .networkFailure
        case ClientBuildError.InvalidServerName, ClientBuildError.WellKnownLookupDisabled,
             ClientBuildError.WellKnownLookupFailed, ClientBuildError.WellKnownDeserializationError:
            return .discoveryFailed
        case ClientBuildError.Sdk, ClientBuildError.Generic:
            // The SDK exposes no transport subtype for these variants.  Their
            // diagnostic strings are not a stable or safe classification API.
            return .serverFailure
        case let ClientError.MatrixApi(kind, code, _, _):
            if case .limitExceeded(let milliseconds) = kind {
                return .rateLimited(retryAfter: milliseconds.map { Double($0) / 1_000 })
            }
            switch kind {
            case .connectionFailed, .connectionTimeout:
                return .networkFailure
            default:
                break
            }
            switch code {
            case "M_LIMIT_EXCEEDED": return .rateLimited(retryAfter: nil)
            case "M_UNKNOWN_TOKEN", "M_FORBIDDEN": return .invalidCredentials
            default: return .serverFailure
            }
        case ClientError.Generic, ClientError.ContentScanner:
            // These variants intentionally carry only unstructured details.
            // Do not infer a network outage from server-controlled text.
            return .serverFailure
        default:
            return .serverFailure
        }
    }

    /// Only an explicit unknown-token response revokes persisted credentials.
    /// A generic transport error or a room-level forbidden response must not
    /// make another account's stored session disappear.
    public static func isExpiredSession(_ error: Error) -> Bool {
        guard case let ClientError.MatrixApi(_, code, _, _) = error else { return false }
        return code == "M_UNKNOWN_TOKEN"
    }

    private static func error(forTransportURLCode code: URLError.Code) -> AuthenticationError {
        switch code {
        case .cannotFindHost, .cannotConnectToHost, .networkConnectionLost,
             .dnsLookupFailed, .notConnectedToInternet, .internationalRoamingOff,
             .callIsActive, .timedOut, .secureConnectionFailed,
             .serverCertificateHasBadDate, .serverCertificateUntrusted,
             .serverCertificateHasUnknownRoot, .serverCertificateNotYetValid,
             .clientCertificateRejected, .clientCertificateRequired:
            return .networkFailure
        default:
            // URL loading also reports request construction and response
            // parsing failures as URLError.  Only the explicit transport
            // codes above are connectivity failures.
            return .serverFailure
        }
    }
}

/// Redirects must stay on the origin the user explicitly approved.  This also
/// closes the development-only loopback exception against cross-origin hops.
public enum HTTPSRedirectPolicy {
    public static func allowsRedirect(from original: URL, to target: URL) -> Bool {
        normalizedOrigin(original) == normalizedOrigin(target)
    }

    private static func normalizedOrigin(_ url: URL) -> String? {
        guard let scheme = url.scheme?.lowercased(), let host = url.host?.lowercased() else { return nil }
        let port = url.port ?? (scheme == "https" ? 443 : scheme == "http" ? 80 : -1)
        return "\(scheme)://\(host):\(port)"
    }
}

final class HTTPSRedirectGuard: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        guard let original = task.originalRequest?.url, let target = request.url,
              HTTPSRedirectPolicy.allowsRedirect(from: original, to: target) else {
            completionHandler(nil)
            return
        }
        completionHandler(request)
    }
}
