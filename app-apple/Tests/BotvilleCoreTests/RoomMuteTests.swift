// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import XCTest
import MatrixRustSDK
import UserNotifications
@testable import BotvilleCore

/// All lifecycle fake clients must return an actual mock here, never enter FFI
/// through the no-handle Client stub when production activation asks for rules.
final class TestNotificationSettings: NotificationSettings, @unchecked Sendable {
    private let lock = NSLock()
    private var muted = Set<String>()
    private var delegate: NotificationSettingsDelegate?
    init() { super.init(noHandle: .init()) }
    required init(unsafeFromHandle handle: UInt64) { fatalError("Unused test initializer") }
    override func setDelegate(delegate: NotificationSettingsDelegate?) { lock.withLock { self.delegate = delegate } }
    private var rawReads = 0
    private var roomReads: [String] = []
    private var failedRooms = Set<String>()
    private var readEncryption: [Bool] = []
    private var unmuteEncryption: [Bool] = []
    private var rawReadContinuation: CheckedContinuation<String?, Error>?
    var suspendRawRead = false
    var missingRules = false
    var rawReadCount: Int { lock.withLock { rawReads } }
    var roomReadIDs: [String] { lock.withLock { roomReads } }
    var roomReadEncryption: [Bool] { lock.withLock { readEncryption } }
    var unmuteEncryptionArguments: [Bool] { lock.withLock { unmuteEncryption } }
    func failReads(for rooms: Set<String>) { lock.withLock { failedRooms = rooms } }
    var isRawReadBlocked: Bool { lock.withLock { rawReadContinuation != nil } }
    func releaseRawRead() {
        let pending = lock.withLock { let value = rawReadContinuation; rawReadContinuation = nil; return value }
        pending?.resume(returning: "{}")
    }
    override func getRawPushRules() async throws -> String? {
        let blocked = lock.withLock { rawReads += 1; return suspendRawRead }
        if blocked {
            return try await withCheckedThrowingContinuation { continuation in
                lock.withLock { rawReadContinuation = continuation }
            }
        }
        return missingRules ? nil : "{}"
    }
    override func getRoomNotificationSettings(roomId: String, isEncrypted: Bool, isOneToOne: Bool) async throws -> RoomNotificationSettings {
        try lock.withLock {
            roomReads.append(roomId)
            readEncryption.append(isEncrypted)
            if failedRooms.contains(roomId) { throw MatrixServiceError.operationFailed }
            return RoomNotificationSettings(mode: muted.contains(roomId) ? .mute : .allMessages, isDefault: false)
        }
    }
    override func setRoomNotificationMode(roomId: String, mode: RoomNotificationMode) async throws {
        let callback = lock.withLock {
            if mode == .mute { muted.insert(roomId) } else { muted.remove(roomId) }
            return delegate
        }
        callback?.settingsDidChange()
    }
    override func unmuteRoom(roomId: String, isEncrypted: Bool, isOneToOne: Bool) async throws {
        lock.withLock { unmuteEncryption.append(isEncrypted) }
        try await setRoomNotificationMode(roomId: roomId, mode: .allMessages)
    }
}

final class MemoryRoomMuteStore: SharedRoomMuteStoring, @unchecked Sendable {
    private let lock = NSLock()
    private var values: Set<String> = []
    var fails = false
    private(set) var saveCount = 0
    func load() throws -> Set<String> { lock.withLock { values } }
    func save(_ hashes: Set<String>) throws {
        saveCount += 1
        if fails { throw CocoaError(.fileWriteNoPermission) }
        lock.withLock { values = hashes }
    }
    func clear() throws { try save([]) }
}

struct UnavailableRoomMuteStore: SharedRoomMuteStoring {
    func load() throws -> Set<String> { throw CocoaError(.fileReadNoPermission) }
    func save(_ hashes: Set<String>) throws { throw CocoaError(.fileWriteNoPermission) }
    func clear() throws { throw CocoaError(.fileWriteNoPermission) }
}

@MainActor
private final class FakeRoomNotificationProvider: RoomNotificationSettingsProviding {
    var muted = false
    var failRead = false
    var failWrite = false
    var ignoreWrite = false
    var writes = 0
    var reads = 0
    var suspendWrite = false
    var pendingWrite: CheckedContinuation<Void, Error>?
    var suspendRead = false
    var pendingRead: CheckedContinuation<Bool, Error>?
    func isMuted(_ room: MatrixRoom) async throws -> Bool {
        reads += 1
        if failRead { throw MatrixServiceError.operationFailed }
        if suspendRead {
            suspendRead = false
            return try await withCheckedThrowingContinuation { pendingRead = $0 }
        }
        return muted
    }
    func setMuted(_ value: Bool, room: MatrixRoom) async throws {
        writes += 1
        if failWrite { throw MatrixServiceError.operationFailed }
        if suspendWrite { try await withCheckedThrowingContinuation { pendingWrite = $0 } }
        if !ignoreWrite { muted = value }
    }
}

