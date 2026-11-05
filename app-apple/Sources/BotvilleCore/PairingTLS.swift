// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import CryptoKit
import Darwin
import Foundation
import Security

/// A narrowly-scoped session used only for the QR-selected pairing endpoint.
/// It rejects cross-origin redirects so a one-time code never follows one.
public enum PairingURLSession {
    static func normalizedHTTPSPort(_ port: Int?) -> Int {
        guard let port, port != 0 else { return 443 }
        return port
    }

    public static func make(fingerprint: Data?, host: String?, port: Int? = nil) -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpCookieStorage = nil
        configuration.urlCredentialStorage = nil
        configuration.httpShouldSetCookies = false
        configuration.urlCache = nil
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.timeoutIntervalForRequest = 30
        configuration.timeoutIntervalForResource = 60
        return URLSession(configuration: configuration, delegate: PairingTLSDelegate(fingerprint: fingerprint, host: host, port: port), delegateQueue: nil)
    }

    /// URLSession reports a rejected trust challenge as a transport error. Keep
    /// the rejection on a per-request delegate so concurrent requests cannot
    /// turn an unrelated network failure into a certificate mismatch.
    static func data(for request: URLRequest, using session: URLSession) async throws -> (Data, URLResponse) {
        guard let configuration = session.delegate as? PairingTLSDelegate else {
            return try await session.data(for: request)
        }
        let delegate = configuration.requestDelegate()
        do {
            return try await session.data(for: request, delegate: delegate)
        } catch {
            if delegate.rejectedCertificate { throw PairingError.certificateMismatch }
            throw error
        }
    }

    /// A QR pin identifies the hs leaf SPKI and is scoped to its HTTPS origin.
    static func fingerprint(_ fingerprint: Data?, homeserver: URL, requestURL: URL) -> Data? {
        guard homeserver.scheme?.lowercased() == "https",
              requestURL.scheme?.lowercased() == "https",
              let host = homeserver.host,
              requestURL.host?.lowercased() == host.lowercased(),
              normalizedHTTPSPort(requestURL.port) == normalizedHTTPSPort(homeserver.port) else { return nil }
        return fingerprint
    }
}

final class PairingTLSDelegate: NSObject, URLSessionTaskDelegate {
    private let fingerprint: Data?
    private let host: String?
    private let port: Int
    private let rejectionLock = NSLock()
    private var didRejectCertificate = false
    init(fingerprint: Data?, host: String?, port: Int? = nil) {
        self.fingerprint = fingerprint
        self.host = host?.lowercased()
        self.port = PairingURLSession.normalizedHTTPSPort(port)
    }

