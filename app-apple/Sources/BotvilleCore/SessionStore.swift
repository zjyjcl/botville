// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation
import MatrixRustSDK
#if canImport(Security)
import Security
import OSLog
#endif

/// Sensitive session material. Instances are encoded only for a Keychain item,
/// never into the SQLite directory or user defaults.
public struct SessionCredentials: Codable, Equatable, Sendable {
    public let accessToken: String
    public let refreshToken: String?
    public let userID: String
    public let deviceID: String
    public let homeserverURL: String
    public let oauthData: String?
    public let storeID: UUID
    public let storeKey: Data
    public let allowLocalHTTP: Bool
    /// Non-secret gateway trust configuration obtained from a confirmed QR.
    /// The pairing code and temporary auth token are deliberately absent.
    public let gatewayURL: String?
    public let gatewayCertificateFingerprint: Data?

    /// Recheck the host on restore as older records may have copied the hs pin
    /// to a different gateway. Cross-host gateways always use system TLS.
    var effectiveGatewayCertificateFingerprint: Data? {
        guard let homeserver = URL(string: homeserverURL), let gatewayURL,
              let gateway = URL(string: gatewayURL) else { return nil }
        return PairingURLSession.fingerprint(gatewayCertificateFingerprint, homeserver: homeserver, requestURL: gateway)
    }

    public init(accessToken: String, refreshToken: String?, userID: String, deviceID: String, homeserverURL: String, oauthData: String?, storeID: UUID, storeKey: Data, allowLocalHTTP: Bool = false,
                gatewayURL: String? = nil, gatewayCertificateFingerprint: Data? = nil) {
        self.accessToken = accessToken
        self.refreshToken = refreshToken
        self.userID = userID
        self.deviceID = deviceID
        self.homeserverURL = homeserverURL
        self.oauthData = oauthData
        self.storeID = storeID
        self.storeKey = storeKey
        self.allowLocalHTTP = allowLocalHTTP
        self.gatewayURL = gatewayURL
        self.gatewayCertificateFingerprint = gatewayCertificateFingerprint
    }

    public init(session: Session, storeID: UUID, storeKey: Data, allowLocalHTTP: Bool = false,
                gatewayURL: String? = nil, gatewayCertificateFingerprint: Data? = nil) {
        self.init(accessToken: session.accessToken, refreshToken: session.refreshToken, userID: session.userId, deviceID: session.deviceId, homeserverURL: session.homeserverUrl, oauthData: session.oauthData, storeID: storeID, storeKey: storeKey, allowLocalHTTP: allowLocalHTTP, gatewayURL: gatewayURL, gatewayCertificateFingerprint: gatewayCertificateFingerprint)
    }

    /// C2.1 uses standard /sync, so restored password sessions always resume
    /// with `.none`; no untrusted serialized SDK state is accepted here.
    public var sdkSession: Session {
        Session(accessToken: accessToken, refreshToken: refreshToken, userId: userID, deviceId: deviceID, homeserverUrl: homeserverURL, oauthData: oauthData, slidingSyncVersion: .none)
    }

    public static func makeStoreKey() throws -> Data {
        #if canImport(Security)
        var bytes = [UInt8](repeating: 0, count: 32)
        guard SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) == errSecSuccess else { throw SessionStoreError.unavailable }
        return Data(bytes)
        #else
        throw SessionStoreError.unavailable
        #endif
    }
}

/// A persisted development exception is never a production capability.  The
/// stored bit predates the build that is restoring it, so the current build
/// must make the final policy decision.
struct RestoredHomeserverPolicy: Sendable {
    let allowLocalHTTP: Bool

    init(storedAllowLocalHTTP: Bool, isDebugBuild: Bool = Self.isDebugBuild) {
        allowLocalHTTP = isDebugBuild && storedAllowLocalHTTP
    }

    func validatedURL(_ value: String) throws -> URL {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        if !allowLocalHTTP,
           let components = URLComponents(string: trimmed),
           components.scheme?.lowercased() == "http" {
            throw SessionRestoreSecurityError.insecureHTTP
        }
        return try HomeserverPolicy(allowLocalHTTP: allowLocalHTTP).validatedURL(trimmed)
    }

    private static let isDebugBuild: Bool = {
        #if DEBUG
        true
        #else
        false
        #endif
    }()
}

enum SessionRestoreSecurityError: LocalizedError {
    case insecureHTTP

