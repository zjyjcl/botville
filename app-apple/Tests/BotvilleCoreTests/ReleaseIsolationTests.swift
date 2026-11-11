// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation
import XCTest

final class ReleaseIsolationTests: XCTestCase {
    private let forbidden: Set<String> = ["NSAppTransportSecurity", "NSAllowsLocalNetworking", "NSLocalNetworkUsageDescription", "BotvilleSygnalNotifyURL"]
    // Controlled production signing changes this to "production" with the entitlement.
    private let releaseAppAPSEnvironment: String? = nil

    func testReleasePlistRecursivelyExcludesDebugKeys() throws {
        XCTAssertTrue(forbidden.isDisjoint(with: keys(try plist("BotvilleApp/Info.plist"))))
        XCTAssertTrue(forbidden.isDisjoint(with: keys(try plist("NotificationService/Info.plist"))))
    }

    func testDebugPlistContainsDebugNetworkingOnly() throws {
        let value = try plist("BotvilleApp/Info-Debug.plist")
        XCTAssertEqual((value["NSAppTransportSecurity"] as? [String: Any])?["NSAllowsLocalNetworking"] as? Bool, true)
        XCTAssertNotNil(value["NSLocalNetworkUsageDescription"])
        XCTAssertEqual(value["BotvilleSygnalNotifyURL"] as? String, "http://sygnal:5000/_matrix/push/v1/notify")
    }

    func testApplicationSigningSupportsDevicesAndAdHocSimulators() throws {
        let objects = try XCTUnwrap(projectJSON()["objects"] as? [String: Any])
        let configurations = objects.values.compactMap { $0 as? [String: Any] }.filter {
            $0["isa"] as? String == "XCBuildConfiguration"
        }
        XCTAssertEqual(configurations.count, 6, "Project, App and NSE each need Debug/Release")
        let signing = try signingDefaults()
        let references = objects.filter { ($0.value as? [String: Any])?["path"] as? String == "Signing.xcconfig" }
        XCTAssertEqual(references.count, 1)
        let referenceID = try XCTUnwrap(references.keys.first)
        let reference = try XCTUnwrap(references[referenceID] as? [String: Any])
        XCTAssertEqual(reference["isa"] as? String, "PBXFileReference")
        XCTAssertEqual(reference["sourceTree"] as? String, "<group>")
        let groupEntry = try XCTUnwrap(objects.first {
            let group = $0.value as? [String: Any]
            return group?["isa"] as? String == "PBXGroup" && group?["path"] as? String == "Config"
                && (group?["children"] as? [String])?.contains(referenceID) == true
        })
        let project = try XCTUnwrap(objects.values.compactMap { $0 as? [String: Any] }.first { $0["isa"] as? String == "PBXProject" })
        let mainGroup = try XCTUnwrap(objects[try XCTUnwrap(project["mainGroup"] as? String)] as? [String: Any])
        XCTAssertTrue((mainGroup["children"] as? [String])?.contains(groupEntry.key) == true)
        for list in objects.values.compactMap({ $0 as? [String: Any] }) where list["isa"] as? String == "XCConfigurationList" {
            let ids = try XCTUnwrap(list["buildConfigurations"] as? [String])
            XCTAssertEqual(ids.count, 2)
            XCTAssertEqual(Set(ids.compactMap { (objects[$0] as? [String: Any])?["name"] as? String }), ["Debug", "Release"])
        }
        for config in configurations {
            XCTAssertEqual(config["baseConfigurationReference"] as? String, referenceID)
            let settings = try XCTUnwrap(config["buildSettings"] as? [String: Any])
            XCTAssertNil(settings["DEVELOPMENT_TEAM"], "Inherit the xcconfig team, including local direct overrides")
            XCTAssertFalse(settings.keys.contains(where: { $0.hasPrefix("BOTVILLE_") }))
            XCTAssertEqual(try expand(signing["DEVELOPMENT_TEAM"]!, using: signing), "3Z8WP6U6F7")
            XCTAssertFalse(settings.keys.contains(where: { $0.hasPrefix("PROVISIONING_PROFILE") }))
            for (key, value) in settings where key.hasPrefix("CODE_SIGNING_ALLOWED") || key.hasPrefix("CODE_SIGNING_REQUIRED") {
                XCTAssertNotEqual(value as? String, "NO", key)
            }
        }
        try assertEveryNativeReleaseTargetIsIsolated(objects)
        try assertTarget(
            objects: objects,
            name: "BotvilleApp-iOS",
            productType: "com.apple.product-type.application",
            bundleID: "com.zjyjcl.botville",
            entitlements: ["Debug": "BotvilleApp/BotvilleApp.entitlements", "Release": "BotvilleApp/BotvilleApp-Release.entitlements"],
            plists: ["Debug": "BotvilleApp/Info-Debug.plist", "Release": "BotvilleApp/Info.plist"]
        )
        try assertTarget(
            objects: objects,
            name: "BotvilleNotificationService",
            productType: "com.apple.product-type.app-extension",
            bundleID: "com.zjyjcl.botville.notification",
            entitlements: ["Debug": "NotificationService/NotificationService.entitlements", "Release": "NotificationService/NotificationService.entitlements"],
            plists: ["Debug": "NotificationService/Info-Debug.plist", "Release": "NotificationService/Info.plist"]
        )
    }

