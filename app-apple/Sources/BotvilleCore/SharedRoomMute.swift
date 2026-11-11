// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import CryptoKit
import Foundation
#if canImport(UserNotifications)
import UserNotifications
#endif
#if DEBUG
import OSLog
#endif

/// Compiled into both the app and NSE. Only full SHA-256 room digests are stored.
/// This is a best-effort offline defense, not a substitute for server push rules.
public protocol SharedRoomMuteStoring: Sendable {
    func load() throws -> Set<String>
    func save(_ hashes: Set<String>) throws
    func clear() throws
}

public enum SharedRoomMuteStoreError: Error {
    case containerUnavailable
}

public struct SharedRoomMuteStore: SharedRoomMuteStoring {
    public static let appGroupIdentifier = "group.com.zjyjcl.botville"
    private let fileURL: URL?

    public init() {
        fileURL = FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: Self.appGroupIdentifier)?
            .appendingPathComponent("Library/Application Support/room-mute-sha256.json")
    }

    public init(fileURL: URL?) { self.fileURL = fileURL }

    public static func hash(_ roomID: String) -> String {
        SHA256.hash(data: Data(roomID.utf8)).map { String(format: "%02x", $0) }.joined()
    }

    public func load() throws -> Set<String> {
        guard let fileURL else { throw SharedRoomMuteStoreError.containerUnavailable }
        guard FileManager.default.fileExists(atPath: fileURL.path) else { return [] }
        let values = try JSONDecoder().decode([String].self, from: Data(contentsOf: fileURL))
        guard values.allSatisfy(Self.validHash) else { throw CocoaError(.fileReadCorruptFile) }
        return Set(values)
    }

    public func save(_ hashes: Set<String>) throws {
        guard let fileURL else { throw SharedRoomMuteStoreError.containerUnavailable }
        guard hashes.allSatisfy(Self.validHash) else { throw CocoaError(.fileWriteInapplicableStringEncoding) }
        try FileManager.default.createDirectory(at: fileURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try JSONEncoder().encode(hashes.sorted()).write(to: fileURL, options: .atomic)
    }

    public func clear() throws { try save([]) }

    private static func validHash(_ value: String) -> Bool {
        value.utf8.count == 64 && value.utf8.allSatisfy { (48...57).contains($0) || (97...102).contains($0) }
    }
}

public enum RoomMutePushPolicy {
    /// A room-only push must still be filtered, even without a valid event ID.
    public static func roomID(in userInfo: [AnyHashable: Any]) -> String? {
        var candidates = [userInfo]
        for key in ["notification", "data"] {
            if let nested = userInfo[key] as? [AnyHashable: Any] {
                candidates.append(nested)
                for inner in ["notification", "data"] {
                    if let value = nested[inner] as? [AnyHashable: Any] { candidates.append(value) }
                }
            }
        }
        return candidates.compactMap { $0["room_id"] as? String }.first {
            $0.first == "!" && $0.count > 1 && $0.utf8.count <= 2048
                && $0.rangeOfCharacter(from: .whitespacesAndNewlines.union(.controlCharacters)) == nil
        }
    }

    public static func isMuted(userInfo: [AnyHashable: Any], hashes: Set<String>) -> Bool {
        roomID(in: userInfo).map { hashes.contains(SharedRoomMuteStore.hash($0)) } ?? false
    }

    /// Server totals may include muted rooms. While any mute is known, keep the
    /// existing icon badge; only the app's synced, filtered total can replace it.
    public static func acceptsServerBadge(hashes: Set<String>) -> Bool { hashes.isEmpty }

#if canImport(UserNotifications)
    @discardableResult
    public static func apply(to content: UNMutableNotificationContent, hashes: Set<String>) -> Bool {
        if !acceptsServerBadge(hashes: hashes) { content.badge = nil }
        guard isMuted(userInfo: content.userInfo, hashes: hashes) else { return false }
        content.title = ""
        content.subtitle = ""
        content.body = ""
        content.sound = nil
        content.attachments = []
        content.categoryIdentifier = ""
        content.threadIdentifier = ""
        content.interruptionLevel = .passive
        content.relevanceScore = 0
        // Without the filtering entitlement iOS does not guarantee suppression.
#if DEBUG
        let scope = String(SharedRoomMuteStore.hash(roomID(in: content.userInfo) ?? "").prefix(8))
        Logger(subsystem: "com.zjyjcl.botville", category: "RoomMute")
            .debug("room_mute_nse degraded scope=\(scope, privacy: .public)")
#endif
        return true
    }
#endif
}
