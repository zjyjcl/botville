// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

@testable import BotvilleCore
import Foundation

extension SessionStoreDirectory {
    /// Tests that restore a saved session with a fake SDK client need an intact
    /// local store; otherwise restore correctly treats it as lost (#81).
    func seedCryptoStoreForTesting(_ storeID: UUID) throws {
        let path = try create(for: storeID)
        try Data(repeating: 1, count: 16).write(to: path.appendingPathComponent(Self.cryptoDatabaseFileName))
    }
}
