// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import CryptoKit
import Foundation
import Security

/// A narrowly-scoped session used only for the QR-selected pairing endpoint.
/// It rejects cross-origin redirects so a one-time code never follows one.
public enum PairingURLSession {
    public static func make(fingerprint: Data?, host: String?) -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpCookieStorage = nil
        configuration.urlCredentialStorage = nil
        configuration.httpShouldSetCookies = false
        configuration.urlCache = nil
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.timeoutIntervalForRequest = 30
        configuration.timeoutIntervalForResource = 60
        return URLSession(configuration: configuration, delegate: PairingTLSDelegate(fingerprint: fingerprint, host: host), delegateQueue: nil)
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

    /// A QR pin identifies the hs leaf SPKI, never an unrelated gateway host.
    static func fingerprint(_ fingerprint: Data?, homeserver: URL, requestURL: URL) -> Data? {
        guard let host = homeserver.host, requestURL.scheme == "https",
              requestURL.host?.lowercased() == host.lowercased() else { return nil }
        return fingerprint
    }
}

final class PairingTLSDelegate: NSObject, URLSessionTaskDelegate {
    private let fingerprint: Data?
    private let host: String?
    private let rejectionLock = NSLock()
    private var didRejectCertificate = false
    init(fingerprint: Data?, host: String?) { self.fingerprint = fingerprint; self.host = host?.lowercased() }

    var rejectedCertificate: Bool { rejectionLock.withLock { didRejectCertificate } }
    func requestDelegate() -> PairingTLSDelegate { PairingTLSDelegate(fingerprint: fingerprint, host: host) }

    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        guard let original = task.originalRequest?.url, let target = request.url, HTTPSRedirectPolicy.allowsRedirect(from: original, to: target) else { completionHandler(nil); return }
        completionHandler(request)
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didReceive challenge: URLAuthenticationChallenge, completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust else { completionHandler(.performDefaultHandling, nil); return }
        guard let fingerprint, let host, challenge.protectionSpace.host.lowercased() == host else {
            completionHandler(.performDefaultHandling, nil); return
        }
        guard let trust = challenge.protectionSpace.serverTrust,
              SecTrustSetPolicies(trust, SecPolicyCreateSSL(true, host as CFString)) == errSecSuccess,
              SecTrustEvaluateWithError(trust, nil),
              let certificate = SecTrustCopyCertificateChain(trust) as? [SecCertificate], let leaf = certificate.first,
              Self.matchesLeafCertificateDER(SecCertificateCopyData(leaf) as Data, fingerprint: fingerprint) else {
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
        guard let spki = subjectPublicKeyInfo(certificate) else { return false }
        return Data(SHA256.hash(data: spki)) == fingerprint
    }
}

private enum DER {
    struct Item { let tag: UInt8; let full: Data; let content: Data }
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
