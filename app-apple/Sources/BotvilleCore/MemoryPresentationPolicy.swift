// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation

/// Shared by the native entry and AI profile; decisions never infer consent
/// from an absent field or from the contract's schema default.
public enum MemoryPresentationPolicy {
    public static let disclosureText = "该 AI 的主人可以查看和删除它与你的聊天记忆"
    public static let ownerExplanationText = "私聊内容是否可见由服务端开关决定"
    public static let nativeRoutes = [
        "chat.direct": "botville://chat", "memory.manage": "botville://memories",
    ]

    public static func isMemoryManagementAvailable(in manifest: GatewayCapabilityManifest?) -> Bool
    {
        manifest?.capabilityProjections(knownNativeRoutes: nativeRoutes)
            .first(where: { $0.capability.id == "memory.manage" })?.isAvailable == true
            && manifest?.capabilities.first(where: { $0.id == "memory.manage" })?.entry.type
                == "native"
    }

    /// Only a capability from the paired local gateway can describe a local AI.
    /// Compare complete Matrix server names (including any port), not URL hosts
    /// or suffixes. The caller obtains the local name from the signed-in MXID.
    /// Aliases/multiple domains intentionally count as cross-server and hide
    /// disclosure: without a trusted alias mapping we deliberately err on caution.
    public static func shouldShowOwnerDisclosure(
        in manifest: GatewayCapabilityManifest?, aiUserID: String, localServerName: String?
    ) -> Bool {
        guard let localServerName, !localServerName.isEmpty,
            matrixServerName(in: aiUserID) == localServerName else { return false }
        return manifest?.capabilities.first(where: { $0.id == "memory.manage" })?.ownerCanReadDM == true
    }

    public static func matrixServerName(in userID: String) -> String? {
        guard userID.hasPrefix("@"), let colon = userID.firstIndex(of: ":"),
            userID.distance(from: userID.startIndex, to: colon) > 1 else { return nil }
        let server = String(userID[userID.index(after: colon)...])
        guard !server.isEmpty, !server.contains(where: { $0.isWhitespace }),
            !server.contains("/"), !server.contains("@") else { return nil }
        return server
    }

    public struct Actions: Equatable, Sendable {
        public let canDelete: Bool
        public let canPin: Bool
        public let canUnpin: Bool
        public var canTogglePin: Bool { canPin || canUnpin }
    }

    /// Unknown layers are read-only. Redaction never grants permission to pin
    /// unseen content, but existing pins can be removed on pinnable layers.
    public static func actions(layer: String, redacted: Bool, pinned: Bool) -> Actions {
        let known = ["recent", "summary", "profile"].contains(layer)
        let pinnable = known && layer != "summary"
        return Actions(canDelete: known, canPin: pinnable && !redacted && !pinned,
                       canUnpin: pinnable && pinned)
    }
}
