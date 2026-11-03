// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

@testable import BotvilleCore
import Foundation
import XCTest

/// Opt-in local acceptance coverage. This deliberately has no dependency on a
/// developer Keychain: the process-lifetime store is sufficient to prove that
/// MatrixService can reconstruct an SDK session from its persisted value.
@MainActor
final class SynapseIntegrationTests: XCTestCase {
    private var api: SynapseAPI!
    private var accounts: [SynapseAccount] = []
    private var artifacts: URL?
    private var sessionStore: InMemorySessionStore?
    private weak var liveService: MatrixService?

    func testLocalSynapseC21MessagingAndRecovery() async throws {
        let homeserver = try requiredHomeserver()
        let root = try requiredArtifactRoot()
        _ = try requiredContainer()
        api = SynapseAPI(homeserver: homeserver)
        let runDirectory = root.appendingPathComponent("c21-e2e-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: runDirectory, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
        artifacts = runDirectory

        do {
            let app = try await registerAccount()
            let peer = try await registerAccount()
            let roomID = try await api.createRoom(accessToken: peer.accessToken, invite: app.userID)

            try await exerciseApp(homeserver: homeserver, directory: runDirectory, app: app, peer: peer, roomID: roomID)
        } catch {
            await cleanupAndAssert()
            throw SynapseTestError(step: safeDescription(error), status: nil, errcode: nil)
        }

        await cleanupAndAssert()
    }

    /// Opt-in C2.6 acceptance coverage. This test neither stops nor starts a
    /// container; the caller supplies an already-running disposable Synapse.
    func testLocalSynapseC26EncryptionOptIn() async throws {
        let homeserver = try requiredHomeserver()
        let root = try requiredArtifactRoot()
        api = SynapseAPI(homeserver: homeserver)
        let runDirectory = root.appendingPathComponent("c26-e2e-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: runDirectory, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
        artifacts = runDirectory

        do {
            let app = try await registerAccount(prefix: "c26")
            let peer = try await registerAccount(prefix: "c26")
            try await exerciseC26Encryption(homeserver: homeserver, directory: runDirectory, app: app, peer: peer)
        } catch {
            await cleanupAndAssert(ensureSynapseRunning: false)
            throw SynapseTestError(step: safeDescription(error), status: nil, errcode: nil)
        }
        await cleanupAndAssert(ensureSynapseRunning: false)
    }

    private func exerciseC26Encryption(homeserver: URL, directory: URL, app: SynapseAccount, peer: SynapseAccount) async throws {
        let store = InMemorySessionStore()
        sessionStore = store
        weak var released: MatrixService?
        do {
            // This prefix is a test-only policy rule; no AI account is created.
            let policy = AIParticipantPolicy(userIDPatterns: ["^@demo_ai_.*:"])
            let service = MatrixService(sessionStore: store, storeDirectory: SessionStoreDirectory(baseURL: directory), monitorNetwork: false, aiParticipantPolicy: policy)
            released = service
            liveService = service
            try await service.login(homeserver: homeserver.absoluteString, username: app.localpart, password: app.password, allowLocalHTTP: true)

            let plainDraft = RoomCreationDraft(isDirect: true, invitees: [peer.userID])
            let plainRoom = try await service.createRoom(draft: plainDraft, consent: EncryptionConsent())
            let plainAlgorithm = try await api.roomEncryptionAlgorithm(accessToken: app.accessToken, roomID: plainRoom)
            XCTAssertNil(plainAlgorithm)
            try await waitFor("plain room SDK badge") { service.rooms.contains { $0.id == plainRoom && $0.encryption == .unencrypted } }
            phase("C2.6 default unencrypted room")

            var newRoomConsent = EncryptionConsent()
            let encryptedDraft = RoomCreationDraft(isDirect: true, invitees: [peer.userID])
            let encryptedTarget = EncryptionTarget.newRoom(encryptedDraft)
            newRoomConsent.request(for: encryptedTarget)
            XCTAssertTrue(newRoomConsent.confirm(for: encryptedTarget))
            let encryptedRoom = try await service.createRoom(draft: encryptedDraft, consent: newRoomConsent)
            let encryptedAlgorithm = try await api.roomEncryptionAlgorithm(accessToken: app.accessToken, roomID: encryptedRoom)
            XCTAssertEqual(encryptedAlgorithm, "m.megolm.v1.aes-sha2")
            try await waitFor("encrypted room SDK badge") { service.rooms.contains { $0.id == encryptedRoom && $0.encryption == .encrypted } }
            phase("C2.6 confirmed encrypted room")

            var existingRoomConsent = EncryptionConsent()
            let existingTarget = EncryptionTarget.existingRoom(plainRoom)
            existingRoomConsent.request(for: existingTarget)
            XCTAssertTrue(existingRoomConsent.confirm(for: existingTarget))
            try await service.enableEncryption(roomID: plainRoom, consent: existingRoomConsent)
            let enabledAlgorithm = try await api.roomEncryptionAlgorithm(accessToken: app.accessToken, roomID: plainRoom)
            XCTAssertEqual(enabledAlgorithm, "m.megolm.v1.aes-sha2")
            try await waitFor("enabled room SDK badge") { service.rooms.contains { $0.id == plainRoom && $0.encryption == .encrypted } }
            // There is no disable API. A repeat enable is a no-op, and state stays encrypted.
            try await service.enableEncryption(roomID: plainRoom, consent: existingRoomConsent)
            let repeatedAlgorithm = try await api.roomEncryptionAlgorithm(accessToken: app.accessToken, roomID: plainRoom)
            XCTAssertEqual(repeatedAlgorithm, "m.megolm.v1.aes-sha2")
            phase("C2.6 existing room encryption is irreversible")
        }
        try await waitFor("C2.6 service release") { released == nil }
    }

    private func exerciseApp(homeserver: URL, directory: URL, app: SynapseAccount, peer: SynapseAccount, roomID: String) async throws {
        let store = InMemorySessionStore()
        sessionStore = store
        weak var released: MatrixService?
        do {
            let first = MatrixService(sessionStore: store, storeDirectory: SessionStoreDirectory(baseURL: directory), monitorNetwork: false)
            released = first
            liveService = first
            try await first.login(homeserver: homeserver.absoluteString, username: app.localpart, password: app.password, allowLocalHTTP: true)
            try await waitFor("invite visible") { first.rooms.contains { $0.id == roomID && $0.membership == .invited } }
            try await first.acceptInvitation(roomID)
            try await waitFor("joined visible") { first.rooms.contains { $0.id == roomID && $0.membership == .joined } }
            phase("login and invitation")
        }
        try await waitFor("first service release") { released == nil }

        weak var restoredReleased: MatrixService?
        do {
            let restored = MatrixService(sessionStore: store, storeDirectory: SessionStoreDirectory(baseURL: directory), monitorNetwork: false)
            restoredReleased = restored
            liveService = restored
            await restored.restoreSession()
            try await waitFor("session restore") { restored.isLoggedIn && restored.userID == app.userID }
            try await waitFor("restored room list") { restored.rooms.contains { $0.id == roomID && $0.membership == .joined } }
            phase("session restore")
            try await restored.openRoom(roomID)
            let peerText = "c21-peer-\(UUID().uuidString)"
            try await api.sendText(accessToken: peer.accessToken, roomID: roomID, body: peerText)
            try await waitFor("peer to app") { restored.messages.contains { $0.body == peerText && $0.sender == peer.userID } }
            let appText = "c21-app-\(UUID().uuidString)"
            try await restored.send(text: appText, to: roomID)
            try await waitFor("app remote echo") { restored.messages.contains { $0.body == appText && $0.sender == app.userID && $0.isOwn && ($0.deliveryState == nil || $0.deliveryState == .sent) } }
            try await waitFor("app to peer") { try await self.api.messageCount(accessToken: peer.accessToken, roomID: roomID, body: appText) == 1 }
            phase("two-way messaging")
            try stopSynapse()
            try await waitFor("disconnect evidence") { if case .reconnecting = restored.connectionStatus { return true }; return restored.connectionStatus == .offline }
            let queuedText = "c21-recovery-\(UUID().uuidString)"
            _ = try? await restored.send(text: queuedText, to: roomID)
            try await waitFor("local queued, paused, or failed echo") {
                restored.messages.contains {
                    $0.body == queuedText && ($0.deliveryState == .queued || $0.deliveryState == .paused || $0.deliveryState == .failed)
                }
            }
            // Leave a genuine sync gap so the peer's new events must be caught
            // up after reconnect, rather than an already-connected loop.
            try await waitFor("retry backoff evidence") {
                if case .reconnecting(let delay) = restored.connectionStatus { return delay >= 8 }
                return false
            }
            // Exhaust the SDK send attempts before bringing the server back,
            // so this exercises an actual disabled queue rather than a send
            // that was still in flight when connectivity returned.
            try await waitFor("SDK send attempts exhausted") {
                restored.messages.contains { $0.body == queuedText && $0.deliveryState == .paused }
            }
            phase("disconnected with one queued, paused, or failed message")
            try await startAndWaitForSynapse()
            let afterRecovery = ["c21-after-\(UUID().uuidString)", "c21-after-\(UUID().uuidString)"]
            for text in afterRecovery { try await api.sendText(accessToken: peer.accessToken, roomID: roomID, body: text) }
            try await waitFor("automatic reconnect") { restored.connectionStatus == .connected }
            try await waitFor("sync catch-up") { afterRecovery.allSatisfy { expected in restored.messages.contains { $0.body == expected && $0.sender == peer.userID } } }
            try await waitFor("recoverable send parked") {
                restored.messages.contains { $0.body == queuedText && $0.isOwn && $0.deliveryState == .paused }
            }
            guard let paused = restored.messages.first(where: { $0.body == queuedText && $0.isOwn && $0.deliveryState == .paused }) else {
                throw SynapseTestError(step: "recoverable send parked", status: nil, errcode: nil)
            }
            let deliveriesBeforeRetry = try await api.messageCount(accessToken: peer.accessToken, roomID: roomID, body: queuedText)
            XCTAssertEqual(deliveriesBeforeRetry, 0, "Successful sync must not resume a paused send queue")
            phase("sync recovered while send remains paused")
            try await restored.retryMessage(paused.id)
            try await waitFor("queued message remote echo") { restored.messages.contains { $0.body == queuedText && $0.isOwn && ($0.deliveryState == nil || $0.deliveryState == .sent) } }
            try await exactlyOnce(accessToken: peer.accessToken, roomID: roomID, body: queuedText)
            phase("outage recovery and exactly once")
        }
        try await waitFor("restored service release") { restoredReleased == nil }
    }

    private func requiredHomeserver() throws -> URL {
        guard let value = ProcessInfo.processInfo.environment["BOTVILLE_E2E_HOMESERVER"] else {
            throw XCTSkip("Set BOTVILLE_E2E_HOMESERVER to the local Synapse URL to run acceptance tests.")
        }
        guard let url = try? HomeserverPolicy(allowLocalHTTP: true).validatedURL(value),
              url.scheme == "http" else {
            throw SynapseTestError(step: "invalid local homeserver", status: nil, errcode: nil)
        }
        return url
    }

    private func requiredArtifactRoot() throws -> URL {
        guard let raw = ProcessInfo.processInfo.environment["BOTVILLE_E2E_ARTIFACT_ROOT"], !raw.isEmpty else {
            throw XCTSkip("Set BOTVILLE_E2E_ARTIFACT_ROOT to a disposable directory inside this worktree.")
        }
        let root = URL(fileURLWithPath: raw, isDirectory: true).resolvingSymlinksInPath().standardizedFileURL
        let worktree = URL(fileURLWithPath: FileManager.default.currentDirectoryPath, isDirectory: true).resolvingSymlinksInPath().standardizedFileURL
        guard root.path.hasPrefix(worktree.path + "/") else {
            throw SynapseTestError(step: "artifact root outside worktree", status: nil, errcode: nil)
        }
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        return root
    }

    private func registerAccount(prefix: String = "c21") async throws -> SynapseAccount {
        let localpart = "\(prefix)_\(String(UUID().uuidString.replacingOccurrences(of: "-", with: "").prefix(16)).lowercased())"
        let password = try randomPassword()
        let account = try await api.register(localpart: localpart, password: password)
        accounts.append(account) // Add immediately so every later failure cleans it.
        return account
    }

    private func randomPassword() throws -> String {
        let key = try SessionCredentials.makeStoreKey()
        return key.base64EncodedString()
    }

    private func waitFor(_ step: String, timeout: Duration = .seconds(35), condition: @escaping () async throws -> Bool) async throws {
        let deadline = ContinuousClock.now + timeout
        while ContinuousClock.now < deadline {
            if try await condition() { return }
            try await Task.sleep(for: .milliseconds(250))
        }
        throw SynapseTestError(step: step, status: nil, errcode: nil)
    }

    private func phase(_ name: String) { print("[local Synapse] PASS: \(name)") }

    private func stopSynapse() throws {
        try docker("stop")
    }

    private func startSynapse() throws {
        try docker("start")
    }

    private func requiredContainer() throws -> String {
        guard let value = ProcessInfo.processInfo.environment["BOTVILLE_E2E_CONTAINER"], !value.isEmpty else {
            throw XCTSkip("Set BOTVILLE_E2E_CONTAINER to the disposable local Synapse container for the outage check.")
        }
        return value
    }

    private func startAndWaitForSynapse() async throws {
        try startSynapse()
        try await api.waitForVersions()
    }

    private func exactlyOnce(accessToken: String, roomID: String, body: String) async throws {
        try await waitFor("queued delivery") { try await self.api.messageCount(accessToken: accessToken, roomID: roomID, body: body) == 1 }
        for _ in 0..<4 {
            guard try await api.messageCount(accessToken: accessToken, roomID: roomID, body: body) == 1 else {
                throw SynapseTestError(step: "queued delivery count", status: nil, errcode: nil)
            }
            try await Task.sleep(for: .seconds(1))
        }
    }

    private func docker(_ verb: String) throws {
        #if os(macOS)
        let container = try requiredContainer()
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = ["docker", verb, container]
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        try process.run()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else { throw SynapseTestError(step: "docker \(verb)", status: Int(process.terminationStatus), errcode: nil) }
        #else
        throw XCTSkip("The local Docker outage check is macOS-only.")
        #endif
    }

    private func cleanupAndAssert(ensureSynapseRunning: Bool = true) async {
        if ensureSynapseRunning {
            do { try await startAndWaitForSynapse() }
            catch { XCTFail("cleanup Synapse start failed: \(safeDescription(error))") }
        }
        var allDeactivated = true
        for (index, account) in accounts.enumerated() {
            var cleanupError: Error?
            var deactivated = false
            var rejectionCode = ""
            for attempt in 0..<3 {
                do {
                    if !deactivated {
                        try await api.deactivate(account)
                        deactivated = true
                    }
                    rejectionCode = try await api.assertDeactivated(account)
                    cleanupError = nil
                    break
                } catch {
                    cleanupError = error
                    if attempt < 2 {
                        let retryMS = (error as? SynapseTestError)?.retryAfterMS ?? 1_000
                        try? await Task.sleep(for: .milliseconds(min(max(retryMS, 1_000), 60_000)))
                    }
                }
            }
            if let cleanupError {
                allDeactivated = false
                XCTFail("cleanup account failed: \(safeDescription(cleanupError))")
            } else {
                phase("account \(index + 1) erase=true; login rejected 403 \(rejectionCode)")
            }
        }
        if allDeactivated { accounts.removeAll() }
        do { try await waitFor("service released before SQLite removal") { self.liveService == nil } }
        catch {
            XCTFail("cleanup service release failed")
            return
        }
        do {
            try sessionStore?.delete()
            XCTAssertTrue(try sessionStore?.load() == nil, "in-memory session must be cleared")
        } catch { XCTFail("cleanup session store failed") }
        if let artifacts {
            do {
                try FileManager.default.removeItem(at: artifacts)
                XCTAssertFalse(FileManager.default.fileExists(atPath: artifacts.path), "SQLite test directory must be removed")
                phase("local cleanup")
            } catch { XCTFail("cleanup SQLite failed") }
        }
    }

    private func safeDescription(_ error: Error) -> String {
        if let error = error as? SynapseTestError { return error.description }
        if let error = error as? AuthenticationError { return error.localizedDescription }
        if let error = error as? MatrixServiceError { return error.localizedDescription }
        if let error = error as? EncryptionAuthorizationError { return error.localizedDescription }
        return "failure type=\(String(reflecting: type(of: error)))"
    }
}

private struct SynapseAccount {
    let localpart: String
    let userID: String
    let password: String
    let accessToken: String
}

private struct SynapseTestError: Error, CustomStringConvertible {
    let step: String
    let status: Int?
    let errcode: String?
    var retryAfterMS: Int? = nil
    var description: String { "\(step) status=\(status.map(String.init) ?? "none") errcode=\(errcode ?? "none")" }
}

private final class SynapseAPI: @unchecked Sendable {
    private let homeserver: URL
    private let session: URLSession

    init(homeserver: URL) {
        self.homeserver = homeserver
        let configuration = URLSessionConfiguration.ephemeral
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.urlCache = nil
        configuration.httpCookieStorage = nil
        configuration.httpShouldSetCookies = false
        configuration.timeoutIntervalForRequest = 10
        configuration.timeoutIntervalForResource = 15
        self.session = URLSession(configuration: configuration, delegate: LocalRedirectBlocker(), delegateQueue: nil)
    }

    func waitForVersions() async throws {
        let deadline = ContinuousClock.now + .seconds(40)
        while ContinuousClock.now < deadline {
            if (try? await request("/_matrix/client/versions", method: "GET")) != nil { return }
            try await Task.sleep(for: .milliseconds(250))
        }
        throw SynapseTestError(step: "Synapse readiness timeout", status: nil, errcode: nil)
    }

    func register(localpart: String, password: String) async throws -> SynapseAccount {
        let body: [String: Any] = ["username": localpart, "password": password, "auth": ["type": "m.login.dummy"]]
        let object = try await request("/_matrix/client/v3/register", body: body)
        guard let userID = object["user_id"] as? String, let token = object["access_token"] as? String else { throw SynapseTestError(step: "register", status: nil, errcode: nil) }
        return SynapseAccount(localpart: localpart, userID: userID, password: password, accessToken: token)
    }

    func createRoom(accessToken: String, invite: String) async throws -> String {
        let object = try await request("/_matrix/client/v3/createRoom", token: accessToken, body: ["invite": [invite]])
        guard let roomID = object["room_id"] as? String else { throw SynapseTestError(step: "createRoom", status: nil, errcode: nil) }
        return roomID
    }

    func roomEncryptionAlgorithm(accessToken: String, roomID: String) async throws -> String? {
        let escaped = roomID.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? roomID
        do {
            let state = try await request("/_matrix/client/v3/rooms/\(escaped)/state/m.room.encryption", method: "GET", token: accessToken)
            guard let algorithm = state["algorithm"] as? String else {
                throw SynapseTestError(step: "room encryption state", status: nil, errcode: nil)
            }
            return algorithm
        } catch let error as SynapseTestError where error.status == 404 {
            return nil
        }
    }

    func sendText(accessToken: String, roomID: String, body: String) async throws {
        _ = try await request("/_matrix/client/v3/rooms/\(roomID.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? roomID)/send/m.room.message/\(UUID().uuidString)", method: "PUT", token: accessToken, body: ["msgtype": "m.text", "body": body])
    }

    func messageCount(accessToken: String, roomID: String, body: String) async throws -> Int {
        let escaped = roomID.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? roomID
        let object = try await request("/_matrix/client/v3/rooms/\(escaped)/messages?dir=b&limit=100", method: "GET", token: accessToken)
        let chunk = object["chunk"] as? [[String: Any]] ?? []
        return chunk.reduce(into: 0) { count, event in
            guard event["type"] as? String == "m.room.message", let content = event["content"] as? [String: Any] else { return }
            if content["body"] as? String == body { count += 1 }
        }
    }

    func deactivate(_ account: SynapseAccount) async throws {
        let auth: [String: Any] = ["type": "m.login.password", "identifier": ["type": "m.id.user", "user": account.userID], "password": account.password]
        _ = try await request("/_matrix/client/v3/account/deactivate", token: account.accessToken, body: ["erase": true, "auth": auth])
    }

    func assertDeactivated(_ account: SynapseAccount) async throws -> String {
        do {
            _ = try await request("/_matrix/client/v3/login", body: ["type": "m.login.password", "identifier": ["type": "m.id.user", "user": account.userID], "password": account.password])
            throw SynapseTestError(step: "deactivated login unexpectedly succeeded", status: nil, errcode: nil)
        } catch let error as SynapseTestError {
            // Synapse may erase the password hash before login classification,
            // returning M_FORBIDDEN. Only accept an auth rejection after the
            // successful deactivate response; throttling is never a pass.
            guard error.status == 403,
                  let code = error.errcode,
                  ["M_USER_DEACTIVATED", "M_FORBIDDEN"].contains(code) else { throw error }
            return code
        }
    }

    private func request(_ path: String, method: String = "POST", token: String? = nil, body: [String: Any]? = nil) async throws -> [String: Any] {
        guard let url = URL(string: path, relativeTo: homeserver) else { throw SynapseTestError(step: "URL", status: nil, errcode: nil) }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let token { request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        if let body { request.httpBody = try JSONSerialization.data(withJSONObject: body) }
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw SynapseTestError(step: path, status: nil, errcode: nil) }
        let object = (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
        guard (200..<300).contains(http.statusCode) else {
            throw SynapseTestError(step: path, status: http.statusCode, errcode: object["errcode"] as? String,
                                   retryAfterMS: object["retry_after_ms"] as? Int)
        }
        return object
    }
}

private final class LocalRedirectBlocker: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        // The test homeserver is explicit loopback only; following a redirect
        // could leak test Authorization headers to another origin.
        completionHandler(nil)
    }
}
