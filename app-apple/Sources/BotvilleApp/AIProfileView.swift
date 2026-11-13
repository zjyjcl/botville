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

    var body: some View {
        List {
            Section {
                HStack(alignment: .center, spacing: 18) {
                    avatar
                        .frame(width: 72, height: 72)
                        .clipShape(RoundedRectangle(cornerRadius: 16))
                        .accessibilityHidden(true)
                    VStack(alignment: .leading, spacing: 8) {
                        Text(profile.displayName).font(.title2.weight(.semibold))
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
            if let signature = profile.signature, !signature.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                Section("签名") { Text(signature) }
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
            let disclosure = await service.aiProfileDisclosure(aiUserID: profile.userID)
            guard !Task.isCancelled else { return }
            showOwnerDisclosure = disclosure
        }
    }

    @ViewBuilder
    private var avatar: some View {
        if let image = avatarImage {
            image.resizable().scaledToFill()
        } else {
            ZStack {
                aiProfileAccent.opacity(0.12)
                Text(String(profile.displayName.prefix(1)).uppercased())
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