@MainActor
final class RoomMuteTests: XCTestCase {
    private let room = MatrixRoom(id: "!mute:test", name: "Test", unreadCount: 7, encryption: .unencrypted)

    private func fixture() -> (RoomMuteController, FakeRoomNotificationProvider, MemoryRoomMuteStore) {
        let store = MemoryRoomMuteStore(), provider = FakeRoomNotificationProvider()
        let controller = RoomMuteController(sharedStore: store)
        controller.configure(provider: provider)
        return (controller, provider, store)
    }

    func testInitialStateUnknownAndCannotWriteUntilReadback() async {
        let (controller, provider, _) = fixture()
        XCTAssertFalse(controller.state(for: room.id).isLoaded)
        await controller.setMuted(true, room: room)
        XCTAssertEqual(provider.writes, 0)
    }

    func testReadFailureRetryFailureThenSuccess() async {
        let (controller, provider, store) = fixture()
        provider.failRead = true
        await controller.refresh(room)
        XCTAssertEqual(controller.state(for: room.id).readStatus, .failed)
        XCTAssertFalse(controller.state(for: room.id).isLoaded)
        XCTAssertNotNil(controller.state(for: room.id).errorMessage)
        await controller.refresh(room)
        XCTAssertEqual(controller.state(for: room.id).readStatus, .failed)
        XCTAssertEqual(store.saveCount, 0)
        provider.failRead = false
        provider.muted = true
        await controller.refresh(room)
        XCTAssertEqual(controller.state(for: room.id).readStatus, .loaded)
        XCTAssertTrue(controller.state(for: room.id).isLoaded)
        XCTAssertTrue(controller.isMuted(room.id))
        XCTAssertNil(controller.state(for: room.id).errorMessage)
    }

    func testPartialBatchFailureAndSingleRoomRetryDoNotChangeOtherRoom() async throws {
        let settings = TestNotificationSettings(), store = MemoryRoomMuteStore()
        let controller = RoomMuteController(sharedStore: store)
        controller.configure(provider: SDKRoomNotificationSettings(settings))
        let other = MatrixRoom(id: "!other:test", name: "Other", encryption: .unencrypted)
        try await settings.setRoomNotificationMode(roomId: other.id, mode: .mute)
        settings.failReads(for: [room.id])
        await controller.refresh([room, other])
        let otherState = controller.state(for: other.id)
        XCTAssertEqual(otherState.readStatus, .loaded)
        XCTAssertTrue(otherState.isMuted)
        XCTAssertEqual(controller.state(for: room.id).readStatus, .failed)
        let readsBefore = settings.roomReadIDs.count
        await controller.refresh(room)
        XCTAssertEqual(Array(settings.roomReadIDs.dropFirst(readsBefore)), [room.id])
        XCTAssertEqual(controller.state(for: other.id), otherState)
        XCTAssertEqual(try store.load(), [SharedRoomMuteStore.hash(other.id)])
    }

    func testRetryShowsLoadingAndLateFailureCannotCrossSession() async {
        let (controller, provider, store) = fixture()
        provider.failRead = true
        await controller.refresh(room)
        provider.failRead = false
        provider.suspendRead = true
        let retry = Task { await controller.refresh(room) }
        while provider.pendingRead == nil { await Task.yield() }
        XCTAssertEqual(controller.state(for: room.id).readStatus, .loading)
        XCTAssertNil(controller.state(for: room.id).errorMessage)
        controller.reset()
        let next = FakeRoomNotificationProvider()
        next.muted = true
        controller.configure(provider: next)
        await controller.refresh(room)
        let saves = store.saveCount
        provider.pendingRead?.resume(throwing: MatrixServiceError.operationFailed)
        await retry.value
        XCTAssertEqual(controller.state(for: room.id).readStatus, .loaded)
        XCTAssertTrue(controller.isMuted(room.id))
        XCTAssertNil(controller.state(for: room.id).errorMessage)
        XCTAssertEqual(store.saveCount, saves)
    }

    func testCancelledRetryDoesNotRemainLoading() async {
        let (controller, provider, _) = fixture()
        provider.failRead = true
        await controller.refresh(room)
        provider.failRead = false
        provider.suspendRead = true
        let retry = Task { await controller.refresh(room) }
        while provider.pendingRead == nil { await Task.yield() }
        retry.cancel()
        provider.pendingRead?.resume(returning: true)
        await retry.value
        XCTAssertEqual(controller.state(for: room.id).readStatus, .failed)
        XCTAssertFalse(controller.state(for: room.id).isLoaded)
        XCTAssertNotNil(controller.state(for: room.id).errorMessage)
    }

    func testFailedToggleSupersedingReadDoesNotRestoreLoading() async {
        let (controller, provider, _) = fixture()
        await controller.refresh(room)
        provider.suspendRead = true
        let read = Task { await controller.refresh(room) }
        while provider.pendingRead == nil { await Task.yield() }
        provider.failWrite = true
        await controller.setMuted(true, room: room)
        provider.pendingRead?.resume(returning: true)
        await read.value
        XCTAssertEqual(controller.state(for: room.id).readStatus, .loaded)
        XCTAssertFalse(controller.isMuted(room.id))
        XCTAssertNotNil(controller.state(for: room.id).errorMessage)
    }

