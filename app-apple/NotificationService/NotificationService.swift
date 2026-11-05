// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation
import UserNotifications

final class NotificationService: UNNotificationServiceExtension {
    private let completionLock = NSLock()
    private var loader: PushNotificationLoader?

    override func didReceive(
        _ request: UNNotificationRequest,
        withContentHandler contentHandler: @escaping (UNNotificationContent) -> Void
    ) {
        guard let content = request.content.mutableCopy() as? UNMutableNotificationContent else {
            contentHandler(request.content)
            return
        }
        content.body = PushNotificationText.placeholder.body
        let loader = PushNotificationLoader(credentialsStore: KeychainSharedPushCredentialsStore())
        completionLock.withLock { self.loader = loader }
        loader.load(userInfo: request.content.userInfo) { text in
            if let title = text.title { content.title = title }
            content.body = text.body
            // Preserve the incoming badge (including nil); unread reconciliation
            // and foreground clearing belong to PR3, not an extension counter.
            contentHandler(content)
        }
    }

    override func serviceExtensionTimeWillExpire() {
        // The loader arbitrates deadline, response and expiry under one lock,
        // cancels the request, and invokes this notification's handler only once.
        completionLock.withLock { loader }?.expire()
    }
}
