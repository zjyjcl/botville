// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import XCTest

@testable import BotvilleCore

final class GatewayActionSelectionPolicyTests: XCTestCase {
    func testHighAndBiometricActionsDoNotReachNormalExecution() throws {
        let high = try card(actionRisk: "unknown")
        XCTAssertEqual(
            GatewayActionSelectionPolicy.decision(for: high, action: high.actions[0]),
            .requireUnavailableAuthorization)

        let biometric = try card(actionRisk: "low", confirmation: #"{"required":false,"biometric":true}"#)
        XCTAssertEqual(
            GatewayActionSelectionPolicy.decision(for: biometric, action: biometric.actions[0]),
            .requireUnavailableAuthorization)
    }

    func testConfirmationAndDisabledActionsHaveExplicitDecisions() throws {
        let confirmation = try card(actionRisk: "low", confirmation: #"{"required":true}"#)
        XCTAssertEqual(
            GatewayActionSelectionPolicy.decision(for: confirmation, action: confirmation.actions[0]),
            .requireConfirmation)

        let disabled = try card(actionRisk: "low", disabledReason: "policy")
        XCTAssertEqual(
            GatewayActionSelectionPolicy.decision(for: disabled, action: disabled.actions[0]), .ignore)
    }

    private func card(
        actionRisk: String, confirmation: String? = nil, disabledReason: String? = nil
    ) throws -> GatewayCard {
        let confirmationJSON = confirmation.map { ",\"confirmation\":\($0)" } ?? ""
        let disabledJSON = disabledReason.map { ",\"disabled_reason\":\"\($0)\"" } ?? ""
        let text = """
        {"id":"card","card_schema_version":"1.0.0","type":"notification","title":"Card","fallback_text":"Card","priority":"normal","risk":"low","status":"active","blocks":[],"actions":[{"id":"action","label":"Action","kind":"deny","style":"secondary","risk":"\(actionRisk)"\(confirmationJSON)\(disabledJSON)}],"source":{"module":"test"},"revision":1}
        """
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return try decoder.decode(GatewayCard.self, from: Data(text.utf8))
    }
}
