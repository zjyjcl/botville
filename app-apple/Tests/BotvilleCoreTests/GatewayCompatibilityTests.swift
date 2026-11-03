// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation
import XCTest

@testable import BotvilleCore

final class GatewayCompatibilityTests: XCTestCase {
    func testManifestSkipsMalformedCapabilitiesAndAcceptsFractionalGeneratedAt() throws {
        let manifest = try GatewayCoding.decoder.decode(
            GatewayCapabilityManifest.self,
            from: Data(
                """
                {"manifest_version":"1.0.0","contract_version":"0.1.0","generated_at":"2026-11-04T12:00:00.125Z","capabilities":[
                  {"id":"malformed","module":"future","version":"1","title":"Bad","summary":"","status":"enabled","entry":{},"scopes":[]},
                  {"id":"web.future","module":"future","version":"1","title":"Good","summary":"","status":"enabled","entry":{"type":"web","web_url":"https://example.test/"},"scopes":[]}
                ]}
                """.utf8))

        XCTAssertEqual(manifest.capabilities.map(\.id), ["web.future"])
        XCTAssertEqual(manifest.generatedAt.timeIntervalSince1970, 1_793_793_600.125, accuracy: 0.001)
    }

    func testFractionalSecondDatesDecodeForSessionCardAndChallenge() throws {
        let decoder = GatewayCoding.decoder
        let token = try decoder.decode(
            GatewaySessionToken.self,
            from: Data(
                """
                {"code":"once","code_ttl_seconds":60,"expires_at":"2026-11-04T12:00:00.250Z","audience":"memory.search","url":"https://example.test/?code=once","allowed_origins":["https://example.test"]}
                """.utf8))
        let card = try decoder.decode(
            GatewayCard.self,
            from: Data(
                """
                {"id":"card","card_schema_version":"1.0.0","type":"notification","title":"T","fallback_text":"T","priority":"normal","risk":"low","status":"active","blocks":[],"actions":[],"source":{"module":"test"},"expires_at":"2026-11-04T12:00:00.500Z"}
                """.utf8))
        let challenge = try decoder.decode(
            GatewayActionChallenge.self,
            from: Data(
                """
                {"challenge":"challenge","expires_at":"2026-11-04T12:00:00.750Z","card_id":"card","action_id":"approve","revision":1,"algorithm":"ES256"}
                """.utf8))

        XCTAssertEqual(token.expiresAt.timeIntervalSince1970, 1_793_793_600.25, accuracy: 0.001)
        XCTAssertEqual(try XCTUnwrap(card.expiresAt).timeIntervalSince1970, 1_793_793_600.5, accuracy: 0.001)
        XCTAssertEqual(challenge.expiresAt.timeIntervalSince1970, 1_793_793_600.75, accuracy: 0.001)
    }

    func testApprovalWithoutExpiryRemainsNonActionable() throws {
        let card = try GatewayCoding.decoder.decode(
            GatewayCard.self,
            from: Data(
                """
                {"id":"approval","card_schema_version":"1.0.0","type":"approval","title":"T","fallback_text":"T","priority":"normal","risk":"low","status":"active","blocks":[],"actions":[],"source":{"module":"test"},"revision":1}
                """.utf8))

        XCTAssertFalse(card.isActionable)
    }

    func testGatewayCodingAcceptsWholeSecondDatesAndRejectsInvalidDates() throws {
        let valid = try GatewayCoding.decoder.decode(
            GatewayCapabilityManifest.self,
            from: Data(
                """
                {"manifest_version":"1.0.0","contract_version":"0.1.0","generated_at":"2026-11-04T12:00:00Z","capabilities":[]}
                """.utf8))
        XCTAssertEqual(valid.generatedAt.timeIntervalSince1970, 1_793_793_600)

        XCTAssertThrowsError(
            try GatewayCoding.decoder.decode(
                GatewayCapabilityManifest.self,
                from: Data(
                    """
                    {"manifest_version":"1.0.0","contract_version":"0.1.0","generated_at":"not-a-date","capabilities":[]}
                    """.utf8)))
    }
}