    var errorDescription: String? {
        "已拒绝并清除旧的 HTTP 本地会话；仅可恢复 HTTPS 会话，请重新登录。"
    }
}

public protocol SessionStore: Sendable {
    func load() throws -> SessionCredentials?
    func save(_ credentials: SessionCredentials) throws
    func delete() throws
}

public final class InMemorySessionStore: SessionStore, @unchecked Sendable {
    private let lock = NSLock()
    private var value: SessionCredentials?
    public init(_ initialValue: SessionCredentials? = nil) { value = initialValue }
    public func load() throws -> SessionCredentials? { lock.withLock { value } }
    public func save(_ credentials: SessionCredentials) throws { lock.withLock { value = credentials } }
    public func delete() throws { lock.withLock { value = nil } }
}

public enum SessionStoreError: Error, Equatable, Sendable, LocalizedError {
    case unavailable
    case malformedItem

    public var errorDescription: String? {
        switch self {
        case .unavailable:
            return "无法访问本机安全存储。请解锁设备后重试，或清除本地会话并重新登录。"
        case .malformedItem:
            return "本机保存的会话数据已损坏。请清除本地会话并重新登录。"
        }
    }
}

/// The small surface used by the session store. Keeping Security.framework on
/// this side of the boundary makes its update/add and error paths testable.
public enum KeychainBackendStatus: Sendable, Equatable { case success, itemNotFound, failure }

public enum KeychainAccessibility: Sendable, Equatable {
    case afterFirstUnlockThisDeviceOnly
}

public protocol KeychainBackend: Sendable {
    func load(service: String, account: String) -> (KeychainBackendStatus, Data?)
    func update(data: Data, service: String, account: String, accessibility: KeychainAccessibility) -> KeychainBackendStatus
    func add(data: Data, service: String, account: String, accessibility: KeychainAccessibility) -> KeychainBackendStatus
    func delete(service: String, account: String) -> KeychainBackendStatus
}

private struct UnavailableKeychainBackend: KeychainBackend {
    func load(service: String, account: String) -> (KeychainBackendStatus, Data?) { (.failure, nil) }
    func update(data: Data, service: String, account: String, accessibility: KeychainAccessibility) -> KeychainBackendStatus { .failure }
    func add(data: Data, service: String, account: String, accessibility: KeychainAccessibility) -> KeychainBackendStatus { .failure }
    func delete(service: String, account: String) -> KeychainBackendStatus { .failure }
}

#if canImport(Security)
private struct SecurityKeychainBackend: KeychainBackend {
    func load(service: String, account: String) -> (KeychainBackendStatus, Data?) {
        var query = baseQuery(service: service, account: account)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        return (map(status, operation: "SecItemCopyMatching"), item as? Data)
    }

    func update(data: Data, service: String, account: String, accessibility: KeychainAccessibility) -> KeychainBackendStatus {
        let attributes: [String: Any] = [
            kSecValueData as String: data,
            kSecAttrAccessible as String: securityAccessibility(accessibility)
        ]
        return map(SecItemUpdate(baseQuery(service: service, account: account) as CFDictionary, attributes as CFDictionary), operation: "SecItemUpdate")
    }

    func add(data: Data, service: String, account: String, accessibility: KeychainAccessibility) -> KeychainBackendStatus {
        var item = baseQuery(service: service, account: account)
        item[kSecValueData as String] = data
        item[kSecAttrAccessible as String] = securityAccessibility(accessibility)
        return map(SecItemAdd(item as CFDictionary, nil), operation: "SecItemAdd")
    }

    func delete(service: String, account: String) -> KeychainBackendStatus {
        map(SecItemDelete(baseQuery(service: service, account: account) as CFDictionary), operation: "SecItemDelete")
    }

    private func baseQuery(service: String, account: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service, kSecAttrAccount as String: account]
    }

    private func securityAccessibility(_ accessibility: KeychainAccessibility) -> CFString {
        switch accessibility { case .afterFirstUnlockThisDeviceOnly: return kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly }
    }

    // Log only the Security operation and numeric status. Never include the
    // query, item data, service/account names, credentials, or error payloads.
    private static let logger = Logger(subsystem: "org.botville.app.session", category: "Keychain")

    private func map(_ status: OSStatus, operation: String) -> KeychainBackendStatus {
        if status == errSecSuccess || status == errSecItemNotFound {
            Self.logger.info("\(operation, privacy: .public) OSStatus=\(status, privacy: .public)")
        } else {
            Self.logger.error("\(operation, privacy: .public) OSStatus=\(status, privacy: .public)")
        }
        switch status {
        case errSecSuccess: return .success
        case errSecItemNotFound: return .itemNotFound
        default: return .failure
        }
    }
}
#endif

