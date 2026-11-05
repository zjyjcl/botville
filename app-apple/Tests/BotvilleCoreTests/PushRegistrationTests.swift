// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

@testable import BotvilleCore
import Foundation
import XCTest

final class PushRegistrationTests: XCTestCase {
    func testRegistrationEncodesMatrixPusherWireNames() throws {
        let registration = PushRegistration(
            appID: "com.zjyjcl.botville.apns",
            pushKey: "placeholder-device-key",
            appDisplayName: "botville",
            data: try PushPusherData(url: XCTUnwrap(URL(string: "https://push.example.org/_matrix/push/v1/notify"))),
            append: false,
            deviceDisplayName: "iPhone",
            language: "zh-CN"
        )
        let json = try jsonObject(registration)

        XCTAssertEqual(json["app_id"] as? String, "com.zjyjcl.botville.apns")
        XCTAssertEqual(json["pushkey"] as? String, "placeholder-device-key")
        XCTAssertEqual(json["app_display_name"] as? String, "botville")
        XCTAssertEqual(json["kind"] as? String, "http")
        XCTAssertEqual(json["append"] as? Bool, false)
        XCTAssertEqual((json["data"] as? [String: Any])?["url"] as? String, "https://push.example.org/_matrix/push/v1/notify")
        XCTAssertEqual((json["data"] as? [String: Any])?["format"] as? String, "event_id_only")
        let aps = try XCTUnwrap(((json["data"] as? [String: Any])?["default_payload"] as? [String: Any])?["aps"] as? [String: Any])
        XCTAssertEqual(Set(aps.keys), ["mutable-content", "alert"])
        XCTAssertEqual(aps["mutable-content"] as? Int, 1)
        XCTAssertEqual((aps["alert"] as? [String: Any])?["body"] as? String, "…")
        XCTAssertEqual(json["device_display_name"] as? String, "iPhone")
        XCTAssertEqual(json["lang"] as? String, "zh-CN")
    }

    func testDeletionEncodesNilKindAndNoPusherURL() throws {
        let deletion = PushRegistration.deletion(
            appID: "com.zjyjcl.botville.apns",
            pushKey: "placeholder-device-key",
            appDisplayName: "botville"
        )
        let json = try jsonObject(deletion)

        XCTAssertEqual(json["app_id"] as? String, "com.zjyjcl.botville.apns")
        XCTAssertEqual(json["pushkey"] as? String, "placeholder-device-key")
        XCTAssertEqual(json["app_display_name"] as? String, "botville")
        XCTAssertTrue(json.keys.contains("kind"))
        XCTAssertTrue(json["kind"] is NSNull)
        XCTAssertFalse(json.keys.contains("data"))
        XCTAssertEqual(json["append"] as? Bool, false)
    }

    func testPusherDataRejectsNonHTTPSCredentialedAndAmbiguousURLs() throws {
        let rejected = [
            "http://push.example.org/_matrix/push/v1/notify",
            "https://user:password@push.example.org/_matrix/push/v1/notify",
            "https:/missing-host",
            "mailto:push@example.org",
            "https://push.example.org:0/_matrix/push/v1/notify",
            "https://push.example.org:/_matrix/push/v1/notify",
            "https://push.example.org:999999999999999999999/_matrix/push/v1/notify",
            "https://push.example.org%40evil.example/_matrix/push/v1/notify",
            "https://user%40push.example.org/_matrix/push/v1/notify",
            "https://push.example.org%00.evil.example/_matrix/push/v1/notify",
            "https://push.example.org%20.evil.example/_matrix/push/v1/notify",
            "https://push.example.org%09.evil.example/_matrix/push/v1/notify",
            "https://push.example.org%5c.evil.example/_matrix/push/v1/notify",
            "https://push.example.org%2f.evil.example/_matrix/push/v1/notify",
            "https://[fe80::1%25en0]/_matrix/push/v1/notify",
            "https://push.example.org/_matrix/push/v1/notify#fragment",
            "https://push.example.org/_matrix/push/v1/notify bad",
            "https://push.example.org/_matrix/push/v1/notify%20bad",
            "https://push.example.org/_matrix/push/v1/notify?next=%09",
            "https://push.example.org/wrong"
        ]

        for value in rejected {
            XCTAssertThrowsError(try PushPusherData(url: XCTUnwrap(URL(string: value)))) { error in
                XCTAssertEqual(error as? PushPusherDataError, .invalidURL, value)
            }
        }
    }

