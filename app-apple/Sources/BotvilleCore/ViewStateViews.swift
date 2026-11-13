// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import SwiftUI

/// Small presentation-only components live here so the existing Core test target
/// can compile their state-only initializer contracts without importing the app.
public struct PersonaMoodBadge: View {
    public let presentation: PersonaMoodPresentation

    public init(presentation: PersonaMoodPresentation) {
        self.presentation = presentation
    }

    public var body: some View {
        HStack {
            Label(presentation.mood?.label ?? "未设置",
                  systemImage: presentation.mood?.symbolName ?? "face.dashed")
            if let marker = presentation.privateMarkerText {
                Text(marker).font(.caption2).foregroundStyle(.secondary)
            }
        }
    }
}

public struct GroupRoomRow: View {
    public let state: GroupRoomRowViewState
    private let onModeChanged: (GroupRoomMode) -> Void
    private let onLeave: () -> Void

    public init(
        state: GroupRoomRowViewState,
        onModeChanged: @escaping (GroupRoomMode) -> Void, onLeave: @escaping () -> Void
    ) {
        self.state = state
        self.onModeChanged = onModeChanged
        self.onLeave = onLeave
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(state.title).font(.headline)
                Spacer()
                Text(state.statusLabel).font(.caption.weight(.medium)).foregroundStyle(.secondary)
            }
            if let joinedAt = state.joinedAt {
                Text("加入于 \(joinedAt.formatted(date: .abbreviated, time: .shortened))")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if let notice = state.modeNotice {
                Text(notice).font(.footnote).foregroundStyle(.secondary)
            }
            ForEach(state.actions) { action in
                switch action {
                case .toggleMode(let current):
                    Toggle("安静模式", isOn: Binding(
                        get: { current == .muted },
                        set: { onModeChanged($0 ? .muted : .mentionOnly) }
                    ))
                    .disabled(state.isDisabled)
                    ForEach(action.helpTexts, id: \.self) { text in
                        Text(text).font(.footnote).foregroundStyle(.secondary)
                    }
                case .muteOneWay:
                    Button("设为安静") { onModeChanged(.muted) }
                        .disabled(state.isDisabled)
                case .mutedStatus:
                    Text("已设为安静").font(.footnote).foregroundStyle(.secondary)
                case .leave:
                    Button("退出群聊", role: .destructive, action: onLeave)
                        .disabled(state.isDisabled)
                }
            }
            if let notice = state.syncNotice {
                Text(notice).font(.footnote).foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 3)
    }
}

public struct NewConversationCreateButton: View {
    public let state: NewConversationFormState
    private let action: () -> Void

    public init(state: NewConversationFormState, action: @escaping () -> Void) {
        self.state = state
        self.action = action
    }

    public var body: some View {
        Button(state.createTitle, action: action)
            .disabled(state.isCreateDisabled)
    }
}
