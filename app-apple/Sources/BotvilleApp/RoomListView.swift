// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import BotvilleCore
import SwiftUI

@MainActor
struct RoomListView: View {
    @ObservedObject var service: MatrixService
    @Binding var selection: String?
    @State private var isPresentingNewConversation = false
    @State private var pendingCreatedRoomID: String?

    var body: some View {
        List(selection: $selection) {
            let invitations = service.rooms.filter { $0.membership == .invited }
            let joined = service.rooms.filter { $0.membership == .joined }
            if joined.isEmpty && invitations.isEmpty {
                ContentUnavailableView(
                    "还没有房间",
                    systemImage: "tray",
                    description: Text("同步完成后，已加入的 Matrix 房间会显示在这里。")
                )
            } else {
                if !invitations.isEmpty {
                    Section("邀请") {
                        ForEach(invitations) { room in
                            InvitationRow(service: service, room: room)
                        }
                    }
                }
                Section("会话") {
                    ForEach(joined) { room in
                        NavigationLink(value: room.id) {
                            RoomRow(room: room)
                        }
                        .tag(room.id)
                    }
                }
            }
        }
        .navigationTitle("房间")
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button {
                    isPresentingNewConversation = true
                } label: {
                    Label("新建会话", systemImage: "square.and.pencil")
                }
                .accessibilityLabel("新建会话")
            }
            ToolbarItem(placement: .automatic) {
                Label(service.connectionStatus.displayName, systemImage: service.isSyncing ? "arrow.triangle.2.circlepath" : "wifi")
                    .font(.caption)
                    .foregroundStyle(service.isSyncing ? .secondary : .primary)
            }
            ToolbarItem(placement: .primaryAction) {
                Button {
                    Task { try? await service.sync() }
                } label: {
                    if service.isSyncing {
                        ProgressView()
                    } else {
                        Image(systemName: "arrow.clockwise")
                    }
                }
                .disabled(service.isSyncing)
                .accessibilityLabel("同步房间")
            }

            ToolbarItem(placement: .automatic) {
                Button("退出") {
                    Task { await service.logout() }
                }
            }
        }
        .safeAreaInset(edge: .bottom) {
            if pendingCreatedRoomID != nil || service.syncError != nil || service.sessionError != nil {
                VStack(spacing: 0) {
                    if pendingCreatedRoomID != nil {
                        HStack(spacing: 8) {
                            Image(systemName: "clock.arrow.circlepath")
                            Text("会话已创建，正在等待同步。无需再次创建。")
                        }
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .padding(8)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    if let syncError = service.syncError ?? service.sessionError {
                        HStack(spacing: 8) {
                            Image(systemName: service.isSyncing ? "arrow.triangle.2.circlepath" : "wifi.exclamationmark")
                            Text(syncError)
                        }
                        .font(.footnote)
                        .foregroundStyle(.red)
                        .padding(8)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
                .background(.bar)
            }
        }
        .sheet(isPresented: $isPresentingNewConversation) {
            NewConversationSheet(service: service) { roomID in
                pendingCreatedRoomID = roomID
                selectPendingCreatedRoomIfAvailable()
            }
        }
        .onChange(of: service.rooms.map(\.id)) { _, _ in
            selectPendingCreatedRoomIfAvailable()
        }
    }

    private func selectPendingCreatedRoomIfAvailable() {
        guard let roomID = pendingCreatedRoomID,
              service.rooms.contains(where: { $0.id == roomID && $0.membership == .joined }) else { return }
        selection = roomID
        pendingCreatedRoomID = nil
    }
}

@MainActor
private struct RoomRow: View {
    let room: MatrixRoom

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: room.isDirect ? "person.crop.circle" : "person.2.circle")
                .foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 2) {
                Text(room.name)
                    .lineLimit(1)
                EncryptionBadge(room: room)
            }
            Spacer()
            if room.unreadCount > 0 {
                Text(room.unreadCount, format: .number)
                    .font(.caption.bold())
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(.tint, in: Capsule())
                    .foregroundStyle(.white)
            }
        }
    }
}

@MainActor
private struct EncryptionBadge: View {
    let room: MatrixRoom

    private var presentation: EncryptionPresentation {
        EncryptionPresentation(encryption: room.encryption, participants: room.participants)
    }

    var body: some View {
        Label(presentation.title, systemImage: presentation.systemImage)
            .font(.caption2)
            .foregroundStyle(.secondary)
            .accessibilityLabel("加密状态：\(presentation.title)。\(presentation.detail)")
    }
}

@MainActor
private struct NewConversationSheet: View {
    @Environment(\.dismiss) private var dismiss
    @ObservedObject var service: MatrixService
    let didCreate: (String) -> Void

    @State private var isDirect = true
    @State private var roomName = ""
    @State private var inviteesText = ""
    @State private var wantsEncryption = false
    @State private var consent = EncryptionConsent()
    @State private var isCreating = false
    @State private var errorMessage: String?
    @State private var isShowingEncryptionConfirmation = false

