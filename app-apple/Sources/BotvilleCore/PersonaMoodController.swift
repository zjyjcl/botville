// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Combine
import Foundation

/// Owner-facing state that updates immediately and restores the last server
/// value if the corresponding PATCH fails.
@MainActor
public final class PersonaMoodController: ObservableObject {
    @Published public private(set) var state: PersonaState?
    @Published public private(set) var isUnavailable = false
    @Published public private(set) var isLoading = false
    @Published public private(set) var isUpdating = false
    @Published public private(set) var errorMessage: String?

    private let personaID: String
    private let client: any GatewayClient

    public init(personaID: String, client: any GatewayClient) {
        self.personaID = personaID
        self.client = client
    }

    public func load() async {
        guard !isUpdating, !isLoading else { return }
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            let result = try await client.personaState(id: personaID)
            guard !Task.isCancelled else { return }
            guard result.personaID == personaID else { throw GatewayClientError.invalidResponse }
            state = result
            isUnavailable = false
        } catch let error as GatewayClientError where error == .problem(status: 404, code: "mood.disabled") {
            guard !Task.isCancelled else { return }
            state = nil
            isUnavailable = true
        } catch {
            guard !Task.isCancelled else { return }
            isUnavailable = false
            errorMessage = Self.message(for: error)
        }
    }

    public func setMood(_ mood: PersonaMood) async {
        await update(PersonaStatePatch(mood: mood))
    }

    public func setMoodPublic(_ moodPublic: Bool) async {
        await update(PersonaStatePatch(moodPublic: moodPublic))
    }

    private func update(_ patch: PersonaStatePatch) async {
        guard let previous = state, !isUpdating, !isLoading else { return }
        isUpdating = true
        defer { isUpdating = false }
        errorMessage = nil
        state = previous.applying(patch)
        do {
            let result = try await client.updatePersonaState(id: personaID, patch: patch)
            guard !Task.isCancelled else { state = previous; return }
            guard result.personaID == personaID else { throw GatewayClientError.invalidResponse }
            state = result
        } catch let error as GatewayClientError where error == .problem(status: 404, code: "mood.disabled") {
            guard !Task.isCancelled else { state = previous; return }
            state = nil
            isUnavailable = true
        } catch {
            guard !Task.isCancelled else {
                state = previous
                return
            }
            state = previous
            errorMessage = Self.message(for: error)
        }
    }

    private static func message(for error: Error) -> String {
        if let error = error as? GatewayClientError, error.httpStatus == 429 {
            return "操作太频繁，请稍后再试"
        }
        return "操作失败，请稍后重试"
    }
}

private extension PersonaState {
    func applying(_ patch: PersonaStatePatch) -> PersonaState {
        PersonaState(personaID: personaID, mood: patch.mood ?? mood,
                     moodPublic: patch.moodPublic ?? moodPublic, updatedAt: updatedAt)
    }
}
