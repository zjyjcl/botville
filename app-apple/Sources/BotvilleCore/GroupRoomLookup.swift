// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation

/// POST /personas/{id}/rooms/lookup reply (contract 0.7.0): index-aligned with
/// the request, `null` = this persona has no such group. Never echoes room ids.
public struct GroupRoomLookupResult: Codable, Sendable, Equatable {
    public let items: [GroupRoom?]
    public let groupsEnabled: Bool
    public init(items: [GroupRoom?], groupsEnabled: Bool) {
        self.items = items
        self.groupsEnabled = groupsEnabled
    }
    enum CodingKeys: String, CodingKey {
        case items
        case groupsEnabled = "groups_enabled"
    }
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        // Decode element-wise so an explicit JSON null stays a nil slot (index alignment).
        items = try c.decode([GroupRoom?].self, forKey: .items)
        groupsEnabled = try c.decode(Bool.self, forKey: .groupsEnabled)
    }
}

/// Which locally known room ids may be sent, and in which batches. The gateway
/// rejects a whole batch with 400 for any alias, cross-server id, malformed id or
/// duplicate, so the App filters exactly the same way before sending.
public enum GroupRoomLookupPlan {
    public static let maxBatchSize = 50
    /// Contract: ≤255 Unicode code points (not grapheme clusters).
    private static let maxLength = 255
    private static let localpartCharacters = CharacterSet(
        charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._~=-")

    /// Group (non-direct) rooms on the signed-in server, first occurrence wins,
    /// original strings kept (the gateway hashes the raw id).
    public static func roomIDs(from rooms: [MatrixRoom], localServerName: String?) -> [String] {
        guard let local = localServerName, !local.isEmpty else { return [] }
        var seen = Set<String>()
        var result: [String] = []
        for room in rooms where !room.isDirect {
            guard let parts = parse(room.id),
                parts.server.caseInsensitiveCompare(local) == .orderedSame,
                seen.insert(parts.key).inserted
            else { continue }
            result.append(room.id)
        }
        return result
    }

    public static func batches(_ ids: [String]) -> [[String]] {
        stride(from: 0, to: ids.count, by: maxBatchSize).map {
            Array(ids[$0..<min($0 + maxBatchSize, ids.count)])
        }
    }

    /// Duplicate identity used by the gateway: localpart exact, server name case-folded.
    /// nil for anything that is not strictly `!<localpart>:<server_name>`.
    public static func duplicateKey(_ id: String) -> String? { parse(id)?.key }

    private static func parse(_ id: String) -> (server: String, key: String)? {
        guard id.unicodeScalars.count <= maxLength, id.hasPrefix("!"),
            id.unicodeScalars.allSatisfy({
                !CharacterSet.whitespacesAndNewlines.contains($0)
                    && !CharacterSet.controlCharacters.contains($0)
            }),
            let colon = id.firstIndex(of: ":")
        else { return nil }
        let localpart = id[id.index(after: id.startIndex)..<colon]
        let server = String(id[id.index(after: colon)...])
        guard !localpart.isEmpty, !server.isEmpty,
            localpart.unicodeScalars.allSatisfy(localpartCharacters.contains)
        else { return nil }
        return (server, "\(localpart):\(server.lowercased())")
    }
}

/// Local Matrix rooms the App already knows. Lookup never discovers rooms.
public struct GroupRoomLookupSource {
    public let localServerName: String?
    public let knownRooms: @MainActor () -> [MatrixRoom]
    public init(localServerName: String?, knownRooms: @escaping @MainActor () -> [MatrixRoom]) {
        self.localServerName = localServerName
        self.knownRooms = knownRooms
    }
}

/// Presentation of the deployment-level `groups.manage` capability (0.7.0).
///
/// - enabled: full entry.
/// - degraded / `groups_disabled`: dimmed (WeChat-style greyed row with an
///   explanation) but still openable, because the contract keeps 安静/退出
///   available; mention_only and the persona settings entry are withheld.
/// - disabled or any unknown status: hidden.
/// - capability missing (gateway < 0.7.0, or manifest not loaded): the
///   pre-0.7.0 entry stays as it was, without lookup.
public struct GroupsManageEntryState: Sendable, Equatable {
    public enum Visibility: Sendable, Equatable { case visible, dimmed, hidden }

    public static let capabilityID = "groups.manage"
    public static let dimmedNotice = "群聊功能已关闭，只能设为安静或退出群聊"

    public let visibility: Visibility
    public let notice: String?
    public let allowsMentionOnly: Bool
    public let canOpenSettings: Bool
    public let supportsRoomLookup: Bool

    public static let legacy = GroupsManageEntryState(
        visibility: .visible, notice: nil, allowsMentionOnly: true, canOpenSettings: true,
        supportsRoomLookup: false)
    static let hidden = GroupsManageEntryState(
        visibility: .hidden, notice: nil, allowsMentionOnly: false, canOpenSettings: false,
        supportsRoomLookup: false)

    init(
        visibility: Visibility, notice: String?, allowsMentionOnly: Bool, canOpenSettings: Bool,
        supportsRoomLookup: Bool
    ) {
        self.visibility = visibility
        self.notice = notice
        self.allowsMentionOnly = allowsMentionOnly
        self.canOpenSettings = canOpenSettings
        self.supportsRoomLookup = supportsRoomLookup
    }

    public init(manifest: GatewayCapabilityManifest?) {
        guard let capability = manifest?.capabilities.first(where: { $0.id == Self.capabilityID })
        else {
            self = .legacy
            return
        }
        let lookup = capability.roomLookup == true
        switch capability.status {
        case "disabled":
            self = .hidden
        case "enabled" where capability.unavailableReason != "groups_disabled":
            self.init(
                visibility: .visible, notice: nil, allowsMentionOnly: true, canOpenSettings: true,
                supportsRoomLookup: lookup)
        case "enabled", "degraded":
            self.init(
                visibility: .dimmed, notice: Self.dimmedNotice, allowsMentionOnly: false,
                canOpenSettings: false, supportsRoomLookup: lookup)
        default:
            self = .hidden
        }
    }

    public var isVisible: Bool { visibility != .hidden }
}
