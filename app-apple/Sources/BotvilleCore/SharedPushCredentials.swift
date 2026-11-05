// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation
#if canImport(Security)
import Security
#endif

/// The smallest Matrix session representation needed by the notification
/// service in a later PR. It deliberately excludes local store keys, OAuth
/// state, gateway configuration, and all other app-only session metadata.
public struct SharedPushCredentials: Codable, Equatable, Sendable, CustomStringConvertible, CustomDebugStringConvertible {
    public let homeserverURL: String
    public let userID: String
    public let accessToken: String
    public let deviceID: String

    public init(homeserverURL: String, userID: String, accessToken: String,
                deviceID: String) {
        self.homeserverURL = homeserverURL
        self.userID = userID
        self.accessToken = accessToken
        self.deviceID = deviceID
    }

    /// Do not expose the access token through interpolation, logs, or debugger
    /// summaries. The extension needs only this deliberately small credential set.
    public var description: String { "SharedPushCredentials(<redacted>)" }
    public var debugDescription: String { description }
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

/// A Keychain boundary local to the shared-credential source file. This file is
/// compiled directly by the notification extension, so it must not depend on
/// BotvilleCore-only Keychain types.
public enum SharedPushCredentialsKeychainStatus: Sendable, Equatable { case success, itemNotFound, failure }

public protocol SharedPushCredentialsKeychainBackend: Sendable {
    func load(service: String, account: String, accessGroup: String) -> (SharedPushCredentialsKeychainStatus, Data?)
    func update(data: Data, service: String, account: String, accessGroup: String) -> SharedPushCredentialsKeychainStatus
    func add(data: Data, service: String, account: String, accessGroup: String) -> SharedPushCredentialsKeychainStatus
    func delete(service: String, account: String, accessGroup: String) -> SharedPushCredentialsKeychainStatus
}

private struct UnavailableSharedPushCredentialsKeychainBackend: SharedPushCredentialsKeychainBackend {
    func load(service: String, account: String, accessGroup: String) -> (SharedPushCredentialsKeychainStatus, Data?) { (.failure, nil) }
    func update(data: Data, service: String, account: String, accessGroup: String) -> SharedPushCredentialsKeychainStatus { .failure }
    func add(data: Data, service: String, account: String, accessGroup: String) -> SharedPushCredentialsKeychainStatus { .failure }
    func delete(service: String, account: String, accessGroup: String) -> SharedPushCredentialsKeychainStatus { .failure }
}

#if canImport(Security)
private struct SecuritySharedPushCredentialsKeychainBackend: SharedPushCredentialsKeychainBackend {
    func load(service: String, account: String, accessGroup: String) -> (SharedPushCredentialsKeychainStatus, Data?) {
        var query = baseQuery(service: service, account: account, accessGroup: accessGroup)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        return (map(SecItemCopyMatching(query as CFDictionary, &item)), item as? Data)
    }

    func update(data: Data, service: String, account: String, accessGroup: String) -> SharedPushCredentialsKeychainStatus {
        let attributes: [String: Any] = [kSecValueData as String: data, kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly]
        return map(SecItemUpdate(baseQuery(service: service, account: account, accessGroup: accessGroup) as CFDictionary, attributes as CFDictionary))
    }

    func add(data: Data, service: String, account: String, accessGroup: String) -> SharedPushCredentialsKeychainStatus {
        var item = baseQuery(service: service, account: account, accessGroup: accessGroup)
        item[kSecValueData as String] = data
        item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        return map(SecItemAdd(item as CFDictionary, nil))
    }

    func delete(service: String, account: String, accessGroup: String) -> SharedPushCredentialsKeychainStatus {
        map(SecItemDelete(baseQuery(service: service, account: account, accessGroup: accessGroup) as CFDictionary))
    }

    private func baseQuery(service: String, account: String, accessGroup: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service, kSecAttrAccount as String: account, kSecAttrAccessGroup as String: accessGroup]
    }

    private func map(_ status: OSStatus) -> SharedPushCredentialsKeychainStatus {
        switch status {
        case errSecSuccess: return .success
        case errSecItemNotFound: return .itemNotFound
        default: return .failure
        }
    }
}
#endif

/// Keychain storage shared by the app and its notification service extension.
/// The access-group string is supplied through each target's Info.plist, so
/// this source stays linkable by the lightweight extension target.
public final class KeychainSharedPushCredentialsStore: SharedPushCredentialsStore, @unchecked Sendable {
    public static let accessGroupInfoKey = "BotvilleSharedKeychainAccessGroup"
    private static let service = "com.zjyjcl.botville.push-credentials"
    private static let account = "matrix-session"
    private let accessGroup: String?
    private let backend: any SharedPushCredentialsKeychainBackend

    public init(accessGroup: String? = nil, backend: (any SharedPushCredentialsKeychainBackend)? = nil) {
        let configuredGroup = accessGroup ?? Bundle.main.object(forInfoDictionaryKey: Self.accessGroupInfoKey) as? String
        self.accessGroup = configuredGroup?.trimmingCharacters(in: .whitespacesAndNewlines)
        #if canImport(Security)
        self.backend = backend ?? SecuritySharedPushCredentialsKeychainBackend()
        #else
        self.backend = backend ?? UnavailableSharedPushCredentialsKeychainBackend()
        #endif
    }

    public func load() throws -> SharedPushCredentials? {
        guard let accessGroup, !accessGroup.isEmpty else { throw SharedPushCredentialsStoreError.unavailable }
        let (status, data) = backend.load(service: Self.service, account: Self.account, accessGroup: accessGroup)
        if status == .itemNotFound { return nil }
        guard status == .success, let data else { throw SharedPushCredentialsStoreError.unavailable }
        do { return try JSONDecoder().decode(SharedPushCredentials.self, from: data) }
        catch { throw SharedPushCredentialsStoreError.malformedItem }
    }

    public func save(_ credentials: SharedPushCredentials) throws {
        guard let accessGroup, !accessGroup.isEmpty else { throw SharedPushCredentialsStoreError.unavailable }
        let data = try JSONEncoder().encode(credentials)
        switch backend.update(data: data, service: Self.service, account: Self.account, accessGroup: accessGroup) {
        case .success: return
        case .itemNotFound:
            guard backend.add(data: data, service: Self.service, account: Self.account, accessGroup: accessGroup) == .success else {
                throw SharedPushCredentialsStoreError.unavailable
            }
        case .failure:
            throw SharedPushCredentialsStoreError.unavailable
        }
    }

    public func delete() throws {
        guard let accessGroup, !accessGroup.isEmpty else { throw SharedPushCredentialsStoreError.unavailable }
        let status = backend.delete(service: Self.service, account: Self.account, accessGroup: accessGroup)
        guard status == .success || status == .itemNotFound else {
            throw SharedPushCredentialsStoreError.unavailable
        }
    }
}
