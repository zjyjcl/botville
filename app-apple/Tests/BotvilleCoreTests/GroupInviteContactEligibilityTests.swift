// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import XCTest

@testable import BotvilleCore

final class GroupInviteContactEligibilityTests: XCTestCase {
    func testStrictJoinedDMReturnsOnlyCounterpart() {
        XCTAssertEqual(
            GroupInviteContactEligibility.counterpartID(
                isDirect: true, localUserID: "@me:example.test",
                joinedMemberIDs: ["@me:example.test", "@other:example.test"]
            ),
            "@other:example.test"
        )
    }

    func testRejectsNonDMAndIncompleteOrDuplicateSnapshots() {
        XCTAssertNil(
            GroupInviteContactEligibility.counterpartID(
                isDirect: false, localUserID: "@me:example.test",
                joinedMemberIDs: ["@me:example.test", "@other:example.test"]
            ))
        XCTAssertNil(
            GroupInviteContactEligibility.counterpartID(
                isDirect: true, localUserID: "@me:example.test",
                joinedMemberIDs: ["@me:example.test"]
            ))
        XCTAssertNil(
            GroupInviteContactEligibility.counterpartID(
                isDirect: true, localUserID: "@me:example.test",
                joinedMemberIDs: ["@me:example.test", "@me:example.test"]
            ))
        XCTAssertNil(
            GroupInviteContactEligibility.counterpartID(
                isDirect: true, localUserID: "@me:example.test",
                joinedMemberIDs: ["@other:example.test", "@third:example.test"]
            ))
    }
}
