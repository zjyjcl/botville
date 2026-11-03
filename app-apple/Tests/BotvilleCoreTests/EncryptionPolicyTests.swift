// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import XCTest
@testable import BotvilleCore

final class EncryptionPolicyTests: XCTestCase {
    func testPolicyUsesKnownAIThenConfiguredRulesAndConservativeUnknown() {
        let policy = AIParticipantPolicy(userIDPatterns: ["^@bot_.*:example\\.org$"], userIDPrefixes: ["@ai:"])
        XCTAssertEqual(policy.classify(memberIDs: ["@alice:example.org"]), .humanOnly)
        XCTAssertEqual(policy.classify(memberIDs: ["@bot_echo:example.org"]), .containsAI)
        XCTAssertEqual(policy.classify(memberIDs: ["@ai:example.org"]), .containsAI)
        XCTAssertEqual(policy.classify(memberIDs: ["@alice:example.org"], knownAIUserIDs: ["@alice:example.org"]), .containsAI)
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
    func testPresentationMapsStatesWithoutColourOnlyMeaning() {
        let encrypted = EncryptionPresentation(encryption: .encrypted, participants: .humanOnly)
        XCTAssertEqual(encrypted.title, "已加密"); XCTAssertEqual(encrypted.systemImage, "lock.fill"); XCTAssertFalse(encrypted.canDisable); XCTAssertFalse(encrypted.canEnable)
        let ai = EncryptionPresentation(encryption: .unencrypted, participants: .containsAI)
        XCTAssertEqual(ai.title, "未加密 · 含 AI"); XCTAssertTrue(ai.detail.contains("AI")); XCTAssertFalse(ai.canEnable)
        XCTAssertTrue(EncryptionPresentation(encryption: .unencrypted, participants: .humanOnly).canEnable)
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
