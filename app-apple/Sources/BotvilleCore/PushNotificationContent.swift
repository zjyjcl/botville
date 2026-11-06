// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation

/// Compiled directly into the NSE as well as BotvilleCore for unit tests.
/// No SDK, application session, or disk storage.
struct PushEventReference: Equatable, Sendable {
    let roomID: String
    let eventID: String

    static func parse(_ userInfo: [AnyHashable: Any]) -> Self? {
        // Sygnal APNs event_id_only places both fields at the top level.
        // Accept known wrappers, never combine identifiers from different objects.
        var candidates = [userInfo]
        for key in ["notification", "data"] {
            if let nested = userInfo[key] as? [AnyHashable: Any] {
                candidates.append(nested)
                for innerKey in ["notification", "data"] {
                    if let inner = nested[innerKey] as? [AnyHashable: Any] {
                        candidates.append(inner)
                    }
                }
            }
        }
        for candidate in candidates {
            guard let room = candidate["room_id"] as? String,
                  let event = candidate["event_id"] as? String,
                  validID(room, prefix: "!"), validID(event, prefix: "$") else { continue }
            return Self(roomID: room, eventID: event)
        }
        return nil
    }

    private static func validID(_ value: String, prefix: Character) -> Bool {
        value.first == prefix && value.count > 1 && value.utf8.count <= 2048
            && value.rangeOfCharacter(from: .whitespacesAndNewlines.union(.controlCharacters)) == nil
    }
}

struct PushNotificationText: Equatable, Sendable {
    let title: String?
    let body: String

    static let placeholder = Self(title: nil, body: "…")
    static let unavailable = Self(title: nil, body: "无法加载")

    struct EventDetails: Equatable, Sendable {
        let text: PushNotificationText
        let senderID: String?
    }

    static func parseEvent(_ data: Data, reference: PushEventReference) -> Self? {
        parseEventDetails(data, reference: reference)?.text
    }

    static func parseEventDetails(_ data: Data, reference: PushEventReference) -> EventDetails? {
        guard let event = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let type = event["type"] as? String, !type.isEmpty else { return nil }
        // A response must not substitute an unrelated event when identifiers exist.
        if let id = event["event_id"], id as? String != reference.eventID { return nil }
        if let id = event["room_id"], id as? String != reference.roomID { return nil }
        let sender = nonempty(event["sender"])
        let title = truncate(sender ?? reference.roomID, limit: 120)
        // Even a malicious encrypted event containing body must never expose it.
        guard type != "m.room.encrypted",
              type == "m.room.message" || type == "m.sticker",
              let content = event["content"] as? [String: Any],
              let body = nonempty(content["body"]) else {
            return EventDetails(text: Self(title: title, body: "加密消息"), senderID: sender)
        }
        return EventDetails(text: Self(title: title, body: truncate(body, limit: 240)), senderID: sender)
    }

    func replacingTitle(_ title: String) -> Self {
        Self(title: Self.truncate(title, limit: 120), body: body)
    }

    private static func nonempty(_ value: Any?) -> String? {
        guard let value = value as? String else { return nil }
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    private static func truncate(_ text: String, limit: Int) -> String {
        text.count <= limit ? text : String(text.prefix(limit - 1)) + "…"
    }
}

/// The event GET and optional profile GET share a delivery deadline;
/// URLSession's request timeout alone is an inactivity timeout.
final class PushNotificationLoader: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    private let credentialsStore: any SharedPushCredentialsStore
    private let configuration: URLSessionConfiguration
    private let requestTimeout: TimeInterval
    private let timeBudget: TimeInterval
    private let lock = NSLock()
    private var started = false
    private var finished = false
    private var completion: ((PushNotificationText) -> Void)?
    private var session: URLSession?
    private var deadline: DispatchWorkItem?
    private var deadlineUptimeNanoseconds: UInt64?
    private var fallbackText: PushNotificationText?

    init(credentialsStore: any SharedPushCredentialsStore,
         configuration: URLSessionConfiguration = .ephemeral,
         requestTimeout: TimeInterval = 8, timeBudget: TimeInterval = 10) {
        self.credentialsStore = credentialsStore
        self.configuration = configuration.copy() as! URLSessionConfiguration
        self.requestTimeout = min(max(requestTimeout, 0.01), 8)
        self.timeBudget = min(max(timeBudget, 0.01), 10)
        super.init()
    }

    func load(userInfo: [AnyHashable: Any], completion: @escaping (PushNotificationText) -> Void) {
        let deadline = DispatchWorkItem { [weak self] in self?.expire() }
        let deadlineUptimeNanoseconds = DispatchTime.now().uptimeNanoseconds + UInt64(timeBudget * 1_000_000_000)
        let begin = lock.withLock { () -> Bool? in
            guard !started else { return nil }
            started = true
            guard !finished else { return false }
            self.completion = completion
            self.deadline = deadline
            self.deadlineUptimeNanoseconds = deadlineUptimeNanoseconds
            return true
        }
        guard let begin else { return }
        // Expiry can race with didReceive before credentials are read.
        if !begin {
            completion(.placeholder)
            return
        }
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + timeBudget, execute: deadline)

