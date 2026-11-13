// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

@testable import BotvilleCore
import CryptoKit
import Darwin
import Foundation
import Security
import XCTest

final class PairingPinningTests: XCTestCase {
    func testSelfSignedCertificateWithMatchingPinAndSANCompletesHTTPRequest() async throws {
        let fixture = try TLSFixture(san: "IP:127.0.0.1")
        defer { fixture.remove() }
        let server = try startTLSServer(certificate: fixture.certificate, key: fixture.key)
        defer { stopTLSServer(server.process) }
        try await waitForTLSServer(port: server.port)

        let session = PairingURLSession.make(fingerprint: fixture.fingerprint, host: "127.0.0.1", port: server.port)
        defer { session.invalidateAndCancel() }
        let request = URLRequest(url: try XCTUnwrap(URL(string: "https://127.0.0.1:\(server.port)/")))
        let (body, response) = try await PairingURLSession.data(for: request, using: session)
        XCTAssertEqual((response as? HTTPURLResponse)?.statusCode, 200)
        XCTAssertFalse(body.isEmpty)
    }

    func testSelfSignedCertificateWithWrongPinMapsToCertificateMismatch() async throws {
        let fixture = try TLSFixture(san: "IP:127.0.0.1")
        defer { fixture.remove() }
        let server = try startTLSServer(certificate: fixture.certificate, key: fixture.key)
        defer { stopTLSServer(server.process) }
        try await waitForTLSServer(port: server.port)

        let session = PairingURLSession.make(fingerprint: Data(repeating: 0, count: 32), host: "127.0.0.1", port: server.port)
        defer { session.invalidateAndCancel() }
        await XCTAssertThrowsErrorAsync(try await PairingURLSession.data(for: URLRequest(url: try XCTUnwrap(URL(string: "https://127.0.0.1:\(server.port)/"))), using: session)) { error in
            XCTAssertEqual(error as? PairingError, .certificateMismatch)
        }
    }

    func testSelfSignedCertificateWithMatchingPinButWrongSANIsRejected() async throws {
        let fixture = try TLSFixture(san: "DNS:localhost")
        defer { fixture.remove() }
        try await assertPinnedRequestRejected(fixture: fixture)
    }

    func testDNSOnlySANDoesNotMatchIPAddress() async throws {
        let fixture = try TLSFixture(san: "DNS:127.0.0.1")
        defer { fixture.remove() }
        try await assertPinnedRequestRejected(fixture: fixture)
    }

    func testDNSOnlySANMatchesDNSHost() async throws {
        let fixture = try TLSFixture(san: "DNS:localhost")
        defer { fixture.remove() }
        let server = try startTLSServer(certificate: fixture.certificate, key: fixture.key)
        defer { stopTLSServer(server.process) }
        try await waitForTLSServer(port: server.port, host: "localhost")
        let session = PairingURLSession.make(fingerprint: fixture.fingerprint, host: "localhost", port: server.port)
        defer { session.invalidateAndCancel() }
        let (_, response) = try await PairingURLSession.data(for: URLRequest(url: try XCTUnwrap(URL(string: "https://localhost:\(server.port)/"))), using: session)
        XCTAssertEqual((response as? HTTPURLResponse)?.statusCode, 200)
    }

    func testDNSWrongSANDoesNotFallBackToLocalhostCN() async throws {
        let fixture = try TLSFixture(san: "DNS:example.invalid", commonName: "localhost")
        defer { fixture.remove() }
        try await assertPinnedRequestRejected(fixture: fixture, host: "localhost")
    }

    func testSelfSignedCertificateWithCNOnlyIsRejected() async throws {
        let fixture = try TLSFixture(san: nil)
        defer { fixture.remove() }
        try await assertPinnedRequestRejected(fixture: fixture)
    }

    func testSelfSignedCertificateWithClientAuthOnlyEKUIsRejected() async throws {
        let fixture = try TLSFixture(san: "IP:127.0.0.1", eku: "clientAuth")
        defer { fixture.remove() }
        try await assertPinnedRequestRejected(fixture: fixture)
    }

