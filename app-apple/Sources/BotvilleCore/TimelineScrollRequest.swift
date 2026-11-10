// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation

/// A completed appearance is a scroll trigger even if its last message is unchanged.
public struct TimelineScrollRequest: Equatable, Sendable {
    public let appearanceID: UUID
    public let latestMessageID: String

    public static func make(
        appearanceID: UUID, completedAppearanceID: UUID?,
        isVisible: Bool, isOpening: Bool, roomID: String, activeRoomID: String?,
        latestMessageID: String?
    ) -> Self? {
        guard isVisible, !isOpening, completedAppearanceID == appearanceID,
              activeRoomID == roomID, let latestMessageID else { return nil }
        return Self(appearanceID: appearanceID, latestMessageID: latestMessageID)
    }
}
