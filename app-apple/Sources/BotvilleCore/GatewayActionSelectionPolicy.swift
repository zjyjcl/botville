// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation

/// UI-facing gate for card actions. Transport repeats these checks so an
/// alternate caller cannot turn a presentation choice into a submission.
public enum GatewayActionSelectionDecision: Sendable, Equatable {
    case ignore
    case requireUnavailableAuthorization
    case requireConfirmation
    case perform
}

public enum GatewayActionSelectionPolicy {
    public static func decision(
        for card: GatewayCard, action: GatewayCardAction
    ) -> GatewayActionSelectionDecision {
        guard card.isActionable, action.isKnownKind, action.disabledReason == nil,
            card.actions.contains(action)
        else {
            return .ignore
        }
        if action.effectiveRisk(cardRisk: card.effectiveRisk) == .high
            || action.confirmation?.biometric == true
        {
            return .requireUnavailableAuthorization
        }
        return action.confirmation?.required == true ? .requireConfirmation : .perform
    }
}
