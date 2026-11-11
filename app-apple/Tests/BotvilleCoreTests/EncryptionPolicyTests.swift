// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import XCTest
@testable import BotvilleCore

final class EncryptionPolicyTests: XCTestCase {
    func testPolicyUsesConfiguredRulesAndConservativeUnknown() {
        let policy = AIParticipantPolicy(userIDPatterns: ["^@bot_.*:example\\.org$"], userIDPrefixes: ["@ai:"])
        XCTAssertEqual(policy.classify(memberIDs: ["@alice:example.org"]), .humanOnly)
        XCTAssertEqual(policy.classify(memberIDs: ["@bot_echo:example.org"]), .containsAI)
        XCTAssertEqual(policy.classify(memberIDs: ["@ai:example.org"]), .containsAI)
        XCTAssertEqual(policy.classify(memberIDs: nil), .unknown)
        XCTAssertEqual(policy.classify(memberIDs: []), .unknown)
        XCTAssertEqual(AIParticipantPolicy().classify(memberIDs: ["@alice:example.org"]), .unknown)
    }
    func testPolicyReadsJSONEnvironmentAndRejectsInvalidRegexAsUnknown() {
        let configured = AIParticipantPolicy.configured(environment: [AIParticipantPolicy.prefixConfigurationKey: "[\"@robot:\"]"], infoDictionary: [:])
        XCTAssertEqual(configured.classify(memberIDs: ["@robot:example.org"]), .containsAI)
        XCTAssertEqual(configured.classify(memberIDs: ["@person:example.org"]), .humanOnly)
        XCTAssertEqual(AIParticipantPolicy(userIDPatterns: ["["]).classify(memberIDs: ["@person:example.org"]), .unknown)
        XCTAssertEqual(AIParticipantPolicy(userIDPatterns: ["^@bot"], userIDPrefixes: ["@ai:"]).classify(memberIDs: ["@person:example.org"]), .humanOnly)
        XCTAssertEqual(AIParticipantPolicy(userIDPatterns: ["^@bot", "["]).classify(memberIDs: ["@person:example.org"]), .unknown)
        XCTAssertEqual(AIParticipantPolicy.configured(environment: [AIParticipantPolicy.prefixConfigurationKey: "invalid"], infoDictionary: [AIParticipantPolicy.prefixConfigurationKey: ["@ai:"]]).classify(memberIDs: ["@person:example.org"]), .unknown)
    }
    func testDefaultRuleMirrorsAppserviceUsersNamespace() {
        let policy = AIParticipantPolicy.configured(environment: [:], infoDictionary: [:])
        XCTAssertTrue(policy.hasConfiguredRules)
        XCTAssertNil(policy.configurationNotice)
        XCTAssertEqual(AIParticipantPolicy.defaultUserIDPatterns, ["^@ai_[^:]*:"])
        // Appservice-namespace accounts are AI; ordinary accounts count as human.
        XCTAssertEqual(policy.classify(memberIDs: ["@ai_helper:matrix.test"]), .containsAI)
        XCTAssertEqual(policy.classify(memberIDs: ["@alice:matrix.test", "@ai_echo:matrix.test"]), .containsAI)
        XCTAssertEqual(policy.classify(memberIDs: ["@alice:matrix.test", "@bob:matrix.test"]), .humanOnly)
        XCTAssertEqual(policy.classify(memberIDs: nil), .unknown)
        // Boundaries.
        XCTAssertEqual(policy.classify(memberIDs: ["@ai_:matrix.test"]), .containsAI, "empty remainder, like ^@ai_.*")
        XCTAssertEqual(policy.classify(memberIDs: ["@ai_x:matrix.test:8448"]), .containsAI, "server with port")
        XCTAssertEqual(policy.classify(memberIDs: ["@ai_helper:other.example"]), .containsAI, "federated ai_ fails safe")
        XCTAssertEqual(policy.classify(memberIDs: ["@AI_helper:matrix.test"]), .humanOnly, "case-sensitive like appservice")
        XCTAssertEqual(policy.classify(memberIDs: ["@Ai_helper:matrix.test"]), .humanOnly)
        XCTAssertEqual(policy.classify(memberIDs: ["@my_ai_helper:matrix.test"]), .humanOnly, "ai_ not at localpart start")
        XCTAssertEqual(policy.classify(memberIDs: ["@xai_bot:matrix.test"]), .humanOnly)
        XCTAssertEqual(policy.classify(memberIDs: ["@ai-helper:matrix.test"]), .humanOnly, "hyphen is outside the namespace")
        XCTAssertEqual(policy.classify(memberIDs: ["@aihelper:matrix.test"]), .humanOnly)
        XCTAssertEqual(policy.classify(memberIDs: ["@alice:ai_server.test"]), .humanOnly, "ai_ in server name only")
    }
    func testDefaultMatchesAppserviceRegexForLocalMXIDs() throws {
        // Template value with BOTVILLE_SERVER_NAME_REGEX substituted (init.sh escapes the server name).
        let appservice = try NSRegularExpression(pattern: "^@ai_.*:matrix\\.test$")
        let app = try NSRegularExpression(pattern: AIParticipantPolicy.defaultUserIDPatterns[0])
        func matches(_ re: NSRegularExpression, _ id: String) -> Bool {
            re.firstMatch(in: id, range: NSRange(id.startIndex..., in: id)) != nil
        }
        for id in ["@ai_:matrix.test", "@ai_a:matrix.test", "@ai_helper.bot=1/x:matrix.test", "@ai__x:matrix.test",
                   "@alice:matrix.test", "@AI_x:matrix.test", "@my_ai_x:matrix.test", "@ai-x:matrix.test", "@ai:matrix.test"] {
            XCTAssertEqual(matches(app, id), matches(appservice, id), id)
        }
    }
    func testOverrideReplacesDefault() {
        let env = AIParticipantPolicy.configured(environment: [AIParticipantPolicy.patternConfigurationKey: "[\"^@bot_[^:]+:example\\\\.org$\"]"], infoDictionary: [:])
        XCTAssertEqual(env.classify(memberIDs: ["@bot_echo:example.org"]), .containsAI)
        XCTAssertEqual(env.classify(memberIDs: ["@ai_helper:example.org"]), .humanOnly, "override is authoritative; default not merged")
        let plist = AIParticipantPolicy.configured(environment: [:], infoDictionary: [AIParticipantPolicy.prefixConfigurationKey: ["@robot:"]])
        XCTAssertEqual(plist.classify(memberIDs: ["@robot:example.org"]), .containsAI)
        XCTAssertEqual(plist.classify(memberIDs: ["@ai_helper:example.org"]), .humanOnly)
        // Environment wins over Info.plist.
        let both = AIParticipantPolicy.configured(environment: [AIParticipantPolicy.prefixConfigurationKey: "[\"@env:\"]"],
                                                  infoDictionary: [AIParticipantPolicy.prefixConfigurationKey: ["@plist:"]])
        XCTAssertEqual(both.classify(memberIDs: ["@plist:example.org"]), .humanOnly)
        XCTAssertEqual(both.classify(memberIDs: ["@env:example.org"]), .containsAI)
        // An explicitly empty override has no rules: conservative unknown, not the default.
        let empty = AIParticipantPolicy.configured(environment: [AIParticipantPolicy.patternConfigurationKey: "[]"], infoDictionary: [:])
        XCTAssertFalse(empty.hasConfiguredRules)
        XCTAssertNotNil(empty.configurationNotice)
        XCTAssertEqual(empty.classify(memberIDs: ["@alice:example.org"]), .unknown)
    }
    func testInvalidOverrideFailsClosedInsteadOfUsingDefault() {
        for env in [[AIParticipantPolicy.patternConfigurationKey: "[\"[\"]"],
                    [AIParticipantPolicy.patternConfigurationKey: "not json"],
                    [AIParticipantPolicy.prefixConfigurationKey: "{}"]] {
            let policy = AIParticipantPolicy.configured(environment: env, infoDictionary: [:])
            XCTAssertFalse(policy.hasConfiguredRules)
            XCTAssertNotNil(policy.configurationNotice)
            XCTAssertEqual(policy.classify(memberIDs: ["@alice:example.org"]), .unknown)
            XCTAssertEqual(policy.classify(memberIDs: ["@ai_helper:example.org"]), .unknown)
        }
        let badPlist = AIParticipantPolicy.configured(environment: [:], infoDictionary: [AIParticipantPolicy.patternConfigurationKey: "^@ai_"])
        XCTAssertEqual(badPlist.classify(memberIDs: ["@alice:example.org"]), .unknown)
        let badRegex = AIParticipantPolicy.configured(environment: [:], infoDictionary: [AIParticipantPolicy.patternConfigurationKey: ["^@ok_", "("]])
        XCTAssertEqual(badRegex.classify(memberIDs: ["@alice:example.org"]), .unknown)
    }
    func testPresentationMapsStatesWithoutColourOnlyMeaning() {
        let encrypted = EncryptionPresentation(encryption: .encrypted, participants: .humanOnly)
        XCTAssertEqual(encrypted.title, "已加密"); XCTAssertEqual(encrypted.systemImage, "lock.fill"); XCTAssertFalse(encrypted.canDisable); XCTAssertFalse(encrypted.canEnable)
        let ai = EncryptionPresentation(encryption: .unencrypted, participants: .containsAI)
        XCTAssertEqual(ai.title, "未加密 · 含 AI"); XCTAssertTrue(ai.detail.contains("AI")); XCTAssertFalse(ai.canEnable)
        XCTAssertTrue(EncryptionPresentation(encryption: .unencrypted, participants: .humanOnly).canEnable)
    }
    func testChangingNewRoomInviteeToAIResetsEncryptionSelectionAndAllowsPlaintextCreation() throws {
        let human = EncryptionPresentation(encryption: .unencrypted, participants: .humanOnly)
        let ai = EncryptionPresentation(encryption: .unencrypted, participants: .containsAI)
        var selection = NewRoomEncryptionSelection()

        selection.setRequested(true, presentation: human)
        XCTAssertTrue(selection.isRequested)

        // This models the reported UI sequence: enable E2EE for a human draft,
        // then replace the invitee with an AI MXID. The old toggle remained on
        // while disabled and blocked plaintext creation.
        selection.reconcile(presentation: ai)
        XCTAssertFalse(selection.isRequested)

        let draft = RoomCreationDraft(isDirect: true, invitees: ["@ai_echo:example.org"])
        XCTAssertFalse(try EncryptionConsent().validatedEncryption(
            for: .newRoom(draft), participants: .containsAI, currentEncryption: .unencrypted
        ))
    }
    func testConsentRequiresMatchingSecondConfirmationAndResetsOnContextChange() {
        let first = EncryptionTarget.existingRoom("!first:example.org"), second = EncryptionTarget.existingRoom("!second:example.org")
        var consent = EncryptionConsent(); XCTAssertFalse(consent.allowsEncryption(for: first)); consent.request(for: first); XCTAssertTrue(consent.isAwaitingConfirmation)
        XCTAssertFalse(consent.confirm(for: second)); XCTAssertTrue(consent.confirm(for: first)); XCTAssertTrue(consent.allowsEncryption(for: first)); consent.contextChanged(to: second); XCTAssertFalse(consent.allowsEncryption(for: first)); consent.request(for: second); consent.cancel(); XCTAssertFalse(consent.isAwaitingConfirmation)
    }
    func testEncryptionGateNeedsConsentHumanParticipantsAndNeverDisables() throws {
        let target = EncryptionTarget.existingRoom("!room:example.org")
        var consent = EncryptionConsent()
        XCTAssertThrowsError(try consent.validatedEncryption(for: target, participants: .humanOnly, currentEncryption: .unencrypted))
        consent.request(for: target); XCTAssertTrue(consent.confirm(for: target))
        XCTAssertTrue(try consent.validatedEncryption(for: target, participants: .humanOnly, currentEncryption: .unencrypted))
        XCTAssertThrowsError(try consent.validatedEncryption(for: target, participants: .containsAI, currentEncryption: .unencrypted))
        XCTAssertFalse(try consent.validatedEncryption(for: target, participants: .humanOnly, currentEncryption: .encrypted))
    }
    func testNewRoomDefaultsToPlaintextButPendingOrStaleIntentCannotDowngrade() throws {
        let target = EncryptionTarget.newRoom(RoomCreationDraft(isDirect: true, invitees: ["@person:example.org"]))
        var consent = EncryptionConsent()
        XCTAssertFalse(try consent.validatedEncryption(for: target, participants: .unknown, currentEncryption: .unencrypted))
        consent.request(for: target)
        XCTAssertThrowsError(try consent.validatedEncryption(for: target, participants: .humanOnly, currentEncryption: .unencrypted))
        let changed = EncryptionTarget.newRoom(RoomCreationDraft(isDirect: true, invitees: ["@other:example.org"]))
        XCTAssertThrowsError(try consent.validatedEncryption(for: changed, participants: .humanOnly, currentEncryption: .unencrypted))
    }
    func testDraftRequiresValidMXIDAndOneDirectParticipant() throws {
        XCTAssertThrowsError(try RoomCreationDraft(isDirect: true, invitees: []).validate()); XCTAssertThrowsError(try RoomCreationDraft(isDirect: true, invitees: ["alice"]).validate()); XCTAssertThrowsError(try RoomCreationDraft(isDirect: true, invitees: ["@a:example.org", "@b:example.org"]).validate())
        XCTAssertEqual(try RoomCreationDraft(isDirect: true, invitees: [" @a:example.org "]).validatedInvitees(), ["@a:example.org"])
    }
}