    func testSelfSignedCertificateWithNoEKUIsRejected() async throws {
        let fixture = try TLSFixture(san: "IP:127.0.0.1", eku: nil)
        defer { fixture.remove() }
        try await assertPinnedRequestRejected(fixture: fixture)
    }

    func testExpiredSelfSignedCertificateWithMatchingPinIsRejected() async throws {
        let fixture = try TLSFixture(san: "IP:127.0.0.1", validity: .expired)
        defer { fixture.remove() }
        try await assertPinnedRequestRejected(fixture: fixture)
    }

    func testNotYetValidSelfSignedCertificateWithMatchingPinIsRejected() async throws {
        let fixture = try TLSFixture(san: "IP:127.0.0.1", validity: .notYetValid)
        defer { fixture.remove() }
        try await assertPinnedRequestRejected(fixture: fixture)
    }

    func testSelfSignedLongLivedCertificateWithServerAuthEKUCompletesHTTPRequest() async throws {
        let fixture = try TLSFixture(san: "IP:127.0.0.1", days: 400)
        defer { fixture.remove() }
        let server = try startTLSServer(certificate: fixture.certificate, key: fixture.key)
        defer { stopTLSServer(server.process) }
        try await waitForTLSServer(port: server.port)
        let session = PairingURLSession.make(fingerprint: fixture.fingerprint, host: "127.0.0.1", port: server.port)
        defer { session.invalidateAndCancel() }
        let (_, response) = try await PairingURLSession.data(for: URLRequest(url: try XCTUnwrap(URL(string: "https://127.0.0.1:\(server.port)/"))), using: session)
        XCTAssertEqual((response as? HTTPURLResponse)?.statusCode, 200)
    }

    func testSelfSignedCertificateWithoutPinStillUsesSystemTrust() async throws {
        let fixture = try TLSFixture(san: "IP:127.0.0.1")
        defer { fixture.remove() }
        let server = try startTLSServer(certificate: fixture.certificate, key: fixture.key)
        defer { stopTLSServer(server.process) }
        try await waitForTLSServer(port: server.port)
        let session = PairingURLSession.make(fingerprint: nil, host: "127.0.0.1", port: server.port)
        defer { session.invalidateAndCancel() }
        await XCTAssertThrowsErrorAsync(try await PairingURLSession.data(for: URLRequest(url: try XCTUnwrap(URL(string: "https://127.0.0.1:\(server.port)/"))), using: session)) { error in
            // With no pin, system trust rejects the self-signed chain; the
            // pairing delegate must not relabel that as a pin mismatch.
            XCTAssertNotEqual(error as? PairingError, .certificateMismatch)
            XCTAssertNotNil(error as? URLError)
            XCTAssertTrue([
                NSURLErrorServerCertificateUntrusted,
                NSURLErrorSecureConnectionFailed
            ].contains((error as NSError).code))
        }
    }

    func testCASignedLeafWithMatchingPinCompletesHTTPRequest() async throws {
        let fixture = try TLSFixture(san: "IP:127.0.0.1", issuer: .certificateAuthority)
        defer { fixture.remove() }
        // A private root anchor models a CA-signed chain without modifying the
        // host system trust store. The following request tests the pin path.
        try assertCAChainIsTrustedLocally(fixture)
        let server = try startTLSServer(certificate: fixture.certificate, key: fixture.key)
        defer { stopTLSServer(server.process) }
        try await waitForTLSServer(port: server.port)
        let session = PairingURLSession.make(fingerprint: fixture.fingerprint, host: "127.0.0.1", port: server.port)
        defer { session.invalidateAndCancel() }
        let (_, response) = try await PairingURLSession.data(for: URLRequest(url: try XCTUnwrap(URL(string: "https://127.0.0.1:\(server.port)/"))), using: session)
        XCTAssertEqual((response as? HTTPURLResponse)?.statusCode, 200)
    }

