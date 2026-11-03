// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

@testable import BotvilleCore
import Foundation
import XCTest

final class PushRegistrationTests: XCTestCase {
    func testRegistrationEncodesMatrixPusherWireNames() throws {
        let registration = PushRegistration(
            appID: "org.example.botville",
            pushKey: "placeholder-device-key",
            appDisplayName: "botville",
            data: PushPusherData(url: try XCTUnwrap(URL(string: "https://push.example.org/_matrix/push/v1/notify"))),
            append: true,
            deviceDisplayName: "iPhone",
            language: "zh-Hans"
        )
        let json = try jsonObject(registration)

        XCTAssertEqual(json["app_id"] as? String, "org.example.botville")
        XCTAssertEqual(json["pushkey"] as? String, "placeholder-device-key")
        XCTAssertEqual(json["app_display_name"] as? String, "botville")
        XCTAssertEqual(json["kind"] as? String, "http")
        XCTAssertEqual(json["append"] as? Bool, true)
        XCTAssertEqual((json["data"] as? [String: Any])?["url"] as? String, "https://push.example.org/_matrix/push/v1/notify")
        XCTAssertEqual(json["device_display_name"] as? String, "iPhone")
        XCTAssertEqual(json["lang"] as? String, "zh-Hans")
    }

    func testDeletionEncodesNilKindAndNoPusherURL() throws {
        let deletion = PushRegistration.deletion(
            appID: "org.example.botville",
            pushKey: "placeholder-device-key",
            appDisplayName: "botville"
        )
        let json = try jsonObject(deletion)

        XCTAssertTrue(json.keys.contains("kind"))
        XCTAssertTrue(json["kind"] is NSNull)
        XCTAssertFalse(json.keys.contains("data"))
        XCTAssertEqual(json["append"] as? Bool, false)
    }

    private func jsonObject(_ value: PushRegistration) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(value)) as? [String: Any])
    }
}
