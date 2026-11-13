// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import BotvilleCore
import SwiftUI

private let memoryAccent = Color(red: 26 / 255, green: 173 / 255, blue: 142 / 255)

struct MyAIListView: View {
    @StateObject private var model: PersonaListModel

    init(client: any GatewayClient) {
        _model = StateObject(wrappedValue: PersonaListModel(client: client))
    }

    var body: some View {
        List {
            if let message = model.errorMessage {
                ContentUnavailableView(
                    "加载失败", systemImage: "exclamationmark.triangle", description: Text(message))
                Button("重试") { Task { await model.load() } }
            } else if model.showsLoading {
                HStack {
                    Spacer()
                    ProgressView("正在加载…")
                    Spacer()
                }
            } else if model.isEmpty {
                ContentUnavailableView(
                    "暂无 AI", systemImage: "person.2", description: Text("可管理记忆的 AI 将显示在这里。"))
            } else {
                Section("我的 AI") {
                    ForEach(model.personas) { persona in
                        NavigationLink {
                            PersonaMemoryView(
                                persona: persona, client: modelClient)
                        } label: {
                            Label(persona.displayName, systemImage: "person.crop.circle")
                        }
                    }
                }
            }
        }
        .navigationTitle("我的 AI")
        .task { await model.load() }
        .refreshable { await model.load() }
    }

    // StateObject keeps the injected client private; this avoids copying a
    // transport or reaching through an environment object from list rows.
    private var modelClient: any GatewayClient { model.client }
}

@MainActor
private final class MemoryListModel: ObservableObject {
    @Published var memories: [Memory] = []
    @Published var isLoading = false
    @Published var isLoadingMore = false
    @Published var errorMessage: String?
    @Published var hasMore = false
    private var cursor: String?
    private var seenCursors: Set<String> = []
    private var mutatingIDs: Set<String> = []
    private let client: any GatewayClient
    private let personaID: String

    init(personaID: String, client: any GatewayClient) {
        self.personaID = personaID
        self.client = client
    }

    func load(reset: Bool = false) async {
        if reset {
            guard !isLoading, !isLoadingMore, mutatingIDs.isEmpty else { return }
            isLoading = true
        } else {
            guard !isLoading, !isLoadingMore, mutatingIDs.isEmpty, hasMore else { return }
            guard let cursor, !seenCursors.contains(cursor) else {
                hasMore = false
                errorMessage = "分页信息异常，已停止继续加载。"
                return
            }
            isLoadingMore = true
        }
        let cursorBeforeRequest = reset ? nil : cursor
        errorMessage = nil
        defer {
            isLoading = false
            isLoadingMore = false
        }
        do {
            let page = try await client.memories(
                personaID: personaID, cursor: reset ? nil : cursor, limit: 30)
            if reset {
                memories = page.items
                seenCursors = []
            } else {
                let existing = Set(memories.map(\.id))
                memories.append(contentsOf: page.items.filter { !existing.contains($0.id) })
            }
            cursor = page.nextCursor
            hasMore = page.hasMore
            if !reset, let requestedCursor = cursorBeforeRequest {
                seenCursors.insert(requestedCursor)
            }
            if page.hasMore, page.nextCursor == nil {
                hasMore = false
                errorMessage = "分页信息异常，已停止继续加载。"
            }
        } catch { errorMessage = userFacingMemoryError(error, fallback: "暂时无法加载记忆，请稍后重试。") }
    }

    func remove(_ memory: Memory) async -> MemoryMutationResult {
        guard memory.actions.canDelete, !isLoading, !isLoadingMore, mutatingIDs.insert(memory.id).inserted
        else {
            return .failed("当前操作尚未完成，请稍后再试。")
        }
        defer { mutatingIDs.remove(memory.id) }
        do {
            try await client.deleteMemory(id: memory.id)
            memories.removeAll { $0.id == memory.id }
            return .deleted
        } catch {
            let message = userFacingMemoryError(error, fallback: "删除未完成，请稍后重试。")
            errorMessage = message
            return .failed(message)
        }
    }

    func togglePin(_ memory: Memory) async -> MemoryMutationResult {
        guard memory.actions.canTogglePin, !isLoading, !isLoadingMore, mutatingIDs.insert(memory.id).inserted
        else {
            return .failed("当前操作尚未完成，请稍后再试。")
        }
        defer { mutatingIDs.remove(memory.id) }
        do {
            let changed =
                try await
                (memory.pinned
                ? client.unpinMemory(id: memory.id) : client.pinMemory(id: memory.id))
            if let index = memories.firstIndex(where: { $0.id == changed.id }) {
                memories[index] = changed
            }
            return .changed(changed)
        } catch {
            let message = userFacingMemoryError(error, fallback: "更新固定状态未完成，请稍后重试。")
            errorMessage = message
            return .failed(message)
        }
    }

