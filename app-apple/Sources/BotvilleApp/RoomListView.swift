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
                            RoomRow(room: room, isMuted: service.roomNotifications.isMuted(room.id))
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
    let isMuted: Bool

    private var notificationPresentation: RoomMuteListPresentation {
        RoomMuteListPresentation(isMuted: isMuted, unreadCount: room.unreadCount)
    }

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
            if let symbol = notificationPresentation.symbol {
                Image(systemName: symbol)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .accessibilityLabel("消息免打扰")
            }
            if notificationPresentation.showsUnreadDot {
                Circle().fill(.red).frame(width: 8, height: 8)
                    .accessibilityLabel("有未读消息")
            }
            if let count = notificationPresentation.unreadNumber {
                Text(count, format: .number)
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
    @State private var encryptionSelection = NewRoomEncryptionSelection()
    @State private var consent = EncryptionConsent()
    @State private var isCreating = false
    @State private var errorMessage: String?
    @State private var isShowingEncryptionConfirmation = false
    @State private var selectedIDs: Set<String> = []
    @State private var candidates: [GroupInviteCandidate] = []
    @State private var isLoadingCandidates = false
    @State private var candidateError: String?

    private var formState: NewConversationFormState {
        NewConversationFormState(
            isDirect: isDirect, roomName: roomName, selectedIDs: selectedIDs,
            inviteesText: inviteesText, isCreating: isCreating)
    }

    private var invitees: [String] { formState.invitees }
    private var draft: RoomCreationDraft { formState.draft }

    private var participantStatus: AIParticipantStatus {
        if GroupInviteSelection.containsAI(invitees: invitees, candidates: candidates,
                                            policy: service.aiParticipantPolicy) { return .containsAI }
        return service.aiParticipantPolicy.classify(memberIDs: invitees + [service.userID].compactMap { $0 })
    }

    private var encryptionPresentation: EncryptionPresentation {
        EncryptionPresentation(encryption: .unencrypted, participants: participantStatus)
    }

    private var wantsEncryption: Binding<Bool> {
        Binding(
            get: { encryptionSelection.isRequested },
            set: { encryptionSelection.setRequested($0, presentation: encryptionPresentation) }
        )
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
                    if formState.showsGroupFields {
                        TextField("群聊名称（可选）", text: $roomName)
                    }
                }

                if formState.showsGroupFields {
                    Section("选择联系人和我的 AI") {
                        if isLoadingCandidates { ProgressView("正在加载联系人…") }
                        ForEach(candidates) { candidate in
                            Toggle(isOn: Binding(
                                get: { selectedIDs.contains(candidate.id) },
                                set: { selected in
                                    if selected { selectedIDs.insert(candidate.id) }
                                    else { selectedIDs.remove(candidate.id) }
                                }
                            )) {
                                HStack {
                                    Text(candidate.displayName)
                                    if candidate.isAI { Text("AI").font(.caption).foregroundStyle(.secondary) }
                                }
                            }
                        }
                        if candidates.isEmpty && !isLoadingCandidates && candidateError == nil {
                            Text("暂无可选联系人，可在下方手填 Matrix ID。")
                                .foregroundStyle(.secondary)
                        }
                        if let candidateError {
                            Text(candidateError).font(.footnote).foregroundStyle(.red)
                            Button("重试加载联系人和 AI") { Task { await loadCandidates() } }
                                .disabled(isLoadingCandidates)
                        }
                        if participantStatus == .containsAI {
                            Text("AI 只加入未加密群；AI 需要主人开启『允许被拉进群』才会加入")
                                .font(.footnote).foregroundStyle(.secondary)
                        }
                    }
                }

                Section(formState.inviteesTitle) {
                    TextField(formState.inviteesPlaceholder, text: $inviteesText, axis: .vertical)
#if os(iOS)
                        .textInputAutocapitalization(.never)
#endif
                        .autocorrectionDisabled()
                    Text(formState.inviteesHelp)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }

                Section("加密") {
                    Toggle("启用端对端加密", isOn: wantsEncryption)
                        .disabled(!encryptionPresentation.canEnable || isCreating)
                    Label(encryptionPresentation.detail, systemImage: encryptionPresentation.systemImage)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                    if let configurationNotice = service.aiParticipantPolicy.configurationNotice {
                        Text(configurationNotice)
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    } else {
                        Text("按部署的 AI 账号规则检查；规则需覆盖所有 AI，未命中不等于已验证真人。")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                    Text("本客户端在开启后不提供关闭入口。botville 的 AI 接入目前不支持加密会话；当前尚未配置只向可信设备分享密钥。开启前请确认设备验证和密钥备份。")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }

                if let errorMessage {
                    Text(errorMessage).foregroundStyle(.red)
                }
            }
            .disabled(isCreating)
            .navigationTitle(formState.title)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消") { dismiss() }.disabled(isCreating)
                }
                ToolbarItem(placement: .confirmationAction) {
                    NewConversationCreateButton(state: formState, action: prepareCreation)
                }
            }
            .task(id: isDirect) { if formState.showsGroupFields { await loadCandidates() } }
            .alert("确认开启端对端加密？", isPresented: $isShowingEncryptionConfirmation) {
                Button("开启并创建") { createAfterConfirmation() }
                Button("返回", role: .cancel) { consent.cancel() }
            } message: {
                Text("本客户端在开启后不提供关闭入口。botville 的 AI 接入目前不支持加密会话；当前尚未配置只向可信设备分享密钥。未验证设备或未设置密钥备份时，换设备可能无法查看历史消息。")
            }
            .onChange(of: isDirect) { _, _ in consent.contextChanged(to: .newRoom(draft)) }
            .onChange(of: roomName) { _, _ in consent.contextChanged(to: .newRoom(draft)) }
            .onChange(of: inviteesText) { _, _ in consent.contextChanged(to: .newRoom(draft)) }
            .onChange(of: selectedIDs) { _, _ in consent.contextChanged(to: .newRoom(draft)) }
            .onChange(of: encryptionPresentation.canEnable) { _, canEnable in
                if !canEnable {
                    encryptionSelection.reconcile(presentation: encryptionPresentation)
                    consent.cancel()
                }
            }
        }
    }

    private func loadCandidates() async {
        guard !isLoadingCandidates else { return }
        isLoadingCandidates = true
        candidateError = nil
        defer { isLoadingCandidates = false }
        let localUserID = service.userID
        var contacts: [GroupInviteCandidate] = []
        var personas: [MemoryPersona] = []
        do { contacts = try await service.groupInviteContacts() }
        catch is CancellationError { return }
        catch { candidateError = "暂时无法加载联系人，请重试或手填 Matrix ID。" }
        do {
            if let client = try service.confirmedGatewayClient() { personas = try await client.personas() }
        } catch is CancellationError { return }
        catch {
            candidateError = (error as? GatewayClientError)?.errorDescription ?? "暂时无法加载 AI，请稍后重试。"
        }
        guard !Task.isCancelled, service.userID == localUserID, service.isLoggedIn else { return }
        candidates = GroupInviteSelection.candidates(contacts: contacts, personas: personas, localUserID: localUserID)
        selectedIDs.formIntersection(Set(candidates.map(\.id)))
    }

    private func prepareCreation() {
        errorMessage = nil
        do {
            try draft.validate()
            guard encryptionSelection.isRequested else { create(draft: draft, consent: EncryptionConsent()); return }
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
        if consent.allowsEncryption(for: .newRoom(draft)) {
            self.consent.cancel()
        }
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
            RoomRow(room: room, isMuted: service.roomNotifications.isMuted(room.id))
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