    func testCASignedLeafWithWrongPinMapsToCertificateMismatch() async throws {
        let fixture = try TLSFixture(san: "IP:127.0.0.1", issuer: .certificateAuthority)
        defer { fixture.remove() }
        let server = try startTLSServer(certificate: fixture.certificate, key: fixture.key)
        defer { stopTLSServer(server.process) }
        try await waitForTLSServer(port: server.port)
        let session = PairingURLSession.make(fingerprint: Data(repeating: 0, count: 32), host: "127.0.0.1", port: server.port)
        defer { session.invalidateAndCancel() }
        await XCTAssertThrowsErrorAsync(try await PairingURLSession.data(for: URLRequest(url: try XCTUnwrap(URL(string: "https://127.0.0.1:\(server.port)/"))), using: session)) { error in
            XCTAssertEqual(error as? PairingError, .certificateMismatch)
        }
    }

    func testMixedDNSSANAndIPSANAuthenticateTheirMatchingHosts() async throws {
        let fixture = try TLSFixture(san: "DNS:localhost,IP:127.0.0.1")
        defer { fixture.remove() }
        let server = try startTLSServer(certificate: fixture.certificate, key: fixture.key)
        defer { stopTLSServer(server.process) }
        try await waitForTLSServer(port: server.port)
        for host in ["localhost", "127.0.0.1"] {
            let session = PairingURLSession.make(fingerprint: fixture.fingerprint, host: host, port: server.port)
            defer { session.invalidateAndCancel() }
            let (_, response) = try await PairingURLSession.data(for: URLRequest(url: try XCTUnwrap(URL(string: "https://\(host):\(server.port)/"))), using: session)
            XCTAssertEqual((response as? HTTPURLResponse)?.statusCode, 200)
        }
    }

    func testWildcardSANDoesNotMatchMultipleSubdomains() throws {
        let fixture = try TLSFixture(san: "DNS:*.example.org")
        defer { fixture.remove() }
        let delegate = PairingTLSDelegate(fingerprint: fixture.fingerprint, host: "a.b.example.org")
        XCTAssertEqual(try trustChallengeResult(delegate: delegate, fixture: fixture, host: "a.b.example.org"), .cancelAuthenticationChallenge)
        XCTAssertTrue(delegate.rejectedCertificate)
    }

    func testWildcardSANMatchesOneSubdomainInDelegate() throws {
        let fixture = try TLSFixture(san: "DNS:*.example.org")
        defer { fixture.remove() }
        let delegate = PairingTLSDelegate(fingerprint: fixture.fingerprint, host: "a.example.org")
        XCTAssertEqual(try trustChallengeResult(delegate: delegate, fixture: fixture, host: "a.example.org"), .useCredential)
        XCTAssertFalse(delegate.rejectedCertificate)
    }

