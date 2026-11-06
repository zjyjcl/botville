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

    /// Bits aligned with `UNNotificationPresentationOptions` (iOS 14+ / SDK 27.2):
    /// badge=1<<0, sound=1<<1, list=1<<3, banner=1<<4. Does not set deprecated `.alert`.
    public var unNotificationPresentationRawValue: UInt {
        var raw: UInt = 0
        if contains(.badge) { raw |= 1 << 0 }
        if contains(.sound) { raw |= 1 << 1 }
        if contains(.list) { raw |= 1 << 3 }
        if contains(.banner) { raw |= 1 << 4 }
        return raw
    }
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


/// DEBUG foreground willPresent evidence. Writes only event names and option
/// raw bits into the App Group cache — never tokens, Authorization, or bodies.
public enum PushForegroundProbeLog {
    public static let appGroupIdentifier = "group.com.zjyjcl.botville"
    public static let fileName = "botville-willpresent-probe.log"

    public static func makeLine(event: String, detail: String = "", at date: Date = Date(),
                                formatter: ISO8601DateFormatter? = nil) -> String {
        let stamp = (formatter ?? iso8601).string(from: date)
        if detail.isEmpty { return "\(stamp) \(event)" }
        return "\(stamp) \(event) \(detail)"
    }

    public static func logURL(fileManager: FileManager = .default) -> URL? {
        guard let root = fileManager.containerURL(forSecurityApplicationGroupIdentifier: appGroupIdentifier) else {
            return nil
        }
        return root
            .appendingPathComponent("Library/Caches", isDirectory: true)
            .appendingPathComponent(fileName)
    }

    /// Appends one line. Safe to call from any thread; failures are ignored so
    /// probe I/O never affects presentation.
    public static func append(event: String, detail: String = "",
                              fileManager: FileManager = .default) {
        guard let url = logURL(fileManager: fileManager) else { return }
        let dir = url.deletingLastPathComponent()
        try? fileManager.createDirectory(at: dir, withIntermediateDirectories: true)
        let line = makeLine(event: event, detail: detail) + "\n"
        guard let data = line.data(using: .utf8) else { return }
        if fileManager.fileExists(atPath: url.path) {
            guard let handle = try? FileHandle(forWritingTo: url) else { return }
            defer { try? handle.close() }
            _ = try? handle.seekToEnd()
            try? handle.write(contentsOf: data)
        } else {
            try? data.write(to: url, options: .atomic)
        }
    }

    public static func readTail(maxBytes: Int = 4096,
                                fileManager: FileManager = .default) -> String? {
        guard let url = logURL(fileManager: fileManager),
              fileManager.fileExists(atPath: url.path),
              let data = try? Data(contentsOf: url) else { return nil }
        let slice = data.count > maxBytes ? data.suffix(maxBytes) : data
        return String(data: Data(slice), encoding: .utf8)
    }

    private static let iso8601: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()
}
