// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation

/// A short-lived QR handoff.  The redemption code deliberately has no public
/// accessor and is never written to disk or logged.
public struct PairingQRCode: Sendable, Equatable {
    public let homeserver: URL
    public let gateway: URL?
    public let fingerprint: Data?
    public let expiresAt: Date
    private let code: Data
    /// Kept in its QR spelling for the redeem response's exact-match check.
    /// URL is intentionally unsuitable for this comparison because it normalizes
    /// forms such as host casing or a trailing slash.
    fileprivate let homeserverSource: String

    private init(homeserver: URL, homeserverSource: String, gateway: URL?, fingerprint: Data?, code: Data, expiresAt: Date) {
        self.homeserver = homeserver
        self.homeserverSource = homeserverSource
        self.gateway = gateway
        self.fingerprint = fingerprint
        self.code = code
        self.expiresAt = expiresAt
    }

    public static func parse(_ value: String, now: Date = Date()) throws -> PairingQRCode {
        // QR values are a small, deliberately non-percent-encoded grammar.
        // Do not use URLComponents.queryItems here: it decodes before validation.
        let source = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard source.hasPrefix("botville://pair?"),
              !source.contains("%"),
              !source.contains("#"),
              EndpointURLPolicy.isSafeURLString(source) else { throw PairingError.invalidQRCode }

        let query = source.dropFirst("botville://pair?".count)
        let items = query.split(separator: "&", omittingEmptySubsequences: false)
        var values: [String: String] = [:]
        for item in items {
            guard !item.isEmpty, let equals = item.firstIndex(of: "=") else { throw PairingError.invalidQRCode }
            let name = String(item[..<equals])
            let itemValue = String(item[item.index(after: equals)...])
            guard ["v", "hs", "gw", "fp", "code", "exp"].contains(name) else { throw PairingError.invalidQRCode }
            guard values[name] == nil else { throw PairingError.duplicateParameter }
            values[name] = itemValue
        }
        guard let version = values["v"], let homeserverValue = values["hs"],
              let codeValue = values["code"], let expirationValue = values["exp"] else {
            throw PairingError.missingParameter
        }
        guard version == "1" else { throw PairingError.unsupportedVersion }
        let homeserver = try PairingURLPolicy.validatedHTTPS(homeserverValue)
        let gateway = try values["gw"].map(PairingURLPolicy.validatedHTTPS)
        let code = try decodeBase64URL(codeValue, expectedLength: 32)
        let fingerprint: Data?
        if let source = values["fp"] {
            guard source.hasPrefix("sha256:") else { throw PairingError.invalidFingerprint }
            fingerprint = try decodeBase64URL(String(source.dropFirst("sha256:".count)), expectedLength: 32, error: .invalidFingerprint)
        } else { fingerprint = nil }
        guard let seconds = Int64(expirationValue), seconds >= 0, String(seconds) == expirationValue else { throw PairingError.invalidExpiration }
        let expiresAt = Date(timeIntervalSince1970: TimeInterval(seconds))
        guard expiresAt > now else { throw PairingError.expiredLocally }
        return PairingQRCode(homeserver: homeserver, homeserverSource: homeserverValue, gateway: gateway, fingerprint: fingerprint, code: code, expiresAt: expiresAt)
    }

    fileprivate var redeemCode: Data { code }

    private static func decodeBase64URL(_ source: String, expectedLength: Int, error: PairingError = .invalidCode) throws -> Data {
        guard !source.isEmpty,
              source.unicodeScalars.allSatisfy({ ($0.value >= 65 && $0.value <= 90) || ($0.value >= 97 && $0.value <= 122) || ($0.value >= 48 && $0.value <= 57) || $0.value == 45 || $0.value == 95 }),
              source.count % 4 != 1 else { throw error }
        let padded = source.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/") + String(repeating: "=", count: (4 - source.count % 4) % 4)
        guard let data = Data(base64Encoded: padded), data.count == expectedLength,
              data.base64URLEncodedString() == source else { throw error }
        return data
    }
}

enum PairingURLPolicy {
    static func validatedHTTPS(_ source: String) throws -> URL {
        guard source.hasPrefix("https://"), EndpointURLPolicy.isSafeURLString(source), let url = URL(string: source),
              let components = try? EndpointURLPolicy.validatedComponents(url, originalSource: source),
              components.scheme == "https", components.query == nil, components.fragment == nil else {
            throw PairingError.invalidEndpoint
        }
        return url
    }

    static func sameEndpoint(_ lhs: URL, _ rhs: URL) -> Bool {
        lhs.absoluteString == rhs.absoluteString
    }
}