        guard let reference = PushEventReference.parse(userInfo) else {
            finish(.placeholder)
            return
        }
        let credentials: SharedPushCredentials
        do {
            guard let loaded = try credentialsStore.load() else {
                finish(.placeholder)
                return
            }
            credentials = loaded
        } catch {
            finish(.placeholder)
            return
        }
        guard let request = makeRequest(reference: reference, credentials: credentials) else {
            finish(.unavailable)
            return
        }
        lock.withLock {
            guard !finished else { return }
            configuration.timeoutIntervalForRequest = requestTimeout
            configuration.timeoutIntervalForResource = requestTimeout
            configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
            configuration.urlCache = nil
            configuration.httpCookieStorage = nil
            configuration.httpShouldSetCookies = false
            configuration.urlCredentialStorage = nil
            configuration.waitsForConnectivity = false
            let session = URLSession(configuration: configuration, delegate: self, delegateQueue: nil)
            self.session = session
            session.dataTask(with: request) { [weak self] data, response, error in
                guard error == nil, let response = response as? HTTPURLResponse,
                      (200..<300).contains(response.statusCode), let data,
                      let details = PushNotificationText.parseEventDetails(data, reference: reference) else {
                    self?.finish(.unavailable)
                    return
                }
                self?.loadProfile(senderID: details.senderID, baseline: details.text, credentials: credentials)
            }.resume()
        }
    }

    func expire() { finish() }

    private func finish(_ explicitText: PushNotificationText? = nil) {
        let delivery = lock.withLock { () -> ((PushNotificationText, ((PushNotificationText) -> Void)?, URLSession?, DispatchWorkItem?))? in
            guard !finished else { return nil }
            finished = true
            let delivery = (explicitText ?? fallbackText ?? .placeholder, completion, session, deadline)
            completion = nil
            session = nil
            deadline = nil
            deadlineUptimeNanoseconds = nil
            fallbackText = nil
            return delivery
        }
        guard let (text, completion, session, deadline) = delivery else { return }
        deadline?.cancel()
        session?.invalidateAndCancel()
        completion?(text)
    }

    private func makeRequest(reference: PushEventReference, credentials: SharedPushCredentials) -> URLRequest? {
        guard var components = validHomeserverComponents(credentials) else { return nil }
        // Encode each entire identifier as one path segment, including /, %, ?, #,
        // colon and +. Keep an optional reverse-proxy base path, already encoded.
        guard let room = reference.roomID.addingPercentEncoding(withAllowedCharacters: Self.unreserved),
              let event = reference.eventID.addingPercentEncoding(withAllowedCharacters: Self.unreserved) else { return nil }
        var base = components.percentEncodedPath
        while base.hasSuffix("/") { base.removeLast() }
        components.percentEncodedPath = base + "/_matrix/client/v3/rooms/" + room + "/event/" + event
        guard let url = components.url else { return nil }
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: requestTimeout)
        request.httpMethod = "GET"
        request.setValue("Bearer " + credentials.accessToken, forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        return request
    }

    private func loadProfile(senderID: String?, baseline: PushNotificationText, credentials: SharedPushCredentials) {
        guard setFallbackIfActive(baseline) else { return }
        let remaining = lock.withLock { remainingTimeBudgetLocked() }
        guard let senderID, validUserID(senderID),
              remaining > 0,
              let request = makeProfileRequest(userID: senderID, credentials: credentials, timeout: min(requestTimeout, remaining)) else {
            finish(baseline)
            return
        }
        lock.withLock {
            guard !finished, remainingTimeBudgetLocked() > 0, let session else { return }
            session.dataTask(with: request) { [weak self] data, response, error in
                guard error == nil, let response = response as? HTTPURLResponse,
                      (200..<300).contains(response.statusCode), let data,
                      let displayname = Self.displayname(from: data) else {
                    self?.finish(baseline)
                    return
                }
                self?.finish(baseline.replacingTitle(displayname))
            }.resume()
        }
    }

    private func setFallbackIfActive(_ text: PushNotificationText) -> Bool {
        lock.withLock {
            guard !finished else { return false }
            fallbackText = text
            return true
        }
    }

    /// Call only while holding `lock`.
    private func remainingTimeBudgetLocked() -> TimeInterval {
        guard let deadlineUptimeNanoseconds else { return 0 }
        let now = DispatchTime.now().uptimeNanoseconds
        guard deadlineUptimeNanoseconds > now else { return 0 }
        return TimeInterval(deadlineUptimeNanoseconds - now) / 1_000_000_000
    }

    private func makeProfileRequest(userID: String, credentials: SharedPushCredentials, timeout: TimeInterval) -> URLRequest? {
        guard var components = validHomeserverComponents(credentials),
              let user = userID.addingPercentEncoding(withAllowedCharacters: Self.unreserved) else { return nil }
        var base = components.percentEncodedPath
        while base.hasSuffix("/") { base.removeLast() }
        components.percentEncodedPath = base + "/_matrix/client/v3/profile/" + user
        guard let url = components.url else { return nil }
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: timeout)
        request.httpMethod = "GET"
        request.setValue("Bearer " + credentials.accessToken, forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        return request
    }

    private func validHomeserverComponents(_ credentials: SharedPushCredentials) -> URLComponents? {
        guard !credentials.accessToken.isEmpty,
              credentials.accessToken.rangeOfCharacter(from: .controlCharacters) == nil,
              let components = URLComponents(string: credentials.homeserverURL),
              let scheme = components.scheme?.lowercased(),
              let host = components.host, !host.isEmpty,
              components.user == nil, components.password == nil,
              components.query == nil, components.fragment == nil else { return nil }
        #if DEBUG
        guard scheme == "https" || scheme == "http" else { return nil }
        #else
        guard scheme == "https" else { return nil }
        #endif
        return components
    }

    private static let unreserved = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")

    private static func displayname(from data: Data) -> String? {
        guard let profile = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let displayname = profile["displayname"] as? String else { return nil }
        let trimmed = displayname.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    private func validUserID(_ value: String) -> Bool {
        value.first == "@" && value.count > 1 && value.utf8.count <= 2048
            && value.rangeOfCharacter(from: .whitespacesAndNewlines.union(.controlCharacters)) == nil
    }

    // Never forward the bearer to a redirect target, even on the same origin.
    // No authentication challenge override: TLS uses system trust, not SPKI pinning.
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}