    var clientForDetail: any GatewayClient { client }
}

private enum MemoryMutationResult {
    case deleted
    case changed(Memory)
    case failed(String)
}

private struct PersonaMemoryView: View {
    let persona: MemoryPersona
    let client: any GatewayClient
    @StateObject private var model: MemoryListModel
    @State private var pendingDeletion: Memory?

    init(persona: MemoryPersona, client: any GatewayClient) {
        self.persona = persona
        self.client = client
        _model = StateObject(wrappedValue: MemoryListModel(personaID: persona.id, client: client))
    }

    var body: some View {
        List {
            Section {
                if let signature = persona.aiProfile(localServerName: nil).signature {
                    Text(signature).font(.subheadline).foregroundStyle(.secondary)
                }
                Label(MemoryPresentationPolicy.ownerExplanationText, systemImage: "info.circle")
                    .font(.footnote).foregroundStyle(.secondary)
            }
            if let message = model.errorMessage {
                Section {
                    Label(message, systemImage: "exclamationmark.triangle").foregroundStyle(
                        .secondary)
                }
            }
            Section("聊天记忆") {
                ForEach(model.memories) { memory in
                    NavigationLink {
                        MemoryDetailView(
                            memoryID: memory.id, initial: memory, client: model.clientForDetail,
                            onDeleted: { memory in await model.remove(memory) },
                            onPinChanged: { memory in await model.togglePin(memory) })
                    } label: {
                        MemoryRow(memory: memory)
                    }
                    .contextMenu {
                        if memory.actions.canTogglePin {
                            Button(memory.pinned ? "取消固定" : "固定记忆") {
                                Task { _ = await model.togglePin(memory) }
                            }
                        }
                        if memory.actions.canDelete {
                            Button("删除", role: .destructive) { pendingDeletion = memory }
                        }
                    }
                    .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                        if memory.actions.canTogglePin {
                            Button {
                                Task { _ = await model.togglePin(memory) }
                            } label: {
                                Label(memory.pinned ? "取消固定" : "固定记忆",
                                      systemImage: memory.pinned ? "pin.slash" : "pin")
                            }
                            .tint(memoryAccent)
                        }
                        if memory.actions.canDelete {
                            Button(role: .destructive) {
                                pendingDeletion = memory
                            } label: {
                                Label("删除", systemImage: "trash")
                            }
                        }
                    }
                }
            }
            if model.isLoading && model.memories.isEmpty {
                HStack {
                    Spacer()
                    ProgressView("正在加载…")
                    Spacer()
                }
            }
            if model.hasMore {
                Button(model.isLoadingMore ? "正在加载…" : "加载更多") { Task { await model.load() } }
                    .disabled(
                        model.isLoadingMore)
            }
            if !model.isLoading && model.memories.isEmpty && model.errorMessage == nil {
                ContentUnavailableView(
                    "暂无记忆", systemImage: "brain.head.profile",
                    description: Text("该 AI 还没有可管理的聊天记忆。"))
            }
        }
        .navigationTitle(persona.displayName)
        .task { await model.load(reset: true) }
        .refreshable { await model.load(reset: true) }
        .confirmationDialog(
            pendingDeletion?.layer == "summary" ? "重置这份摘要？" : "删除这条记忆？",
            isPresented: Binding(
                get: { pendingDeletion != nil }, set: { if !$0 { pendingDeletion = nil } }),
            titleVisibility: .visible
        ) {
            if let pendingDeletion {
                Button("删除", role: .destructive) {
                    let deleting = pendingDeletion
                    self.pendingDeletion = nil
                    Task { _ = await model.remove(deleting) }
                }
            }
            Button("取消", role: .cancel) { pendingDeletion = nil }
        } message: {
            Text(pendingDeletion?.layer == "summary" ? "这会重置摘要，不会遗忘或删除原始聊天内容。" : "删除后无法恢复。")
        }
    }
}

private struct MemoryRow: View {
    let memory: Memory

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack {
                Text(memoryLayerTitle(memory.layer)).font(.caption).foregroundStyle(.secondary)
                if memory.pinned {
                    Label("已固定", systemImage: "pin.fill").font(.caption).foregroundStyle(
                        memoryAccent)
                }
                Spacer()
                Text(memory.updatedAt, style: .relative).font(.caption2).foregroundStyle(.tertiary)
            }
            Text(memory.redacted ? "内容不可见" : memoryDisplayText(memory))
                .lineLimit(2).foregroundStyle(memory.redacted ? .secondary : .primary)
        }
        .padding(.vertical, 2)
    }
}

