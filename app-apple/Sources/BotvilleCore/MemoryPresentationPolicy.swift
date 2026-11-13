// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation

/// Shared by the native entry and AI profile; decisions never infer consent
/// from an absent field or from the contract's schema default.
public enum MemoryPresentationPolicy {
    public static let disclosureText = "该 AI 的主人可以查看和删除它与你的聊天记忆"
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

    public static func shouldShowOwnerDisclosure(in manifest: GatewayCapabilityManifest?) -> Bool {
        manifest?.capabilities.first(where: { $0.id == "memory.manage" })?.ownerCanReadDM == true
    }
}