    private var invitees: [String] {
        inviteesText.split(whereSeparator: { $0 == "," || $0 == "\n" || $0 == " " })
            .map(String.init)
    }

    private var draft: RoomCreationDraft {
        RoomCreationDraft(isDirect: isDirect, name: roomName, invitees: invitees)
    }

    private var participantStatus: AIParticipantStatus {
        service.aiParticipantPolicy.classify(memberIDs: invitees + [service.userID].compactMap { $0 })
    }

    private var encryptionPresentation: EncryptionPresentation {
        EncryptionPresentation(encryption: .unencrypted, participants: participantStatus)
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("会话类型") {
                    Picker("类型", selection: $isDirect) {
                        Text("私聊").tag(true)
                        Text("群聊").tag(false)
                    }
                    .pickerStyle(.segmented)
                    if !isDirect {
                        TextField("群聊名称（可选）", text: $roomName)
                    }
                }

                Section(isDirect ? "对方 Matrix ID" : "邀请 Matrix ID") {
                    TextField(isDirect ? "例如 @alice:example.org" : "每行或逗号分隔一个 MXID", text: $inviteesText, axis: .vertical)
#if os(iOS)
                        .textInputAutocapitalization(.never)
#endif
                        .autocorrectionDisabled()
                    Text(isDirect ? "私聊必须填写一个 MXID。" : "群聊至少填写一个 MXID；输入的 MXID 会在创建后邀请。")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }

                Section("加密") {
                    Toggle("启用端对端加密", isOn: $wantsEncryption)
                        .disabled(!encryptionPresentation.canEnable || isCreating)
                    Label(encryptionPresentation.detail, systemImage: encryptionPresentation.systemImage)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                    if !service.aiParticipantPolicy.hasConfiguredRules {
                        Text("尚未完成 AI 账号识别设置；为避免误加密，含成员的会话按可能含 AI 处理。")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                    Text("加密一旦开启不能关闭；AI 无法读取消息。开启前请确认设备验证和密钥备份。")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }

                if let errorMessage {
                    Text(errorMessage).foregroundStyle(.red)
                }
            }
            .navigationTitle("新建会话")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消") { dismiss() }.disabled(isCreating)
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button(isCreating ? "创建中…" : "创建") { prepareCreation() }
                        .disabled(isCreating)
                }
            }
            .alert("确认开启端对端加密？", isPresented: $isShowingEncryptionConfirmation) {
                Button("开启并创建") { createAfterConfirmation() }
                Button("返回", role: .cancel) { consent.cancel() }
            } message: {
                Text("开启后不可关闭，AI 将无法读取消息；未验证设备或未设置密钥备份时，换设备可能无法查看历史消息。")
            }
            .onChange(of: isDirect) { _, _ in consent.contextChanged(to: .newRoom(draft)) }
            .onChange(of: roomName) { _, _ in consent.contextChanged(to: .newRoom(draft)) }
            .onChange(of: inviteesText) { _, _ in consent.contextChanged(to: .newRoom(draft)) }
        }
    }

    private func prepareCreation() {
        errorMessage = nil
        do {
            try draft.validate()
            guard wantsEncryption else { create(draft: draft, consent: EncryptionConsent()); return }
            guard encryptionPresentation.canEnable else {
                errorMessage = encryptionPresentation.detail
                return
            }
            consent.request(for: .newRoom(draft))
            isShowingEncryptionConfirmation = true
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func createAfterConfirmation() {
        let target = EncryptionTarget.newRoom(draft)
        guard consent.confirm(for: target) else { return }
        create(draft: draft, consent: consent)
    }

    private func create(draft: RoomCreationDraft, consent: EncryptionConsent) {
        guard !isCreating else { return }
        isCreating = true
        Task {
            defer { isCreating = false }
            do {
                let roomID = try await service.createRoom(draft: draft, consent: consent)
                didCreate(roomID)
                dismiss()
            } catch {
                errorMessage = error.localizedDescription
            }
        }
    }
}

@MainActor
private struct InvitationRow: View {
    @ObservedObject var service: MatrixService
    let room: MatrixRoom
    @State private var isResponding = false
    @State private var errorMessage: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            RoomRow(room: room)
            HStack {
                Button("拒绝", role: .destructive) { respond(accept: false) }
                Button("接受") { respond(accept: true) }
                    .buttonStyle(.borderedProminent)
                if isResponding { ProgressView().controlSize(.small) }
            }
            .disabled(isResponding)
            if let errorMessage {
                Text(errorMessage).font(.caption).foregroundStyle(.red)
            }
        }
    }

    private func respond(accept: Bool) {
        guard !isResponding else { return }
        isResponding = true
        errorMessage = nil
        Task {
            defer { isResponding = false }
            do {
                if accept { try await service.acceptInvitation(room.id) }
                else { try await service.rejectInvitation(room.id) }
            } catch { errorMessage = error.localizedDescription }
        }
    }
}