    func testTLSFixtureCleansPrivateKeyDirectoryWhenOpenSSLFails() throws {
        // Observe only this test's fixtures: other test processes may create
        // or remove their own certificates while OpenSSL is running.
        let parent = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: parent) }
        XCTAssertThrowsError(try TLSFixture(san: "IP:127.0.0.1", openssl: "/usr/bin/false", parentDirectory: parent))
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: parent.path), [])
    }

    func testTLSFixtureCleansDefaultBuildDirectoryWhenOpenSSLFails() throws {
        // Check the default .build parent through this fixture's own unique
        // directory name. A scan of every pairing-fixture-* entry would race
        // with fixtures created by other parallel test processes.
        let name = "pairing-fixture-\(UUID().uuidString)"
        let directory = TLSFixture.defaultParentDirectory.appendingPathComponent(name)
        defer { try? FileManager.default.removeItem(at: directory) }
        XCTAssertThrowsError(try TLSFixture(san: "IP:127.0.0.1", openssl: "/usr/bin/false", directoryName: name))
        XCTAssertFalse(FileManager.default.fileExists(atPath: directory.path))
    }
    func testRejectedTLSRequestMapsToCertificateMismatch() async throws {
        let directory = try makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let key = directory.appendingPathComponent("key.pem")
        let certificate = directory.appendingPathComponent("cert.pem")
        try run("/usr/bin/openssl", ["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key.path, "-out", certificate.path, "-subj", "/CN=127.0.0.1", "-days", "1"])

        let server = try startTLSServer(certificate: certificate, key: key)
        defer { stopTLSServer(server.process) }
        try await waitForTLSServer(port: server.port)
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

    func testDifferentHostOrPortTrustChallengeUsesSystemTLSAndDoesNotSetRejection() {
        let delegate = PairingTLSDelegate(fingerprint: Data(repeating: 0, count: 32), host: "homeserver.example.invalid", port: 8448)
        let result = challengeResult(delegate: delegate, host: "gateway.example.invalid", port: 8448)
        XCTAssertEqual(result, .performDefaultHandling)
        XCTAssertFalse(delegate.rejectedCertificate)
        XCTAssertEqual(challengeResult(delegate: delegate, host: "homeserver.example.invalid", port: 443), .performDefaultHandling)
        XCTAssertFalse(delegate.rejectedCertificate)
    }

    func testRejectedTrustStateIsPerRequestDelegate() {
        let configuration = PairingTLSDelegate(fingerprint: Data(repeating: 0, count: 32), host: "homeserver.example.invalid", port: 443)
        let rejected = configuration.requestDelegate()
        XCTAssertEqual(challengeResult(delegate: rejected, host: "homeserver.example.invalid"), .cancelAuthenticationChallenge)
        XCTAssertTrue(rejected.rejectedCertificate)

        let unrelatedRequest = configuration.requestDelegate()
        XCTAssertFalse(unrelatedRequest.rejectedCertificate)
    }

    func testFingerprintIsLimitedToHTTPSHomeserverOrigin() throws {
        let fingerprint = Data(repeating: 9, count: 32)
        let homeserver = try XCTUnwrap(URL(string: "https://homeserver.example.invalid:8448"))
        XCTAssertEqual(PairingURLSession.fingerprint(fingerprint, homeserver: homeserver, requestURL: try XCTUnwrap(URL(string: "https://homeserver.example.invalid:8448/path"))), fingerprint)
        XCTAssertNil(PairingURLSession.fingerprint(fingerprint, homeserver: homeserver, requestURL: try XCTUnwrap(URL(string: "https://gateway.example.invalid/path"))))
        XCTAssertNil(PairingURLSession.fingerprint(fingerprint, homeserver: homeserver, requestURL: try XCTUnwrap(URL(string: "https://homeserver.example.invalid/path"))))
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

    private func challengeResult(delegate: PairingTLSDelegate, host: String, port: Int = 443) -> URLSession.AuthChallengeDisposition {
        let protectionSpace = URLProtectionSpace(host: host, port: port, protocol: "https", realm: nil, authenticationMethod: NSURLAuthenticationMethodServerTrust)
        let challenge = URLAuthenticationChallenge(protectionSpace: protectionSpace, proposedCredential: nil, previousFailureCount: 0, failureResponse: nil, error: nil, sender: ChallengeSender())
        var result: URLSession.AuthChallengeDisposition?
        delegate.urlSession(URLSession.shared, task: URLSession.shared.dataTask(with: URL(string: "https://\(host)/")!), didReceive: challenge) { disposition, _ in result = disposition }
        return result!
    }

    private func trustChallengeResult(delegate: PairingTLSDelegate, fixture: TLSFixture, host: String) throws -> URLSession.AuthChallengeDisposition {
        let der = try runOutput("/usr/bin/openssl", ["x509", "-in", fixture.certificate.path, "-outform", "DER"])
        let certificate = try XCTUnwrap(SecCertificateCreateWithData(nil, der as CFData))
        var trust: SecTrust?
        XCTAssertEqual(SecTrustCreateWithCertificates(certificate, SecPolicyCreateSSL(true, host as CFString), &trust), errSecSuccess)
        let protectionSpace = TrustProtectionSpace(host: host, trust: try XCTUnwrap(trust))
        let challenge = URLAuthenticationChallenge(protectionSpace: protectionSpace, proposedCredential: nil, previousFailureCount: 0, failureResponse: nil, error: nil, sender: ChallengeSender())
        var result: URLSession.AuthChallengeDisposition?
        delegate.urlSession(URLSession.shared, task: URLSession.shared.dataTask(with: URL(string: "https://\(host)/")!), didReceive: challenge) { disposition, _ in result = disposition }
        return try XCTUnwrap(result)
    }

    private func makeTemporaryDirectory() throws -> URL {
        let directory = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent(".build/pairing-test-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    private func assertCAChainIsTrustedLocally(_ fixture: TLSFixture) throws {
        let leafDER = try runOutput("/usr/bin/openssl", ["x509", "-in", fixture.certificate.path, "-outform", "DER"])
        let leaf = try XCTUnwrap(SecCertificateCreateWithData(nil, leafDER as CFData))
        let root = try XCTUnwrap(fixture.issuerCertificate)
        let rootDER = try runOutput("/usr/bin/openssl", ["x509", "-in", root.path, "-outform", "DER"])
        let rootCertificate = try XCTUnwrap(SecCertificateCreateWithData(nil, rootDER as CFData))
        var trust: SecTrust?
        XCTAssertEqual(SecTrustCreateWithCertificates(leaf, SecPolicyCreateSSL(true, "127.0.0.1" as CFString), &trust), errSecSuccess)
        let evaluatedTrust = try XCTUnwrap(trust)
        XCTAssertEqual(SecTrustSetAnchorCertificates(evaluatedTrust, [rootCertificate] as CFArray), errSecSuccess)
        XCTAssertEqual(SecTrustSetAnchorCertificatesOnly(evaluatedTrust, true), errSecSuccess)
        XCTAssertTrue(SecTrustEvaluateWithError(evaluatedTrust, nil))
    }

    private func startTLSServer(certificate: URL, key: URL) throws -> (process: Process, port: Int) {
        for _ in 0..<20 {
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
        throw NSError(domain: "PairingPinningTests", code: 3, userInfo: [
            NSLocalizedDescriptionKey: "Environment failure: unable to reserve a local TLS port for the certificate-mismatch test."
        ])
    }

    private func assertPinnedRequestRejected(fixture: TLSFixture, host: String = "127.0.0.1") async throws {
        let server = try startTLSServer(certificate: fixture.certificate, key: fixture.key)
        defer { stopTLSServer(server.process) }
        try await waitForTLSServer(port: server.port, host: host)
        let session = PairingURLSession.make(fingerprint: fixture.fingerprint, host: host, port: server.port)
        defer { session.invalidateAndCancel() }
        await XCTAssertThrowsErrorAsync(try await PairingURLSession.data(for: URLRequest(url: try XCTUnwrap(URL(string: "https://\(host):\(server.port)/"))), using: session)) { error in
            XCTAssertEqual(error as? PairingError, .certificateMismatch)
        }
    }

    private func waitForTLSServer(port: Int, host: String = "127.0.0.1") async throws {
        let url = try XCTUnwrap(URL(string: "https://\(host):\(port)/"))
        for _ in 0..<25 {
            let probe = URLSession(configuration: {
                let configuration = URLSessionConfiguration.ephemeral
                configuration.timeoutIntervalForRequest = 0.2
                configuration.timeoutIntervalForResource = 0.2
                return configuration
            }())
            defer { probe.invalidateAndCancel() }
            do {
                _ = try await probe.data(from: url)
                return
            } catch {
                let code = (error as NSError).code
                if code == NSURLErrorServerCertificateUntrusted || code == NSURLErrorSecureConnectionFailed { return }
            }
            try await Task.sleep(for: .milliseconds(40))
        }
        XCTFail("openssl s_server did not become ready")
    }
}

private func stopTLSServer(_ process: Process) {
    guard process.isRunning else { return }
    process.terminate()
    let deadline = Date().addingTimeInterval(0.25)
    while process.isRunning && Date() < deadline {
        RunLoop.current.run(until: Date().addingTimeInterval(0.01))
    }
    if process.isRunning {
        kill(process.processIdentifier, SIGKILL)
        process.waitUntilExit()
    }
}

private final class TLSFixture {
    enum Validity { case current, expired, notYetValid }
    enum Issuer { case selfSigned, certificateAuthority }
    let directory: URL
    let key: URL
    let certificate: URL
    let issuerCertificate: URL?
    let fingerprint: Data
    static let defaultParentDirectory = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent(".build")

    init(san: String?, eku: String? = "serverAuth", days: Int = 1, validity: Validity = .current, commonName: String = "127.0.0.1", issuer: Issuer = .selfSigned, openssl: String = "/usr/bin/openssl", parentDirectory: URL? = nil, directoryName: String = "pairing-fixture-\(UUID().uuidString)") throws {
        let parent = parentDirectory ?? Self.defaultParentDirectory
        directory = parent.appendingPathComponent(directoryName)
        key = directory.appendingPathComponent("key.pem")
        certificate = directory.appendingPathComponent("cert.pem")
        issuerCertificate = issuer == .certificateAuthority ? directory.appendingPathComponent("ca-cert.pem") : nil
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            if issuer == .certificateAuthority {
                guard validity == .current else { throw NSError(domain: "PairingPinningTests", code: 2) }
                guard let issuerCertificate else { throw NSError(domain: "PairingPinningTests", code: 3) }
                try Self.createCASignedLeaf(in: directory, key: key, certificate: certificate, caCertificate: issuerCertificate, san: san, eku: eku, days: days, commonName: commonName, openssl: openssl)
            } else {
                var arguments = validity == .current
                    ? ["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key.path, "-out", certificate.path, "-subj", "/CN=\(commonName)", "-days", String(days)]
                    : ["req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", key.path, "-out", directory.appendingPathComponent("request.csr").path, "-subj", "/CN=\(commonName)"]
                if let san { arguments += ["-addext", "subjectAltName = \(san)"] }
                if let eku { arguments += ["-addext", "extendedKeyUsage = \(eku)"] }
                try Self.run(openssl, arguments)
                if validity != .current {
                    let database = directory.appendingPathComponent("index.txt")
                    let serial = directory.appendingPathComponent("serial")
                    let newCertificates = directory.appendingPathComponent("newcerts")
                    try FileManager.default.createDirectory(at: newCertificates, withIntermediateDirectories: true)
                    try Data().write(to: database)
                    try Data("01\n".utf8).write(to: serial)
                    let configuration = directory.appendingPathComponent("openssl.cnf")
                    let config = """
                    [ ca ]
                    default_ca = local_ca
                    [ local_ca ]
                    database = \(database.path)
                    serial = \(serial.path)
                    new_certs_dir = \(newCertificates.path)
                    default_md = sha256
                    policy = any_name
                    copy_extensions = copy
                    [ any_name ]
                    commonName = supplied
                    """
                    try config.write(to: configuration, atomically: true, encoding: .utf8)
                    let dates: [String]
                    switch validity {
                    case .expired: dates = ["-startdate", "20240101000000Z", "-enddate", "20240102000000Z"]
                    case .notYetValid: dates = ["-startdate", "20990101000000Z", "-enddate", "20990102000000Z"]
                    case .current: dates = []
                    }
                    try Self.run(openssl, ["ca", "-selfsign", "-batch", "-config", configuration.path, "-keyfile", key.path, "-in", directory.appendingPathComponent("request.csr").path, "-out", certificate.path] + dates)
                }
            }
            let der = directory.appendingPathComponent("cert.der")
            try Self.run(openssl, ["x509", "-in", certificate.path, "-outform", "DER", "-out", der.path])
            guard let spki = PairingTLSDelegate.subjectPublicKeyInfo(try Data(contentsOf: der)) else { throw NSError(domain: "PairingPinningTests", code: 1) }
            fingerprint = Data(SHA256.hash(data: spki))
        } catch {
            // Initialization may fail before fingerprint is set, so do not
            // invoke an instance method on the partially initialized fixture.
            try? FileManager.default.removeItem(at: directory)
            throw error
        }
    }

    func remove() { try? FileManager.default.removeItem(at: directory) }

    private static func run(_ executable: String, _ arguments: [String]) throws {
        let process = Process(); process.executableURL = URL(fileURLWithPath: executable); process.arguments = arguments; process.standardOutput = FileHandle.nullDevice; process.standardError = FileHandle.nullDevice
        try process.run(); process.waitUntilExit()
        guard process.terminationStatus == 0 else { throw NSError(domain: "PairingPinningTests", code: Int(process.terminationStatus)) }
    }

    private static func createCASignedLeaf(in directory: URL, key: URL, certificate: URL, caCertificate: URL, san: String?, eku: String?, days: Int, commonName: String, openssl: String) throws {
        let caKey = directory.appendingPathComponent("ca-key.pem")
        let request = directory.appendingPathComponent("request.csr")
        let extensions = directory.appendingPathComponent("extensions.cnf")
        try run(openssl, ["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", caKey.path, "-out", caCertificate.path, "-subj", "/CN=pairing-test-ca.invalid", "-days", String(days), "-addext", "basicConstraints = critical,CA:TRUE", "-addext", "keyUsage = critical,keyCertSign,cRLSign"])
        try run(openssl, ["req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", key.path, "-out", request.path, "-subj", "/CN=\(commonName)"])
        var contents = "[ leaf ]\nbasicConstraints = critical,CA:FALSE\n"
        if let san { contents += "subjectAltName = \(san)\n" }
        if let eku { contents += "extendedKeyUsage = \(eku)\n" }
        try contents.write(to: extensions, atomically: true, encoding: .utf8)
        // Explicitly pin the serial file's location inside this fixture. Without
        // -CAserial, openssl writes .srl to the shared working directory.
        let serial = directory.appendingPathComponent("ca-cert.srl")
        try run(openssl, ["x509", "-req", "-in", request.path, "-CA", caCertificate.path, "-CAkey", caKey.path, "-CAcreateserial", "-CAserial", serial.path, "-out", certificate.path, "-days", String(days), "-extfile", extensions.path, "-extensions", "leaf"])
    }
}

private func XCTAssertThrowsErrorAsync<T>(_ expression: @autoclosure () async throws -> T, _ handler: (Error) -> Void = { _ in }) async {
    do { _ = try await expression(); XCTFail("Expected an error") } catch { handler(error) }
}

private final class ChallengeSender: NSObject, URLAuthenticationChallengeSender {
    func use(_ credential: URLCredential, for challenge: URLAuthenticationChallenge) {}
    func continueWithoutCredential(for challenge: URLAuthenticationChallenge) {}
    func cancel(_ challenge: URLAuthenticationChallenge) {}
    func performDefaultHandling(for challenge: URLAuthenticationChallenge) {}
    func rejectProtectionSpaceAndContinue(with challenge: URLAuthenticationChallenge) {}
}

private final class TrustProtectionSpace: URLProtectionSpace, @unchecked Sendable {
    private let suppliedTrust: SecTrust

    init(host: String, trust: SecTrust) {
        suppliedTrust = trust
        super.init(host: host, port: 443, protocol: "https", realm: nil, authenticationMethod: NSURLAuthenticationMethodServerTrust)
    }

    required init?(coder: NSCoder) { return nil }

    override var serverTrust: SecTrust? { suppliedTrust }
}
