// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation

/// Whether the client can safely treat all loaded participants as people.
/// An explicitly empty override has no rules, so it stays unknown instead of
/// being silently treated as human-only.
public enum AIParticipantStatus: Sendable, Hashable { case humanOnly, containsAI, unknown }

/// AI MXID recognition. Without any override the default mirrors the gateway
/// appservice users namespace (`server/compose/appservice/botville-gateway.yaml.tmpl`:
/// `^@ai_.*:<server>$`, matched against the full MXID).
/// Overrides: environment values are JSON string arrays; Info.plist values are
/// arrays. If either key is present (environment first, then Info.plist), only
/// the override rules apply and the default is not merged in.
public struct AIParticipantPolicy: Sendable {
    public static let patternConfigurationKey = "BOTVILLE_AI_USER_ID_PATTERNS"
    public static let prefixConfigurationKey = "BOTVILLE_AI_USER_ID_PREFIXES"
    /// `@ai_` at the start of the localpart, any (possibly empty) remainder up
    /// to the first `:`, on any server. Localparts cannot contain `:`, so for
    /// local MXIDs this equals the appservice regex (including `@ai_:server`);
    /// it is deliberately broader for federated `@ai_*` accounts (fail safe:
    /// they are treated as AI). Case-sensitive like the appservice regex.
    public static let defaultUserIDPatterns = ["^@ai_[^:]*:"]
    private let patterns: [NSRegularExpression]
    private let prefixes: [String]
    public let hasConfiguredRules: Bool
    public let configurationNotice: String?

    public init(userIDPatterns: [String] = [], userIDPrefixes: [String] = []) {
        let compiledPatterns = userIDPatterns.map { try? NSRegularExpression(pattern: $0) }
        let containsInvalidPattern = compiledPatterns.contains(where: { $0 == nil })
        patterns = containsInvalidPattern ? [] : compiledPatterns.compactMap { $0 }
        prefixes = userIDPrefixes.filter { !$0.isEmpty }
        hasConfiguredRules = !containsInvalidPattern && (!patterns.isEmpty || !prefixes.isEmpty)
        if containsInvalidPattern {
            configurationNotice = "AI 用户 ID 正则配置无效，按可能含 AI 保守处理。"
        } else if !hasConfiguredRules {
            configurationNotice = "AI 用户 ID 覆盖配置为空，按可能含 AI 保守处理。"
        } else {
            configurationNotice = nil
        }
    }

    public static func configured(environment: [String: String] = ProcessInfo.processInfo.environment,
                                  infoDictionary: [String: Any] = Bundle.main.infoDictionary ?? [:]) -> AIParticipantPolicy {
        let patterns = values(for: patternConfigurationKey, environment: environment, infoDictionary: infoDictionary)
        let prefixes = values(for: prefixConfigurationKey, environment: environment, infoDictionary: infoDictionary)
        guard !patterns.invalid, !prefixes.invalid else { return AIParticipantPolicy(userIDPatterns: ["["]) }
        guard patterns.present || prefixes.present else {
            return AIParticipantPolicy(userIDPatterns: defaultUserIDPatterns)
        }
        return AIParticipantPolicy(userIDPatterns: patterns.values, userIDPrefixes: prefixes.values)
    }

    public func classify(memberIDs: [String]?) -> AIParticipantStatus {
        guard let memberIDs, !memberIDs.isEmpty else { return .unknown }
        if memberIDs.contains(where: matchesAI) { return .containsAI }
        return hasConfiguredRules ? .humanOnly : .unknown
    }

    private func matchesAI(_ userID: String) -> Bool {
        if prefixes.contains(where: { userID.hasPrefix($0) }) { return true }
        let range = NSRange(userID.startIndex..., in: userID)
        return patterns.contains { $0.firstMatch(in: userID, range: range) != nil }
    }

    private static func values(for key: String, environment: [String: String], infoDictionary: [String: Any]) -> (values: [String], present: Bool, invalid: Bool) {
        if let raw = environment[key] {
            guard let data = raw.data(using: .utf8), let values = try? JSONDecoder().decode([String].self, from: data) else { return ([], true, true) }
            return (values, true, false)
        }
        guard let value = infoDictionary[key] else { return ([], false, false) }
        guard let values = value as? [String] else { return ([], true, true) }
        return (values, true, false)
    }
}

public enum RoomEncryptionState: Sendable, Hashable { case unknown, unencrypted, encrypted }

/// Text and symbol always carry the status; colour is not the only signal.
public struct EncryptionPresentation: Sendable, Hashable {
    public let encryption: RoomEncryptionState
    public let participants: AIParticipantStatus
    public init(encryption: RoomEncryptionState, participants: AIParticipantStatus) { self.encryption = encryption; self.participants = participants }
    public var title: String {
        switch encryption {
        case .encrypted: return "已加密"
        case .unknown: return "加密状态加载中"
        case .unencrypted where participants == .containsAI: return "未加密 · 含 AI"
        case .unencrypted where participants == .unknown: return "未加密 · 成员待确认"
        case .unencrypted: return "未加密"
        }
    }
    public var systemImage: String { switch encryption { case .encrypted: return "lock.fill"; case .unknown: return "lock.questionmark"; case .unencrypted: return "lock.open" } }
    public var detail: String {
        switch encryption {
        case .encrypted: return "此房间已端到端加密；本客户端不提供关闭入口。"
        case .unknown: return "正在读取房间加密状态与成员；按可能含 AI 保守处理。"
        case .unencrypted where participants == .containsAI: return "此房间含 AI。botville 的 AI 接入目前不支持加密会话，因此保持未加密。"
        case .unencrypted where participants == .unknown: return "成员尚未加载或 AI 规则未配置，按可能含 AI 保守处理。"
        case .unencrypted: return "此房间未加密；本客户端开启后不提供关闭入口。botville 的 AI 接入目前不支持加密会话。"
        }
    }
    public var canEnable: Bool { encryption == .unencrypted && participants == .humanOnly }
    public var canDisable: Bool { false }
}

