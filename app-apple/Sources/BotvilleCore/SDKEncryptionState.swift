// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import MatrixRustSDK

/// Adapts Matrix SDK encryption and member information to the app's
/// deliberately conservative policy model.
enum SDKEncryptionState {
    static func map(_ state: EncryptionState) -> RoomEncryptionState {
        switch state {
        case .encrypted:
            .encrypted
        case .notEncrypted:
            .unencrypted
        case .unknown:
            .unknown
        }
    }

    /// Reads every chunk before making a policy decision. A partial member list
    /// must never make an AI-containing room look safe to encrypt.
    static func participants(
        _ iterator: RoomMembersIterator,
        expectedActiveCount: UInt64?,
        policy: AIParticipantPolicy,
        membersAreComplete: Bool = false
    ) -> AIParticipantStatus {
        let expectedTotal = iterator.len()
        var members: [RoomMember] = []
        while let chunk = iterator.nextChunk(chunkSize: 100), !chunk.isEmpty {
            members.append(contentsOf: chunk)
        }
        // FFI conversion can drop an invalid member. Never accept that as a
        // complete human-only list, even when members() itself succeeded.
        guard members.count == Int(expectedTotal) else { return .unknown }
        return participants(members, expectedActiveCount: expectedActiveCount, policy: policy,
                            membersAreComplete: membersAreComplete)
    }

    /// Kept separate from the SDK iterator to make the conservative rules
    /// directly testable without manufacturing an FFI-backed iterator.
    static func participants(
        _ members: [RoomMember],
        expectedActiveCount: UInt64?,
        policy: AIParticipantPolicy,
        membersAreComplete: Bool = false
    ) -> AIParticipantStatus {
        let active = members.filter { member in
            switch member.membership {
            case .join, .invite:
                true
            default:
                false
            }
        }

        let status = policy.classify(memberIDs: active.map(\.userId))
        if status == .containsAI { return .containsAI }
        guard !active.isEmpty else { return .unknown }
        // members() guarantees a full fetch; /sync summary counts may still be
        // zero. Only a membersNoSync() cache needs the summary count check.
        if !membersAreComplete, expectedActiveCount != UInt64(active.count) { return .unknown }
        guard status != .humanOnly else {
            // Matrix service membership alone does not prove an AI identity;
            // without an explicit configured match it cannot prove human-only.
            return active.contains(where: \.isServiceMember) ? .unknown : .humanOnly
        }
        return status
    }

    static func warnings(
        verification: VerificationState,
        backup: BackupState,
        recovery: RecoveryState
    ) -> [String] {
        var result: [String] = []

        switch verification {
        case .unverified:
            result.append("本设备未验证，换设备可能无法读取加密消息。")
        case .unknown:
            result.append("无法确认本设备是否已验证，换设备可能无法读取加密消息。")
        case .verified:
            break
        }

        switch recovery {
        case .disabled:
            result.append("未设置密钥备份恢复功能，换设备可能看不到历史消息。")
        case .incomplete:
            result.append("密钥备份恢复尚未完成，换设备可能看不到历史消息。")
        case .unknown:
            result.append("无法确认密钥备份恢复状态，换设备可能看不到历史消息。")
        case .enabled:
            break
        }

        switch backup {
        case .unknown:
            result.append("无法确认密钥备份状态。")
        case .creating, .enabling, .resuming, .downloading, .disabling:
            result.append("密钥备份正在更新，换设备可能暂时看不到历史消息。")
        case .enabled:
            break
        }

        return result
    }
}
