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
            data: try PushPusherData(url: XCTUnwrap(URL(string: "https://push.example.org/_matrix/push/v1/notify"))),
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
        XCTAssertEqual((json["data"] as? [String: Any])?["format"] as? String, "event_id_only")
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

    func testPusherDataRejectsNonHTTPSCredentialedAndAmbiguousURLs() throws {
        let rejected = [
            "http://push.example.org/notify",
            "https://user:password@push.example.org/notify",
            "https:/missing-host",
            "mailto:push@example.org",
            "https://push.example.org:0/notify",
            "https://push.example.org:/notify",
            "https://push.example.org:999999999999999999999/notify",
            "https://push.example.org%40evil.example/notify",
            "https://user%40push.example.org/notify",
            "https://push.example.org%00.evil.example/notify",
            "https://push.example.org%20.evil.example/notify",
            "https://push.example.org%09.evil.example/notify",
            "https://push.example.org%5c.evil.example/notify",
            "https://push.example.org%2f.evil.example/notify",
            "https://[fe80::1%25en0]/notify",
            "https://push.example.org/#fragment",
            "https://push.example.org/a b",
            "https://push.example.org/a%20b",
            "https://push.example.org/?next=%09"
        ]

        for value in rejected {
            XCTAssertThrowsError(try PushPusherData(url: XCTUnwrap(URL(string: value)))) { error in
                XCTAssertEqual(error as? PushPusherDataError, .invalidURL, value)
            }
        }
    }

    func testPusherDataRejectsInvalidURLWhenDecoded() throws {
        let rejected = [
            "http://push.example.org/notify",
            "https://push.example.org%40evil.example/notify",
            "https://push.example.org:999999999999999999999/notify",
            "https://push.example.org/a b",
            "https://push.example.org/a\nb",
            "https://push.example.org/a\tb",
            "https://push.example.org/a\u{0085}b",
            "https://push.example.org/?next=%09"
        ]
        for url in rejected {
            let data = try JSONSerialization.data(withJSONObject: ["url": url])
            XCTAssertThrowsError(try JSONDecoder().decode(PushPusherData.self, from: data), url) { error in
                XCTAssertEqual(error as? PushPusherDataError, .invalidURL, url)
            }
        }
    }

    func testPusherDataAndRegistrationRoundTrip() throws {
        let data = try PushPusherData(url: XCTUnwrap(URL(string: "https://push.example.org/notify")), format: .eventIDOnly)
        let registration = PushRegistration(
            appID: "org.example.botville",
            pushKey: "placeholder-device-key",
            appDisplayName: "botville",
            data: data
        )

        XCTAssertEqual(try JSONDecoder().decode(PushPusherData.self, from: JSONEncoder().encode(data)), data)
        XCTAssertEqual(try JSONDecoder().decode(PushRegistration.self, from: JSONEncoder().encode(registration)), registration)
    }

    func testPusherDataDefaultsToEventIDOnly() throws {
        let data = try PushPusherData(url: XCTUnwrap(URL(string: "https://push.example.org/notify")))

        XCTAssertEqual(data.format, .eventIDOnly)
    }

    func testPusherDataDecodesMissingOrNullFormatAsEventIDOnly() throws {
        for json in [
            #"{"url":"https://push.example.org/notify"}"#,
            #"{"url":"https://push.example.org/notify","format":null}"#
        ] {
            let data = try JSONDecoder().decode(PushPusherData.self, from: Data(json.utf8))
            let encoded = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(data)) as? [String: Any])

            XCTAssertEqual(data.format, .eventIDOnly)
            XCTAssertEqual(encoded["format"] as? String, "event_id_only")
        }
    }

    func testPusherDataRejectsUnknownFormat() throws {
        let json = Data(#"{"url":"https://push.example.org/notify","format":"future_format"}"#.utf8)

        XCTAssertThrowsError(try JSONDecoder().decode(PushPusherData.self, from: json)) { error in
            guard case DecodingError.dataCorrupted(let context) = error else {
                return XCTFail("Expected unknown format to fail decoding, got \(error)")
            }
            XCTAssertEqual(context.codingPath.map(\.stringValue), ["format"])
        }
    }

    func testRegistrationRejectsNestedUnknownPusherFormat() throws {
        let json = Data("""
        {"app_id":"org.example.botville","pushkey":"placeholder-device-key","app_display_name":"botville","kind":"http","append":false,"data":{"url":"https://push.example.org/notify","format":"future_format"}}
        """.utf8)

        XCTAssertThrowsError(try JSONDecoder().decode(PushRegistration.self, from: json)) { error in
            guard case DecodingError.dataCorrupted(let context) = error else {
                return XCTFail("Expected unknown format to fail decoding, got \(error)")
            }
            XCTAssertEqual(context.codingPath.map(\.stringValue), ["data", "format"])
        }
    }

    func testPusherDataAcceptsCaseInsensitiveHTTPSAndMaximumValidPort() throws {
        let url = try XCTUnwrap(URL(string: "HTTPS://push.example.org:65535/notify"))
        XCTAssertEqual(try PushPusherData(url: url).url.absoluteString, "https://push.example.org:65535/notify")
    }

    func testRegistrationRejectsNestedInvalidPusherURL() throws {
        let data = Data("""
        {"app_id":"org.example.botville","pushkey":"placeholder-device-key","app_display_name":"botville","kind":"http","append":false,"data":{"url":"https://push.example.org%40evil.example/notify"}}
        """.utf8)

        XCTAssertThrowsError(try JSONDecoder().decode(PushRegistration.self, from: data)) { error in
            XCTAssertEqual(error as? PushPusherDataError, .invalidURL)
        }
    }

    func testNoopRegistrarAcceptsValidatedHTTPSRegistration() async throws {
        let registration = PushRegistration(
            appID: "org.example.botville",
            pushKey: "placeholder-device-key",
            appDisplayName: "botville",
            data: try PushPusherData(url: XCTUnwrap(URL(string: "https://push.example.org/notify")))
        )

        try await NoopPushRegistrar().register(registration)
    }

    private func jsonObject(_ value: PushRegistration) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(value)) as? [String: Any])
    }
}