/// Keeps a new-room encryption request valid only while the current draft can
/// still be encrypted. This prevents a changed invitee from leaving a disabled
/// toggle selected and blocking plaintext creation.
public struct NewRoomEncryptionSelection: Equatable, Sendable {
    public private(set) var isRequested = false

    public init() {}

    public mutating func setRequested(_ requested: Bool, presentation: EncryptionPresentation) {
        isRequested = requested && presentation.canEnable
    }

    public mutating func reconcile(presentation: EncryptionPresentation) {
        if !presentation.canEnable { isRequested = false }
    }
}

public enum RoomCreationValidationError: Error, Equatable, LocalizedError, Sendable {
    case missingInvitee, invalidMatrixUserID(String), directMessageRequiresOneInvitee
    public var errorDescription: String? { switch self { case .missingInvitee: return "请输入至少一个 Matrix 用户 ID。"; case .invalidMatrixUserID: return "请输入形如 @name:server 的 Matrix 用户 ID。"; case .directMessageRequiresOneInvitee: return "私聊只能邀请一位用户。" } }
}

public struct RoomCreationDraft: Equatable, Hashable, Sendable {
    public var isDirect: Bool; public var name: String; public var invitees: [String]
    public init(isDirect: Bool, name: String = "", invitees: [String]) { self.isDirect = isDirect; self.name = name; self.invitees = invitees }
    public func validatedInvitees() throws -> [String] {
        let normalized = invitees.map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
        guard !normalized.isEmpty else { throw RoomCreationValidationError.missingInvitee }
        for userID in normalized where !Self.isMatrixUserID(userID) { throw RoomCreationValidationError.invalidMatrixUserID(userID) }
        if isDirect, normalized.count != 1 { throw RoomCreationValidationError.directMessageRequiresOneInvitee }
        return normalized
    }
    public func validate() throws { _ = try validatedInvitees() }
    private static func isMatrixUserID(_ value: String) -> Bool { value.range(of: "^@[^:@\\s]+:[^\\s:]+(?::[0-9]+)?$", options: .regularExpression) != nil }
}

public enum EncryptionTarget: Hashable, Sendable { case newRoom(RoomCreationDraft), existingRoom(String) }

/// Consent is bound to exactly one draft/room; editing context invalidates it.
public struct EncryptionConsent: Equatable, Sendable {
    public private(set) var pendingTarget: EncryptionTarget?
    public private(set) var confirmedTarget: EncryptionTarget?

    public init() {}

    public var isAwaitingConfirmation: Bool { pendingTarget != nil }

    public mutating func request(for target: EncryptionTarget) {
        pendingTarget = target
        confirmedTarget = nil
    }

    @discardableResult
    public mutating func confirm(for target: EncryptionTarget) -> Bool {
        guard pendingTarget == target else { return false }
        pendingTarget = nil
        confirmedTarget = target
        return true
    }

    public mutating func cancel() {
        pendingTarget = nil
        confirmedTarget = nil
    }
    public mutating func contextChanged(to target: EncryptionTarget) {
        guard pendingTarget != target && confirmedTarget != target else { return }
        cancel()
    }

    public func allowsEncryption(for target: EncryptionTarget) -> Bool {
        confirmedTarget == target
    }

    /// The only policy gate used by room creation and existing-room enablement.
    /// It deliberately offers no downgrade path for encrypted rooms.
    public func validatedEncryption(for target: EncryptionTarget,
                                    participants: AIParticipantStatus,
                                    currentEncryption: RoomEncryptionState) throws -> Bool {
        if currentEncryption == .encrypted { return false }
        guard currentEncryption == .unencrypted else { throw EncryptionAuthorizationError.stateUnknown }
        if case .newRoom = target, pendingTarget == nil, confirmedTarget == nil { return false }
        guard pendingTarget == nil, confirmedTarget == target else {
            throw EncryptionAuthorizationError.confirmationRequired
        }
        guard participants == .humanOnly else {
            throw participants == .containsAI
                ? EncryptionAuthorizationError.containsAI
                : EncryptionAuthorizationError.participantsUnknown
        }
        return true
    }
}

public enum EncryptionAuthorizationError: Error, Equatable, LocalizedError, Sendable {
    case confirmationRequired, containsAI, participantsUnknown, stateUnknown
    public var errorDescription: String? {
        switch self {
        case .confirmationRequired: return "请先确认：本客户端在开启后不提供关闭入口；botville 的 AI 接入目前不支持加密会话。当前也尚未配置只向可信设备分享密钥；请验证设备并设置密钥备份。"
        case .containsAI: return "房间含 AI，不能开启端到端加密。"
        case .participantsUnknown: return "成员或 AI 识别规则尚未确认，暂不能开启端到端加密。"
        case .stateUnknown: return "房间加密状态尚未读取完成，暂不能更改。"
        }
    }
}
