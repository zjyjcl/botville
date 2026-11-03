// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

@testable import BotvilleCore
import Foundation
import MatrixRustSDK
import XCTest

@MainActor
final class RoomEncryptionServiceTests: XCTestCase {
    private var temporaryDirectories: [URL] = []

    override func tearDown() {
        for directory in temporaryDirectories {
            try? FileManager.default.removeItem(at: directory)
        }
        temporaryDirectories = []
        super.tearDown()
    }

    func testUnknownOrAIInviteCreatesPlaintextRoomByDefault() async throws {
        let client = SecurityClient()
        let service = try await readyService(client: client, policy: AIParticipantPolicy(userIDPrefixes: ["@ai-"]))
        let draft = RoomCreationDraft(isDirect: true, invitees: ["@ai-helper:matrix.test"])

        _ = try await service.createRoom(draft: draft, consent: EncryptionConsent())

        XCTAssertEqual(client.createRequests.last?.isEncrypted, false)

        let unknownClient = SecurityClient()
        let unknownService = try await readyService(client: unknownClient, policy: AIParticipantPolicy())
        _ = try await unknownService.createRoom(draft: RoomCreationDraft(isDirect: true, invitees: ["@person:matrix.test"]), consent: EncryptionConsent())
        XCTAssertEqual(unknownClient.createRequests.last?.isEncrypted, false)
    }

    func testConfirmedHumanDraftCreatesEncryptedAndChangingDraftDoesNotReuseConsent() async throws {
        let client = SecurityClient()
        let policy = AIParticipantPolicy(userIDPrefixes: ["@ai-"])
        let service = try await readyService(client: client, policy: policy)
        let draft = RoomCreationDraft(isDirect: true, invitees: ["@person:matrix.test"])
        var consent = EncryptionConsent()
        consent.request(for: .newRoom(draft))
        XCTAssertTrue(consent.confirm(for: .newRoom(draft)))

        _ = try await service.createRoom(draft: draft, consent: consent)
        XCTAssertEqual(client.createRequests.last?.isEncrypted, true)

        let changed = RoomCreationDraft(isDirect: true, invitees: ["@other:matrix.test"])
        await XCTAssertThrowsErrorAsync(try await service.createRoom(draft: changed, consent: consent)) { error in
            XCTAssertEqual(error as? EncryptionAuthorizationError, .confirmationRequired)
        }
    }

    func testEnableEncryptionRechecksInvitedAIAndUnknownMembers() async throws {
        let room = SecurityRoom(members: [securityMember("@person:matrix.test", .join), securityMember("@ai-helper:matrix.test", .invite)], activeCount: 2)
        let client = SecurityClient(room: room)
        let service = try await readyService(client: client, policy: AIParticipantPolicy(userIDPrefixes: ["@ai-"]))
        var consent = EncryptionConsent()
        consent.request(for: .existingRoom(room.id()))
        XCTAssertTrue(consent.confirm(for: .existingRoom(room.id())))

        await XCTAssertThrowsErrorAsync(try await service.enableEncryption(roomID: room.id(), consent: consent)) { error in
            XCTAssertEqual(error as? EncryptionAuthorizationError, .containsAI)
        }
        XCTAssertEqual(room.enableCalls, 0)

        room.membersError = ClientError.Generic(msg: "unavailable", details: nil)
        await XCTAssertThrowsErrorAsync(try await service.enableEncryption(roomID: room.id(), consent: consent))
        XCTAssertEqual(room.enableCalls, 0)
    }

    func testFullMemberFetchAllowsHumansWhenSyncSummaryIsZero() async throws {
        let room = SecurityRoom(
            members: [securityMember("@owner:matrix.test", .join), securityMember("@person:matrix.test", .join)],
            activeCount: 0
        )
        let client = SecurityClient(room: room)
        let service = try await readyService(client: client, policy: AIParticipantPolicy(userIDPrefixes: ["@ai-"]))
        var consent = EncryptionConsent()
        consent.request(for: .existingRoom(room.id()))
        XCTAssertTrue(consent.confirm(for: .existingRoom(room.id())))

        try await service.enableEncryption(roomID: room.id(), consent: consent)

        XCTAssertEqual(room.enableCalls, 1)
    }

