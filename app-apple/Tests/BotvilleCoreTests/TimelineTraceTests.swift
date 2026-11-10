// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

#if DEBUG
@testable import BotvilleCore
import Foundation
import XCTest

final class TimelineTraceTests: XCTestCase {
    func testAllEventsHashIdentifiersAndOnlyUseAllowlistedFields() {
        let id = "synthetic-sensitive-identifier\nforged=value"
        let scope = UUID()
        for event in TimelineTrace.Event.allCases {
            for reason in TimelineTrace.Reason.allCases {
                let line = TimelineTrace.format(event, reason: reason, scope: scope,
                                                targetID: id, count: 30, now: 12.5)
                XCTAssertFalse(line.contains(id))
                XCTAssertFalse(line.contains("synthetic"))
                XCTAssertFalse(line.contains(scope.uuidString))
                XCTAssertFalse(line.contains("\n"))
                XCTAssertTrue(line.contains("t=12.5"))
                XCTAssertTrue(line.contains("event=\(event.rawValue)"))
                XCTAssertTrue(line.contains("target=\(TimelineTrace.fingerprint(id))"))
                XCTAssertTrue(line.contains("elapsed=- reached_start=- visible=- offset=-"))
            }
        }
        XCTAssertEqual(TimelineTrace.fingerprint(id).count, 8)
        XCTAssertEqual(TimelineTrace.fingerprint("abc"), "ba7816bf")
        XCTAssertEqual(TimelineTrace.fingerprint(nil), "-")
    }
}
#endif
