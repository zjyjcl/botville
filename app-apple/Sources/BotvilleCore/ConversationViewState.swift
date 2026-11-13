// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation

/// Ordered controls for an owner group row. Unknown/terminal rooms have no actions.
public struct GroupRoomRowViewState: Sendable, Equatable {
    public enum Action: Sendable, Hashable, Identifiable {
        case toggleMode(current: GroupRoomMode)
        case muteOneWay
        case mutedStatus
        case leave

        public var id: Self { self }

        public var helpTexts: [String] {
            guard case .toggleMode(let current) = self else { return [] }
            let quiet = "开启后被 @ 也不回复，仍会记住群里的内容。"
            return current == .mentionOnly
                ? [quiet, "关闭后，被 @ 或被回复时才会回复。"] : [quiet]
        }
    }

    public let title: String
    public let statusLabel: String
    public let joinedAt: Date?
    public let modeNotice: String?
    public let syncNotice: String?
    public let actions: [Action]
    public let isDisabled: Bool

    public init(
        room: GroupRoom, ordinal: Int, groupsEnabled: Bool,
        canEnableMentionOnly: Bool, isSubmitting: Bool
    ) {
        title = "群聊 \(ordinal)"
        statusLabel = GroupRoomPresentation.label(room)
        joinedAt = room.joinedAt
        modeNotice = if case .unknown = room.mode { "模式未知" } else { nil }
        syncNotice = room.state == .left && room.leavePending == true ? "正在同步" : nil
        isDisabled = isSubmitting
        if GroupRoomPresentation.canChangeMode(room) {
            let modeAction: Action = groupsEnabled && canEnableMentionOnly
                ? .toggleMode(current: room.mode)
                : (room.mode == .muted ? .mutedStatus : .muteOneWay)
            actions = [modeAction, .leave]
        } else {
            actions = []
        }
    }
}

public struct GroupRoomListViewState: Sendable, Equatable {
    public let disabledNotice: String?

    public init(groupsEnabled: Bool) {
        disabledNotice = groupsEnabled ? nil : "群聊功能已关闭，AI 不会在群里发言"
    }
}

/// Shares the exact parsed invitees between the create button and submitted draft.
/// Syntax/cardinality validation remains in RoomCreationDraft at submission time.
public struct NewConversationFormState: Sendable, Equatable {
    public let draft: RoomCreationDraft
    public let isCreating: Bool
    public var invitees: [String] { draft.invitees }
    public var showsGroupFields: Bool { !draft.isDirect }
    public var isCreateDisabled: Bool { isCreating || invitees.isEmpty }
    public var title: String { draft.isDirect ? "新建会话" : "发起群聊" }
    public var createTitle: String { isCreating ? "创建中…" : "创建" }
    public var inviteesTitle: String { draft.isDirect ? "对方 Matrix ID" : "手填 Matrix ID（可选）" }
    public var inviteesPlaceholder: String {
        draft.isDirect ? "例如 @alice:example.org" : "每行或逗号分隔一个 MXID"
    }
    public var inviteesHelp: String {
        draft.isDirect ? "私聊必须填写一个 MXID。"
            : "至少勾选一位联系人或手填一个 MXID；创建后会向这些账号发送邀请。"
    }

    public init(
        isDirect: Bool, roomName: String = "", selectedIDs: Set<String> = [],
        inviteesText: String, isCreating: Bool
    ) {
        draft = RoomCreationDraft(
            isDirect: isDirect, name: roomName,
            invitees: GroupInviteSelection.invitees(
                selectedIDs: isDirect ? [] : selectedIDs, manualText: inviteesText))
        self.isCreating = isCreating
    }
}
