// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import XCTest

@testable import BotvilleCore

final class GroupInviteSelectionTests: XCTestCase {
    private let policy = AIParticipantPolicy.configured(environment: [:], infoDictionary: [:])

    func testFiltersCrossServerAIUsingFullServerNameIncludingPort() throws {
        let contacts: [GroupInviteCandidate] = [
            .init(id: "@friend:remote.test", displayName: "朋友", isAI: false),
            .init(id: "@ai_dm:local.test:8448", displayName: "本服 AI", isAI: true),
            .init(id: "@ai_remote:local.test", displayName: "其他端口", isAI: true),
            .init(id: "@ai_remote:sub.local.test:8448", displayName: "其他域", isAI: true),
            .init(id: "@me:local.test:8448", displayName: "自己", isAI: false),
        ]
        let candidates = GroupInviteSelection.candidates(
            contacts: contacts,
            personas: [
                try persona("@ai_one:LOCAL.test:8448"), try persona("@ai_two:local.test:8448"),
                try persona("@ai_foreign:remote.test"),
            ], localUserID: "@me:local.test:8448")
        XCTAssertEqual(
            Set(candidates.map(\.id)),
            [
                "@friend:remote.test", "@ai_dm:local.test:8448",
                "@ai_one:LOCAL.test:8448", "@ai_two:local.test:8448",
            ])
    }

    func testMissingLocalServerHidesAIAndOwnerClassificationWinsDuplicates() throws {
        let contacts = [
            GroupInviteCandidate(id: "@custom:local.test", displayName: "DM", isAI: false)
        ]
        let personas = [try persona("@custom:local.test")]
        let known = GroupInviteSelection.candidates(
            contacts: contacts, personas: personas, localUserID: "@me:local.test")
        XCTAssertEqual(known.count, 1)
        XCTAssertTrue(known[0].isAI)
        // Even if the DM policy missed an owner-listed AI, it cannot leak through
        // the contact branch when its full server name is unavailable or remote.
        XCTAssertTrue(
            GroupInviteSelection.candidates(
                contacts: contacts, personas: personas, localUserID: nil
            )
            .isEmpty)
    }

    func testMultiAISelectionMergesManualInputAndDeduplicates() throws {
        let invitees = GroupInviteSelection.invitees(
            selectedIDs: ["@ai_one:local.test", "@ai_two:local.test"],
            manualText: "@ai_two:local.test, @human:remote.test\n@human:remote.test")
        XCTAssertEqual(
            invitees, ["@ai_one:local.test", "@ai_two:local.test", "@human:remote.test"])
        XCTAssertNoThrow(try RoomCreationDraft(isDirect: false, invitees: invitees).validate())
        XCTAssertTrue(
            GroupInviteSelection.containsAI(invitees: invitees, candidates: [], policy: policy))
    }

    func testRequiresAtLeastOneInviteeAndKeepsDirectValidation() {
        let empty = GroupInviteSelection.invitees(selectedIDs: [], manualText: " , \n")
        XCTAssertThrowsError(try RoomCreationDraft(isDirect: false, invitees: empty).validate()) {
            XCTAssertEqual($0 as? RoomCreationValidationError, .missingInvitee)
        }
        XCTAssertThrowsError(
            try RoomCreationDraft(isDirect: true, invitees: ["@a:test", "@b:test"]).validate())
    }

    func testOwnerKnownAICannotEnableEncryptionEvenWithCustomPolicy() {
        let candidates = [
            GroupInviteCandidate(id: "@custom:local.test", displayName: "AI", isAI: true)
        ]
        let containsAI = GroupInviteSelection.containsAI(
            invitees: ["@custom:local.test"], candidates: candidates,
            policy: AIParticipantPolicy(userIDPrefixes: ["@other:"]))
        XCTAssertTrue(containsAI)
        let presentation = EncryptionPresentation(
            encryption: .unencrypted, participants: containsAI ? .containsAI : .humanOnly)
        XCTAssertFalse(presentation.canEnable)
        var selection = NewRoomEncryptionSelection()
        selection.setRequested(true, presentation: presentation)
        XCTAssertFalse(selection.isRequested)
    }

    private func persona(_ id: String) throws -> MemoryPersona {
        let data = try JSONSerialization.data(withJSONObject: [
            "id": id, "display_name": "我的 AI", "owner_id": "@me:local.test", "kind": "custom",
            "status": "active", "revision": 1, "created_at": "2026-11-13T00:00:00Z",
            "updated_at": "2026-11-13T00:00:00Z", "visibility": "private",
        ])
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return try decoder.decode(MemoryPersona.self, from: data)
    }
}
