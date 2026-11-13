// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import XCTest
@testable import BotvilleCore

final class AIProfileTests: XCTestCase {
    private let policy = AIParticipantPolicy(userIDPatterns: ["^@ai_[^:]*:"])
    private let local = "@person:matrix.test"
    private let ai = "@ai_helper:matrix.test"

    func testEligibilityReturnsTheOnlyJoinedAICounterpart() {
        XCTAssertEqual(AIProfileEligibility.aiCounterpartID(
            isDirect: true, localUserID: local,
            members: [.init(userID: local, isJoined: true), .init(userID: ai, isJoined: true)], policy: policy
        ), ai)
    }

    func testEligibilityFailsClosedForNonDirectOrIncompleteMembership() {
        let directMembers: [AIProfileMember] = [.init(userID: local, isJoined: true), .init(userID: ai, isJoined: true)]
        XCTAssertNil(AIProfileEligibility.aiCounterpartID(isDirect: false, localUserID: local, members: directMembers, policy: policy))
        XCTAssertNil(AIProfileEligibility.aiCounterpartID(
            isDirect: true, localUserID: local,
            members: directMembers + [.init(userID: "@extra:matrix.test", isJoined: true)], policy: policy
        ))
        XCTAssertNil(AIProfileEligibility.aiCounterpartID(
            isDirect: true, localUserID: local,
            members: [.init(userID: local, isJoined: true), .init(userID: ai, isJoined: true, isActive: false)], policy: policy
        ))
    }

    func testEligibilityRejectsHumanOrUnconfiguredCounterpart() {
        XCTAssertNil(AIProfileEligibility.aiCounterpartID(
            isDirect: true, localUserID: local,
            members: [.init(userID: local, isJoined: true), .init(userID: "@human:matrix.test", isJoined: true)], policy: policy
        ))
        XCTAssertNil(AIProfileEligibility.aiCounterpartID(
            isDirect: true, localUserID: local,
            members: [.init(userID: local, isJoined: true), .init(userID: ai, isJoined: true)], policy: AIParticipantPolicy()
        ))
    }

    func testEligibilityRejectsIncompleteSnapshotsMissingSelfAndDuplicates() {
        XCTAssertNil(AIProfileEligibility.aiCounterpartID(
            isDirect: true, localUserID: local,
            members: [.init(userID: local, isJoined: true), .init(userID: ai, isJoined: true)],
            snapshotComplete: false, policy: policy
        ))
        XCTAssertNil(AIProfileEligibility.aiCounterpartID(
            isDirect: true, localUserID: local,
            members: [.init(userID: ai, isJoined: true), .init(userID: "@other:matrix.test", isJoined: true)], policy: policy
        ))
        XCTAssertNil(AIProfileEligibility.aiCounterpartID(
            isDirect: true, localUserID: local,
            members: [.init(userID: local, isJoined: true), .init(userID: local, isJoined: true)], policy: policy
        ))
    }

    func testDefaultAndOverrideRulesRecognizeCrossServerAI() {
        let federatedAI = "@ai_helper:other.test"
        let members: [AIProfileMember] = [.init(userID: local, isJoined: true), .init(userID: federatedAI, isJoined: true)]
        XCTAssertEqual(AIProfileEligibility.aiCounterpartID(
            isDirect: true, localUserID: local, members: members,
            policy: AIParticipantPolicy.configured(environment: [:], infoDictionary: [:])
        ), federatedAI)
        XCTAssertEqual(AIProfileEligibility.aiCounterpartID(
            isDirect: true, localUserID: local,
            members: [.init(userID: local, isJoined: true), .init(userID: "@robot:other.test", isJoined: true)],
            policy: AIParticipantPolicy(userIDPrefixes: ["@robot:"])
        ), "@robot:other.test")
    }
}
