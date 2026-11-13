// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation

public struct GroupInviteCandidate: Identifiable, Equatable, Sendable {
    public let id: String
    public let displayName: String
    public let isAI: Bool

    public init(id: String, displayName: String, isAI: Bool) {
        self.id = id
        self.displayName = displayName
        self.isAI = isAI
    }
}

/// Selection contains only identities already known to Matrix or the owner API.
/// Full server-name comparison shares the counterpart-profile disclosure rule.
public enum GroupInviteSelection {
    public static func candidates(
        contacts: [GroupInviteCandidate], personas: [MemoryPersona], localUserID: String?
    ) -> [GroupInviteCandidate] {
        let localServer = localUserID.flatMap(MemoryPresentationPolicy.matrixServerName(in:))
        let ownAI = personas.map {
            GroupInviteCandidate(id: $0.id, displayName: $0.displayName, isAI: true)
        }
        let knownAI = Set(ownAI.map(\.id))
        // Prefer the owner API's AI classification and name over a DM snapshot.
        var seen = Set<String>()
        return (ownAI + contacts.filter { !knownAI.contains($0.id) }).filter { candidate in
            guard candidate.id != localUserID else { return false }
            if candidate.isAI {
                let profile = AIProfile(
                    userID: candidate.id, displayName: candidate.displayName,
                    avatarData: nil, signature: nil, localServerName: localServer)
                guard profile.canFetchGatewayDetails else { return false }
            }
            return seen.insert(candidate.id).inserted
        }.sorted {
            if $0.isAI != $1.isAI { return !$0.isAI }
            if $0.displayName != $1.displayName { return $0.displayName < $1.displayName }
            return $0.id < $1.id
        }
    }

    public static func invitees(selectedIDs: Set<String>, manualText: String) -> [String] {
        var seen = Set<String>()
        let manual = manualText.split(whereSeparator: { $0 == "," || $0.isWhitespace }).map(
            String.init)
        return (selectedIDs.sorted() + manual).filter { seen.insert($0).inserted }
    }

    public static func containsAI(
        invitees: [String], candidates: [GroupInviteCandidate],
        policy: AIParticipantPolicy
    ) -> Bool {
        let knownAI = Set(candidates.filter(\.isAI).map(\.id))
        return invitees.contains(where: knownAI.contains)
            || policy.classify(memberIDs: invitees) == .containsAI
    }
}
