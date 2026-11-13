// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation
import XCTest

@testable import BotvilleCore

final class MemoryPresentationPolicyTests: XCTestCase {
    private func manifest(_ capabilities: [[String: Any]]) throws -> GatewayCapabilityManifest {
        try GatewayCoding.decoder.decode(
            GatewayCapabilityManifest.self,
            from: JSONSerialization.data(
                withJSONObject: [
                    "manifest_version": "1.3.0", "contract_version": "0.2.0",
                    "generated_at": "2026-11-13T00:00:00Z", "capabilities": capabilities,
                ]))
    }

    private func capability(
        status: String = "enabled", disclosure: Bool? = nil,
        route: String = "botville://memories", requires: [String]? = nil
    ) -> [String: Any] {
        var value: [String: Any] = [
            "id": "memory.manage", "module": "memory", "version": "0.2.0",
            "title": "记忆", "summary": "管理记忆", "status": status,
            "entry": ["type": "native", "deep_link": route],
            "scopes": ["memory.read", "memory.write"],
        ]
        if let disclosure { value["owner_can_read_dm"] = disclosure }
        if let requires { value["requires"] = requires }
        return value
    }

    func testAvailableNativeMemoryEntryIsVisible() throws {
        XCTAssertTrue(
            MemoryPresentationPolicy.isMemoryManagementAvailable(in: try manifest([capability()])))
    }

    func testAbsentMemoryEntryIsHidden() throws {
        XCTAssertFalse(MemoryPresentationPolicy.isMemoryManagementAvailable(in: try manifest([])))
        XCTAssertFalse(MemoryPresentationPolicy.isMemoryManagementAvailable(in: nil))
    }

    func testDisabledAndUnknownStatusAreHidden() throws {
        for status in ["disabled", "future"] {
            XCTAssertFalse(
                MemoryPresentationPolicy.isMemoryManagementAvailable(
                    in: try manifest([capability(status: status)])))
        }
    }

    func testDegradedButAvailableCapabilityCanBeOpened() throws {
        XCTAssertTrue(
            MemoryPresentationPolicy.isMemoryManagementAvailable(
                in: try manifest([capability(status: "degraded")])))
    }

    func testMissingOrCyclicDependencyHidesEntry() throws {
        for dependency in ["absent", "memory.manage"] {
            XCTAssertFalse(
                MemoryPresentationPolicy.isMemoryManagementAvailable(
                    in: try manifest([capability(requires: [dependency])])))
        }
    }

    func testUnknownNativeRouteAndNonNativeEntryAreHidden() throws {
        XCTAssertFalse(
            MemoryPresentationPolicy.isMemoryManagementAvailable(
                in: try manifest([capability(route: "botville://memoriesx")])))
        var web = capability()
        web["entry"] = ["type": "web", "web_url": "https://example.invalid/memory"]
        XCTAssertFalse(
            MemoryPresentationPolicy.isMemoryManagementAvailable(in: try manifest([web])))
    }

    func testExplicitTrueShowsOwnerDisclosure() throws {
        let value = try manifest([capability(disclosure: true)])
        XCTAssertEqual(value.capabilities.first?.ownerCanReadDM, true)
        XCTAssertTrue(MemoryPresentationPolicy.shouldShowOwnerDisclosure(in: value))
        XCTAssertEqual(MemoryPresentationPolicy.disclosureText, "该 AI 的主人可以查看和删除它与你的聊天记忆")
    }

    func testExplicitFalseHidesOwnerDisclosure() throws {
        XCTAssertFalse(
            MemoryPresentationPolicy.shouldShowOwnerDisclosure(
                in: try manifest([capability(disclosure: false)])))
    }

    func testMissingFieldAndMissingCapabilityHideOwnerDisclosure() throws {
        let value = try manifest([capability()])
        XCTAssertNil(value.capabilities.first?.ownerCanReadDM)
        XCTAssertFalse(MemoryPresentationPolicy.shouldShowOwnerDisclosure(in: value))
        XCTAssertFalse(MemoryPresentationPolicy.shouldShowOwnerDisclosure(in: try manifest([])))
        XCTAssertFalse(MemoryPresentationPolicy.shouldShowOwnerDisclosure(in: nil))
    }

    func testOtherCapabilityCannotEnableDisclosure() throws {
        var other = capability(disclosure: true)
        other["id"] = "memory.search"
        XCTAssertFalse(
            MemoryPresentationPolicy.shouldShowOwnerDisclosure(in: try manifest([other])))
    }

    func testMockManifestVersionCapabilityAndDisclosure() async throws {
        let value = try await MockGatewayClient().capabilities()
        XCTAssertEqual(value.manifestVersion, "1.3.0")
        XCTAssertEqual(value.contractVersion, "0.2.0")
        XCTAssertTrue(MemoryPresentationPolicy.isMemoryManagementAvailable(in: value))
        XCTAssertEqual(
            value.capabilities.first(where: { $0.id == "memory.manage" })?.ownerCanReadDM, false)
        XCTAssertFalse(MemoryPresentationPolicy.shouldShowOwnerDisclosure(in: value))
    }
}