private struct MemoryDetailView: View {
    let memoryID: String
    let client: any GatewayClient
    let onDeleted: (Memory) async -> MemoryMutationResult
    let onPinChanged: (Memory) async -> MemoryMutationResult
    @State private var memory: Memory
    @State private var isLoading = false
    @State private var hasLoadedDetail = false
    @State private var showDeleteConfirmation = false
    @State private var detailError: String?
    @Environment(\.dismiss) private var dismiss

    init(
        memoryID: String, initial: Memory, client: any GatewayClient,
        onDeleted: @escaping (Memory) async -> MemoryMutationResult,
        onPinChanged: @escaping (Memory) async -> MemoryMutationResult
    ) {
        self.memoryID = memoryID
        self.client = client
        self.onDeleted = onDeleted
        self.onPinChanged = onPinChanged
        _memory = State(initialValue: initial)
    }

    var body: some View {
        List {
            if hasLoadedDetail {
                Section("记忆内容") {
                    Text(memory.redacted ? "内容不可见" : memoryDisplayText(memory))
                        .foregroundStyle(memory.redacted ? .secondary : .primary)
                }
                Section("详情") {
                    LabeledContent("层级", value: memoryLayerTitle(memory.layer))
                    LabeledContent("范围", value: memoryScopeTitle(memory.scope.kind))
                    LabeledContent("更新时间") { Text(memory.updatedAt, style: .date) }
                }
                if memory.actions.canDelete {
                    Section {
                        if memory.actions.canTogglePin {
                            Button(memory.pinned ? "取消固定" : "固定记忆") { Task { await pin() } }
                        }
                        Button("删除记忆", role: .destructive) { showDeleteConfirmation = true }
                    }
                } else {
                    Section { Text("此记忆来自未知层级，仅显示元数据。").foregroundStyle(.secondary) }
                }
            } else if isLoading {
                HStack {
                    Spacer()
                    ProgressView("正在获取详情…")
                    Spacer()
                }
            } else {
                ContentUnavailableView(
                    "无法获取记忆详情", systemImage: "exclamationmark.triangle",
                    description: Text(detailError ?? "请稍后重试。"))
                Button("重试") { Task { await load() } }
            }
            if hasLoadedDetail, let detailError {
                Section {
                    Label(detailError, systemImage: "exclamationmark.triangle").foregroundStyle(
                        .secondary)
                }
            }
        }
        .navigationTitle("记忆详情")
        .task { await load() }
        .confirmationDialog(
            deleteTitle, isPresented: $showDeleteConfirmation, titleVisibility: .visible
        ) {
            Button("删除", role: .destructive) { Task { await delete() } }
            Button("取消", role: .cancel) {}
        } message: {
            Text(deleteMessage)
        }
    }

    private func load() async {
        guard !isLoading else { return }
        isLoading = true
        detailError = nil
        hasLoadedDetail = false
        defer { isLoading = false }
        do {
            memory = try await client.memory(id: memoryID)
            hasLoadedDetail = true
        } catch { detailError = userFacingMemoryError(error, fallback: "暂时无法获取最新记忆详情。") }
    }
    private func pin() async {
        detailError = nil
        switch await onPinChanged(memory) {
        case .changed(let changed): memory = changed
        case .failed(let message): detailError = message
        case .deleted: break
        }
    }
    private func delete() async {
        detailError = nil
        switch await onDeleted(memory) {
        case .deleted: dismiss()
        case .failed(let message): detailError = message
        case .changed: break
        }
    }
    private var deleteTitle: String { memory.layer == "summary" ? "重置这份摘要？" : "删除这条记忆？" }
    private var deleteMessage: String {
        memory.layer == "summary" ? "这会重置摘要，不会遗忘或删除原始聊天内容。" : "删除后无法恢复。"
    }
}

private func memoryDisplayText(_ memory: Memory) -> String {
    guard memory.canMutate else { return "较新版本的记忆，内容仅可在支持的版本查看。" }
    if let text = memory.text, !text.isEmpty { return text }
    if let key = memory.key, let value = memory.value { return "\(key)：\(value)" }
    return "内容不可见"
}

private func memoryLayerTitle(_ layer: String) -> String {
    ["recent": "近期记忆", "summary": "摘要", "profile": "资料"][layer] ?? "未知层级"
}

private func memoryScopeTitle(_ scope: String) -> String {
    ["dm": "私聊", "group": "群聊"][scope] ?? "未知范围"
}

private func userFacingMemoryError(_ error: Error, fallback: String) -> String {
    if let gatewayError = error as? GatewayClientError, let message = gatewayError.errorDescription,
        !message.isEmpty
    {
        return message
    }
    return fallback
}