    func testBatchSavesOnceAndKeepsUnrelatedHashes() async throws {
        let (controller, provider, store) = fixture()
        provider.muted = true
        await controller.refresh(room)
        let rooms = (0..<8).map { MatrixRoom(id: "!batch\($0):test", name: "Test") }
        let saves = store.saveCount
        await controller.refresh(rooms)
        XCTAssertEqual(store.saveCount - saves, 1)
        XCTAssertEqual(try store.load(), Set((rooms + [room]).map { SharedRoomMuteStore.hash($0.id) }))
    }

    func testBatchWriteFailurePreservesConfirmedModesAndWarnsEveryAffectedRoom() async throws {
        let (controller, provider, store) = fixture()
        let other = MatrixRoom(id: "!other:test", name: "Other")
        provider.muted = true
        store.fails = true
        await controller.refresh([room, other])
        XCTAssertEqual(store.saveCount, 1)
        XCTAssertTrue(try store.load().isEmpty)
        for value in [room, other] {
            XCTAssertTrue(controller.isMuted(value.id))
            XCTAssertEqual(controller.state(for: value.id).readStatus, .loaded)
            XCTAssertTrue(controller.state(for: value.id).errorMessage?.contains("缓存") == true)
        }
        store.fails = false
        await controller.refresh([room, other])
        XCTAssertEqual(store.saveCount, 2)
        XCTAssertEqual(try store.load().count, 2)
        XCTAssertNil(controller.state(for: room.id).errorMessage)
    }

