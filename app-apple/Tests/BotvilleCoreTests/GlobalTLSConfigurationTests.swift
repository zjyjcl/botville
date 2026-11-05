// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

@testable import BotvilleCore
import Foundation
import XCTest

final class GlobalTLSConfigurationTests: XCTestCase {
    // This is a conservative static guard against ATS overrides that weaken
    // transport security. NSAllowsLocalNetworking is intentionally allowed:
    // it does not disable HTTPS certificate validation.
    private let forbiddenATSKeys: Set<String> = [
        "NSAllowsArbitraryLoads",
        "NSAllowsArbitraryLoadsForMedia",
        "NSAllowsArbitraryLoadsInWebContent",
        "NSExceptionAllowsInsecureHTTPLoads",
        "NSExceptionMinimumTLSVersion",
        "NSExceptionRequiresForwardSecrecy",
        "NSTemporaryExceptionAllowsInsecureHTTPLoads",
        "NSTemporaryExceptionMinimumTLSVersion",
        "NSTemporaryExceptionRequiresForwardSecrecy",
        "NSThirdPartyExceptionAllowsInsecureHTTPLoads",
        "NSThirdPartyExceptionMinimumTLSVersion",
        "NSThirdPartyExceptionRequiresForwardSecrecy"
    ]

    func testAppSourceHasNoGlobalTLSBypass() throws {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let fileManager = FileManager.default
        let enumerator = try XCTUnwrap(fileManager.enumerator(
            at: root,
            includingPropertiesForKeys: [.isDirectoryKey],
            errorHandler: { url, error in
                XCTFail("Unable to inspect \(url.path): \(error)")
                return false
            }
        ))
        var inspectedFiles = 0

        for case let fileURL as URL in enumerator {
            let values = try fileURL.resourceValues(forKeys: [.isDirectoryKey])
            if values.isDirectory == true {
                if fileURL.lastPathComponent == ".build" || fileURL.lastPathComponent == ".git" {
                    enumerator.skipDescendants()
                }
                continue
            }

            if fileURL.pathComponents.contains(".build") || fileURL.pathComponents.contains(".git") { continue }
            let name = fileURL.lastPathComponent
            if fileURL.pathExtension == "plist" {
                inspectedFiles += 1
                let plist = try PropertyListSerialization.propertyList(from: Data(contentsOf: fileURL), options: [], format: nil)
                let keys = plistKeys(in: plist)
                let forbidden = keys.intersection(forbiddenATSKeys)
                XCTAssertTrue(forbidden.isEmpty, "\(fileURL.path) contains forbidden ATS keys: \(forbidden.sorted())")
            } else if fileURL.pathExtension == "xcconfig" || name == "project.pbxproj" {
                inspectedFiles += 1
                try assertNoTLSBypassText(in: fileURL)
            } else if fileURL.pathExtension == "swift" && fileURL.pathComponents.contains("Sources") {
                inspectedFiles += 1
                try assertNoTLSBypassText(in: fileURL)
            }
        }

        XCTAssertGreaterThan(inspectedFiles, 0, "TLS guard did not inspect any application files")
    }

    private func assertNoTLSBypassText(in fileURL: URL) throws {
        let contents = String(decoding: try Data(contentsOf: fileURL), as: UTF8.self)
        XCTAssertFalse(contents.contains("disableSslVerification"), "\(fileURL.path) disables SDK TLS verification")
        for key in forbiddenATSKeys {
            XCTAssertFalse(contents.contains(key), "\(fileURL.path) contains forbidden ATS key \(key)")
        }
    }

    private func plistKeys(in value: Any) -> Set<String> {
        if let dictionary = value as? [String: Any] {
            return dictionary.reduce(into: Set<String>()) { result, entry in
                result.insert(entry.key)
                result.formUnion(plistKeys(in: entry.value))
            }
        }
        if let array = value as? [Any] {
            return array.reduce(into: Set<String>()) { result, element in
                result.formUnion(plistKeys(in: element))
            }
        }
        return []
    }
}
