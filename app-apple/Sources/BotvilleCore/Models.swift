// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation

public struct MatrixRoom: Identifiable, Hashable, Sendable {
    public let id: String
    public let name: String
}

public struct MatrixMessage: Identifiable, Equatable, Sendable {
    public let id: String
    public let sender: String
    public let body: String
    public let timestamp: Date
    public let isOwn: Bool
    public let deliveryState: String?
}

public enum MatrixServiceError: LocalizedError {
    case invalidHomeserver, missingCredentials, notLoggedIn, roomUnavailable, emptyMessage, busy

    public var errorDescription: String? {
        switch self {
        case .invalidHomeserver: return "请输入完整的 HTTPS homeserver 地址，且不要在地址中包含账号、密码、查询参数或片段。"
        case .missingCredentials: return "请输入 Matrix 用户名和密码。"
        case .notLoggedIn: return "请先登录 Matrix。"
        case .roomUnavailable: return "房间尚未就绪或已离开，请重新选择房间。"
        case .emptyMessage: return "不能发送空白消息。"
        case .busy: return "操作正在进行，请稍候。"
        }
    }
}

enum InputValidation {
    static func homeserver(_ value: String) throws -> String {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let url = URLComponents(string: trimmed), url.scheme?.lowercased() == "https",
              let host = url.host, !host.isEmpty, url.user == nil, url.password == nil,
              url.query == nil, url.fragment == nil, url.url != nil else {
            throw MatrixServiceError.invalidHomeserver
        }
        return trimmed
    }
}
