// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation
import SwiftUI
import XCTest

@testable import BotvilleCore

final class ConversationViewStateTests: XCTestCase {
    private let states: [GroupRoomState] = [
        .accepted, .encryptedDisabled, .left, .rejectedEncrypted, .unknown("future")
    ]
    private let modes: [GroupRoomMode] = [.muted, .mentionOnly, .unknown("future")]
    private let joinedAt = Date(timeIntervalSince1970: 1_700_000_000)

    private func room(_ status: GroupRoomState, _ mode: GroupRoomMode, pending: Bool? = nil) -> GroupRoom {
        GroupRoom(roomRef: "opaque-reference", state: status, mode: mode,
                  joinedAt: joinedAt, updatedAt: joinedAt, leavePending: pending)
    }

    func testClosedGroupsNeverOfferMentionOnlyEvenWithStalePermission() {
        for status in states {
            for mode in modes {
                for canEnable in [false, true] {
                    for submitting in [false, true] {
                        let state = GroupRoomRowViewState(
                            room: room(status, mode), ordinal: 2, groupsEnabled: false,
                            canEnableMentionOnly: canEnable, isSubmitting: submitting)
                        let expected: [GroupRoomRowViewState.Action]
                        switch (status, mode) {
                        case (.accepted, .muted), (.encryptedDisabled, .muted):
                            expected = [.mutedStatus, .leave]
                        case (.accepted, .mentionOnly), (.encryptedDisabled, .mentionOnly):
                            expected = [.muteOneWay, .leave]
                        default: expected = []
                        }
                        XCTAssertEqual(state.actions, expected, "\(status) / \(mode) / \(canEnable) / \(submitting)")
                        XCTAssertEqual(state.isDisabled, submitting)
                        XCTAssertTrue(state.actions.flatMap(\.helpTexts).isEmpty)
                    }
                }
            }
        }
    }

    func testEnabledGroupsOfferOrderedBidirectionalToggleOnlyForManageableRooms() {
        for status in states {
            for mode in modes {
                for submitting in [false, true] {
                    let state = GroupRoomRowViewState(
                        room: room(status, mode), ordinal: 1, groupsEnabled: true,
                        canEnableMentionOnly: true, isSubmitting: submitting)
                    let expected: [GroupRoomRowViewState.Action]
                    switch (status, mode) {
                    case (.accepted, .muted), (.encryptedDisabled, .muted):
                        expected = [.toggleMode(current: .muted), .leave]
                    case (.accepted, .mentionOnly), (.encryptedDisabled, .mentionOnly):
                        expected = [.toggleMode(current: .mentionOnly), .leave]
                    default: expected = []
                    }
                    XCTAssertEqual(state.actions, expected, "\(status) / \(mode) / \(submitting)")
                    XCTAssertEqual(state.isDisabled, submitting)
                    XCTAssertEqual(Set(state.actions.map(\.id)).count, state.actions.count)
                }
            }
        }
        XCTAssertEqual(GroupRoomRowViewState.Action.toggleMode(current: .muted).helpTexts,
                       ["开启后被 @ 也不回复，仍会记住群里的内容。"])
        XCTAssertEqual(GroupRoomRowViewState.Action.toggleMode(current: .mentionOnly).helpTexts,
                       ["开启后被 @ 也不回复，仍会记住群里的内容。", "关闭后，被 @ 或被回复时才会回复。"])
    }

    func testUnconfirmedSettingsKeepEnabledGroupsOneWay() {
        for status in states {
            for mode in modes {
                for submitting in [false, true] {
                    let state = GroupRoomRowViewState(
                        room: room(status, mode), ordinal: 1, groupsEnabled: true,
                        canEnableMentionOnly: false, isSubmitting: submitting)
                    let expected: [GroupRoomRowViewState.Action]
                    switch (status, mode) {
                    case (.accepted, .muted), (.encryptedDisabled, .muted):
                        expected = [.mutedStatus, .leave]
                    case (.accepted, .mentionOnly), (.encryptedDisabled, .mentionOnly):
                        expected = [.muteOneWay, .leave]
                    default: expected = []
                    }
                    XCTAssertEqual(state.actions, expected)
                    XCTAssertEqual(state.isDisabled, submitting)
                }
            }
        }
    }

    func testGroupLabelsUnknownModeAndLeavePendingPreserveCopy() {
        let labels: [(GroupRoomState, String)] = [
            (.accepted, "已加入"), (.encryptedDisabled, "加密群，AI 不参与"),
            (.left, "已退出"), (.rejectedEncrypted, "已拒绝（加密）"), (.unknown("future"), "状态未知")
        ]
        for (status, label) in labels {
            for mode in modes {
                for pending in [nil, false, true] as [Bool?] {
                    let state = GroupRoomRowViewState(
                        room: room(status, mode, pending: pending), ordinal: 3,
                        groupsEnabled: true, canEnableMentionOnly: true, isSubmitting: false)
                    let unknown = mode == .unknown("future")
                    XCTAssertEqual(state.statusLabel, unknown && status != .left ? "状态未知" : label)
                    XCTAssertEqual(state.modeNotice, unknown ? "模式未知" : nil)
                    XCTAssertEqual(state.syncNotice, status == .left && pending == true ? "正在同步" : nil)
                    XCTAssertEqual(state.title, "群聊 3")
                    XCTAssertEqual(state.joinedAt, joinedAt)
                }
            }
        }
        XCTAssertNil(GroupRoomListViewState(groupsEnabled: true).disabledNotice)
        XCTAssertEqual(GroupRoomListViewState(groupsEnabled: false).disabledNotice,
                       "群聊功能已关闭，AI 不会在群里发言")
    }