    func testBatchFileBytesMatchExistingSortedJSONArrayAndNSECanLoad() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("mute.json")
        let store = SharedRoomMuteStore(fileURL: url)
        let controller = RoomMuteController(sharedStore: store), provider = FakeRoomNotificationProvider()
        controller.configure(provider: provider)
        provider.muted = true
        let rooms = [room, MatrixRoom(id: "!other:test", name: "Other")]
        await controller.refresh(rooms)
        let hashes = Set(rooms.map { SharedRoomMuteStore.hash($0.id) })
        XCTAssertEqual(try Data(contentsOf: url), try JSONEncoder().encode(hashes.sorted()))
        let loaded = try SharedRoomMuteStore(fileURL: url).load()
        XCTAssertEqual(loaded, hashes)
        XCTAssertTrue(RoomMutePushPolicy.isMuted(userInfo: ["room_id": room.id], hashes: loaded))
    }

    func testReadbackUsesServerAndUpdatesSharedDigest() async throws {
        let (controller, provider, store) = fixture()
        provider.muted = true
        await controller.refresh(room)
        XCTAssertTrue(controller.state(for: room.id).isLoaded)
        XCTAssertTrue(controller.isMuted(room.id))
        XCTAssertEqual(try store.load(), [SharedRoomMuteStore.hash(room.id)])
    }

    func testStartupUsesCachedHashUntilConfirmedReadback() async throws {
        let store = MemoryRoomMuteStore()
        try store.save([SharedRoomMuteStore.hash(room.id)])
        let controller = RoomMuteController(sharedStore: store), provider = FakeRoomNotificationProvider()
        XCTAssertTrue(controller.isMuted(room.id))
        XCTAssertFalse(controller.state(for: room.id).isLoaded)
        controller.configure(provider: provider)
        await controller.refresh(room)
        XCTAssertFalse(controller.isMuted(room.id))
        XCTAssertTrue(try store.load().isEmpty)
    }

    func testMuteAndUnmuteSuccessBothReadBack() async throws {
        let (controller, provider, store) = fixture()
        await controller.refresh(room)
        await controller.setMuted(true, room: room)
        XCTAssertTrue(controller.isMuted(room.id))
        await controller.setMuted(false, room: room)
        XCTAssertFalse(controller.isMuted(room.id))
        XCTAssertFalse(controller.state(for: room.id).isUpdating)
        XCTAssertEqual(provider.reads, 3)
        XCTAssertEqual(provider.writes, 2)
        XCTAssertTrue(try store.load().isEmpty)
    }

    func testOptimisticToggleBlocksDuplicatesAndRefreshUntilCompletion() async {
        let (controller, provider, _) = fixture()
        await controller.refresh(room)
        provider.suspendWrite = true
        let change = Task { await controller.setMuted(true, room: room) }
        while provider.pendingWrite == nil { await Task.yield() }
        XCTAssertTrue(controller.isMuted(room.id))
        XCTAssertTrue(controller.state(for: room.id).isUpdating)
        await controller.setMuted(false, room: room)
        await controller.refresh(room)
        XCTAssertEqual(provider.writes, 1)
        XCTAssertEqual(provider.reads, 1)
        provider.pendingWrite?.resume()
        await change.value
        XCTAssertTrue(controller.isMuted(room.id))
        XCTAssertFalse(controller.state(for: room.id).isUpdating)
    }

    func testWriteFailureRollsBackUIAndSharedCacheWithChineseError() async throws {
        let (controller, provider, store) = fixture()
        provider.muted = true
        await controller.refresh(room)
        provider.failWrite = true
        await controller.setMuted(false, room: room)
        XCTAssertTrue(controller.isMuted(room.id))
        XCTAssertFalse(controller.state(for: room.id).isUpdating)
        XCTAssertTrue(controller.state(for: room.id).errorMessage?.contains("已恢复原设置") == true)
        XCTAssertEqual(try store.load(), [SharedRoomMuteStore.hash(room.id)])
    }

    func testReadFailureDoesNotInventConfirmedStateOrEraseCachedMute() async throws {
        let (controller, provider, store) = fixture()
        provider.muted = true
        await controller.refresh(room)
        provider.failRead = true
        await controller.refresh(room)
        XCTAssertTrue(controller.isMuted(room.id))
        XCTAssertEqual(controller.state(for: room.id).readStatus, .failed)
        XCTAssertNotNil(controller.state(for: room.id).errorMessage)
        XCTAssertFalse(try store.load().isEmpty)
    }

    func testPostWriteReadFailureRollsBackAndNextRefreshReconciles() async {
        let (controller, provider, _) = fixture()
        await controller.refresh(room)
        provider.failRead = true
        await controller.setMuted(true, room: room)
        XCTAssertFalse(controller.isMuted(room.id))
        XCTAssertNotNil(controller.state(for: room.id).errorMessage)
        provider.failRead = false
        await controller.refresh(room)
        XCTAssertTrue(controller.isMuted(room.id))
    }

    func testServerDisagreementUsesReadbackInsteadOfRequestedValue() async {
        let (controller, provider, _) = fixture()
        await controller.refresh(room)
        provider.ignoreWrite = true
        await controller.setMuted(true, room: room)
        XCTAssertFalse(controller.isMuted(room.id))
        XCTAssertNotNil(controller.state(for: room.id).errorMessage)
    }

    func testOlderReadCannotUndoSuccessfulToggle() async {
        let (controller, provider, _) = fixture()
        await controller.refresh(room)
        provider.suspendRead = true
        let read = Task { await controller.refresh(room) }
        while provider.pendingRead == nil { await Task.yield() }
        await controller.setMuted(true, room: room)
        provider.pendingRead?.resume(returning: false)
        await read.value
        XCTAssertTrue(controller.isMuted(room.id))
    }

    func testLogoutClearsAndRejectsLateWriteCompletion() async throws {
        let (controller, provider, store) = fixture()
        await controller.refresh(room)
        provider.suspendWrite = true
        let write = Task { await controller.setMuted(true, room: room) }
        while provider.pendingWrite == nil { await Task.yield() }
        controller.reset()
        provider.pendingWrite?.resume()
        await write.value
        XCTAssertTrue(controller.states.isEmpty)
        XCTAssertTrue(try store.load().isEmpty)
    }

    func testLogoutRejectsLateReadCompletion() async throws {
        let (controller, provider, store) = fixture()
        provider.suspendRead = true
        let read = Task { await controller.refresh(room) }
        while provider.pendingRead == nil { await Task.yield() }
        controller.reset()
        provider.pendingRead?.resume(returning: true)
        await read.value
        XCTAssertTrue(controller.states.isEmpty)
        XCTAssertTrue(try store.load().isEmpty)
    }

    func testSharedCleanupFailureDoesNotThrowAndCanRetry() async throws {
        let (controller, provider, store) = fixture()
        provider.muted = true
        await controller.refresh(room)
        store.fails = true
        controller.reset()
        XCTAssertFalse(controller.isMuted(room.id))
        XCTAssertTrue(controller.states.isEmpty)
        store.fails = false
        controller.reset()
        XCTAssertTrue(try store.load().isEmpty)
    }

    func testSharedWriteFailureKeepsConfirmedModeAndWarns() async {
        let (controller, provider, store) = fixture()
        store.fails = true
        provider.muted = true
        await controller.refresh(room)
        XCTAssertTrue(controller.isMuted(room.id))
        XCTAssertTrue(controller.state(for: room.id).errorMessage?.contains("缓存") == true)
    }

    func testMissingContainerSilentlyDegradesConfirmedToggles() async throws {
        let store = SharedRoomMuteStore(fileURL: nil)
        XCTAssertThrowsError(try store.load()) { error in
            guard case SharedRoomMuteStoreError.containerUnavailable = error else {
                return XCTFail("Missing container must have a distinct error")
            }
        }
        let controller = RoomMuteController(sharedStore: store)
        let provider = FakeRoomNotificationProvider()
        controller.configure(provider: provider)
        await controller.refresh(room)
        await controller.setMuted(true, room: room)
        XCTAssertTrue(controller.isMuted(room.id))
        XCTAssertNil(controller.state(for: room.id).errorMessage)
        await controller.setMuted(false, room: room)
        XCTAssertFalse(controller.isMuted(room.id))
        XCTAssertNil(controller.state(for: room.id).errorMessage)
        controller.reset()
        XCTAssertTrue(controller.states.isEmpty)
    }

    func testSDKBatchChecksRawRulesOnceAndReadsEveryRoomFromServer() async throws {
        let settings = TestNotificationSettings()
        try await settings.setRoomNotificationMode(roomId: room.id, mode: .mute)
        let other = MatrixRoom(id: "!other:test", name: "Other", encryption: .unencrypted)
        let values = try await SDKRoomNotificationSettings(settings).mutedStates(for: [room, other])
        XCTAssertEqual(settings.rawReadCount, 1)
        XCTAssertEqual(settings.roomReadIDs, [room.id, other.id])
        XCTAssertEqual(values, [room.id: true, other.id: false])
    }

    func testMissingRawRulesCannotConfirmUnmutedBatch() async {
        let settings = TestNotificationSettings()
        settings.missingRules = true
        let controller = RoomMuteController(sharedStore: MemoryRoomMuteStore())
        controller.configure(provider: SDKRoomNotificationSettings(settings))
        await controller.refresh([room, MatrixRoom(id: "!other:test", name: "Other")])
        XCTAssertEqual(settings.rawReadCount, 1)
        XCTAssertTrue(settings.roomReadIDs.isEmpty)
        XCTAssertFalse(controller.state(for: room.id).isLoaded)
        XCTAssertNotNil(controller.state(for: room.id).errorMessage)
    }

    func testSDKBatchMiddleFailurePreservesOnlyThatRoomAndRetries() async throws {
        let settings = TestNotificationSettings(), store = MemoryRoomMuteStore()
        let controller = RoomMuteController(sharedStore: store)
        controller.configure(provider: SDKRoomNotificationSettings(settings))
        let middle = MatrixRoom(id: "!middle:test", name: "Middle", encryption: .encrypted)
        let last = MatrixRoom(id: "!last:test", name: "Last", encryption: .unencrypted)
        let rooms = [room, middle, last]
        try await settings.setRoomNotificationMode(roomId: middle.id, mode: .mute)
        await controller.refresh(rooms)
        let previous = controller.state(for: middle.id)
        settings.failReads(for: [middle.id])
        try await settings.setRoomNotificationMode(roomId: middle.id, mode: .allMessages)
        for room in [room, last] { try await settings.setRoomNotificationMode(roomId: room.id, mode: .mute) }
        await controller.refresh(rooms)
        XCTAssertEqual(settings.rawReadCount, 2)
        XCTAssertEqual(Array(settings.roomReadIDs.suffix(3)), rooms.map(\.id))
        XCTAssertTrue(controller.isMuted(room.id))
        XCTAssertTrue(controller.isMuted(last.id))
        XCTAssertEqual(controller.state(for: middle.id).isMuted, previous.isMuted)
        XCTAssertEqual(controller.state(for: middle.id).isLoaded, previous.isLoaded)
        XCTAssertEqual(controller.state(for: middle.id).readStatus, .failed)
        XCTAssertNotNil(controller.state(for: middle.id).errorMessage)
        XCTAssertEqual(try store.load(), Set(rooms.map { SharedRoomMuteStore.hash($0.id) }))
        settings.failReads(for: [])
        await controller.refresh(rooms)
        XCTAssertEqual(settings.rawReadCount, 3)
        XCTAssertFalse(controller.isMuted(middle.id))
        XCTAssertTrue(controller.state(for: middle.id).isLoaded)
    }

    func testRawFailurePreservesLoadedBatchAndDoesNotReadRooms() async throws {
        let settings = TestNotificationSettings()
        let controller = RoomMuteController(sharedStore: MemoryRoomMuteStore())
        controller.configure(provider: SDKRoomNotificationSettings(settings))
        try await settings.setRoomNotificationMode(roomId: room.id, mode: .mute)
        await controller.refresh([room])
        settings.missingRules = true
        await controller.refresh([room])
        XCTAssertEqual(settings.rawReadCount, 2)
        XCTAssertEqual(settings.roomReadIDs, [room.id])
        XCTAssertTrue(controller.isMuted(room.id))
        XCTAssertTrue(controller.state(for: room.id).isLoaded)
    }

    func testUnknownEncryptionRejectsUnmuteAndRestoresUIWithoutSDKWrite() async throws {
        let settings = TestNotificationSettings(), store = MemoryRoomMuteStore()
        let controller = RoomMuteController(sharedStore: store)
        controller.configure(provider: SDKRoomNotificationSettings(settings))
        try await settings.setRoomNotificationMode(roomId: room.id, mode: .mute)
        await controller.refresh(room)
        let unknown = room.withSecurity(encryption: .unknown, participants: .unknown)
        await controller.setMuted(false, room: unknown)
        XCTAssertTrue(settings.unmuteEncryptionArguments.isEmpty)
        XCTAssertTrue(controller.isMuted(room.id))
        XCTAssertFalse(controller.state(for: room.id).isUpdating)
        XCTAssertTrue(controller.state(for: room.id).errorMessage?.contains("无法确认房间加密状态") == true)
        XCTAssertEqual(try store.load(), [SharedRoomMuteStore.hash(room.id)])
    }

    func testUnknownEncryptionReadKeepsOldOrUnloadedStateWithoutSDKModeRead() async throws {
        let settings = TestNotificationSettings()
        let controller = RoomMuteController(sharedStore: MemoryRoomMuteStore())
        controller.configure(provider: SDKRoomNotificationSettings(settings))
        try await settings.setRoomNotificationMode(roomId: room.id, mode: .mute)
        await controller.refresh(room)
        let previous = controller.state(for: room.id)
        await controller.refresh([room.withSecurity(encryption: .unknown, participants: .unknown),
                                  MatrixRoom(id: "!unknown:test", name: "Unknown")])
        XCTAssertEqual(controller.state(for: room.id).isMuted, previous.isMuted)
        XCTAssertEqual(controller.state(for: room.id).isLoaded, previous.isLoaded)
        XCTAssertEqual(controller.state(for: room.id).readStatus, .failed)
        XCTAssertNotNil(controller.state(for: room.id).errorMessage)
        XCTAssertFalse(controller.state(for: "!unknown:test").isLoaded)
        XCTAssertEqual(controller.state(for: "!unknown:test").readStatus, .failed)
        XCTAssertEqual(settings.roomReadIDs, [room.id])
        do {
            _ = try await SDKRoomNotificationSettings(settings).isMuted(MatrixRoom(id: "!unknown:test", name: "Unknown"))
            XCTFail("A single unknown read must throw, never return false")
        } catch { XCTAssertTrue(error is RoomNotificationSettingsError) }
    }

    func testNilEncryptionResolverKeepsUnknownRoomsFailClosed() async throws {
        let settings = TestNotificationSettings()
        let provider = SDKRoomNotificationSettings(settings, resolveEncryption: nil)
        let unknown = room.withSecurity(encryption: .unknown, participants: .unknown)
        do {
            _ = try await provider.isMuted(unknown)
            XCTFail("Unknown encryption must reject reads")
        } catch { XCTAssertTrue(error is RoomNotificationSettingsError) }
        do {
            try await provider.setMuted(false, room: unknown)
            XCTFail("Unknown encryption must reject unmute")
        } catch { XCTAssertTrue(error is RoomNotificationSettingsError) }
        XCTAssertTrue(settings.roomReadEncryption.isEmpty)
        XCTAssertTrue(settings.unmuteEncryptionArguments.isEmpty)
        let muted = try await provider.isMuted(room)
        XCTAssertFalse(muted)
    }

    func testResolvedEncryptionIsPassedToUnmuteAndReadback() async throws {
        for encryption in [RoomEncryptionState.encrypted, .unencrypted] {
            let settings = TestNotificationSettings()
            var resolutions = 0
            let provider = SDKRoomNotificationSettings(settings) { _ in
                resolutions += 1
                return encryption
            }
            let unknown = room.withSecurity(encryption: .unknown, participants: .unknown)
            try await provider.setMuted(false, room: unknown)
            let muted = try await provider.isMuted(unknown)
            XCTAssertFalse(muted)
            XCTAssertEqual(resolutions, 2)
            XCTAssertEqual(settings.unmuteEncryptionArguments, [encryption == .encrypted])
            XCTAssertEqual(settings.roomReadEncryption, [encryption == .encrypted])
        }
    }

    func testEncryptionResolutionFailureDoesNotCallUnmuteOrModeRead() async {
        let settings = TestNotificationSettings()
        let provider = SDKRoomNotificationSettings(settings) { _ in throw MatrixServiceError.operationFailed }
        let unknown = room.withSecurity(encryption: .unknown, participants: .unknown)
        do {
            try await provider.setMuted(false, room: unknown)
            XCTFail("Resolution failure must reject unmute")
        } catch { XCTAssertTrue(error is RoomNotificationSettingsError) }
        let values = try? await provider.mutedStates(for: [unknown, room])
        XCTAssertEqual(values, [room.id: false])
        XCTAssertTrue(settings.unmuteEncryptionArguments.isEmpty)
        XCTAssertEqual(settings.roomReadIDs, [room.id])
    }

    func testSDKSingleReadFailureThrowsInsteadOfConfirmingUnmuted() async {
        let settings = TestNotificationSettings()
        settings.failReads(for: [room.id])
        do {
            _ = try await SDKRoomNotificationSettings(settings).isMuted(room)
            XCTFail("Failed post-write readback must throw")
        } catch { XCTAssertEqual(settings.roomReadIDs, [room.id]) }
    }

    func testOldBatchCannotPopulateNewSession() async {
        let settings = TestNotificationSettings()
        settings.suspendRawRead = true
        let controller = RoomMuteController(sharedStore: MemoryRoomMuteStore())
        controller.configure(provider: SDKRoomNotificationSettings(settings))
        let oldRead = Task { await controller.refresh([room]) }
        while !settings.isRawReadBlocked { await Task.yield() }
        controller.reset()
        let next = FakeRoomNotificationProvider()
        controller.configure(provider: next)
        await controller.refresh(room)
        try? await settings.setRoomNotificationMode(roomId: room.id, mode: .mute)
        settings.releaseRawRead()
        await oldRead.value
        XCTAssertFalse(controller.isMuted(room.id))
        XCTAssertTrue(controller.state(for: room.id).isLoaded)
    }

    func testListMuteIconDotAndNormalNumericUnreadMapping() {
        let muted = RoomMuteListPresentation(isMuted: true, unreadCount: 7)
        XCTAssertEqual(muted.symbol, "bell.slash.fill")
        XCTAssertTrue(muted.showsUnreadDot)
        XCTAssertNil(muted.unreadNumber)
        XCTAssertFalse(RoomMuteListPresentation(isMuted: true, unreadCount: 0).showsUnreadDot)
        let normal = RoomMuteListPresentation(isMuted: false, unreadCount: 7)
        XCTAssertNil(normal.symbol)
        XCTAssertFalse(normal.showsUnreadDot)
        XCTAssertEqual(normal.unreadNumber, 7)
    }

    func testBadgeExcludesMutedRoomsAndRetainsSaturation() {
        let rooms = [room, MatrixRoom(id: "!normal:test", name: "Other", unreadCount: 3)]
        XCTAssertEqual(PushBadge.total(rooms: rooms, isMuted: { $0 == self.room.id }), 3)
        XCTAssertEqual(PushBadge.total(rooms: rooms, isMuted: { _ in true }), 0)
        XCTAssertEqual(PushBadge.total(rooms: [MatrixRoom(id: "!large:test", name: "Large", unreadCount: .max)], isMuted: { _ in false }), Int.max)
    }

    func testForegroundMutedEventDoesNotReserveStoreKeyOrReplayOnContextChange() {
        let store = ForegroundInAppBannerStore()
        let candidate = ForegroundBannerCandidate(roomID: room.id, eventID: "$old", latestActivity: Date(), title: "Test", preview: "Test")
        store.updateContext(isForeground: true, visibleRoomID: nil, isInputActive: false, mutedRoomIDs: [room.id])
        store.receive(candidate)
        XCTAssertNil(store.current)
        store.updateContext(isForeground: true, visibleRoomID: nil, isInputActive: false)
        XCTAssertNil(store.current) // Unmuting alone never replays the old event.
        store.receive(candidate) // Explicit fresh delivery is eligible at the Store boundary.
        XCTAssertEqual(store.current, candidate)
        store.receive(ForegroundBannerCandidate(roomID: room.id, eventID: "$new", latestActivity: Date(), title: "Test", preview: "Test"))
        XCTAssertNotNil(store.current)
        store.reset()
    }

    func testMutingDismissesAlreadyVisibleBanner() {
        let store = ForegroundInAppBannerStore()
        store.updateContext(isForeground: true, visibleRoomID: nil, isInputActive: false)
        store.receive(ForegroundBannerCandidate(roomID: room.id, eventID: "$one", latestActivity: Date(), title: "Test", preview: "Test"))
        XCTAssertNotNil(store.current)
        store.updateContext(isForeground: true, visibleRoomID: nil, isInputActive: false, mutedRoomIDs: [room.id])
        XCTAssertNil(store.current)
    }

    func testWillPresentNeverRequestsBannerOrSoundAndRejectsUnfilteredBadge() {
        let options = PushNotificationPresentation.foreground(mutedRoomHashes: [SharedRoomMuteStore.hash(room.id)])
        XCTAssertEqual(options, [])
        XCTAssertFalse(options.contains(.sound))
        XCTAssertFalse(options.contains(.banner))
        XCTAssertEqual(PushNotificationPresentation.foreground(mutedRoomHashes: []), [.badge])
    }

    func testNSEMatchesRoomOnlyAndNestedPayloadsWithoutEventID() {
        let hashes: Set<String> = [SharedRoomMuteStore.hash(room.id)]
        for info: [AnyHashable: Any] in [["room_id": room.id], ["notification": ["data": ["room_id": room.id]]], ["data": ["room_id": room.id]]] {
            XCTAssertTrue(RoomMutePushPolicy.isMuted(userInfo: info, hashes: hashes))
        }
        XCTAssertFalse(RoomMutePushPolicy.isMuted(userInfo: ["room_id": "!other:test"], hashes: hashes))
        XCTAssertFalse(RoomMutePushPolicy.isMuted(userInfo: ["room_id": "!bad\n"], hashes: hashes))
        XCTAssertFalse(RoomMutePushPolicy.isMuted(userInfo: [:], hashes: hashes))
    }

    func testNSEDegradesMutedContentAndPreservesExistingBadge() {
        let content = UNMutableNotificationContent()
        content.userInfo = ["room_id": room.id]
        content.title = "Test"; content.subtitle = "Test"; content.body = "Test"
        content.sound = .default; content.badge = 20; content.interruptionLevel = .timeSensitive
        XCTAssertTrue(RoomMutePushPolicy.apply(to: content, hashes: [SharedRoomMuteStore.hash(room.id)]))
        XCTAssertEqual(content.title, ""); XCTAssertEqual(content.subtitle, ""); XCTAssertEqual(content.body, "")
        XCTAssertNil(content.sound); XCTAssertNil(content.badge)
        XCTAssertEqual(content.interruptionLevel, .passive)
        XCTAssertEqual(content.relevanceScore, 0)
    }

    func testNSENormalContentKeepsAlertButCannotApplyServerTotalWhileAnyRoomMuted() {
        let content = UNMutableNotificationContent()
        content.userInfo = ["room_id": "!other:test"]
        content.title = "Test"; content.body = "Test"; content.sound = .default; content.badge = 20
        XCTAssertFalse(RoomMutePushPolicy.apply(to: content, hashes: [SharedRoomMuteStore.hash(room.id)]))
        XCTAssertEqual(content.body, "Test"); XCTAssertNotNil(content.sound); XCTAssertNil(content.badge)
        content.badge = 4
        XCTAssertFalse(RoomMutePushPolicy.apply(to: content, hashes: []))
        XCTAssertEqual(content.badge, 4)
    }

    func testSharedFileRoundTripOnlyContainsFullHashesAndClear() throws {
        let directory = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent(".build/mute-tests/" + UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("mute.json"), hash = SharedRoomMuteStore.hash(room.id)
        let store = SharedRoomMuteStore(fileURL: url)
        XCTAssertTrue(try store.load().isEmpty)
        try store.save([hash])
        XCTAssertEqual(try SharedRoomMuteStore(fileURL: url).load(), [hash])
        let content = try String(contentsOf: url, encoding: .utf8)
        XCTAssertFalse(content.contains(room.id))
        XCTAssertEqual(hash.count, 64)
        XCTAssertEqual(SharedRoomMuteStore.hash("abc"), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        try store.clear()
        XCTAssertTrue(try store.load().isEmpty)
    }

    func testSharedFileRejectsPlaintextAndCorruption() throws {
        let directory = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent(".build/mute-tests/" + UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("mute.json"), store = SharedRoomMuteStore(fileURL: directory.appendingPathComponent("mute.json"))
        XCTAssertThrowsError(try store.save([room.id]))
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        try Data("[\"plaintext\"]".utf8).write(to: url)
        XCTAssertThrowsError(try store.load())
    }

    func testSDKAdapterUsesMuteAndUnmuteAndReadsBack() async throws {
        let adapter = SDKRoomNotificationSettings(TestNotificationSettings())
        let initial = try await adapter.isMuted(room)
        XCTAssertFalse(initial)
        try await adapter.setMuted(true, room: room)
        let muted = try await adapter.isMuted(room)
        XCTAssertTrue(muted)
        try await adapter.setMuted(false, room: room)
        let unmuted = try await adapter.isMuted(room)
        XCTAssertFalse(unmuted)
    }
}

/// #180: the App Group comes from the expanded `BotvilleAppGroupIdentifier`
/// Info.plist key (`group.$(BOTVILLE_BUNDLE_ID_PREFIX)`), never a constant.
final class SharedRoomMuteAppGroupTests: XCTestCase {
    func testAppGroupReadsExpandedInfoPlistValueOnly() {
        for group in ["group.com.zjyjcl.botville", "group.com.example.custom"] {
            XCTAssertEqual(BotvilleAppGroup.identifier(in: [BotvilleAppGroup.infoKey: group]), group)
        }
        for invalid in ["", "group.", "com.example", "group.$(BOTVILLE_BUNDLE_ID_PREFIX)", "group.a b", "group.a\n"] {
            XCTAssertNil(BotvilleAppGroup.identifier(in: [BotvilleAppGroup.infoKey: invalid]), invalid)
        }
        XCTAssertNil(BotvilleAppGroup.identifier(in: [:]))
#if DEBUG
        XCTAssertEqual(PushForegroundProbeLog.appGroupInfoKey, BotvilleAppGroup.infoKey)
#endif
    }

    func testMissingAppGroupIsContainerUnavailableNotAnotherGroup() {
        let store = SharedRoomMuteStore(appGroupIdentifier: nil)
        XCTAssertThrowsError(try store.load()) { error in
            guard case SharedRoomMuteStoreError.containerUnavailable = error else {
                return XCTFail("unexpected error \(error)")
            }
        }
        XCTAssertThrowsError(try store.save([]))
    }

    func testRuntimeSourcesDoNotHardCodeAppGroup() throws {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        for path in ["Sources/BotvilleCore/SharedRoomMute.swift", "Sources/BotvilleCore/PushNotificationUX.swift",
                     "NotificationService/NotificationService.swift", "Sources/BotvilleApp/BotvilleApp.swift"] {
            let text = try String(contentsOf: root.appendingPathComponent(path), encoding: .utf8)
            // A literal concrete group such as "group.com…"; the bare "group." prefix check is fine.
            XCTAssertNil(text.range(of: #""group\.[A-Za-z0-9]"#, options: .regularExpression), path)
        }
    }
}
