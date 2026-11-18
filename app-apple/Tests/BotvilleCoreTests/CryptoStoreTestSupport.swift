// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

@testable import BotvilleCore
import Foundation

extension SessionStoreDirectory {
    /// The only seed helper for an SDK store that already exists on disk: the
    /// store directory plus the crypto database file. Tests that restore a saved
    /// session with a fake SDK client need it; otherwise restore correctly treats
    /// the store as lost (#81). `bytes: 0` simulates an interrupted first build.
    func seedCryptoStoreForTesting(_ storeID: UUID, bytes: Int = 16) throws {
        let path = try create(for: storeID)
        try Data(repeating: 1, count: bytes).write(to: path.appendingPathComponent(Self.cryptoDatabaseFileName))
    }
}