    func testCreateAvailabilityForEmptyAndNonemptyInviteesDirectAndGroupWhileCreating() {
        for isDirect in [false, true] {
            for isCreating in [false, true] {
                for text in ["", " ,\n\t ", "@alice:example.org"] {
                    let state = NewConversationFormState(
                        isDirect: isDirect, inviteesText: text, isCreating: isCreating)
                    XCTAssertEqual(state.isCreateDisabled, text != "@alice:example.org" || isCreating)
                    XCTAssertEqual(state.isCreating, isCreating)
                    XCTAssertEqual(state.createTitle, isCreating ? "创建中…" : "创建")
                    XCTAssertEqual(state.title, isDirect ? "新建会话" : "发起群聊")
                    XCTAssertEqual(state.showsGroupFields, !isDirect)
                    XCTAssertEqual(state.draft.isDirect, isDirect)
                }
            }
        }
    }

    func testInviteeSelectionParsingAndSubmittedDraftUseSameOrderedIdentities() {
        let selected: Set<String> = ["@b:example.org", "@a:example.org"]
        for isCreating in [false, true] {
            let group = NewConversationFormState(
                isDirect: false, roomName: "一起聊", selectedIDs: selected,
                inviteesText: " @b:example.org, @c:example.org\n@a:example.org\t@d:example.org ",
                isCreating: isCreating)
            XCTAssertEqual(group.invitees, ["@a:example.org", "@b:example.org", "@c:example.org", "@d:example.org"])
            XCTAssertEqual(group.draft.name, "一起聊")
            XCTAssertEqual(group.isCreateDisabled, isCreating)
            let selectedOnly = NewConversationFormState(
                isDirect: false, selectedIDs: selected, inviteesText: "", isCreating: isCreating)
            XCTAssertEqual(selectedOnly.invitees, ["@a:example.org", "@b:example.org"])
            XCTAssertEqual(selectedOnly.isCreateDisabled, isCreating)
            let direct = NewConversationFormState(
                isDirect: true, selectedIDs: selected, inviteesText: "", isCreating: isCreating)
            XCTAssertTrue(direct.invitees.isEmpty, "Hidden group selection must not enable direct creation")
            XCTAssertTrue(direct.isCreateDisabled)
            let manualDirect = NewConversationFormState(
                isDirect: true, selectedIDs: selected, inviteesText: "@c:example.org", isCreating: isCreating)
            XCTAssertEqual(manualDirect.invitees, ["@c:example.org"])
            XCTAssertEqual(manualDirect.isCreateDisabled, isCreating)
        }
    }

    func testFormCopyAndSubmissionValidationRemainUnchanged() {
        let direct = NewConversationFormState(isDirect: true, inviteesText: "bad-id", isCreating: false)
        XCTAssertFalse(direct.isCreateDisabled, "Nonempty invalid input is still validated at submission")
        XCTAssertThrowsError(try direct.draft.validate())
        XCTAssertEqual(direct.inviteesTitle, "对方 Matrix ID")
        XCTAssertEqual(direct.inviteesPlaceholder, "例如 @alice:example.org")
        XCTAssertEqual(direct.inviteesHelp, "私聊必须填写一个 MXID。")
        let group = NewConversationFormState(isDirect: false, inviteesText: "", isCreating: false)
        XCTAssertEqual(group.inviteesTitle, "手填 Matrix ID（可选）")
        XCTAssertEqual(group.inviteesPlaceholder, "每行或逗号分隔一个 MXID")
        XCTAssertEqual(group.inviteesHelp, "至少勾选一位联系人或手填一个 MXID；创建后会向这些账号发送邀请。")
        let multiple = NewConversationFormState(
            isDirect: true, inviteesText: "@a:example.org,@b:example.org", isCreating: false)
        XCTAssertFalse(multiple.isCreateDisabled)
        XCTAssertThrowsError(try multiple.draft.validate())
    }

    @MainActor
    func testPresentationOnlyViewsAcceptTypedStateAndRetainIt() {
        // Compile-time wiring guard, not a rendering/accessibility assertion.
        let badgeFactory: (PersonaMoodPresentation) -> PersonaMoodBadge = PersonaMoodBadge.init(presentation:)
        let rowFactory: (GroupRoomRowViewState, @escaping (GroupRoomMode) -> Void, @escaping () -> Void) -> GroupRoomRow = GroupRoomRow.init
        let buttonFactory: (NewConversationFormState, @escaping () -> Void) -> NewConversationCreateButton = NewConversationCreateButton.init
        let owner = PersonaMoodPresentation(ownerState: PersonaState(personaID: "p", mood: .happy, moodPublic: false))
        XCTAssertEqual(badgeFactory(owner).presentation, owner)
        let row = GroupRoomRowViewState(room: room(.accepted, .mentionOnly), ordinal: 1,
                                       groupsEnabled: false, canEnableMentionOnly: false, isSubmitting: false)
        XCTAssertEqual(rowFactory(row, { _ in }, {}).state, row)
        let form = NewConversationFormState(isDirect: false, inviteesText: "", isCreating: false)
        XCTAssertEqual(buttonFactory(form, {}).state, form)
    }
}
