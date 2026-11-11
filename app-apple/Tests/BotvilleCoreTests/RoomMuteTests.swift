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
    private var rawReadContinuation: CheckedContinuation<String?, Error>?
    var suspendRawRead = false
    var missingRules = false
    var rawReadCount: Int { lock.withLock { rawReads } }
    var roomReadIDs: [String] { lock.withLock { roomReads } }
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
        lock.withLock { roomReads.append(roomId); return RoomNotificationSettings(mode: muted.contains(roomId) ? .mute : .allMessages, isDefault: false) }
    }
    override func setRoomNotificationMode(roomId: String, mode: RoomNotificationMode) async throws {
        let callback = lock.withLock {
            if mode == .mute { muted.insert(roomId) } else { muted.remove(roomId) }
            return delegate
        }
        callback?.settingsDidChange()
    }
    override func unmuteRoom(roomId: String, isEncrypted: Bool, isOneToOne: Bool) async throws {
        try await setRoomNotificationMode(roomId: roomId, mode: .allMessages)
    }
}

final class MemoryRoomMuteStore: SharedRoomMuteStoring, @unchecked Sendable {
    private let lock = NSLock()
    private var values: Set<String> = []
    var fails = false
    func load() throws -> Set<String> { lock.withLock { values } }
    func save(_ hashes: Set<String>) throws {
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
    private let room = MatrixRoom(id: "!mute:test", name: "Test", unreadCount: 7)

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
        let other = MatrixRoom(id: "!other:test", name: "Other")
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

    func testForegroundMutedEventConsumedAndUnmutingOnlyAllowsNewEvent() {
        let store = ForegroundInAppBannerStore()
        let candidate = ForegroundBannerCandidate(roomID: room.id, eventID: "$old", latestActivity: Date(), title: "Test", preview: "Test")
        store.updateContext(isForeground: true, visibleRoomID: nil, isInputActive: false, mutedRoomIDs: [room.id])
        store.receive(candidate)
        XCTAssertNil(store.current)
        store.updateContext(isForeground: true, visibleRoomID: nil, isInputActive: false)
        store.receive(candidate)
        XCTAssertNil(store.current)
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