    var rejectedCertificate: Bool { rejectionLock.withLock { didRejectCertificate } }
    func requestDelegate() -> PairingTLSDelegate { PairingTLSDelegate(fingerprint: fingerprint, host: host, port: port) }

    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        guard let original = task.originalRequest?.url, let target = request.url, HTTPSRedirectPolicy.allowsRedirect(from: original, to: target) else { completionHandler(nil); return }
        completionHandler(request)
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didReceive challenge: URLAuthenticationChallenge, completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust else { completionHandler(.performDefaultHandling, nil); return }
        // A pin only applies to its QR-selected origin. If host or normalized
        // port differs, use the platform's system trust rather than pinning a
        // different endpoint.
        guard let fingerprint, let host, challenge.protectionSpace.host.lowercased() == host,
              PairingURLSession.normalizedHTTPSPort(challenge.protectionSpace.port) == port else {
            completionHandler(.performDefaultHandling, nil); return
        }
        guard let trust = challenge.protectionSpace.serverTrust,
              let certificates = SecTrustCopyCertificateChain(trust) as? [SecCertificate], let leaf = certificates.first else {
            rejectionLock.withLock { didRejectCertificate = true }
            completionHandler(.cancelAuthenticationChallenge, nil); return
        }
        let leafDER = SecCertificateCopyData(leaf) as Data
        guard Self.matchesLeafCertificateDER(leafDER, fingerprint: fingerprint),
              Self.leafAllowsServerAuthentication(leafDER, host: host),
              SecTrustSetPolicies(trust, SecPolicyCreateSSL(true, host as CFString)) == errSecSuccess,
              SecTrustSetAnchorCertificates(trust, [leaf] as CFArray) == errSecSuccess,
              SecTrustSetAnchorCertificatesOnly(trust, true) == errSecSuccess,
              SecTrustEvaluateWithError(trust, nil) else {
            rejectionLock.withLock { didRejectCertificate = true }
            completionHandler(.cancelAuthenticationChallenge, nil); return
        }
        completionHandler(.useCredential, URLCredential(trust: trust))
    }

    /// Extracts the leaf SubjectPublicKeyInfo DER from an X.509 certificate;
    /// hashing a SecKey representation would not meet the QR contract.
    static func subjectPublicKeyInfo(_ certificate: Data) -> Data? {
        guard let outer = DER.element(certificate, at: 0), outer.tag == 0x30,
              let tbs = DER.element(outer.content, at: 0), tbs.tag == 0x30 else { return nil }
        let children = DER.children(tbs.content)
        guard !children.isEmpty else { return nil }
        let offset = children.first?.tag == 0xA0 ? 1 : 0
        let spkiIndex = offset + 5 // serial, signature, issuer, validity, subject, SPKI
        guard children.indices.contains(spkiIndex), children[spkiIndex].tag == 0x30 else { return nil }
        return children[spkiIndex].full
    }

    static func matchesLeafCertificateDER(_ certificate: Data, fingerprint: Data) -> Bool {
        guard fingerprint.count == SHA256.Digest.byteCount, let spki = subjectPublicKeyInfo(certificate) else { return false }
        // Compare every byte, without a data-dependent early exit.
        let digest = SHA256.hash(data: spki)
        var difference: UInt8 = 0
        for (actual, expected) in zip(digest, fingerprint) { difference |= actual ^ expected }
        return difference == 0
    }

    /// A trust anchor can receive different validity/EKU treatment from a
    /// normal leaf. Check leaf validity, required SAN type (and exact IP SAN
    /// bytes) and serverAuth EKU here; Security's SSL policy performs the
    /// hostname and wildcard comparison.
    /// SecCertificateCopyValues is unavailable on iOS, so use its DER fields.
    private static func leafAllowsServerAuthentication(_ certificate: Data, host: String, now: Date = Date()) -> Bool {
        guard let outer = DER.element(certificate, at: 0), outer.tag == 0x30,
              let tbs = DER.element(outer.content, at: 0), tbs.tag == 0x30 else { return false }
        let fields = DER.children(tbs.content)
        let offset = fields.first?.tag == 0xA0 ? 1 : 0
        guard fields.indices.contains(offset + 3), fields[offset + 3].tag == 0x30 else { return false }
        let validity = DER.children(fields[offset + 3].content)
        guard validity.count == 2,
              let notBefore = DER.date(validity[0]), let notAfter = DER.date(validity[1]),
              notBefore <= now, now <= notAfter,
              let extensions = fields.first(where: { $0.tag == 0xA3 }),
              let sequence = DER.element(extensions.content, at: 0), sequence.tag == 0x30,
              let san = DER.extensionValue(oid: [0x55, 0x1D, 0x11], in: sequence.content),
              let names = DER.element(san, at: 0), names.tag == 0x30,
              let eku = DER.extensionValue(oid: [0x55, 0x1D, 0x25], in: sequence.content),
              let usages = DER.element(eku, at: 0), usages.tag == 0x30,
              DER.children(usages.content).contains(where: {
                  $0.tag == 0x06 && $0.content == Data([0x2B, 0x06, 0x01, 0x05, 0x05, 0x07, 0x03, 0x01]) // serverAuth
              }) else { return false }

        // Require a SAN of the right type so SSL policy cannot fall back to
        // CN. For literal IPs also require the exact SAN address bytes; a DNS
        // SAN containing the textual IP must never authenticate an IP URL.
        var ipv4 = in_addr()
        var ipv6 = in6_addr()
        let address: Data?
        if inet_pton(AF_INET, host, &ipv4) == 1 { address = withUnsafeBytes(of: ipv4) { Data($0) } }
        else if inet_pton(AF_INET6, host, &ipv6) == 1 { address = withUnsafeBytes(of: ipv6) { Data($0) } }
        else { address = nil }
        return DER.children(names.content).contains {
            if let address { return $0.tag == 0x87 && $0.content == address }
            return $0.tag == 0x82 && !$0.content.isEmpty
        }
    }
}

private enum DER {
    struct Item { let tag: UInt8; let full: Data; let content: Data }

    static func extensionValue(oid: [UInt8], in extensions: Data) -> Data? {
        let matching = children(extensions).filter {
            guard $0.tag == 0x30, let identifier = element($0.content, at: 0) else { return false }
            return identifier.tag == 0x06 && identifier.content == Data(oid)
        }
        guard matching.count == 1, let value = children(matching[0].content).last, value.tag == 0x04 else { return nil }
        return value.content
    }

    static func date(_ item: Item) -> Date? {
        // X.509 time is UTC with mandatory seconds: YY...Z or YYYY...Z.
        guard (item.tag == 0x17 && item.content.count == 13) || (item.tag == 0x18 && item.content.count == 15),
              item.content.last == 0x5A,
              item.content.dropLast().allSatisfy({ (0x30...0x39).contains($0) }),
              var value = String(data: item.content, encoding: .ascii) else { return nil }
        if item.tag == 0x17 {
            guard let year = Int(value.prefix(2)) else { return nil }
            value = (year < 50 ? "20" : "19") + value
        }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "yyyyMMddHHmmss'Z'"
        formatter.isLenient = false
        guard let date = formatter.date(from: value), formatter.string(from: date) == value else { return nil }
        return date
    }

    static func element(_ data: Data, at start: Int) -> Item? {
        guard start + 2 <= data.count else { return nil }
        let tag = data[data.startIndex + start]
        let first = Int(data[data.startIndex + start + 1]); var length = 0; var header = 2
        if first & 0x80 == 0 { length = first } else {
            let bytes = first & 0x7f
            guard bytes > 0, bytes <= 4, start + 2 + bytes <= data.count else { return nil }
            for i in 0..<bytes { length = (length << 8) | Int(data[data.startIndex + start + 2 + i]) }
            header += bytes
        }
        guard start + header + length <= data.count else { return nil }
        let full = data.subdata(in: start..<(start + header + length))
        return Item(tag: tag, full: full, content: data.subdata(in: (start + header)..<(start + header + length)))
    }
    static func children(_ data: Data) -> [Item] {
        var result: [Item] = []; var offset = 0
        while let item = element(data, at: offset) { result.append(item); offset += item.full.count }
        return offset == data.count ? result : []
    }
}