    func testReleaseEntitlementsAllowOnlySharedCredentialInfrastructure() throws {
        try assertEntitlements("BotvilleApp/BotvilleApp.entitlements", privateKeychainGroup: "$(AppIdentifierPrefix)com.zjyjcl.botville", apsEnvironment: "development")
        // Temporary release gate; the NSE intentionally remains APNs-free.
        try assertEntitlements("BotvilleApp/BotvilleApp-Release.entitlements", privateKeychainGroup: "$(AppIdentifierPrefix)com.zjyjcl.botville", apsEnvironment: releaseAppAPSEnvironment)
        try assertEntitlements("NotificationService/NotificationService.entitlements", privateKeychainGroup: "$(AppIdentifierPrefix)com.zjyjcl.botville.notification", apsEnvironment: nil)

        let defaults = try signingDefaults()
        for relative in ["BotvilleApp/Info.plist", "BotvilleApp/Info-Debug.plist",
                         "NotificationService/Info.plist", "NotificationService/Info-Debug.plist"] {
            let info = try plist(relative)
            let shared = try XCTUnwrap(info["BotvilleSharedKeychainAccessGroup"] as? String)
            let group = try XCTUnwrap(info["BotvilleAppGroupIdentifier"] as? String)
            XCTAssertEqual(shared, "$(AppIdentifierPrefix)$(BOTVILLE_BUNDLE_ID_PREFIX).shared")
            XCTAssertEqual(group, "group.$(BOTVILLE_BUNDLE_ID_PREFIX)")
            XCTAssertEqual(try expand(shared, using: defaults), "LEGACY1234.com.zjyjcl.botville.shared")
            XCTAssertEqual(try expand(group, using: defaults), "group.com.zjyjcl.botville")
        }
    }

