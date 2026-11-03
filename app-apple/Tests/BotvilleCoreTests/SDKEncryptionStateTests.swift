// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

@testable import BotvilleCore
import MatrixRustSDK
import XCTest

final class SDKEncryptionStateTests: XCTestCase {
    func testMapsSDKEncryptionStates() {
        XCTAssertEqual(SDKEncryptionState.map(.encrypted), .encrypted)
        XCTAssertEqual(SDKEncryptionState.map(.notEncrypted), .unencrypted)
        XCTAssertEqual(SDKEncryptionState.map(.unknown), .unknown)
    }

    func testWarningsKeepUnknownSeparateFromNotConfigured() {
        let warnings = SDKEncryptionState.warnings(
            verification: .unknown,
            backup: .unknown,
            recovery: .unknown
        )

        XCTAssertTrue(warnings.contains("无法确认本设备是否已验证，换设备可能无法读取加密消息。"))
        XCTAssertTrue(warnings.contains("无法确认密钥备份状态。"))
        XCTAssertTrue(warnings.contains("无法确认密钥备份恢复状态，换设备可能看不到历史消息。"))
        XCTAssertFalse(warnings.contains(where: { $0.contains("未设置密钥备份恢复功能") }))
    }

    func testWarningsExplainUnverifiedAndDisabledRecovery() {
        let warnings = SDKEncryptionState.warnings(
            verification: .unverified,
            backup: .enabled,
            recovery: .disabled
        )

        XCTAssertEqual(warnings, [
            "本设备未验证，换设备可能无法读取加密消息。",
            "未设置密钥备份恢复功能，换设备可能看不到历史消息。"
        ])
    }

    func testParticipantsIncludesInvitedAIAndExcludesFormerMembers() {
        let policy = AIParticipantPolicy(userIDPrefixes: ["@ai-"])
        let result = SDKEncryptionState.participants(
            [member("@person:example.org", .join), member("@ai-helper:example.org", .invite), member("@ai-former:example.org", .leave)],
            expectedActiveCount: 2,
            policy: policy
        )

        XCTAssertEqual(result, .containsAI)
    }

    func testParticipantsRejectsPartialOrEmptyMembership() {
        let policy = AIParticipantPolicy()
        XCTAssertEqual(
            SDKEncryptionState.participants([member("@person:example.org", .join)], expectedActiveCount: 2, policy: policy),
            .unknown
        )
        XCTAssertEqual(
            SDKEncryptionState.participants([], expectedActiveCount: 0, policy: policy),
            .unknown
        )
    }

    func testParticipantsDoesNotTreatUnclassifiedServiceAsHuman() {
        let result = SDKEncryptionState.participants(
            [member("@bridge:example.org", .join, isServiceMember: true)],
            expectedActiveCount: 1,
            policy: AIParticipantPolicy()
        )

        XCTAssertEqual(result, .unknown)
    }

    func testFullMemberFetchDoesNotDependOnMissingSyncSummary() {
        let members = [member("@a:example.org", .join), member("@b:example.org", .invite)]
        let policy = AIParticipantPolicy(userIDPrefixes: ["@ai-"])
        XCTAssertEqual(SDKEncryptionState.participants(members, expectedActiveCount: 0, policy: policy), .unknown)
        XCTAssertEqual(SDKEncryptionState.participants(members, expectedActiveCount: 0, policy: policy,
                                                       membersAreComplete: true), .humanOnly)
    }

    func testCompleteIteratorRejectsConvertedMemberCountMismatch() {
        let iterator = SDKTestMembersIterator(reportedCount: 2, chunks: [[member("@person:example.org", .join)]])

        XCTAssertEqual(
            SDKEncryptionState.participants(iterator, expectedActiveCount: nil,
                                            policy: AIParticipantPolicy(userIDPrefixes: ["@ai-"]),
                                            membersAreComplete: true),
            .unknown
        )
    }

    func testCompleteIteratorFindsInvitedAIInFinalChunk() {
        let iterator = SDKTestMembersIterator(reportedCount: 2, chunks: [
            [member("@person:example.org", .join)],
            [member("@ai-helper:example.org", .invite)]
        ])

        XCTAssertEqual(
            SDKEncryptionState.participants(iterator, expectedActiveCount: 0,
                                            policy: AIParticipantPolicy(userIDPrefixes: ["@ai-"]),
                                            membersAreComplete: true),
            .containsAI
        )
    }

    func testCompleteIteratorAcceptsEmptyChunkAsEndWhenReportedCountMatches() {
        let iterator = SDKTestMembersIterator(reportedCount: 1, chunks: [
            [member("@person:example.org", .join)], []
        ])

        XCTAssertEqual(
            SDKEncryptionState.participants(iterator, expectedActiveCount: 1,
                                            policy: AIParticipantPolicy(userIDPrefixes: ["@ai-"]),
                                            membersAreComplete: true),
            .humanOnly
        )
    }

    private func member(
        _ userID: String,
        _ membership: MembershipState,
        isServiceMember: Bool = false
    ) -> RoomMember {
        RoomMember(
            userId: userID,
            displayName: nil,
            avatarUrl: nil,
            status: nil,
            call: nil,
            membership: membership,
            isNameAmbiguous: false,
            powerLevel: .value(value: 0),
            isIgnored: false,
            suggestedRoleForPowerLevel: .user,
            membershipChangeReason: nil,
            isServiceMember: isServiceMember
        )
    }
}

private final class SDKTestMembersIterator: RoomMembersIterator, @unchecked Sendable {
    private let reportedCount: UInt32
    private var chunks: [[RoomMember]]

    init(reportedCount: UInt32, chunks: [[RoomMember]]) {
        self.reportedCount = reportedCount
        self.chunks = chunks
        super.init(noHandle: .init())
    }

    required init(unsafeFromHandle handle: UInt64) {
        reportedCount = 0
        chunks = []
        super.init(unsafeFromHandle: handle)
    }

    override func len() -> UInt32 { reportedCount }
    override func nextChunk(chunkSize: UInt32) -> [RoomMember]? {
        guard !chunks.isEmpty else { return nil }
        return chunks.removeFirst()
    }
}
