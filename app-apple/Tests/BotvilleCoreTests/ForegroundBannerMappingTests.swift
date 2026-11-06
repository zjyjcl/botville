// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

@testable import BotvilleCore
import Foundation
import MatrixRustSDK
import XCTest

@MainActor
final class ForegroundBannerMappingTests: XCTestCase {
    func testLocalTextAndCachedDisplayNameBecomeDirectRoomPreview() throws {
        let candidate = try XCTUnwrap(map(text("本地同步正文"), direct: true))
        XCTAssertEqual(candidate.title, "发送者")
        XCTAssertEqual(candidate.preview, "本地同步正文")
        XCTAssertEqual(candidate.latestActivity, Date(timeIntervalSince1970: 2))
        XCTAssertNil(candidate.eventID) // SDK latestEvent currently exposes only time.
        XCTAssertEqual(candidate.route, .room("!room:example.test"))
    }

    func testGroupUsesRoomNameAndMissingProfilePreservesBody() throws {
        XCTAssertEqual(try XCTUnwrap(map(text("正文"))).title, "测试房间")
        let candidate = try XCTUnwrap(map(text("正文"), direct: true, profile: .error(message: "unused")))
        XCTAssertEqual(candidate.title, "测试房间")
        XCTAssertEqual(candidate.preview, "正文")
    }

    func testEncryptedRoomNeverExposesLocallyDecryptedBody() throws {
        XCTAssertEqual(try XCTUnwrap(map(text("不可出现在横幅的内容"), encryption: .encrypted)).preview, "加密消息")
        let undecryptable = TimelineItemContent.msgLike(content: MsgLikeContent(
            kind: .unableToDecrypt(msg: .unknown), inReplyTo: nil, threadRoot: nil, threadSummary: nil))
        XCTAssertEqual(try XCTUnwrap(map(undecryptable)).preview, "加密消息")
    }

    func testPlaceholderAndParseFailureDoNotExposeErrorDetails() throws {
        XCTAssertEqual(try XCTUnwrap(map(text(" \n "))).preview, "…")
        let failure = TimelineItemContent.failedToParseMessageLike(eventType: "m.room.message", error: "internal detail")
        XCTAssertEqual(try XCTUnwrap(map(failure)).preview, "无法加载")
        XCTAssertEqual(try XCTUnwrap(map(failure, encryption: .encrypted)).preview, "加密消息")
    }

    func testOwnSenderIsDetectedEvenIfSDKFlagIsFalse() throws {
        XCTAssertTrue(try XCTUnwrap(map(text("自己"), sender: "@me:example.test")).isOwn)
        XCTAssertTrue(try XCTUnwrap(map(text("自己"), isOwn: true)).isOwn)
    }

    func testLocalEchoInviteAndNonMessageChangesAreIgnored() {
        let room = MatrixRoom(id: "!room:example.test", name: "房间")
        XCTAssertNil(MatrixService.foregroundBannerCandidate(for: room, latest: .none, currentUserID: nil))
        XCTAssertNil(MatrixService.foregroundBannerCandidate(for: room, latest: .remoteInvite(
            timestamp: 2_000, inviter: nil, inviterProfile: .unavailable), currentUserID: nil))
        XCTAssertNil(map(.profileChange(displayName: "新名字", prevDisplayName: nil, avatarUrl: nil, prevAvatarUrl: nil)))
        XCTAssertNil(map(.msgLike(content: MsgLikeContent(kind: .redacted, inReplyTo: nil, threadRoot: nil, threadSummary: nil))))
        let invite = MatrixRoom(id: room.id, name: room.name, membership: .invited)
        XCTAssertNil(MatrixService.foregroundBannerCandidate(for: invite, latest: remote(text("旧消息")), currentUserID: nil))
    }

    func testPreviewLimitsUseCharacters() throws {
        let candidate = try XCTUnwrap(map(text(String(repeating: "👨‍👩‍👧‍👦", count: 300)), direct: true,
                                         profile: .ready(displayName: String(repeating: "名", count: 130),
                                                         displayNameAmbiguous: false, avatarUrl: nil, status: nil, call: nil)))
        XCTAssertEqual(candidate.title.count, 120)
        XCTAssertEqual(candidate.preview.count, 240)
        XCTAssertTrue(candidate.preview.hasSuffix("…"))
    }

    private func text(_ body: String) -> TimelineItemContent {
        .msgLike(content: MsgLikeContent(kind: .message(content: MessageContent(
            msgType: .text(content: TextMessageContent(body: body, formatted: nil)), body: body, isEdited: false, mentions: nil)),
            inReplyTo: nil, threadRoot: nil, threadSummary: nil))
    }

    private func remote(_ content: TimelineItemContent, sender: String = "@sender:example.test", isOwn: Bool = false,
                        profile: ProfileDetails = .ready(displayName: "发送者", displayNameAmbiguous: false,
                                                        avatarUrl: nil, status: nil, call: nil)) -> LatestEventValue {
        .remote(timestamp: 2_000, sender: sender, isOwn: isOwn, profile: profile, content: content)
    }

    private func map(_ content: TimelineItemContent, direct: Bool = false, encryption: RoomEncryptionState = .unencrypted,
                     sender: String = "@sender:example.test", isOwn: Bool = false,
                     profile: ProfileDetails = .ready(displayName: "发送者", displayNameAmbiguous: false,
                                                     avatarUrl: nil, status: nil, call: nil)) -> ForegroundBannerCandidate? {
        MatrixService.foregroundBannerCandidate(
            for: MatrixRoom(id: "!room:example.test", name: "测试房间", isDirect: direct, encryption: encryption),
            latest: remote(content, sender: sender, isOwn: isOwn, profile: profile), currentUserID: "@me:example.test")
    }
}
