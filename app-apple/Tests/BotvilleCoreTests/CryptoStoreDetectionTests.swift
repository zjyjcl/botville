// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

@testable import BotvilleCore
import Foundation
import MatrixRustSDK
import XCTest

final class CryptoStoreDetectionTests: XCTestCase {
    private var temporary: URL!

    override func setUpWithError() throws {
        temporary = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent(".build/crypto-store-tests/\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: temporary, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: temporary)
    }

    func testOnlyNonEmptyRegularCryptoDatabaseCountsAsIntactAndCheckCreatesNothing() throws {
        let directory = SessionStoreDirectory(baseURL: temporary.appendingPathComponent("sessions"))
        let storeID = UUID()
        XCTAssertEqual(directory.cryptoStoreState(for: storeID), .missing)
        XCTAssertFalse(FileManager.default.fileExists(atPath: directory.baseURL.path), "detection must not create the store")

        let path = try directory.create(for: storeID)
        XCTAssertEqual(directory.cryptoStoreState(for: storeID), .missing)
        let file = path.appendingPathComponent(SessionStoreDirectory.cryptoDatabaseFileName)
        try FileManager.default.createDirectory(at: file, withIntermediateDirectories: false)
        XCTAssertEqual(directory.cryptoStoreState(for: storeID), .missing, "a directory with the database name is not a database")
        try FileManager.default.removeItem(at: file)
        let elsewhere = temporary.appendingPathComponent("real.sqlite3")
        try Data(repeating: 1, count: 16).write(to: elsewhere)
        try FileManager.default.createSymbolicLink(at: file, withDestinationURL: elsewhere)
        XCTAssertEqual(directory.cryptoStoreState(for: storeID), .missing, "a symlink is deliberately treated as lost")
        try FileManager.default.removeItem(at: file)
        try Data().write(to: file)
        XCTAssertEqual(directory.cryptoStoreState(for: storeID), .missing, "zero-byte database has no Olm account")
        try Data(repeating: 1, count: 16).write(to: file)
        XCTAssertEqual(directory.cryptoStoreState(for: storeID), .present)
        XCTAssertEqual(directory.cryptoStoreState(for: UUID()), .missing, "another store ID is unaffected")
    }

    /// Only a definite "does not exist" counts as missing. Permission / protected
    /// data errors (before first unlock) and anything unexpected are undetermined.
    func testMetadataErrorsOtherThanNotFoundAreUndetermined() {
        let directory = SessionStoreDirectory(baseURL: temporary)
        let cases: [(Error, SessionStoreDirectory.CryptoStoreState)] = [
            (CocoaError(.fileReadNoSuchFile), .missing),
            (CocoaError(.fileNoSuchFile), .missing),
            (NSError(domain: NSPOSIXErrorDomain, code: Int(ENOENT)), .missing),
            (NSError(domain: NSCocoaErrorDomain, code: CocoaError.fileReadNoSuchFile.rawValue,
                     userInfo: [NSUnderlyingErrorKey: NSError(domain: NSPOSIXErrorDomain, code: Int(ENOENT))]), .missing),
            (CocoaError(.fileReadNoPermission), .undetermined),
            (NSError(domain: NSPOSIXErrorDomain, code: Int(EACCES)), .undetermined),
            (NSError(domain: NSPOSIXErrorDomain, code: Int(EPERM)), .undetermined),
            (CocoaError(.fileReadUnknown), .undetermined),
            (URLError(.unknown), .undetermined),
        ]
        for (error, expected) in cases {
            XCTAssertEqual(directory.cryptoStoreState(for: UUID(), fileManager: ThrowingAttributesFileManager(error: error)),
                           expected, "\(error)")
        }
    }

    /// Guards the SDK-specific file name: the production client factory with the
    /// real SDK must create the database exactly where detection looks. Offline:
    /// restoring a fixture session builds local stores without any request.
    @MainActor
    func testRealSDKCreatesCryptoDatabaseWhereDetectionLooks() async throws {
        let directory = SessionStoreDirectory(baseURL: temporary.appendingPathComponent("sdk"))
        let storeID = UUID()
        let path = try directory.create(for: storeID)
        let service = MatrixService(sessionStore: InMemorySessionStore(), storeDirectory: directory, monitorNetwork: false)
        let client = try await service.clientFactory("https://matrix.invalid", path, Data(repeating: 3, count: 32))
        try await client.restoreSession(session: Session(
            accessToken: "fixture-only", refreshToken: nil, userId: "@probe:matrix.invalid", deviceId: "PROBE",
            homeserverUrl: "https://matrix.invalid", oauthData: nil, slidingSyncVersion: .none))
        XCTAssertEqual(directory.cryptoStoreState(for: storeID), .present,
                      "SDK crypto database name or location changed; update SessionStoreDirectory.cryptoDatabaseFileName")
    }
}

private final class ThrowingAttributesFileManager: FileManager {
    private let error: Error
    init(error: Error) { self.error = error; super.init() }
    override func attributesOfItem(atPath path: String) throws -> [FileAttributeKey: Any] { throw error }
}