public final class KeychainSessionStore: SessionStore, @unchecked Sendable {
    private let service: String
    private let account: String
    private let backend: any KeychainBackend

    public init(service: String = "org.botville.app.session", account: String = "current", backend: (any KeychainBackend)? = nil) {
        self.service = service
        self.account = account
        #if canImport(Security)
        self.backend = backend ?? SecurityKeychainBackend()
        #else
        self.backend = backend ?? UnavailableKeychainBackend()
        #endif
    }

    public func load() throws -> SessionCredentials? {
        let (status, data) = backend.load(service: service, account: account)
        if status == .itemNotFound { return nil }
        guard status == .success, let data else { throw SessionStoreError.unavailable }
        do { return try JSONDecoder().decode(SessionCredentials.self, from: data) }
        catch { throw SessionStoreError.malformedItem }
    }

    public func save(_ credentials: SessionCredentials) throws {
        let data = try JSONEncoder().encode(credentials)
        let accessibility = KeychainAccessibility.afterFirstUnlockThisDeviceOnly
        switch backend.update(data: data, service: service, account: account, accessibility: accessibility) {
        case .success: return
        case .itemNotFound:
            guard backend.add(data: data, service: service, account: account, accessibility: accessibility) == .success else {
                throw SessionStoreError.unavailable
            }
        case .failure: throw SessionStoreError.unavailable
        }
    }

    public func delete() throws {
        let status = backend.delete(service: service, account: account)
        guard status == .success || status == .itemNotFound else { throw SessionStoreError.unavailable }
    }
}

public struct SessionStoreDirectory: Sendable {
    public let baseURL: URL
    /// Resolves Application Support but deliberately does not create it. The
    /// service creates it while preparing login or restoring a session.
    public init(fileManager: FileManager = .default) throws {
        let support = try fileManager.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: false)
        baseURL = support.appendingPathComponent("botville", isDirectory: true).appendingPathComponent("matrix-sessions", isDirectory: true)
    }

    public init(baseURL: URL) { self.baseURL = baseURL }
    public func url(for storeID: UUID) -> URL { baseURL.appendingPathComponent(storeID.uuidString, isDirectory: true) }
    public func create(for storeID: UUID, fileManager: FileManager = .default) throws -> URL {
        let directory = url(for: storeID)
        let baseExisted = fileManager.fileExists(atPath: baseURL.path)
        let directoryExisted = fileManager.fileExists(atPath: directory.path)
        do {
            try fileManager.createDirectory(at: baseURL, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            try fileManager.setAttributes([.posixPermissions: 0o700], ofItemAtPath: baseURL.path)
            if !directoryExisted {
                try fileManager.createDirectory(at: directory, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
            }
            try fileManager.setAttributes([.posixPermissions: 0o700], ofItemAtPath: directory.path)
            var values = URLResourceValues()
            values.isExcludedFromBackup = true
            var protectedDirectory = directory
            try protectedDirectory.setResourceValues(values)
            #if os(iOS)
            try fileManager.setAttributes([.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication], ofItemAtPath: directory.path)
            #endif
            return directory
        } catch {
            if !directoryExisted { try? fileManager.removeItem(at: directory) }
            if !baseExisted, let contents = try? fileManager.contentsOfDirectory(atPath: baseURL.path), contents.isEmpty {
                try? fileManager.removeItem(at: baseURL)
            }
            throw error
        }
    }
    public func remove(for storeID: UUID, fileManager: FileManager = .default) throws {
        let directory = url(for: storeID)
        guard fileManager.fileExists(atPath: directory.path) else { return }
        try fileManager.removeItem(at: directory)
    }
    /// Removes all app-owned encrypted stores, including ones whose Keychain
    /// metadata can no longer be decoded. The base URL is app-specific.
    public func removeAll(fileManager: FileManager = .default) throws {
        guard fileManager.fileExists(atPath: baseURL.path) else { return }
        try fileManager.removeItem(at: baseURL)
    }
}