public enum PairingError: Error, LocalizedError, Equatable, Sendable {
    case invalidQRCode, duplicateParameter, missingParameter, unsupportedVersion
    case invalidEndpoint, invalidFingerprint, invalidCode, invalidExpiration, expiredLocally
    case invalidResponse, homeserverMismatch, certificateMismatch, networkFailure, serverFailure
    case invalid, expired, revoked, used, rateLimited(retryAfter: TimeInterval?)

    public var errorDescription: String? {
        switch self {
        case .invalidQRCode: return "二维码链接格式无效。"
        case .duplicateParameter: return "二维码包含重复参数，已拒绝。"
        case .missingParameter: return "二维码缺少必要参数。"
        case .unsupportedVersion: return "请升级 App。"
        case .invalidEndpoint: return "二维码中的服务器地址必须是严格的 HTTPS URL。"
        case .invalidFingerprint: return "二维码中的证书指纹无效。"
        case .invalidCode: return "二维码中的配对码无效。"
        case .invalidExpiration: return "二维码中的过期时间无效。"
        case .expiredLocally: return "二维码已过期，请重新扫码。"
        case .invalidResponse: return "配对服务返回了无效响应。"
        case .homeserverMismatch: return "配对服务返回的 Homeserver 与二维码不一致，已拒绝。"
        case .certificateMismatch: return "服务器证书校验失败，已拒绝。请检查二维码指纹、证书主机名、有效期及设备时间。"
        case .networkFailure: return "无法连接配对服务，请检查网络后重试。"
        case .serverFailure: return "配对服务暂时无法处理请求，请稍后重试。"
        case .invalid: return "配对码无效，请重新扫码。"
        case .expired: return "配对码已过期，请重新扫码。"
        case .revoked: return "配对码已撤销，请重新扫码。"
        case .used: return "配对码已使用，请重新生成二维码。"
        case .rateLimited(let delay):
            if let delay { return "请求过于频繁，请等待至少 \(Int(min(max(delay.rounded(.up), 0), 86_400))) 秒后重试。" }
            return "请求过于频繁，请稍后重试。"
        }
    }
}

public enum PairingAuthType: String, Codable, Equatable, Sendable { case registrationToken = "registration_token", loginToken = "login_token" }
public struct PairingAuthentication: Codable, Equatable, Sendable {
    public let type: PairingAuthType
    public let token: String
    public let expiresInMS: Int
    enum CodingKeys: String, CodingKey { case type, token; case expiresInMS = "expires_in_ms" }
}
public struct PairingRedeemResponse: Equatable, Sendable {
    public let homeserver: URL
    public let gateway: URL?
    public let auth: PairingAuthentication
    public let featuresURL: URL?
    public let receivedAt: Date

    public init(homeserver: URL, gateway: URL?, auth: PairingAuthentication, featuresURL: URL?, receivedAt: Date) {
        self.homeserver = homeserver
        self.gateway = gateway
        self.auth = auth
        self.featuresURL = featuresURL
        self.receivedAt = receivedAt
    }

    public var authExpiresAt: Date {
        receivedAt.addingTimeInterval(Double(auth.expiresInMS) / 1_000)
    }

    public func isAuthExpired(at date: Date = Date()) -> Bool { authExpiresAt <= date }
}

public protocol PairingService: Sendable {
    func redeem(_ pairing: PairingQRCode, deviceDisplayName: String) async throws -> PairingRedeemResponse
}

public struct HTTPPairingService: PairingService {
    public init() {}
    public func redeem(_ pairing: PairingQRCode, deviceDisplayName: String) async throws -> PairingRedeemResponse {
        guard pairing.expiresAt > Date() else { throw PairingError.expiredLocally }
        let request = try Self.redeemRequest(pairing: pairing, deviceDisplayName: deviceDisplayName)
        // The QR pin protects the homeserver's redeem request. A gateway is not
        // an alternate redeem endpoint; pinning gateway traffic is decided when
        // its origin is known to equal this homeserver origin.
        let session = PairingURLSession.make(fingerprint: pairing.fingerprint, host: pairing.homeserver.host, port: pairing.homeserver.port)
        defer { session.invalidateAndCancel() }
        do {
            let (data, response) = try await PairingURLSession.data(for: request, using: session)
            guard let http = response as? HTTPURLResponse else { throw PairingError.networkFailure }
            guard http.statusCode == 200 else { throw Self.serverError(status: http.statusCode, data: data) }
            return try Self.decodeResponse(data, pairing: pairing, redeemHost: pairing.homeserver.host, receivedAt: Date())
        } catch let error as PairingError { throw error }
        catch is DecodingError { throw PairingError.invalidResponse }
        catch { throw PairingError.networkFailure }
    }

