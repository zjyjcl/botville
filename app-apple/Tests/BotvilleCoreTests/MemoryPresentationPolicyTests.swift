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

    private func disclosure(in manifest: GatewayCapabilityManifest?) -> Bool {
        MemoryPresentationPolicy.shouldShowOwnerDisclosure(
            in: manifest, aiUserID: "@ai_test:local.invalid", localServerName: "local.invalid")
    }

    func testFederatedAIAndUnknownLocalServerHideDisclosure() throws {
        let value = try manifest([capability(disclosure: true)])
        for server in [nil, "", "other.invalid", "local.invalid:8448", "invalid"] as [String?] {
            XCTAssertFalse(MemoryPresentationPolicy.shouldShowOwnerDisclosure(
                in: value, aiUserID: "@ai_test:local.invalid", localServerName: server))
        }
        XCTAssertFalse(MemoryPresentationPolicy.shouldShowOwnerDisclosure(
            in: value, aiUserID: "@ai_test:sub.local.invalid", localServerName: "local.invalid"))
    }

    func testMatrixServerNamePreservesPortAndIPv6() {
        XCTAssertEqual(MemoryPresentationPolicy.matrixServerName(in: "@person:local.invalid:8448"), "local.invalid:8448")
        XCTAssertEqual(MemoryPresentationPolicy.matrixServerName(in: "@person:[::1]:8448"), "[::1]:8448")
        for invalid in ["person:local.invalid", "@:local.invalid", "@person:", "@person:bad host", "@person:https://local.invalid"] {
            XCTAssertNil(MemoryPresentationPolicy.matrixServerName(in: invalid))
        }
    }

    func testOwnerExplanationIsDistinctFromCounterpartDisclosure() {
        XCTAssertEqual(MemoryPresentationPolicy.ownerExplanationText, "私聊内容是否可见由服务端开关决定")
        XCTAssertNotEqual(MemoryPresentationPolicy.ownerExplanationText, MemoryPresentationPolicy.disclosureText)
    }

    func testOwnerAndCounterpartViewsUseTheirRespectiveCopy() throws {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        let ownerView = try String(contentsOf: root.appendingPathComponent("Sources/BotvilleApp/MemoryViews.swift"), encoding: .utf8)
        let profileView = try String(contentsOf: root.appendingPathComponent("Sources/BotvilleApp/AIProfileView.swift"), encoding: .utf8)
        XCTAssertTrue(ownerView.contains("MemoryPresentationPolicy.ownerExplanationText"))
        XCTAssertFalse(ownerView.contains("MemoryPresentationPolicy.disclosureText"))
        XCTAssertFalse(ownerView.contains("shouldShowOwnerDisclosure"))
        XCTAssertTrue(profileView.contains("MemoryPresentationPolicy.disclosureText"))
        XCTAssertTrue(profileView.contains("service.aiProfileDisclosure"))
    }

    func testRedactedActionsAllowDeleteAndOnlyExistingPinRemoval() {
        for layer in ["recent", "profile"] {
            let unpinned = MemoryPresentationPolicy.actions(layer: layer, redacted: true, pinned: false)
            XCTAssertTrue(unpinned.canDelete)
            XCTAssertFalse(unpinned.canPin)
            XCTAssertFalse(unpinned.canUnpin)
            XCTAssertFalse(unpinned.canTogglePin)
            let pinned = MemoryPresentationPolicy.actions(layer: layer, redacted: true, pinned: true)
            XCTAssertTrue(pinned.canDelete)
            XCTAssertFalse(pinned.canPin)
            XCTAssertTrue(pinned.canUnpin)
            XCTAssertTrue(pinned.canTogglePin)
        }
    }

    func testVisibleSummaryAndUnknownLayerActions() {
        let visible = MemoryPresentationPolicy.actions(layer: "recent", redacted: false, pinned: false)
        XCTAssertTrue(visible.canPin)
        XCTAssertFalse(visible.canUnpin)
        let pinned = MemoryPresentationPolicy.actions(layer: "profile", redacted: false, pinned: true)
        XCTAssertFalse(pinned.canPin)
        XCTAssertTrue(pinned.canUnpin)
        for redacted in [true, false] {
            for pinned in [true, false] {
                let summary = MemoryPresentationPolicy.actions(layer: "summary", redacted: redacted, pinned: pinned)
                XCTAssertTrue(summary.canDelete)
                XCTAssertFalse(summary.canTogglePin)
                let unknown = MemoryPresentationPolicy.actions(layer: "future", redacted: redacted, pinned: pinned)
                XCTAssertFalse(unknown.canDelete)
                XCTAssertFalse(unknown.canTogglePin)
            }
        }
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
        XCTAssertTrue(disclosure(in: value))
        XCTAssertEqual(MemoryPresentationPolicy.disclosureText, "该 AI 的主人可以查看和删除它与你的聊天记忆")
    }

    func testExplicitFalseHidesOwnerDisclosure() throws {
        XCTAssertFalse(
            disclosure(
                in: try manifest([capability(disclosure: false)])))
    }

    func testMissingFieldAndMissingCapabilityHideOwnerDisclosure() throws {
        let value = try manifest([capability()])
        XCTAssertNil(value.capabilities.first?.ownerCanReadDM)
        XCTAssertFalse(disclosure(in: value))
        XCTAssertFalse(disclosure(in: try manifest([])))
        XCTAssertFalse(disclosure(in: nil))
    }

    func testOtherCapabilityCannotEnableDisclosure() throws {
        var other = capability(disclosure: true)
        other["id"] = "memory.search"
        XCTAssertFalse(
            disclosure(in: try manifest([other])))
    }

    func testMockManifestVersionCapabilityAndDisclosure() async throws {
        let value = try await MockGatewayClient().capabilities()
        XCTAssertEqual(value.manifestVersion, "1.3.0")
        XCTAssertEqual(value.contractVersion, "0.4.0")
        XCTAssertTrue(MemoryPresentationPolicy.isMemoryManagementAvailable(in: value))
        XCTAssertEqual(
            value.capabilities.first(where: { $0.id == "memory.manage" })?.ownerCanReadDM, false)
        XCTAssertFalse(disclosure(in: value))
    }
}
