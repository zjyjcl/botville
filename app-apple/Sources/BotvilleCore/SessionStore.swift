// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation
import MatrixRustSDK
#if canImport(Security)
import Security
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

    public init(accessToken: String, refreshToken: String?, userID: String, deviceID: String, homeserverURL: String, oauthData: String?, storeID: UUID, storeKey: Data, allowLocalHTTP: Bool = false) {
        self.accessToken = accessToken
        self.refreshToken = refreshToken
        self.userID = userID
        self.deviceID = deviceID
        self.homeserverURL = homeserverURL
        self.oauthData = oauthData
        self.storeID = storeID
        self.storeKey = storeKey
        self.allowLocalHTTP = allowLocalHTTP
    }

    public init(session: Session, storeID: UUID, storeKey: Data, allowLocalHTTP: Bool = false) {
        self.init(accessToken: session.accessToken, refreshToken: session.refreshToken, userID: session.userId, deviceID: session.deviceId, homeserverURL: session.homeserverUrl, oauthData: session.oauthData, storeID: storeID, storeKey: storeKey, allowLocalHTTP: allowLocalHTTP)
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

public enum SessionStoreError: Error, Equatable, Sendable { case unavailable, malformedItem }

public final class KeychainSessionStore: SessionStore, @unchecked Sendable {
    private let service: String
    private let account: String

    public init(service: String = "org.botville.app.session", account: String = "current") {
        self.service = service
        self.account = account
    }

    public func load() throws -> SessionCredentials? {
        #if canImport(Security)
        var query = baseQuery
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = item as? Data else { throw SessionStoreError.unavailable }
        do { return try JSONDecoder().decode(SessionCredentials.self, from: data) }
        catch { throw SessionStoreError.malformedItem }
        #else
        throw SessionStoreError.unavailable
        #endif
    }

    public func save(_ credentials: SessionCredentials) throws {
        #if canImport(Security)
        let data = try JSONEncoder().encode(credentials)
        let attributes: [String: Any] = [kSecValueData as String: data, kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly]
        let status = SecItemUpdate(baseQuery as CFDictionary, attributes as CFDictionary)
        if status == errSecSuccess { return }
        guard status == errSecItemNotFound else { throw SessionStoreError.unavailable }
        var item = baseQuery
        attributes.forEach { item[$0.key] = $0.value }
        guard SecItemAdd(item as CFDictionary, nil) == errSecSuccess else { throw SessionStoreError.unavailable }
        #else
        throw SessionStoreError.unavailable
        #endif
    }

    public func delete() throws {
        #if canImport(Security)
        let status = SecItemDelete(baseQuery as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else { throw SessionStoreError.unavailable }
        #else
        throw SessionStoreError.unavailable
        #endif
    }

    #if canImport(Security)
    private var baseQuery: [String: Any] { [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service, kSecAttrAccount as String: account] }
    #endif
}

public struct SessionStoreDirectory: Sendable {
    public let baseURL: URL
    /// Resolves Application Support but deliberately does not create it. The
    /// service creates its session directory only after a successful login.
    public init(fileManager: FileManager = .default) throws {
        let support = try fileManager.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: false)
        baseURL = support.appendingPathComponent("botville", isDirectory: true).appendingPathComponent("matrix-sessions", isDirectory: true)
    }

    public init(baseURL: URL) { self.baseURL = baseURL }
    public func url(for storeID: UUID) -> URL { baseURL.appendingPathComponent(storeID.uuidString, isDirectory: true) }
    public func create(for storeID: UUID, fileManager: FileManager = .default) throws -> URL {
        let directory = url(for: storeID)
        try fileManager.createDirectory(at: baseURL, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        try fileManager.setAttributes([.posixPermissions: 0o700], ofItemAtPath: baseURL.path)
        if !fileManager.fileExists(atPath: directory.path) {
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
    }
    public func remove(for storeID: UUID, fileManager: FileManager = .default) throws {
        let directory = url(for: storeID)
        guard fileManager.fileExists(atPath: directory.path) else { return }
        try fileManager.removeItem(at: directory)
    }
}
