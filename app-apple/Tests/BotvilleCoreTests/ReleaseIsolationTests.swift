// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation
import XCTest

final class ReleaseIsolationTests: XCTestCase {
    private let forbidden: Set<String> = ["NSAppTransportSecurity", "NSAllowsLocalNetworking", "NSLocalNetworkUsageDescription", "BotvilleSygnalNotifyURL"]

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
        for config in configurations {
            let settings = try XCTUnwrap(config["buildSettings"] as? [String: Any])
            XCTAssertFalse(settings.keys.contains(where: { $0.hasPrefix("PROVISIONING_PROFILE") }))
            for (key, value) in settings where key.hasPrefix("CODE_SIGNING_ALLOWED") || key.hasPrefix("CODE_SIGNING_REQUIRED") {
                XCTAssertNotEqual(value as? String, "NO", key)
            }
        }
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
            plists: ["Debug": "NotificationService/Info.plist", "Release": "NotificationService/Info.plist"]
        )
    }

    func testReleaseEntitlementsAllowOnlySharedCredentialInfrastructure() throws {
        try assertEntitlements("BotvilleApp/BotvilleApp.entitlements", privateKeychainGroup: "$(AppIdentifierPrefix)com.zjyjcl.botville", apsEnvironment: "development")
        try assertEntitlements("BotvilleApp/BotvilleApp-Release.entitlements", privateKeychainGroup: "$(AppIdentifierPrefix)com.zjyjcl.botville", apsEnvironment: nil)
        try assertEntitlements("NotificationService/NotificationService.entitlements", privateKeychainGroup: "$(AppIdentifierPrefix)com.zjyjcl.botville.notification", apsEnvironment: nil)

        XCTAssertEqual(try plist("BotvilleApp/Info.plist")["BotvilleSharedKeychainAccessGroup"] as? String, "$(AppIdentifierPrefix)com.zjyjcl.botville.shared")
        XCTAssertEqual(try plist("BotvilleApp/Info-Debug.plist")["BotvilleSharedKeychainAccessGroup"] as? String, "$(AppIdentifierPrefix)com.zjyjcl.botville.shared")
        XCTAssertEqual(try plist("NotificationService/Info.plist")["BotvilleSharedKeychainAccessGroup"] as? String, "$(AppIdentifierPrefix)com.zjyjcl.botville.shared")
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
        for url in try FileManager.default.contentsOfDirectory(at: sources, includingPropertiesForKeys: nil).filter({ $0.pathExtension == "swift" }) {
            let text = try String(contentsOf: url, encoding: .utf8)
            for token in ["PushDiagnosticsView(", "ManualHomeserverLoginView("] where text.contains(token) { try guarded(token, at: url) }
        }
        let core = root.appendingPathComponent("Sources/BotvilleCore")
        try guarded("static let isDebugBuild = true", at: core.appendingPathComponent("PushRegistrar.swift"))
        let matrixService = try String(contentsOf: core.appendingPathComponent("MatrixService.swift"), encoding: .utf8)
        XCTAssertTrue(matrixService.contains("PushHTTPPolicy.permitsHTTP(allowHTTP)"))
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
            XCTAssertEqual(settings["DEVELOPMENT_TEAM"] as? String, "3Z8WP6U6F7", name)
            XCTAssertEqual(settings["CODE_SIGN_IDENTITY[sdk=iphonesimulator*]"] as? String, "-", name)
            XCTAssertEqual(settings["PRODUCT_BUNDLE_IDENTIFIER"] as? String, bundleID, name)
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
    private func assertEntitlements(_ relative: String, privateKeychainGroup: String, apsEnvironment: String?) throws {
        let value = try plist(relative)
        XCTAssertEqual(value["com.apple.security.application-groups"] as? [String], ["group.com.zjyjcl.botville"])
        XCTAssertEqual(value["keychain-access-groups"] as? [String], [privateKeychainGroup, "$(AppIdentifierPrefix)com.zjyjcl.botville.shared"])
        XCTAssertEqual(value["aps-environment"] as? String, apsEnvironment)
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
}