    func testPusherDataRejectsInvalidURLWhenDecoded() throws {
        let rejected = [
            "http://push.example.org/_matrix/push/v1/notify",
            "https://push.example.org%40evil.example/_matrix/push/v1/notify",
            "https://push.example.org:999999999999999999999/_matrix/push/v1/notify",
            "https://push.example.org/_matrix/push/v1/notify bad",
            "https://push.example.org/a\nb",
            "https://push.example.org/a\tb",
            "https://push.example.org/a\u{0085}b",
            "https://push.example.org/_matrix/push/v1/notify?next=%09"
        ]
        for url in rejected {
            let data = try JSONSerialization.data(withJSONObject: ["url": url])
            XCTAssertThrowsError(try JSONDecoder().decode(PushPusherData.self, from: data), url) { error in
                XCTAssertEqual(error as? PushPusherDataError, .invalidURL, url)
            }
        }
    }

    func testPusherDataAndRegistrationRoundTrip() throws {
        let data = try PushPusherData(url: XCTUnwrap(URL(string: "https://push.example.org/_matrix/push/v1/notify")), format: .eventIDOnly)
        let registration = PushRegistration(
            appID: "com.zjyjcl.botville.apns",
            pushKey: "placeholder-device-key",
            appDisplayName: "botville",
            data: data
        )

        XCTAssertEqual(try JSONDecoder().decode(PushPusherData.self, from: JSONEncoder().encode(data)), data)
        XCTAssertEqual(try JSONDecoder().decode(PushRegistration.self, from: JSONEncoder().encode(registration)), registration)
    }

    func testPusherDataDefaultsToEventIDOnly() throws {
        let data = try PushPusherData(url: XCTUnwrap(URL(string: "https://push.example.org/_matrix/push/v1/notify")))

        XCTAssertEqual(data.format, .eventIDOnly)
        XCTAssertEqual(data.defaultPayload, PushDefaultPayload())
    }

    func testPusherDataDecodesMissingOrNullFormatAsEventIDOnly() throws {
        for json in [
            #"{"url":"https://push.example.org/_matrix/push/v1/notify"}"#,
            #"{"url":"https://push.example.org/_matrix/push/v1/notify","format":null}"#
        ] {
            let data = try JSONDecoder().decode(PushPusherData.self, from: Data(json.utf8))
            let encoded = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(data)) as? [String: Any])

