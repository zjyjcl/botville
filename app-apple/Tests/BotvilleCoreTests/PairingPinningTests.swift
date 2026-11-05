// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

@testable import BotvilleCore
import CryptoKit
import Foundation
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
        await XCTAssertThrowsErrorAsync(try await PairingURLSession.data(for: URLRequest(url: try XCTUnwrap(URL(string: "https://127.0.0.1:\(server.port)/"))), using: session))
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

    func testAppSourceHasNoGlobalTLSBypass() throws {
        let appRoot = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let allFiles = FileManager.default.enumerator(at: appRoot.appendingPathComponent("Sources"), includingPropertiesForKeys: nil)?.allObjects as? [URL] ?? []
        let source = try allFiles.filter { $0.pathExtension == "swift" }.map { try String(contentsOf: $0, encoding: .utf8) }.joined(separator: "\n")
        let plists = ["BotvilleApp/Info.plist", "BotvilleApp/Info-Debug.plist", "NotificationService/Info.plist"].map { appRoot.appendingPathComponent($0) }
        let plistContents = try plists.map { try PropertyListSerialization.propertyList(from: Data(contentsOf: $0), format: nil) }.map(String.init(describing:)).joined(separator: "\n")
        XCTAssertFalse((source + plistContents).contains("disableSslVerification"))
        XCTAssertFalse((source + plistContents).contains("NSAllowsArbitraryLoads"))
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
            Thread.sleep(forTimeInterval: 0.05)
            if process.isRunning { return (process, port) }
        }
        throw XCTSkip("Unable to reserve a local TLS port for the certificate-mismatch test.")
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
}

private final class TLSFixture {
    enum Validity { case current, expired, notYetValid }
    let directory: URL
    let key: URL
    let certificate: URL
    let fingerprint: Data

    init(san: String?, eku: String? = "serverAuth", days: Int = 1, validity: Validity = .current, commonName: String = "127.0.0.1") throws {
        directory = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent(".build/pairing-fixture-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        key = directory.appendingPathComponent("key.pem")
        certificate = directory.appendingPathComponent("cert.pem")
        var arguments = validity == .current
            ? ["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key.path, "-out", certificate.path, "-subj", "/CN=\(commonName)", "-days", String(days)]
            : ["req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", key.path, "-out", directory.appendingPathComponent("request.csr").path, "-subj", "/CN=\(commonName)"]
        if let san { arguments += ["-addext", "subjectAltName = \(san)"] }
        if let eku { arguments += ["-addext", "extendedKeyUsage = \(eku)"] }
        try Self.run("/usr/bin/openssl", arguments)
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
            try Self.run("/usr/bin/openssl", ["ca", "-selfsign", "-batch", "-config", configuration.path, "-keyfile", key.path, "-in", directory.appendingPathComponent("request.csr").path, "-out", certificate.path] + dates)
        }
        let der = directory.appendingPathComponent("cert.der")
        try Self.run("/usr/bin/openssl", ["x509", "-in", certificate.path, "-outform", "DER", "-out", der.path])
        guard let spki = PairingTLSDelegate.subjectPublicKeyInfo(try Data(contentsOf: der)) else { throw NSError(domain: "PairingPinningTests", code: 1) }
        fingerprint = Data(SHA256.hash(data: spki))
    }

    func remove() { try? FileManager.default.removeItem(at: directory) }

    private static func run(_ executable: String, _ arguments: [String]) throws {
        let process = Process(); process.executableURL = URL(fileURLWithPath: executable); process.arguments = arguments; process.standardOutput = FileHandle.nullDevice; process.standardError = FileHandle.nullDevice
        try process.run(); process.waitUntilExit()
        guard process.terminationStatus == 0 else { throw NSError(domain: "PairingPinningTests", code: Int(process.terminationStatus)) }
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
