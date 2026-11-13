// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import BotvilleCore
import SwiftUI
#if os(iOS)
import UIKit
#else
import AppKit
#endif

let aiProfileAccent = Color(red: 26 / 255, green: 173 / 255, blue: 142 / 255)

/// Counterpart-facing profile. Owner memory management has separate copy.
@MainActor
struct AIProfileView: View {
    @ObservedObject var service: MatrixService
    let profile: AIProfile
    @State private var showOwnerDisclosure = false
    @State private var detailedProfile: AIProfile?
    @State private var detailError: String?
    @State private var isLoading = true

    private var displayedProfile: AIProfile { detailedProfile ?? profile }

    var body: some View {
        List {
            Section {
                HStack(alignment: .center, spacing: 18) {
                    avatar
                        .frame(width: 72, height: 72)
                        .clipShape(RoundedRectangle(cornerRadius: 16))
                        .accessibilityHidden(true)
                    VStack(alignment: .leading, spacing: 8) {
                        Text(displayedProfile.displayName).font(.title2.weight(.semibold))
                        if let mood = displayedProfile.mood {
                            PersonaMoodBadge(mood: mood)
                        }
                        if let signature = displayedProfile.signature {
                            Text(signature)
                                .font(.subheadline).foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        Text("AI")
                            .font(.caption.weight(.semibold))
                            .padding(.horizontal, 9).padding(.vertical, 3)
                            .foregroundStyle(aiProfileAccent)
                            .background(aiProfileAccent.opacity(0.12), in: Capsule())
                            .accessibilityLabel("AI 账号")
                    }
                }
                .padding(.vertical, 12)
            }
            if isLoading {
                ProgressView("正在加载…")
            } else if let detailError {
                Section {
                    Text(detailError).foregroundStyle(.secondary)
                    Button("重试") { Task { await loadDetails() } }
                }
            }
            if showOwnerDisclosure {
                Section {
                    Label(MemoryPresentationPolicy.disclosureText, systemImage: "info.circle")
                        .font(.footnote).foregroundStyle(.secondary)
                }
            }
        }
        .navigationTitle("AI 资料")
        .tint(aiProfileAccent)
        .task(id: service.userID) {
            showOwnerDisclosure = false
            detailedProfile = nil
            await loadDetails()
            guard !Task.isCancelled else { return }
            let disclosure = await service.aiProfileDisclosure(aiUserID: profile.userID)
            guard !Task.isCancelled else { return }
            showOwnerDisclosure = disclosure
        }
    }

    private func loadDetails() async {
        let userID = service.userID
        isLoading = true
        detailError = nil
        do {
            let result = try await service.aiProfileDetails(profile)
            guard !Task.isCancelled, userID == service.userID else { return }
            detailedProfile = result
        } catch {
            guard !Task.isCancelled, userID == service.userID else { return }
            detailedProfile = nil
            detailError = (error as? GatewayClientError)?.errorDescription
                ?? "暂时无法加载 AI 资料，请稍后重试。"
        }
        isLoading = false
    }

    @ViewBuilder
    private var avatar: some View {
        if let image = avatarImage {
            image.resizable().scaledToFill()
        } else {
            ZStack {
                aiProfileAccent.opacity(0.12)
                Text(String(displayedProfile.displayName.prefix(1)).uppercased())
                    .font(.largeTitle.weight(.medium)).foregroundStyle(aiProfileAccent)
            }
        }
    }

    private var avatarImage: Image? {
        guard let data = profile.avatarData else { return nil }
#if os(iOS)
        return UIImage(data: data).map { Image(uiImage: $0) }
#else
        return NSImage(data: data).map { Image(nsImage: $0) }
#endif
    }
}

private struct PersonaMoodBadge: View {
    let mood: PersonaMood

    var body: some View {
        Label(mood.label, systemImage: mood.symbolName)
            .font(.caption.weight(.medium))
            .foregroundStyle(aiProfileAccent)
            .accessibilityLabel("当前心情：\(mood.label)")
    }
}