    func testEncryptedRoomDoesNotEnableAgainAndAIInviteNeedsAcknowledgement() async throws {
        let room = SecurityRoom(state: .encrypted, members: [securityMember("@person:matrix.test", .join)], activeCount: 1)
        let client = SecurityClient(room: room)
        let service = try await readyService(client: client, policy: AIParticipantPolicy(userIDPrefixes: ["@ai-"]))

        try await service.enableEncryption(roomID: room.id(), consent: EncryptionConsent())
        XCTAssertEqual(room.enableCalls, 0)

        await XCTAssertThrowsErrorAsync(try await service.inviteUser("@ai-helper:matrix.test", to: room.id())) { error in
            guard case MatrixServiceError.aiInvitationConfirmationRequired = error else {
                return XCTFail("Expected AI invitation confirmation, got \(error)")
            }
        }
        try await service.inviteUser("@ai-helper:matrix.test", to: room.id(), acknowledgedEncryptionWarning: true)
        XCTAssertEqual(room.invitedUsers, ["@ai-helper:matrix.test"])
    }

    func testRefreshUsesSDKReadbackForBadgeAndCryptoWarnings() async throws {
        let room = SecurityRoom(state: .encrypted, members: [securityMember("@person:matrix.test", .join)], activeCount: 1)
        let crypto = SecurityEncryption(verification: .unverified, backup: .unknown, recovery: .disabled)
        let client = SecurityClient(room: room, encryption: crypto)
        let service = try await readyService(client: client, policy: AIParticipantPolicy(userIDPrefixes: ["@ai-"]))

        await service.refreshRoomSecurity(room.id())

        XCTAssertEqual(service.rooms.first?.encryption, .encrypted)
        XCTAssertTrue(service.cryptoSafetyWarnings.contains(where: { $0.contains("本设备未验证") }))
        XCTAssertTrue(service.cryptoSafetyWarnings.contains(where: { $0.contains("无法确认密钥备份状态") }))
    }

    private func readyService(client: SecurityClient, policy: AIParticipantPolicy) async throws -> MatrixService {
        let store = SecurityStore(credentials: credentials())
        let appDirectory = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let baseDirectory = appDirectory.appendingPathComponent(".build/c26-service-tests/\(UUID().uuidString)")
        temporaryDirectories.append(baseDirectory)
        let directory = SessionStoreDirectory(baseURL: baseDirectory)
        let service = MatrixService(sessionStore: store, storeDirectory: directory, monitorNetwork: false, aiParticipantPolicy: policy)
        service.clientFactory = { _, _, _ in client }
        await service.restoreSession()
        XCTAssertTrue(service.isLoggedIn)
        return service
    }

    private func credentials() -> SessionCredentials {
        SessionCredentials(accessToken: "test", refreshToken: nil, userID: "@owner:matrix.test", deviceID: "TEST", homeserverURL: "https://matrix.test", oauthData: nil, storeID: UUID(), storeKey: Data(repeating: 1, count: 32))
    }
}

private final class SecurityStore: SessionStore, @unchecked Sendable {
    var credentials: SessionCredentials?
    init(credentials: SessionCredentials?) { self.credentials = credentials }
    func load() throws -> SessionCredentials? { credentials }
    func save(_ credentials: SessionCredentials) throws { self.credentials = credentials }
    func delete() throws { credentials = nil }
}

