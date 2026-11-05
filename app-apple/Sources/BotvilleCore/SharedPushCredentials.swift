// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation
#if canImport(Security)
import Security
#endif

/// The smallest Matrix session representation needed by the notification
/// service in a later PR. It deliberately excludes local store keys, OAuth
/// state, gateway configuration, and all other app-only session metadata.
public struct SharedPushCredentials: Codable, Equatable, Sendable {
    public let homeserverURL: String
    public let userID: String
    public let accessToken: String
    public let deviceID: String
    public let refreshToken: String?

    public init(homeserverURL: String, userID: String, accessToken: String,
                deviceID: String, refreshToken: String?) {
        self.homeserverURL = homeserverURL
        self.userID = userID
        self.accessToken = accessToken
        self.deviceID = deviceID
        self.refreshToken = refreshToken
    }
}

public protocol SharedPushCredentialsStore: Sendable {
    func load() throws -> SharedPushCredentials?
    func save(_ credentials: SharedPushCredentials) throws
    func delete() throws
}

public final class InMemorySharedPushCredentialsStore: SharedPushCredentialsStore, @unchecked Sendable {
    private let lock = NSLock()
    private var value: SharedPushCredentials?

    public init(_ initialValue: SharedPushCredentials? = nil) { value = initialValue }
    public func load() throws -> SharedPushCredentials? { lock.withLock { value } }
    public func save(_ credentials: SharedPushCredentials) throws { lock.withLock { value = credentials } }
    public func delete() throws { lock.withLock { value = nil } }
}

public enum SharedPushCredentialsStoreError: Error, Equatable, Sendable {
    case unavailable
    case malformedItem
}

/// Keychain storage shared by the app and its notification service extension.
/// The access-group string is supplied through each target's Info.plist, so
/// this source stays linkable by the lightweight extension target.
public final class KeychainSharedPushCredentialsStore: SharedPushCredentialsStore, @unchecked Sendable {
    public static let accessGroupInfoKey = "BotvilleSharedKeychainAccessGroup"
    private static let service = "com.zjyjcl.botville.push-credentials"
    private static let account = "matrix-session"
    private let accessGroup: String?

    public init(accessGroup: String? = nil) {
        let configuredGroup = accessGroup ?? Bundle.main.object(forInfoDictionaryKey: Self.accessGroupInfoKey) as? String
        self.accessGroup = configuredGroup?.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    public func load() throws -> SharedPushCredentials? {
        #if canImport(Security)
        guard hasAccessGroup else { throw SharedPushCredentialsStoreError.unavailable }
        var query = baseQuery()
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = item as? Data else {
            throw SharedPushCredentialsStoreError.unavailable
        }
        do { return try JSONDecoder().decode(SharedPushCredentials.self, from: data) }
        catch { throw SharedPushCredentialsStoreError.malformedItem }
        #else
        throw SharedPushCredentialsStoreError.unavailable
        #endif
    }

    public func save(_ credentials: SharedPushCredentials) throws {
        #if canImport(Security)
        guard hasAccessGroup else { throw SharedPushCredentialsStoreError.unavailable }
        let data = try JSONEncoder().encode(credentials)
        let attributes: [String: Any] = [
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        ]
        let updateStatus = SecItemUpdate(baseQuery() as CFDictionary, attributes as CFDictionary)
        if updateStatus == errSecSuccess { return }
        guard updateStatus == errSecItemNotFound else { throw SharedPushCredentialsStoreError.unavailable }
        var item = baseQuery()
        item[kSecValueData as String] = data
        item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        guard SecItemAdd(item as CFDictionary, nil) == errSecSuccess else {
            throw SharedPushCredentialsStoreError.unavailable
        }
        #else
        throw SharedPushCredentialsStoreError.unavailable
        #endif
    }

    public func delete() throws {
        #if canImport(Security)
        guard hasAccessGroup else { throw SharedPushCredentialsStoreError.unavailable }
        let status = SecItemDelete(baseQuery() as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw SharedPushCredentialsStoreError.unavailable
        }
        #else
        throw SharedPushCredentialsStoreError.unavailable
        #endif
    }

    #if canImport(Security)
    private var hasAccessGroup: Bool { !(accessGroup?.isEmpty ?? true) }

    private func baseQuery() -> [String: Any] {
        var query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: Self.service,
            kSecAttrAccount as String: Self.account
        ]
        // Access-group isolation is required; each target passes the same
        // explicit group through its Info.plist.
        query[kSecAttrAccessGroup as String] = accessGroup ?? ""
        return query
    }
    #endif
}