    static func serverError(status: Int, data: Data) -> PairingError {
        let error = try? JSONDecoder().decode(ErrorBody.self, from: data)
        switch error?.errcode {
        case "BV_PAIR_INVALID": return .invalid
        case "BV_PAIR_EXPIRED": return .expired
        case "BV_PAIR_REVOKED": return .revoked
        case "BV_PAIR_USED": return .used
        case "BV_PAIR_RATE_LIMITED": return .rateLimited(retryAfter: error?.retryAfterMS.flatMap { $0 >= 0 ? Double($0) / 1000 : nil })
        default: return .serverFailure
        }
    }

    static func redeemRequest(pairing: PairingQRCode, deviceDisplayName: String) throws -> URLRequest {
        let endpoint = pairing.homeserver.appendingPathComponent("_botville/pair/redeem")
        var request = URLRequest(url: endpoint)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        let truncatedName = String(deviceDisplayName.prefix(128))
        request.httpBody = try JSONEncoder().encode(RequestBody(code: pairing.redeemCode.base64URLEncodedString(), deviceDisplayName: truncatedName.isEmpty ? "Botville" : truncatedName))
        return request
    }

    static func decodeResponse(_ data: Data, pairing: PairingQRCode, redeemHost: String?, receivedAt: Date) throws -> PairingRedeemResponse {
        let wire = try JSONDecoder().decode(ResponseBody.self, from: data)
        guard wire.v == "1", let homeserver = try? PairingURLPolicy.validatedHTTPS(wire.homeserver) else { throw PairingError.invalidResponse }
        guard wire.homeserver.utf8.elementsEqual(pairing.homeserverSource.utf8) else { throw PairingError.homeserverMismatch }
        guard let authType = PairingAuthType(rawValue: wire.auth.type), !wire.auth.token.isEmpty, wire.auth.expiresInMS >= 1 else { throw PairingError.invalidResponse }
        // B3 divergence from the A-line contract: a QR without gw is chat-only.
        // Do not let a response re-enable gateway features that QR omitted.
        let responseGateway = try wire.gateway.map(PairingURLPolicy.validatedHTTPS)
        let responseFeatures: URL?
        if responseGateway == nil {
            responseFeatures = nil
        } else {
            guard let featuresURL = wire.featuresURL else { throw PairingError.invalidResponse }
            responseFeatures = try PairingURLPolicy.validatedHTTPS(featuresURL)
        }
        let gateway = pairing.gateway == nil ? nil : responseGateway
        let features = pairing.gateway == nil ? nil : responseFeatures
        return PairingRedeemResponse(homeserver: homeserver, gateway: gateway, auth: PairingAuthentication(type: authType, token: wire.auth.token, expiresInMS: wire.auth.expiresInMS), featuresURL: features, receivedAt: receivedAt)
    }

    private struct RequestBody: Encodable { let code: String; let deviceDisplayName: String; enum CodingKeys: String, CodingKey { case code; case deviceDisplayName = "device_display_name" } }
    private struct ResponseBody: Decodable { let v: String; let homeserver: String; let gateway: String?; let auth: Auth; let featuresURL: String?; enum CodingKeys: String, CodingKey { case v, homeserver, gateway, auth; case featuresURL = "features_url" }; struct Auth: Decodable { let type: String; let token: String; let expiresInMS: Int; enum CodingKeys: String, CodingKey { case type, token; case expiresInMS = "expires_in_ms" } } }
    private struct ErrorBody: Decodable { let errcode: String?; let retryAfterMS: Int?; enum CodingKeys: String, CodingKey { case errcode; case retryAfterMS = "retry_after_ms" } }
}

private extension Data {
    func base64URLEncodedString() -> String { base64EncodedString().replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: "") }
}

#if DEBUG
public enum PairingMockData {
    public static let exampleURI = "botville://pair?v=1&hs=https://matrix.example.invalid&gw=https://gateway.example.invalid&fp=sha256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA&code=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA&exp=4102444800"
}
public struct MockPairingService: PairingService {
    public init() {}
    public func redeem(_ pairing: PairingQRCode, deviceDisplayName: String) async throws -> PairingRedeemResponse {
        guard let url = Bundle.module.url(forResource: "pairing-mock", withExtension: "json") else { throw PairingError.invalidResponse }
        return try HTTPPairingService.decodeResponse(Data(contentsOf: url), pairing: pairing,
                                                     redeemHost: pairing.homeserver.host,
                                                     receivedAt: Date())
    }
}
#endif
