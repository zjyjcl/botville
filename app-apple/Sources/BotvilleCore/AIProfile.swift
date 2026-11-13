// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation
import MatrixRustSDK

/// Read-only Matrix profile data for the sole AI counterpart in a direct room.
public struct AIProfile: Equatable, Sendable {
    public let userID: String
    public let displayName: String
    public let avatarData: Data?
    /// The current Matrix SDK profile has no bio or signature field.
    public let signature: String?
    /// The signed-in account's Matrix server name, used for disclosure policy.
    public let localServerName: String?

    public init(userID: String, displayName: String, avatarData: Data?, signature: String?, localServerName: String?) {
        self.userID = userID
        self.displayName = displayName
        self.avatarData = avatarData
        self.signature = signature
        self.localServerName = localServerName
    }
}

/// A complete room-member snapshot used to decide whether a direct room has
/// exactly one AI counterpart. Keeping this independent of the SDK makes the
/// identity boundary testable.
public struct AIProfileMember: Equatable, Sendable {
    public let userID: String
    public let isJoined: Bool
    public let isActive: Bool

    public init(userID: String, isJoined: Bool, isActive: Bool = true) {
        self.userID = userID
        self.isJoined = isJoined
        self.isActive = isActive
    }
}

public enum AIProfileEligibility {
    /// Returns the counterpart only for a direct room whose complete
    /// active-member list contains the signed-in joined user and one active AI
    /// account. Any extra, duplicate, or incomplete snapshot fails closed.
    public static func aiCounterpartID(
        isDirect: Bool,
        localUserID: String?,
        members: [AIProfileMember],
        snapshotComplete: Bool = true,
        policy: AIParticipantPolicy
    ) -> String? {
        guard isDirect, snapshotComplete, let localUserID, members.count == 2,
              members.allSatisfy(\.isActive) else { return nil }
        let memberIDs = members.map(\.userID)
        guard Set(memberIDs).count == 2,
              members.contains(where: { $0.userID == localUserID && $0.isJoined }) else { return nil }
        let counterpart = memberIDs.first(where: { $0 != localUserID })
        guard let counterpart, policy.classify(memberIDs: [counterpart]) == .containsAI else { return nil }
        return counterpart
    }
}

enum AIProfileLoader {
    static func load(client: Client, roomID: String, localUserID: String,
                     policy: AIParticipantPolicy) async throws -> AIProfile? {
        try Task.checkCancellation()
        guard let room = try client.getRoom(roomId: roomID), room.membership() == .joined,
              await room.isDirect() else { return nil }
        let iterator = try await room.members()
        let expectedMemberCount = iterator.len()
        var roomMembers = [RoomMember]()
        while let chunk = iterator.nextChunk(chunkSize: 100) {
            guard !chunk.isEmpty else { return nil }
            roomMembers.append(contentsOf: chunk)
        }
        guard UInt32(roomMembers.count) == expectedMemberCount else { return nil }
        let activeMembers = roomMembers.filter { $0.membership == .join || $0.membership == .invite }
        let members = activeMembers.map {
            AIProfileMember(userID: $0.userId, isJoined: $0.membership == .join)
        }
        guard let counterpartID = AIProfileEligibility.aiCounterpartID(
            isDirect: true, localUserID: localUserID, members: members, snapshotComplete: true, policy: policy
        ), let counterpart = activeMembers.first(where: { $0.userId == counterpartID }) else { return nil }

        try Task.checkCancellation()
        let profile = try? await client.getProfile(userId: counterpartID)
        try Task.checkCancellation()
        let name = nonEmpty(profile?.displayName) ?? nonEmpty(counterpart.displayName) ?? fallbackDisplayName(for: counterpartID)
        let avatarURL = nonEmpty(profile?.avatarUrl) ?? nonEmpty(counterpart.avatarUrl)
        let avatarData: Data?
        if let avatarURL, let source = try? MediaSource.fromUrl(url: avatarURL) {
            avatarData = try? await client.getMediaContent(mediaSource: source)
            try Task.checkCancellation()
        } else {
            avatarData = nil
        }
        return AIProfile(userID: counterpartID, displayName: name, avatarData: avatarData,
                         signature: nil, localServerName: MemoryPresentationPolicy.matrixServerName(in: localUserID))
    }

    private static func nonEmpty(_ value: String?) -> String? {
        guard let value else { return nil }
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    private static func fallbackDisplayName(for userID: String) -> String {
        guard userID.hasPrefix("@"), let colon = userID.firstIndex(of: ":") else { return "AI" }
        let localpart = String(userID[userID.index(after: userID.startIndex)..<colon])
        return localpart.isEmpty ? "AI" : localpart
    }
}
