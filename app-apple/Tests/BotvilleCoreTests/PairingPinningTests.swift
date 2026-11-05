// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

@testable import BotvilleCore
import CryptoKit
import Foundation
import XCTest

final class PairingPinningTests: XCTestCase {
    func testExtractsSPKIFromFreshTemporaryCertificate() throws {
        let directory = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent(".build/pairing-test-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let key = directory.appendingPathComponent("key.pem")
        let pem = directory.appendingPathComponent("cert.pem")
        let der = directory.appendingPathComponent("cert.der")
        try run("/usr/bin/openssl", ["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key.path, "-out", pem.path, "-subj", "/CN=pairing-test.invalid", "-days", "1"])
        try run("/usr/bin/openssl", ["x509", "-in", pem.path, "-outform", "DER", "-out", der.path])
        let publicKeyPEM = try runOutput("/usr/bin/openssl", ["x509", "-in", pem.path, "-pubkey", "-noout"])
        let expectedSPKI = try XCTUnwrap(Data(base64Encoded: String(decoding: publicKeyPEM, as: UTF8.self)
            .split(whereSeparator: \.isNewline).filter { !$0.hasPrefix("---") }.joined()))
        let extracted = try XCTUnwrap(PairingTLSDelegate.subjectPublicKeyInfo(Data(contentsOf: der)))
        let expected = Data(SHA256.hash(data: expectedSPKI))
        XCTAssertEqual(Data(SHA256.hash(data: extracted)), expected)
        XCTAssertTrue(PairingTLSDelegate.matchesLeafCertificateDER(try Data(contentsOf: der), fingerprint: expected))
        XCTAssertFalse(PairingTLSDelegate.matchesLeafCertificateDER(try Data(contentsOf: der), fingerprint: Data(repeating: 0, count: 32)))
    }

    private func run(_ executable: String, _ arguments: [String]) throws {
        let process = Process(); process.executableURL = URL(fileURLWithPath: executable); process.arguments = arguments; process.standardOutput = FileHandle.nullDevice; process.standardError = FileHandle.nullDevice
        try process.run(); process.waitUntilExit()
        XCTAssertEqual(process.terminationStatus, 0)
    }

    private func runOutput(_ executable: String, _ arguments: [String]) throws -> Data {
        let output = Pipe(); let process = Process(); process.executableURL = URL(fileURLWithPath: executable); process.arguments = arguments; process.standardOutput = output; process.standardError = FileHandle.nullDevice
        try process.run(); process.waitUntilExit(); XCTAssertEqual(process.terminationStatus, 0)
        return output.fileHandleForReading.readDataToEndOfFile()
    }
}
