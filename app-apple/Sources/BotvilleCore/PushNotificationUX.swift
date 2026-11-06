// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation

/// The destination represented by a push notification. Parsing accepts the
/// same Sygnal wrappers as the NSE, but deliberately needs only a room ID.
public enum PushNotificationRoute: Equatable, Sendable {
    case room(String)
    case roomList

    public static func parse(_ userInfo: [AnyHashable: Any]) -> Self {
        for candidate in candidates(in: userInfo) {
            if let roomID = candidate["room_id"] as? String, validRoomID(roomID) {
                return .room(roomID)
            }
        }
        return .roomList
    }

    private static func candidates(in userInfo: [AnyHashable: Any]) -> [[AnyHashable: Any]] {
        var result = [userInfo]
        for key in ["notification", "data"] {
            guard let nested = userInfo[key] as? [AnyHashable: Any] else { continue }
            result.append(nested)
            for innerKey in ["notification", "data"] {
                if let inner = nested[innerKey] as? [AnyHashable: Any] { result.append(inner) }
            }
        }
        return result
    }

    private static func validRoomID(_ value: String) -> Bool {
        value.first == "!" && value.count > 1 && value.utf8.count <= 2048
            && value.rangeOfCharacter(from: .whitespacesAndNewlines.union(.controlCharacters)) == nil
    }
}

/// Presentation choices are platform-neutral so notification policy remains
/// independently testable from the UIApplication delegate wiring.
public struct PushNotificationPresentation: OptionSet, Equatable, Sendable {
    public let rawValue: UInt
    public init(rawValue: UInt) { self.rawValue = rawValue }

    public static let banner = Self(rawValue: 1 << 0)
    public static let list = Self(rawValue: 1 << 1)
    public static let sound = Self(rawValue: 1 << 2)
    public static let badge = Self(rawValue: 1 << 3)
    public static let foreground: Self = [.banner, .list, .sound, .badge]
}

public enum PushBadge {
    /// Saturates at Int.max so malformed or unusually large server totals
    /// cannot wrap the application icon badge.
    public static func total(unreadCounts: [UInt64]) -> Int {
        let total = unreadCounts.reduce(UInt64.zero) { partial, count in
            let (next, overflow) = partial.addingReportingOverflow(count)
            return overflow ? UInt64.max : next
        }
        return total > UInt64(Int.max) ? Int.max : Int(total)
    }
}
