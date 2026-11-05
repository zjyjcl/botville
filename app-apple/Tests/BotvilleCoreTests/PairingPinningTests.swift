// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

@testable import BotvilleCore
import CryptoKit
import Foundation
import XCTest

final class PairingPinningTests: XCTestCase {
    func testRejectedTLSRequestMapsToCertificateMismatch() async throws {
        let directory = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let key = directory.appendingPathComponent("key.pem")
        let certificate = directory.appendingPathComponent("cert.pem")
        try run("/usr/bin/openssl", ["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key.path, "-out", certificate.path, "-subj", "/CN=127.0.0.1", "-days", "1"])

        let server = try startTLSServer(certificate: certificate, key: key)
        defer { server.process.terminate(); server.process.waitUntilExit() }
        let url = try XCTUnwrap(URL(string: "https://127.0.0.1:\(server.port)/"))
        let fakeValue = String(repeating: "A", count: 43)
        let pairing = try PairingQRCode.parse("botville://pair?v=1&hs=\(url.absoluteString)&fp=sha256:\(fakeValue)&code=\(fakeValue)&exp=4102444800")

        do {
            // Exercise the public redeem service as well as its per-request
            // helper: a trust rejection must survive the service's catch path.
            _ = try await HTTPPairingService().redeem(pairing, deviceDisplayName: "TLS test")
            XCTFail("The self-signed server must not satisfy the pairing TLS policy")
        } catch let error as PairingError {
            XCTAssertEqual(error, .certificateMismatch)
        }
    }

    func testDifferentHostTrustChallengeUsesSystemTLSAndDoesNotSetRejection() {
        let delegate = PairingTLSDelegate(fingerprint: Data(repeating: 0, count: 32), host: "homeserver.example.invalid")
        let result = challengeResult(delegate: delegate, host: "gateway.example.invalid")
        XCTAssertEqual(result, .performDefaultHandling)
        XCTAssertFalse(delegate.rejectedCertificate)
    }

    func testRejectedTrustStateIsPerRequestDelegate() {
        let configuration = PairingTLSDelegate(fingerprint: Data(repeating: 0, count: 32), host: "homeserver.example.invalid")
        let rejected = configuration.requestDelegate()
        XCTAssertEqual(challengeResult(delegate: rejected, host: "homeserver.example.invalid"), .cancelAuthenticationChallenge)
        XCTAssertTrue(rejected.rejectedCertificate)

        let unrelatedRequest = configuration.requestDelegate()
        XCTAssertFalse(unrelatedRequest.rejectedCertificate)
    }

    func testFingerprintIsLimitedToHTTPSHomeserverHost() throws {
        let fingerprint = Data(repeating: 9, count: 32)
        let homeserver = try XCTUnwrap(URL(string: "https://homeserver.example.invalid"))
        XCTAssertEqual(PairingURLSession.fingerprint(fingerprint, homeserver: homeserver, requestURL: try XCTUnwrap(URL(string: "https://homeserver.example.invalid/path"))), fingerprint)
        XCTAssertNil(PairingURLSession.fingerprint(fingerprint, homeserver: homeserver, requestURL: try XCTUnwrap(URL(string: "https://gateway.example.invalid/path"))))
        XCTAssertNil(PairingURLSession.fingerprint(fingerprint, homeserver: homeserver, requestURL: try XCTUnwrap(URL(string: "http://homeserver.example.invalid/path"))))
    }

    func testExtractsSPKIFromFreshTemporaryCertificate() throws {
        let directory = try makeTemporaryDirectory()
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

    private func challengeResult(delegate: PairingTLSDelegate, host: String) -> URLSession.AuthChallengeDisposition {
        let protectionSpace = URLProtectionSpace(host: host, port: 443, protocol: "https", realm: nil, authenticationMethod: NSURLAuthenticationMethodServerTrust)
        let challenge = URLAuthenticationChallenge(protectionSpace: protectionSpace, proposedCredential: nil, previousFailureCount: 0, failureResponse: nil, error: nil, sender: ChallengeSender())
        var result: URLSession.AuthChallengeDisposition?
        delegate.urlSession(URLSession.shared, task: URLSession.shared.dataTask(with: URL(string: "https://\(host)/")!), didReceive: challenge) { disposition, _ in result = disposition }
        return result!
    }

    private func makeTemporaryDirectory() throws -> URL {
        let directory = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent(".build/pairing-test-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    private func startTLSServer(certificate: URL, key: URL) throws -> (process: Process, port: Int) {
        for _ in 0..<10 {
            let port = Int.random(in: 30_000...50_000)
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/openssl")
            process.arguments = ["s_server", "-accept", String(port), "-www", "-cert", certificate.path, "-key", key.path]
            process.standardOutput = FileHandle.nullDevice
            process.standardError = FileHandle.nullDevice
            try process.run()
            Thread.sleep(forTimeInterval: 0.1)
            if process.isRunning { return (process, port) }
        }
        throw XCTSkip("Unable to reserve a local TLS port for the certificate-mismatch test.")
    }
}

private final class ChallengeSender: NSObject, URLAuthenticationChallengeSender {
    func use(_ credential: URLCredential, for challenge: URLAuthenticationChallenge) {}
    func continueWithoutCredential(for challenge: URLAuthenticationChallenge) {}
    func cancel(_ challenge: URLAuthenticationChallenge) {}
    func performDefaultHandling(for challenge: URLAuthenticationChallenge) {}
    func rejectProtectionSpaceAndContinue(with challenge: URLAuthenticationChallenge) {}
}
