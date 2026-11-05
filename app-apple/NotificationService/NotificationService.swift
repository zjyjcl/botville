// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation
import OSLog
import UserNotifications

final class NotificationService: UNNotificationServiceExtension {
    private let logger = Logger(subsystem: "com.zjyjcl.botville.notification", category: "credentials")
    private let credentialsStore = KeychainSharedPushCredentialsStore()
    private let completionLock = NSLock()
    private var contentHandler: ((UNNotificationContent) -> Void)?
    private var bestAttemptContent: UNNotificationContent?

    override func didReceive(
        _ request: UNNotificationRequest,
        withContentHandler contentHandler: @escaping (UNNotificationContent) -> Void
    ) {
        completionLock.withLock {
            self.contentHandler = contentHandler
            bestAttemptContent = (request.content.mutableCopy() as? UNMutableNotificationContent) ?? request.content
        }

        do {
            let present = try credentialsStore.load() != nil
            logger.debug("Shared push credentials present: \(present, privacy: .public)")
        } catch {
            // Missing entitlements / locked Keychain / simulator failures must
            // never prevent delivery. Do not log credentials or error payloads.
            logger.warning("Shared push credentials unavailable")
        }

        // TODO(PR2): Use event_id / room_id from request.content.userInfo and
        // the shared credentials to fetch the event body. Encrypted rooms must
        // display “加密消息”. PR1 does no network work and returns the original
        // bestAttemptContent, preserving the default payload's U+2026 “…” body.
        // PR1 intentionally delivers synchronously and immediately; it does not
        // consume the extension's time budget while no event fetch is implemented.
        deliverBestAttemptContent()
    }

    override func serviceExtensionTimeWillExpire() {
        deliverBestAttemptContent()
    }

    private func deliverBestAttemptContent() {
        let delivery = completionLock.withLock { () -> (((UNNotificationContent) -> Void), UNNotificationContent)? in
            guard let contentHandler, let bestAttemptContent else { return nil }
            self.contentHandler = nil
            return (contentHandler, bestAttemptContent)
        }
        // Clear the handler before invoking it so expiry cannot deliver twice.
        if let (handler, content) = delivery { handler(content) }
    }
}
