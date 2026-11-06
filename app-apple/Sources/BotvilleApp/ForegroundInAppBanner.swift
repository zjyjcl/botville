// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import BotvilleCore
import SwiftUI

@MainActor
struct ForegroundInAppBannerView: View {
    let candidate: ForegroundBannerCandidate?
    let open: () -> Void
    let dismiss: () -> Void

    var body: some View {
        if let candidate {
            HStack(spacing: 12) {
                Button(action: open) {
                    HStack(spacing: 12) {
                        initials(for: candidate.title)
                        VStack(alignment: .leading, spacing: 3) {
                            Text(candidate.title)
                                .font(.subheadline.weight(.semibold))
                                .foregroundStyle(.primary)
                                .lineLimit(1)
                            Text(candidate.preview)
                                .font(.subheadline)
                                .foregroundStyle(.secondary)
                                .lineLimit(2)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("\(candidate.title)，\(candidate.preview)。点按打开房间")
                Button(action: dismiss) {
                    Image(systemName: "xmark")
                        .font(.caption.weight(.bold))
                        .foregroundStyle(.secondary)
                        .padding(8)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("关闭前台消息横幅")
            }
            .padding(12)
            .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
            .overlay {
                RoundedRectangle(cornerRadius: 16, style: .continuous)
                    .stroke(Color.primary.opacity(0.08), lineWidth: 1)
            }
            .shadow(color: .black.opacity(0.14), radius: 14, y: 6)
            .padding(.horizontal, 12)
            .transition(.move(edge: .top).combined(with: .opacity))
        }
    }

    private func initials(for title: String) -> some View {
        let words = title.split(whereSeparator: { $0.isWhitespace })
        let letters = words.prefix(2).compactMap(\.first)
        let text = letters.isEmpty ? "?" : String(letters).uppercased()
        return Text(text)
            .font(.subheadline.weight(.bold))
            .foregroundStyle(.white)
            .frame(width: 38, height: 38)
            .background(Color.accentColor, in: Circle())
            .accessibilityHidden(true)
    }
}
