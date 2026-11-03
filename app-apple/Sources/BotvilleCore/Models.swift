// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation

public enum MatrixRoomMembership: Sendable, Hashable { case joined, invited }

public struct MatrixRoom: Identifiable, Hashable, Sendable {
    public let id: String
    public let name: String
    public let membership: MatrixRoomMembership
    public let isDirect: Bool
    public let unreadCount: UInt64
    public let latestActivity: Date?

    public init(id: String, name: String, membership: MatrixRoomMembership = .joined,
                isDirect: Bool = false, unreadCount: UInt64 = 0, latestActivity: Date? = nil) {
        self.id = id
        self.name = name
        self.membership = membership
        self.isDirect = isDirect
        self.unreadCount = unreadCount
        self.latestActivity = latestActivity
    }

    static func sortedByActivity(_ rooms: [MatrixRoom]) -> [MatrixRoom] {
        rooms.sorted {
            let lhs = $0.latestActivity ?? .distantPast
            let rhs = $1.latestActivity ?? .distantPast
            return lhs == rhs ? $0.id < $1.id : lhs > rhs
        }
    }
}

public struct MatrixMessage: Identifiable, Equatable, Sendable {
    public let id: String
    public let sender: String
    public let body: String
    public let timestamp: Date
    public let isOwn: Bool
    public let deliveryState: MessageDeliveryState?
}

public enum MatrixServiceError: LocalizedError {
    case invalidHomeserver, missingCredentials, notLoggedIn, roomUnavailable, emptyMessage, busy, sendNoLongerFailed, storageUnavailable, sessionExpired, operationFailed

    public var errorDescription: String? {
        switch self {
        case .invalidHomeserver: return "请输入完整的 HTTPS homeserver 地址，且不要在地址中包含账号、密码、查询参数或片段。"
        case .missingCredentials: return "请输入 Matrix 用户名和密码。"
        case .notLoggedIn: return "请先登录 Matrix。"
        case .roomUnavailable: return "房间尚未就绪或已离开，请重新选择房间。"
        case .emptyMessage: return "不能发送空白消息。"
        case .sendNoLongerFailed: return "消息状态已变化，请查看最新发送结果。"
        case .storageUnavailable: return "无法安全保存或清理本地会话，请重试。"
        case .sessionExpired: return "会话已失效，请重新登录。"
        case .operationFailed: return "操作失败，请稍后重试。"
        case .busy: return "操作正在进行，请稍候。"
        }
    }
}

enum InputValidation {
    static func homeserver(_ value: String) throws -> String {
        do { return try HomeserverPolicy().validatedURL(value).absoluteString }
        catch { throw MatrixServiceError.invalidHomeserver }
    }
}