private final class SecurityClient: Client, @unchecked Sendable {
    let room: SecurityRoom?
    let securityEncryption: SecurityEncryption
    private(set) var createRequests: [CreateRoomParameters] = []
    init(room: SecurityRoom? = nil, encryption: SecurityEncryption = SecurityEncryption()) {
        self.room = room; securityEncryption = encryption; super.init(noHandle: .init())
    }
    required init(unsafeFromHandle handle: UInt64) { fatalError("test fake must use noHandle") }
    override func restoreSession(session: Session) async throws {}
    override func setDelegate(delegate: ClientDelegate?) throws -> TaskHandle? { nil }
    override func rooms() -> [Room] { room.map { [$0] } ?? [] }
    override func getRoom(roomId: String) throws -> Room? { room?.id() == roomId ? room : nil }
    override func createRoom(request: CreateRoomParameters) async throws -> String { createRequests.append(request); return "!created:matrix.test" }
    override func enableAllSendQueues(enable: Bool) async {}
    override func syncOnceV2(settings: SyncSettingsV2) async throws -> SyncResponseV2 { throw ClientError.Generic(msg: "offline", details: nil) }
    override func encryption() -> Encryption { securityEncryption }
}

private final class SecurityRoom: Room, @unchecked Sendable {
    private let roomID = "!security:matrix.test"
    var state: EncryptionState
    var memberValues: [RoomMember]
    var activeCount: UInt64
    var membersError: Error?
    private(set) var enableCalls = 0
    private(set) var invitedUsers: [String] = []
    init(state: EncryptionState = .notEncrypted, members: [RoomMember] = [securityMember("@person:matrix.test", .join)], activeCount: UInt64 = 1) {
        self.state = state; memberValues = members; self.activeCount = activeCount; super.init(noHandle: .init())
    }
    required init(unsafeFromHandle handle: UInt64) { fatalError("test fake must use noHandle") }
    override func id() -> String { roomID }
    override func displayName() -> String? { "Security" }
    override func membership() -> Membership { .joined }
    override func roomInfo() async throws -> RoomInfo { throw ClientError.Generic(msg: "not needed", details: nil) }
    override func latestEvent() async -> LatestEventValue { .none }
    override func isDirect() async -> Bool { false }
    override func latestEncryptionState() async throws -> EncryptionState { state }
    override func encryptionState() -> EncryptionState { state }
    override func members() async throws -> RoomMembersIterator { if let membersError { throw membersError }; return SecurityMembersIterator(memberValues) }
    override func activeMembersCount() -> UInt64 { activeCount }
    override func enableEncryption() async throws { enableCalls += 1; state = .encrypted }
    override func inviteUserById(userId: String) async throws { invitedUsers.append(userId) }
}

private final class SecurityMembersIterator: RoomMembersIterator, @unchecked Sendable {
    private var values: [RoomMember]?
    init(_ values: [RoomMember]) { self.values = values; super.init(noHandle: .init()) }
    required init(unsafeFromHandle handle: UInt64) { values = nil; super.init(unsafeFromHandle: handle) }
    override func len() -> UInt32 { UInt32(values?.count ?? 0) }
    override func nextChunk(chunkSize: UInt32) -> [RoomMember]? { defer { values = nil }; return values }
}

private final class SecurityEncryption: Encryption, @unchecked Sendable {
    let verification: VerificationState; let backup: BackupState; let recovery: RecoveryState
    init(verification: VerificationState = .verified, backup: BackupState = .enabled, recovery: RecoveryState = .enabled) { self.verification = verification; self.backup = backup; self.recovery = recovery; super.init(noHandle: .init()) }
    required init(unsafeFromHandle handle: UInt64) { fatalError("test fake must use noHandle") }
    override func verificationState() -> VerificationState { verification }
    override func backupState() -> BackupState { backup }
    override func recoveryState() -> RecoveryState { recovery }
}

private func securityMember(_ userID: String, _ membership: MembershipState) -> RoomMember {
    RoomMember(userId: userID, displayName: nil, avatarUrl: nil, status: nil, call: nil, membership: membership, isNameAmbiguous: false, powerLevel: .value(value: 0), isIgnored: false, suggestedRoleForPowerLevel: .user, membershipChangeReason: nil, isServiceMember: false)
}

private func XCTAssertThrowsErrorAsync<T>(_ expression: @autoclosure () async throws -> T, _ handler: ((Error) -> Void)? = nil) async {
    do { _ = try await expression(); XCTFail("Expected an error") }
    catch { handler?(error) }
}