            XCTAssertEqual(data.format, .eventIDOnly)
            XCTAssertEqual(encoded["format"] as? String, "event_id_only")
        }
    }

    func testPusherDataDecodesOnlyTheExactNotificationServiceDefaultPayload() throws {
        let exact = Data(#"{"url":"https://push.example.org/_matrix/push/v1/notify","format":"event_id_only","default_payload":{"aps":{"mutable-content":1,"alert":{"body":"…"}}}}"#.utf8)
        let payload = try JSONDecoder().decode(PushPusherData.self, from: exact)
        XCTAssertEqual(payload.defaultPayload, PushDefaultPayload())

        let encoded = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(payload)) as? [String: Any])
        let defaultPayload = try XCTUnwrap(encoded["default_payload"] as? [String: Any])
        XCTAssertEqual(Set(defaultPayload.keys), ["aps"])
        let aps = try XCTUnwrap(defaultPayload["aps"] as? [String: Any])
        XCTAssertEqual(Set(aps.keys), ["mutable-content", "alert"])
        let alert = try XCTUnwrap(aps["alert"] as? [String: Any])
        XCTAssertEqual(Set(alert.keys), ["body"])
        let body = try XCTUnwrap(alert["body"] as? String)
        XCTAssertEqual(body.unicodeScalars.map(\.value), [0x2026])

        for invalid in [
            #"{"aps":{"mutable-content":1,"alert":{"body":"新消息"}}}"#,
            #"{"aps":{"mutable-content":0,"alert":{"body":"…"}}}"#,
            #"{"aps":{"mutable-content":1,"alert":{"body":"…"}},"extra":null}"#,
            #"{"aps":{"mutable-content":1,"alert":{"body":"…"},"sound":"default"}}"#,
            #"{"aps":{"mutable-content":1,"alert":{"body":"…","title":"extra"}}}"#
        ] {
            let json = Data("{\"url\":\"https://push.example.org/_matrix/push/v1/notify\",\"default_payload\":\(invalid)}".utf8)
            XCTAssertThrowsError(try JSONDecoder().decode(PushPusherData.self, from: json), invalid)
        }
    }

    func testLegacyPusherDataNormalizesMissingOrNullDefaultPayload() throws {
        for json in [
            #"{"url":"https://push.example.org/_matrix/push/v1/notify"}"#,
            #"{"url":"https://push.example.org/_matrix/push/v1/notify","default_payload":null}"#
        ] {
            let data = try JSONDecoder().decode(PushPusherData.self, from: Data(json.utf8))
            XCTAssertEqual(data.defaultPayload, PushDefaultPayload())
            let encoded = try jsonObject(data)
            let payload = try XCTUnwrap(encoded["default_payload"] as? [String: Any])
            XCTAssertEqual(Set(payload.keys), ["aps"])
            let aps = try XCTUnwrap(payload["aps"] as? [String: Any])
            XCTAssertEqual(aps["mutable-content"] as? Int, 1)
            XCTAssertEqual((aps["alert"] as? [String: Any])?["body"] as? String, "…")
        }
    }

    func testPusherDataRejectsUnknownFormat() throws {
        let json = Data(#"{"url":"https://push.example.org/_matrix/push/v1/notify","format":"future_format"}"#.utf8)

        XCTAssertThrowsError(try JSONDecoder().decode(PushPusherData.self, from: json)) { error in
            guard case DecodingError.dataCorrupted(let context) = error else {
                return XCTFail("Expected unknown format to fail decoding, got \(error)")
            }
            XCTAssertEqual(context.codingPath.map(\.stringValue), ["format"])
        }
    }

    func testRegistrationRejectsNestedUnknownPusherFormat() throws {
        let json = Data("""
        {"app_id":"com.zjyjcl.botville.apns","pushkey":"placeholder-device-key","app_display_name":"botville","kind":"http","append":false,"data":{"url":"https://push.example.org/_matrix/push/v1/notify","format":"future_format"}}
        """.utf8)

        XCTAssertThrowsError(try JSONDecoder().decode(PushRegistration.self, from: json)) { error in
            guard case DecodingError.dataCorrupted(let context) = error else {
                return XCTFail("Expected unknown format to fail decoding, got \(error)")
            }
            XCTAssertEqual(context.codingPath.map(\.stringValue), ["data", "format"])
        }
    }

    func testPusherDataAcceptsCaseInsensitiveHTTPSAndMaximumValidPort() throws {
        let url = try XCTUnwrap(URL(string: "HTTPS://push.example.org:65535/_matrix/push/v1/notify"))
        XCTAssertEqual(try PushPusherData(url: url).url.absoluteString, "https://push.example.org:65535/_matrix/push/v1/notify")
    }

    func testRegistrationRejectsNestedInvalidPusherURL() throws {
        let data = Data("""
        {"app_id":"com.zjyjcl.botville.apns","pushkey":"placeholder-device-key","app_display_name":"botville","kind":"http","append":false,"data":{"url":"https://push.example.org%40evil.example/notify"}}
        """.utf8)

        XCTAssertThrowsError(try JSONDecoder().decode(PushRegistration.self, from: data)) { error in
            XCTAssertEqual(error as? PushPusherDataError, .invalidURL)
        }
    }

    func testNoopRegistrarAcceptsValidatedHTTPSRegistration() async throws {
        let registration = PushRegistration(
            appID: "com.zjyjcl.botville.apns",
            pushKey: "placeholder-device-key",
            appDisplayName: "botville",
            data: try PushPusherData(url: XCTUnwrap(URL(string: "https://push.example.org/_matrix/push/v1/notify")))
        )

        try await NoopPushRegistrar().register(registration)
    }

    func testDeviceTokenUsesStandardBase64WithPadding() {
        XCTAssertEqual(DevicePushToken.base64(Data()), "")
        XCTAssertEqual(
            DevicePushToken.base64(Data(0x00...0x1F)),
            "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
        )
    }

    func testReleaseNotifyURLPolicyRejectsHTTPRegardlessOfOptIn() throws {
        for allowHTTP in [false, true] {
            XCTAssertThrowsError(try PushNotifyURLPolicy.validatedURL(
                "http://localhost:5000/_matrix/push/v1/notify", allowHTTP: allowHTTP, isDebugBuild: false
            )) { error in
                XCTAssertEqual(error as? PushPusherDataError, .invalidURL)
            }
            XCTAssertEqual(try PushNotifyURLPolicy.validatedURL(
                "https://sygnal.example.invalid/_matrix/push/v1/notify", allowHTTP: allowHTTP, isDebugBuild: false
            ).scheme, "https")
        }
    }

    func testDebugNotifyURLPolicyRequiresExplicitOptIn() throws {
        let http = "http://sygnal:5000/_matrix/push/v1/notify"
        XCTAssertThrowsError(try PushNotifyURLPolicy.validatedURL(
            http, allowHTTP: false, isDebugBuild: true
        ))
        XCTAssertEqual(try PushNotifyURLPolicy.validatedURL(
            http, allowHTTP: true, isDebugBuild: true
        ).absoluteString, http)
    }

    func testReleasePusherDataRejectsHTTPRegardlessOfOptIn() throws {
        let http = try XCTUnwrap(URL(string: "http://sygnal:5000/_matrix/push/v1/notify"))
        let https = try XCTUnwrap(URL(string: "https://sygnal.example.invalid/_matrix/push/v1/notify"))
        for allowHTTP in [false, true] {
            XCTAssertThrowsError(try PushPusherData(url: http, allowHTTP: allowHTTP, isDebugBuild: false)) { error in
                XCTAssertEqual(error as? PushPusherDataError, .invalidURL)
            }
            XCTAssertEqual(try PushPusherData(url: https, allowHTTP: allowHTTP, isDebugBuild: false).url, https)
        }
    }

    func testDebugPusherDataRequiresExplicitOptIn() throws {
        let http = try XCTUnwrap(URL(string: "http://sygnal:5000/_matrix/push/v1/notify"))
        XCTAssertThrowsError(try PushPusherData(url: http, allowHTTP: false, isDebugBuild: true)) { error in
            XCTAssertEqual(error as? PushPusherDataError, .invalidURL)
        }
        let data = try PushPusherData(url: http, allowHTTP: true, isDebugBuild: true)
        XCTAssertEqual(data.url, http)
        XCTAssertEqual(data.format, .eventIDOnly)
    }

    func testPublicPushHTTPEntryPointsUseCurrentBuildConfiguration() throws {
        let http = "http://sygnal:5000/_matrix/push/v1/notify"
        let url = try XCTUnwrap(URL(string: http))
        for allowHTTP in [false, true] {
            #if DEBUG
            let permitsHTTP = allowHTTP
            #else
            let permitsHTTP = false
            #endif
            if permitsHTTP {
                XCTAssertEqual(try PushNotifyURLPolicy.validatedURL(http, allowHTTP: allowHTTP), url)
                XCTAssertEqual(try PushNotifyURLPolicy.validate(http, allowHTTP: allowHTTP), url)
                XCTAssertEqual(try PushPusherData(url: url, allowHTTP: allowHTTP).url, url)
            } else {
                XCTAssertThrowsError(try PushNotifyURLPolicy.validatedURL(http, allowHTTP: allowHTTP))
                XCTAssertThrowsError(try PushNotifyURLPolicy.validate(http, allowHTTP: allowHTTP))
                XCTAssertThrowsError(try PushPusherData(url: url, allowHTTP: allowHTTP))
            }
        }
    }

    func testPushHTTPPolicyDefaultsToCurrentBuildConfiguration() {
        XCTAssertFalse(PushHTTPPolicy.permitsHTTP(false))
        #if DEBUG
        XCTAssertTrue(PushHTTPPolicy.permitsHTTP(true))
        #else
        XCTAssertFalse(PushHTTPPolicy.permitsHTTP(true))
        #endif
    }

    func testNotifyURLPolicyRejectsUnsafeURLsEvenWithHTTPOptIn() throws {
        let https = "https://sygnal.example.invalid/_matrix/push/v1/notify"
        XCTAssertEqual(try PushNotifyURLPolicy.validate(https).absoluteString, https)
        XCTAssertThrowsError(try PushNotifyURLPolicy.validate("http://sygnal.example.invalid/_matrix/push/v1/notify"))
        for unsafe in [
            "https://user:password@sygnal.example.invalid/_matrix/push/v1/notify",
            "https://sygnal.example.invalid/_matrix/push/v1/notify?x=1",
            "https://sygnal.example.invalid/_matrix/push/v1/notify#fragment",
            "https://sygnal.example.invalid/other",
            "https://sygnal.example.invalid/_matrix/push/v1/%6Eotify",
            "https://sygnal.example.invalid/_matrix/push/v1/notify/",
            "https://sygnal.example.invalid/_matrix/push/v1/notify?",
            "https://sygnal.example.invalid/_matrix/push/v1/notify#"
        ] {
            XCTAssertThrowsError(try PushNotifyURLPolicy.validate(unsafe), unsafe)
            XCTAssertThrowsError(try PushNotifyURLPolicy.validate(unsafe, allowHTTP: true), unsafe)
        }
    }

    func testPusherListMatchesOnlyBothIdentifiers() throws {
        let response = try JSONDecoder().decode(MatrixPushersResponse.self, from: Data(#"{"pushers":[{"app_id":"com.zjyjcl.botville.apns","pushkey":"abc"},{"app_id":"other","pushkey":"def","extra":true}]}"#.utf8))
        XCTAssertTrue(response.contains(appID: "com.zjyjcl.botville.apns", pushKey: "abc"))
        XCTAssertFalse(response.contains(appID: "com.zjyjcl.botville.apns", pushKey: "def"))
    }

    func testURLSessionTransportUsesBasePathBearerAndRequiredWireBody() async throws {
        let recorder = URLProtocolRecorder()
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [RecordingURLProtocol.self]
        let session = URLSession(configuration: configuration)
        RecordingURLProtocol.recorder = recorder
        defer { RecordingURLProtocol.recorder = nil }
        let transport = URLSessionMatrixPusherTransport(session: session)
        let registration = PushRegistration(appID: "com.zjyjcl.botville.apns", pushKey: "AKs=", appDisplayName: "botville",
                                            data: try PushPusherData(url: XCTUnwrap(URL(string: "https://sygnal.example.invalid/_matrix/push/v1/notify"))),
                                            append: false, deviceDisplayName: "iPhone", language: "zh-CN")
        try await transport.setPusher(homeserver: XCTUnwrap(URL(string: "https://matrix.test/base")), accessToken: "token", registration: registration)
        let request = try XCTUnwrap(recorder.requests.first)
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(request.url?.absoluteString, "https://matrix.test/base/_matrix/client/v3/pushers/set")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer token")
        let body = try XCTUnwrap(JSONSerialization.jsonObject(with: XCTUnwrap(request.httpBody)) as? [String: Any])
        XCTAssertEqual(body["kind"] as? String, "http")
        XCTAssertEqual(body["app_id"] as? String, "com.zjyjcl.botville.apns")
        XCTAssertEqual(body["pushkey"] as? String, "AKs=")
        XCTAssertEqual(body["app_display_name"] as? String, "botville")
        XCTAssertEqual(body["device_display_name"] as? String, "iPhone")
        XCTAssertEqual(body["lang"] as? String, "zh-CN")
        XCTAssertEqual(body["append"] as? Bool, false)
        XCTAssertEqual((body["data"] as? [String: Any])?["url"] as? String, "https://sygnal.example.invalid/_matrix/push/v1/notify")
        XCTAssertEqual((body["data"] as? [String: Any])?["format"] as? String, "event_id_only")
        let payload = try XCTUnwrap((body["data"] as? [String: Any])?["default_payload"] as? [String: Any])
        XCTAssertEqual(Set(payload.keys), ["aps"])
        let aps = try XCTUnwrap(payload["aps"] as? [String: Any])
        XCTAssertEqual(Set(aps.keys), ["mutable-content", "alert"])
        XCTAssertEqual(aps["mutable-content"] as? Int, 1)
        XCTAssertEqual((aps["alert"] as? [String: Any])?["body"] as? String, "…")

        _ = try await transport.pushers(homeserver: XCTUnwrap(URL(string: "https://matrix.test/base")), accessToken: "token")
        let get = try XCTUnwrap(recorder.requests.last)
        XCTAssertEqual(get.httpMethod, "GET")
        XCTAssertEqual(get.url?.absoluteString, "https://matrix.test/base/_matrix/client/v3/pushers")
        XCTAssertEqual(get.value(forHTTPHeaderField: "Authorization"), "Bearer token")
    }

    private func jsonObject(_ value: some Encodable) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(value)) as? [String: Any])
    }
}

private final class URLProtocolRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var values: [URLRequest] = []
    var requests: [URLRequest] { lock.withLock { values } }
    func append(_ request: URLRequest) { lock.withLock { values.append(request) } }
}

private final class RecordingURLProtocol: URLProtocol {
    static var recorder: URLProtocolRecorder?
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        var normalized = request
        if normalized.httpBody == nil, let stream = normalized.httpBodyStream {
            stream.open()
            defer { stream.close() }
            var data = Data()
            let buffer = UnsafeMutablePointer<UInt8>.allocate(capacity: 4096)
            defer { buffer.deallocate() }
            while stream.hasBytesAvailable {
                let count = stream.read(buffer, maxLength: 4096)
                if count <= 0 { break }
                data.append(buffer, count: count)
            }
            normalized.httpBody = data
        }
        Self.recorder?.append(normalized)
        let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        if request.httpMethod == "GET" {
            client?.urlProtocol(self, didLoad: Data(#"{"pushers":[]}"#.utf8))
        }
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}
