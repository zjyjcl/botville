// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation
import XCTest

final class ReleaseIsolationTests: XCTestCase {
    private let forbidden: Set<String> = ["NSAppTransportSecurity", "NSAllowsLocalNetworking", "NSLocalNetworkUsageDescription", "BotvilleSygnalNotifyURL"]

    func testReleasePlistRecursivelyExcludesDebugKeys() throws {
        XCTAssertTrue(forbidden.isDisjoint(with: keys(try plist("BotvilleApp/Info.plist"))))
    }

    func testDebugPlistContainsDebugNetworkingOnly() throws {
        let value = try plist("BotvilleApp/Info-Debug.plist")
        XCTAssertEqual((value["NSAppTransportSecurity"] as? [String: Any])?["NSAllowsLocalNetworking"] as? Bool, true)
        XCTAssertNotNil(value["NSLocalNetworkUsageDescription"])
        XCTAssertEqual(value["BotvilleSygnalNotifyURL"] as? String, "")
    }

    func testEveryApplicationReleaseTargetUsesShippingPlist() throws {
        let objects = try XCTUnwrap(projectJSON()["objects"] as? [String: Any])
        let apps = objects.values.compactMap { $0 as? [String: Any] }.filter { $0["isa"] as? String == "PBXNativeTarget" && $0["productType"] as? String == "com.apple.product-type.application" }
        XCTAssertFalse(apps.isEmpty)
        var releaseCount = 0
        for app in apps {
            let list = try XCTUnwrap(objects[try XCTUnwrap(app["buildConfigurationList"] as? String)] as? [String: Any])
            for id in try XCTUnwrap(list["buildConfigurations"] as? [String]) {
                let config = try XCTUnwrap(objects[id] as? [String: Any])
                if config["name"] as? String == "Release" {
                    releaseCount += 1
                    let settings = try XCTUnwrap(config["buildSettings"] as? [String: Any])
                    XCTAssertEqual(settings["INFOPLIST_FILE"] as? String, "BotvilleApp/Info.plist")
                    for (key, value) in settings where key.hasPrefix("INFOPLIST_FILE[") {
                        XCTAssertEqual(value as? String, "BotvilleApp/Info.plist", key)
                    }
                }
            }
        }
        XCTAssertGreaterThan(releaseCount, 0)
    }

    func testEntitlementAndDebugViewsAreIsolated() throws {
        let project = try String(contentsOf: root.appendingPathComponent("Botville.xcodeproj/project.pbxproj"), encoding: .utf8)
        XCTAssertTrue(project.contains("CODE_SIGN_ENTITLEMENTS = BotvilleApp/BotvilleApp.entitlements;"))
        XCTAssertTrue(project.contains("com.apple.Push = { enabled = 1; };"))
        XCTAssertFalse(project.contains("DEVELOPMENT_TEAM"))
        XCTAssertEqual(try plist("BotvilleApp/BotvilleApp.entitlements")["aps-environment"] as? String, "development")
        let sources = root.appendingPathComponent("Sources/BotvilleApp")
        try guarded("struct PushDiagnosticsView", at: sources.appendingPathComponent("PushDiagnosticsView.swift"))
        try guarded("ManualHomeserverLoginView", at: sources.appendingPathComponent("LoginView.swift"))
        for url in try FileManager.default.contentsOfDirectory(at: sources, includingPropertiesForKeys: nil).filter({ $0.pathExtension == "swift" }) {
            let text = try String(contentsOf: url, encoding: .utf8)
            for token in ["PushDiagnosticsView(", "ManualHomeserverLoginView("] where text.contains(token) { try guarded(token, at: url) }
        }
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