    func testSigningDefaultsAndLocalOverrideContract() throws {
        let defaults = try signingDefaults()
        let text = try String(contentsOf: root.appendingPathComponent("Config/Signing.xcconfig"), encoding: .utf8)
        XCTAssertEqual(text.trimmingCharacters(in: .whitespacesAndNewlines).components(separatedBy: "\n").last,
                       #"#include? "Local.xcconfig""#)
        XCTAssertEqual(defaults["BOTVILLE_DEVELOPMENT_TEAM"], "3Z8WP6U6F7")
        XCTAssertEqual(defaults["DEVELOPMENT_TEAM"], "$(BOTVILLE_DEVELOPMENT_TEAM)")
        XCTAssertEqual(defaults["BOTVILLE_BUNDLE_ID_PREFIX"], "com.zjyjcl.botville")
        // Read only the example, never a developer's real Local.xcconfig.
        let local = try xcconfigAssignments("Config/Local.xcconfig.example")
        var overridden = defaults.merging(local) { _, local in local }
        XCTAssertEqual(try expand(overridden["DEVELOPMENT_TEAM"]!, using: overridden), "YOURTEAMID")
        XCTAssertEqual(try expand("group.$(BOTVILLE_BUNDLE_ID_PREFIX)", using: overridden), "group.com.example.botville")
        XCTAssertEqual(try expand("$(AppIdentifierPrefix)$(BOTVILLE_BUNDLE_ID_PREFIX).shared", using: overridden),
                       "LEGACY1234.com.example.botville.shared")
        overridden["DEVELOPMENT_TEAM"] = "DIRECT1234"
        XCTAssertEqual(try expand(overridden["DEVELOPMENT_TEAM"]!, using: overridden), "DIRECT1234")
    }

    func testEntitlementAndDebugViewsAreIsolated() throws {
        let project = try String(contentsOf: root.appendingPathComponent("Botville.xcodeproj/project.pbxproj"), encoding: .utf8)
        XCTAssertTrue(project.contains("CODE_SIGN_ENTITLEMENTS = BotvilleApp/BotvilleApp.entitlements;"))
        XCTAssertTrue(project.contains("Embed Foundation Extensions"))
        XCTAssertTrue(project.contains("com.apple.Push = { enabled = 1; };"))
        XCTAssertEqual(try plist("BotvilleApp/BotvilleApp.entitlements")["aps-environment"] as? String, "development")
        let sources = root.appendingPathComponent("Sources/BotvilleApp")
        try guarded("struct PushDiagnosticsView", at: sources.appendingPathComponent("PushDiagnosticsView.swift"))
        try guarded("ManualHomeserverLoginView", at: sources.appendingPathComponent("LoginView.swift"))
        for directory in [sources, root.appendingPathComponent("NotificationService")] {
            for url in try swiftSources(in: directory) {
                let text = try String(contentsOf: url, encoding: .utf8)
                for token in ["PushDiagnosticsView(", "ManualHomeserverLoginView(",
                              "registerForRemoteNotifications(", "requestAuthorization(",
                              "didRegisterForRemoteNotificationsWithDeviceToken",
                              "didFailToRegisterForRemoteNotificationsWithError"] where text.contains(token) {
                    try guarded(token, at: url)
                }
            }
        }
        let core = root.appendingPathComponent("Sources/BotvilleCore")
        try guarded("static let isDebugBuild = true", at: core.appendingPathComponent("PushRegistrar.swift"))
        let matrixService = try String(contentsOf: core.appendingPathComponent("MatrixService.swift"), encoding: .utf8)
        XCTAssertTrue(matrixService.contains("PushHTTPPolicy.permitsHTTP(allowHTTP)"))
        let objects = try XCTUnwrap(projectJSON()["objects"] as? [String: Any])
        try assertNotificationServiceBoundary(objects)
        try assertNotificationServiceIsEmbedded(objects)
    }

    private var root: URL { URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent() }
    private func plist(_ relative: String) throws -> [String: Any] { try XCTUnwrap(PropertyListSerialization.propertyList(from: Data(contentsOf: root.appendingPathComponent(relative)), format: nil) as? [String: Any]) }
    private func keys(_ value: Any) -> Set<String> {
        if let dictionary = value as? [String: Any] { return Set(dictionary.keys).union(dictionary.values.reduce(into: Set<String>()) { $0.formUnion(keys($1)) }) }
        if let array = value as? [Any] { return array.reduce(into: Set<String>()) { $0.formUnion(keys($1)) } }
        return []
    }
    private func projectJSON() throws -> [String: Any] {
        let task = Process(); task.executableURL = URL(fileURLWithPath: "/usr/bin/plutil"); task.arguments = ["-convert", "json", "-o", "-", root.appendingPathComponent("Botville.xcodeproj/project.pbxproj").path]
        let pipe = Pipe(); task.standardOutput = pipe; try task.run(); task.waitUntilExit(); XCTAssertEqual(task.terminationStatus, 0)
        return try XCTUnwrap(JSONSerialization.jsonObject(with: pipe.fileHandleForReading.readDataToEndOfFile()) as? [String: Any])
    }
    private func assertTarget(
        objects: [String: Any], name: String, productType: String, bundleID: String,
        entitlements: [String: String], plists: [String: String]
    ) throws {
        let target = try XCTUnwrap(objects.values.compactMap { $0 as? [String: Any] }.first {
            $0["isa"] as? String == "PBXNativeTarget" && $0["name"] as? String == name && $0["productType"] as? String == productType
        })
        let list = try XCTUnwrap(objects[try XCTUnwrap(target["buildConfigurationList"] as? String)] as? [String: Any])
        let configurationIDs = try XCTUnwrap(list["buildConfigurations"] as? [String])
        XCTAssertEqual(configurationIDs.count, 2)
        for id in configurationIDs {
            let config = try XCTUnwrap(objects[id] as? [String: Any])
            let name = try XCTUnwrap(config["name"] as? String)
            let settings = try XCTUnwrap(config["buildSettings"] as? [String: Any])
            XCTAssertEqual(settings["CODE_SIGN_STYLE"] as? String, "Automatic", name)
            let defaults = try signingDefaults()
            XCTAssertNil(settings["DEVELOPMENT_TEAM"], "Inherit the xcconfig signing team")
            XCTAssertEqual(try expand(defaults["DEVELOPMENT_TEAM"]!, using: defaults), "3Z8WP6U6F7")
            XCTAssertEqual(settings["CODE_SIGN_IDENTITY[sdk=iphonesimulator*]"] as? String, "-", name)
            let bundle = try XCTUnwrap(settings["PRODUCT_BUNDLE_IDENTIFIER"] as? String)
            XCTAssertEqual(bundle, bundleID.replacingOccurrences(of: "com.zjyjcl.botville", with: "$(BOTVILLE_BUNDLE_ID_PREFIX)"), name)
            XCTAssertEqual(try expand(bundle, using: defaults), bundleID, name)
            XCTAssertEqual(settings["CODE_SIGN_ENTITLEMENTS"] as? String, entitlements[name], name)
            XCTAssertEqual(settings["INFOPLIST_FILE"] as? String, plists[name], name)
            for (key, value) in settings where key.hasPrefix("CODE_SIGN_ENTITLEMENTS[") {
                XCTAssertEqual(value as? String, entitlements[name], key)
            }
            for (key, value) in settings where key.hasPrefix("INFOPLIST_FILE[") {
                XCTAssertEqual(value as? String, plists[name], key)
            }
        }
    }
    private func assertEveryNativeReleaseTargetIsIsolated(_ objects: [String: Any]) throws {
        for target in objects.values.compactMap({ $0 as? [String: Any] }) where target["isa"] as? String == "PBXNativeTarget" {
            let targetName = target["name"] as? String ?? "unnamed target"
            let listID = try XCTUnwrap(target["buildConfigurationList"] as? String)
            let list = try XCTUnwrap(objects[listID] as? [String: Any])
            let release = try XCTUnwrap((try XCTUnwrap(list["buildConfigurations"] as? [String])).compactMap { objects[$0] as? [String: Any] }.first { $0["name"] as? String == "Release" }, "\(targetName) needs Release")
            let settings = try XCTUnwrap(release["buildSettings"] as? [String: Any])
            let plistSettings = settings.filter { $0.key.hasPrefix("INFOPLIST_FILE") }
            if let type = target["productType"] as? String, ["com.apple.product-type.application", "com.apple.product-type.app-extension"].contains(type) {
                XCTAssertFalse(plistSettings.isEmpty, "\(targetName) Release needs a shipping plist")
            }
            for (key, value) in plistSettings {
                let relative = try XCTUnwrap(value as? String, "\(targetName) \(key)")
                XCTAssertTrue(FileManager.default.fileExists(atPath: root.appendingPathComponent(relative).path), "\(targetName) Release plist missing")
                XCTAssertTrue(forbidden.isDisjoint(with: keys(try plist(relative))), "\(targetName) Release plist leaks DEBUG-only keys")
            }
            for (key, value) in settings where key.hasPrefix("CODE_SIGN_ENTITLEMENTS") {
                let relative = try XCTUnwrap(value as? String, "\(targetName) \(key)")
                XCTAssertTrue(FileManager.default.fileExists(atPath: root.appendingPathComponent(relative).path), "\(targetName) Release entitlements missing")
                XCTAssertNotEqual(try plist(relative)["aps-environment"] as? String, "development", "\(targetName) Release must not use development APNs")
            }
        }
    }
    private func assertEntitlements(_ relative: String, privateKeychainGroup: String, apsEnvironment: String?) throws {
        let value = try plist(relative)
        let groups = try XCTUnwrap(value["com.apple.security.application-groups"] as? [String])
        let keychain = try XCTUnwrap(value["keychain-access-groups"] as? [String])
        XCTAssertEqual(groups, ["group.$(BOTVILLE_BUNDLE_ID_PREFIX)"])
        XCTAssertEqual(keychain, [privateKeychainGroup.replacingOccurrences(of: "com.zjyjcl.botville", with: "$(BOTVILLE_BUNDLE_ID_PREFIX)"),
                                 "$(AppIdentifierPrefix)$(BOTVILLE_BUNDLE_ID_PREFIX).shared"])
        let defaults = try signingDefaults()
        XCTAssertEqual(try groups.map { try expand($0, using: defaults) }, ["group.com.zjyjcl.botville"])
        XCTAssertEqual(try keychain.map { try expand($0, using: defaults) },
                       [privateKeychainGroup.replacingOccurrences(of: "$(AppIdentifierPrefix)", with: "LEGACY1234."),
                        "LEGACY1234.com.zjyjcl.botville.shared"])
        XCTAssertEqual(value["aps-environment"] as? String, apsEnvironment)
    }
    // Deliberately accept only this small assignment-only config contract, so
    // additional includes/conditional signing overrides cannot evade isolation.
    private func xcconfigAssignments(_ relative: String) throws -> [String: String] {
        var result: [String: String] = [:]
        for raw in try String(contentsOf: root.appendingPathComponent(relative), encoding: .utf8).components(separatedBy: "\n") {
            let line = raw.trimmingCharacters(in: .whitespaces)
            if line.isEmpty || line.hasPrefix("//") { continue }
            if relative == "Config/Signing.xcconfig" && line == #"#include? "Local.xcconfig""# { continue }
            let parts = line.components(separatedBy: "=")
            XCTAssertEqual(parts.count, 2, "Unsupported xcconfig statement: \(line)")
            guard parts.count == 2 else { continue }
            let key = parts[0].trimmingCharacters(in: .whitespaces)
            XCTAssertNil(result[key], "Duplicate xcconfig assignment")
            result[key] = parts[1].trimmingCharacters(in: .whitespaces)
        }
        XCTAssertEqual(Set(result.keys), ["BOTVILLE_DEVELOPMENT_TEAM", "DEVELOPMENT_TEAM", "BOTVILLE_BUNDLE_ID_PREFIX"])
        return result
    }
    private func signingDefaults() throws -> [String: String] {
        var values = try xcconfigAssignments("Config/Signing.xcconfig")
        XCTAssertEqual(values["BOTVILLE_DEVELOPMENT_TEAM"], "3Z8WP6U6F7")
        XCTAssertEqual(values["DEVELOPMENT_TEAM"], "$(BOTVILLE_DEVELOPMENT_TEAM)")
        XCTAssertEqual(values["BOTVILLE_BUNDLE_ID_PREFIX"], "com.zjyjcl.botville")
        // An independent fixture: Apple's AppIdentifierPrefix can differ from Team ID.
        values["AppIdentifierPrefix"] = "LEGACY1234."
        return values
    }
    private func expand(_ raw: String, using settings: [String: String]) throws -> String {
        var result = raw
        for _ in 0..<8 {
            let previous = result
            for (key, value) in settings { result = result.replacingOccurrences(of: "$(\(key))", with: value) }
            if previous == result { break }
        }
        XCTAssertFalse(result.contains("$"), "Unresolved or cyclic build variable: \(result)")
        return result
    }
    private func guarded(_ needle: String, at url: URL) throws {
        var stack: [Bool] = []; var found = false
        for (offset, raw) in try String(contentsOf: url, encoding: .utf8).split(separator: "\n", omittingEmptySubsequences: false).enumerated() {
            let line = String(raw).trimmingCharacters(in: .whitespaces)
            if line.hasPrefix("#if ") { stack.append(line == "#if DEBUG"); continue }
            if line.hasPrefix("#elseif") || line == "#else" { XCTAssertFalse(stack.isEmpty); if stack.last == true { stack[stack.count - 1] = false }; continue }
            if line == "#endif" { XCTAssertFalse(stack.isEmpty); if !stack.isEmpty { stack.removeLast() }; continue }
            if line.contains(needle) { found = true; XCTAssertTrue(stack.contains(true), "\(needle) must be DEBUG-guarded at \(url):\(offset + 1)") }
        }
        XCTAssertTrue(stack.isEmpty); XCTAssertTrue(found, "\(needle) missing from \(url)")
    }
    private func assertNotificationServiceBoundary(_ objects: [String: Any]) throws {
        let sources = try notificationServiceSources(objects)
        XCTAssertEqual(Set(sources.map(\.lastPathComponent)), ["NotificationService.swift", "SharedPushCredentials.swift", "PushNotificationContent.swift"])
        for source in sources {
            let text = try String(contentsOf: source, encoding: .utf8)
            XCTAssertFalse(text.contains("import MatrixRustSDK"), "NSE must not import MatrixRustSDK")
            XCTAssertFalse(text.contains("import BotvilleCore"), "NSE must not import BotvilleCore")
            if source.lastPathComponent != "PushNotificationContent.swift" {
                XCTAssertFalse(text.contains("URLSession"), "Only PushNotificationContent may use URLSession in NSE")
                XCTAssertFalse(text.contains("URLRequest"), "Only PushNotificationContent may use URLRequest in NSE")
            }
            XCTAssertNil(text.range(of: #"\b(?:print|debugPrint|dump|NSLog|os_log)\s*\("#, options: .regularExpression), "NSE must use only its safe Logger diagnostics")
            for line in text.split(separator: "\n") where line.range(of: #"\b\w+\.(?:trace|debug|info|notice|warning|error|fault|critical|log)\s*\("#, options: .regularExpression) != nil {
                let lower = line.lowercased()
                XCTAssertFalse(lower.contains("token"), "NSE logs must not expose tokens")
                XCTAssertTrue([
                    "logger.debug(\"Shared push credentials present: \\(present, privacy: .public)\")",
                    "logger.warning(\"Shared push credentials unavailable\")"
                ].contains(String(line).trimmingCharacters(in: .whitespaces)), "NSE only permits its two safe diagnostic logs")
            }
            for forbidden in ["UserDefaults", "FileHandle", "write(to:", "Data(contentsOf:"] {
                XCTAssertFalse(text.contains(forbidden), "NSE must not use plaintext credential storage: \(forbidden)")
            }
        }
        let content = try String(contentsOf: root.appendingPathComponent("Sources/BotvilleCore/PushNotificationContent.swift"), encoding: .utf8)
        XCTAssertTrue(content.contains("URLSession"))
        XCTAssertTrue(content.contains("/_matrix/client/v3/rooms/"))
        XCTAssertTrue(content.contains("/event/"))
        XCTAssertEqual(content.components(separatedBy: "dataTask(with:").count - 1, 2)
        XCTAssertEqual(content.components(separatedBy: "URLRequest(url:").count - 1, 2)
        XCTAssertEqual(content.components(separatedBy: "httpMethod =").count - 1, 2)
        XCTAssertEqual(content.components(separatedBy: "httpMethod = \"GET\"").count - 1, 2)
        XCTAssertTrue(content.contains("Authorization"))
        XCTAssertTrue(content.contains(".ephemeral"))
        XCTAssertTrue(content.contains("willPerformHTTPRedirection"))
        XCTAssertTrue(content.contains("requestTimeout"))
        XCTAssertTrue(content.contains("timeBudget"))
        XCTAssertEqual(content.components(separatedBy: "/_matrix/client/v3/rooms/").count - 1, 1)
        XCTAssertEqual(content.components(separatedBy: "/_matrix/client/v3/profile/").count - 1, 1)
        XCTAssertEqual(content.components(separatedBy: "/_matrix/client/").count - 1, 2)
        XCTAssertTrue(content.contains("components.percentEncodedPath = base + \"/_matrix/client/v3/rooms/\" + room + \"/event/\" + event"))
        XCTAssertTrue(content.split(separator: "\n").contains {
            $0.trimmingCharacters(in: .whitespaces) == "components.percentEncodedPath = base + \"/_matrix/client/v3/profile/\" + user"
        })
    }
    private func notificationServiceSources(_ objects: [String: Any]) throws -> [URL] {
        let target = try XCTUnwrap(objects.values.compactMap { $0 as? [String: Any] }.first { $0["isa"] as? String == "PBXNativeTarget" && $0["name"] as? String == "BotvilleNotificationService" })
        XCTAssertTrue((target["packageProductDependencies"] as? [String] ?? []).isEmpty, "NSE must not link package products")
        let phases = target["buildPhases"] as? [String] ?? []
        for phase in phases.compactMap({ objects[$0] as? [String: Any] }) where phase["isa"] as? String == "PBXFrameworksBuildPhase" {
            XCTAssertTrue((phase["files"] as? [String] ?? []).isEmpty, "NSE must not link frameworks")
        }
        let sourcePhase = try XCTUnwrap(phases.compactMap { objects[$0] as? [String: Any] }.first { $0["isa"] as? String == "PBXSourcesBuildPhase" })
        return try XCTUnwrap(sourcePhase["files"] as? [String]).map { identifier in
            let buildFile = try XCTUnwrap(objects[identifier] as? [String: Any])
            let fileRef = try XCTUnwrap(buildFile["fileRef"] as? String)
            let file = try XCTUnwrap(objects[fileRef] as? [String: Any])
            return try notificationServiceSourceURL(file: file, fileRefID: fileRef, objects: objects)
        }
    }
    private func notificationServiceSourceURL(file: [String: Any], fileRefID: String, objects: [String: Any]) throws -> URL {
        let path = try XCTUnwrap(file["path"] as? String)
        let sourceTree = try XCTUnwrap(file["sourceTree"] as? String)
        let expected: [String: (String, String)] = [
            "NotificationService.swift": ("<group>", "NotificationService/NotificationService.swift"),
            "Sources/BotvilleCore/SharedPushCredentials.swift": ("SOURCE_ROOT", "Sources/BotvilleCore/SharedPushCredentials.swift"),
            "Sources/BotvilleCore/PushNotificationContent.swift": ("SOURCE_ROOT", "Sources/BotvilleCore/PushNotificationContent.swift")
        ]
        let value = try XCTUnwrap(expected[path], "Unexpected NSE source")
        XCTAssertEqual(sourceTree, value.0, "NSE source must use its approved source tree")
        if sourceTree == "<group>" {
            let containingGroup = try XCTUnwrap(objects.values.compactMap { $0 as? [String: Any] }.first { ($0["children"] as? [String] ?? []).contains(fileRefID) })
            XCTAssertEqual(containingGroup["path"] as? String, "NotificationService")
        }
        return root.appendingPathComponent(value.1)
    }
    private func swiftSources(in directory: URL) throws -> [URL] {
        let enumerator = try XCTUnwrap(FileManager.default.enumerator(at: directory, includingPropertiesForKeys: nil))
        return enumerator.compactMap { $0 as? URL }.filter { $0.pathExtension == "swift" }
    }
    private func assertNotificationServiceIsEmbedded(_ objects: [String: Any]) throws {
        let targets = objects.compactMap { identifier, value -> (String, [String: Any])? in
            guard let target = value as? [String: Any], target["isa"] as? String == "PBXNativeTarget" else { return nil }
            return (identifier, target)
        }
        let app = try XCTUnwrap(targets.first { $0.1["name"] as? String == "BotvilleApp-iOS" })
        let nse = try XCTUnwrap(targets.first { $0.1["name"] as? String == "BotvilleNotificationService" })
        let nseProduct = try XCTUnwrap(nse.1["productReference"] as? String)
        let dependencies = app.1["dependencies"] as? [String] ?? []
        XCTAssertTrue(dependencies.contains {
            let dependency = objects[$0] as? [String: Any]
            return dependency?["isa"] as? String == "PBXTargetDependency" && dependency?["target"] as? String == nse.0
        })
        let phases = app.1["buildPhases"] as? [String] ?? []
        XCTAssertTrue(phases.contains { phaseID in
            guard let phase = objects[phaseID] as? [String: Any], phase["isa"] as? String == "PBXCopyFilesBuildPhase", String(describing: phase["dstSubfolderSpec"] ?? "") == "13" else { return false }
            return (phase["files"] as? [String] ?? []).contains {
                let file = objects[$0] as? [String: Any]
                return file?["isa"] as? String == "PBXBuildFile" && file?["fileRef"] as? String == nseProduct
            }
        })
    }
}
