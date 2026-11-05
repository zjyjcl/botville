// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import BotvilleCore
import SwiftUI

@MainActor
final class DynamicUIModel: ObservableObject {
    @Published var manifest: GatewayCapabilityManifest?
    @Published var cards: [GatewayCard] = []
    @Published var loading = false
    @Published var notice: String?
    @Published var selectedCapability: GatewayCapability?
    @Published var busyCardIDs: Set<String> = []
    private(set) var client: any GatewayClient = MockGatewayClient()
    private(set) var isMock = true
    private var generation = 0
    private let appVersion = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String
    private var locale: String {
        (Locale.preferredLanguages.first ?? Locale.current.identifier)
            .replacingOccurrences(of: "_", with: "-")
    }

    func configure(mock: Bool, baseURL: String, allowLocalHTTP: Bool) async {
        do {
            let replacement: any GatewayClient =
                mock
                ? MockGatewayClient()
                : try HTTPGatewayClient(baseURL: baseURL, allowLocalHTTP: allowLocalHTTP)
            generation += 1
            client = replacement
            isMock = mock
            manifest = nil
            cards = []
            selectedCapability = nil
            await reload()
        } catch {
            notice = "网关地址无效。请使用 HTTPS；开发 HTTP 仅允许 localhost 或 127.0.0.1。"
        }
    }

    /// The service creates this transport from the paired session's stored
    /// gateway URL and non-secret pin.  UI settings never reconstruct it,
    /// which avoids silently losing certificate pinning after pairing.
    func configureConfirmedGateway(_ replacement: HTTPGatewayClient) async {
        generation += 1
        client = replacement
        isMock = false
        manifest = nil
        cards = []
        selectedCapability = nil
        await reload()
    }

    func reload() async {
        let currentGeneration = generation
        loading = true
        notice = nil
        defer { if currentGeneration == generation { loading = false } }
        do {
            let nextManifest = try await client.capabilities(
                locale: locale, appVersion: appVersion)
            guard currentGeneration == generation else { return }
            manifest = nextManifest
            let nextCards = try await client.cards(
                capabilityID: selectedCapability?.id, cardTypes: selectedCapability?.entry.cardTypes)
            guard currentGeneration == generation else { return }
            cards = nextCards
        } catch {
            guard currentGeneration == generation else { return }
            notice = manifest == nil ? "暂时无法获取网关功能。会话入口仍可使用；可切回示例数据查看动态界面。" : "刷新失败，保留本次会话中上次成功加载的内容。"
        }
    }

    func select(_ capability: GatewayCapability?) async {
        selectedCapability = capability
        generation += 1
        cards = []
        await reload()
    }

    func perform(card: GatewayCard, action: GatewayCardAction) async {
        guard !busyCardIDs.contains(card.id), card.isActionable else { return }
        busyCardIDs.insert(card.id)
        let actionGeneration = generation
        let actionClient = client
        defer { busyCardIDs.remove(card.id) }
        do {
            let receipt = try await actionClient.perform(card: card, action: action)
            guard actionGeneration == generation else { return }
            if let index = cards.firstIndex(where: { $0.id == receipt.cardID }) {
                cards[index] = receipt.card
            }
            notice = receipt.status == "accepted" ? "操作已完成（\(isMock ? "模拟回执" : "网关回执")）。" : "网关未接受此操作。"
        } catch {
            guard actionGeneration == generation else { return }
            notice = "操作未完成，请刷新卡片后重试。"
        }
    }
}

@MainActor
struct DynamicUIView: View {
    let onOpenChat: () -> Void
    @ObservedObject var matrixService: MatrixService
    @StateObject private var model = DynamicUIModel()
    @State private var showSettings = false
    @State private var useMock = true
    @State private var baseURL = "https://gateway.example.invalid"
    @State private var allowLocalHTTP = false
    @State private var usesPairedGateway = false
    @State private var webDestination: WebDestination?
    @State private var pendingAction: ActionSelection?
    @State private var externalURL: URL?
    @State private var highRiskMessage = false
    @Environment(\.openURL) private var openURL

    private var visibleCapabilities: [GatewayCapability] {
        guard let manifest = model.manifest else { return [] }
        return manifest.capabilityProjections(knownNativeRoutes: ["chat.direct": "botville://chat"])
            .filter(\.isVisible).map(\.capability)
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    HStack(alignment: .top) {
                        VStack(alignment: .leading, spacing: 6) {
                            Text("你的小镇，随时相连").font(.title2.bold())
                            Text(model.isMock ? "示例数据 · 模块与卡片预览" : "来自已配置的网关")
                                .font(.subheadline).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Image(systemName: "square.stack.3d.up.fill")
                            .font(.largeTitle).foregroundStyle(.tint)
                    }
                    .padding(20).frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.accentColor.opacity(0.08), in: RoundedRectangle(cornerRadius: 20))

                    if let notice = model.notice {
                        Label(notice, systemImage: "info.circle").font(.callout).foregroundStyle(.secondary)
                    }
                    if model.loading { ProgressView("正在更新…") }

                    VStack(alignment: .leading, spacing: 12) {
                        Text("模块").font(.headline)
                        LazyVGrid(columns: [GridItem(.adaptive(minimum: 155), spacing: 12)], spacing: 12) {
                            ForEach(visibleCapabilities, id: \.id) { capability in
                                Button {
                                    open(capability)
                                } label: {
                                    VStack(alignment: .leading, spacing: 8) {
                                        Label(capability.title, systemImage: entryIcon(capability))
                                            .font(.headline)
                                        Text(moduleTitle(capability.module)).font(.caption2).foregroundStyle(
                                            .secondary)
                                        Text(capability.summary).font(.caption).foregroundStyle(.secondary)
                                            .lineLimit(3)
                                        if capability.status == "degraded" {
                                            Text(
                                                capability.unavailableReason == "push_channel_degraded"
                                                    ? "推送通道暂不可用"
                                                    : (capability.unavailableReason ?? "部分服务暂不可用")
                                            )
                                            .font(.caption2).foregroundStyle(.orange)
                                        }
                                    }
                                    .frame(maxWidth: .infinity, minHeight: 84, alignment: .topLeading)
                                    .padding(14)
                                    .background(
                                        .quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 14))
                                }
                                .buttonStyle(.plain)
                                .disabled(
                                    capability.entry.type == "native"
                                        && !(capability.id == "chat.direct"
                                            && capability.entry.deepLink == "botville://chat")
                                )
                            }
                        }
                        if visibleCapabilities.isEmpty && !model.loading {
                            Button("打开会话", action: onOpenChat).buttonStyle(.bordered)
                        }
                    }

                    HStack {
                        Text(model.selectedCapability?.title ?? "最新卡片").font(.headline)
                        Spacer()
                        if model.selectedCapability != nil {
                            Button("全部") { Task { await model.select(nil) } }.font(.subheadline)
                        }
                    }
                    ForEach(model.cards, id: \.id) { card in
                        DynamicCardView(card: card, isBusy: model.busyCardIDs.contains(card.id)) { action in
                            choose(card: card, action: action)
                        }
                    }
                    if model.cards.isEmpty && !model.loading {
                        ContentUnavailableView(
                            "暂无卡片", systemImage: "rectangle.stack", description: Text("模块发布的通知和结果将在这里显示。"))
                    }
                }
                .padding().frame(maxWidth: 800).frame(maxWidth: .infinity)
            }
            .navigationTitle("动态")
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button {
                        Task { await model.reload() }
                    } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                    .disabled(model.loading).accessibilityLabel("刷新动态")
                }
                ToolbarItem(placement: .automatic) {
                    Button {
                        showSettings = true
                    } label: {
                        Image(systemName: "slider.horizontal.3")
                    }
                    .accessibilityLabel("网关设置")
                }
            }
            .task {
                await activateConfirmedGatewayIfAvailable()
                if model.manifest == nil { await model.reload() }
#if DEBUG
                // Deterministic offline browser smoke entry for Simulator.
                if ProcessInfo.processInfo.arguments.contains("-dynamic-ui-web-mock"), model.isMock,
                   let capability = visibleCapabilities.first(where: { $0.entry.type == "web" }) {
                    open(capability)
                }
#endif
            }
            .refreshable { await model.reload() }
            .onChange(of: matrixService.isLoggedIn) { _, loggedIn in
                Task {
                    if loggedIn { await activateConfirmedGatewayIfAvailable() }
                    else { usesPairedGateway = false; useMock = true; await model.configure(mock: true, baseURL: baseURL, allowLocalHTTP: false) }
                }
            }
            .sheet(isPresented: $showSettings) { settings }
            .sheet(item: $webDestination) { destination in
                ModuleWebSheet(
                    capabilityID: destination.capability.id, url: destination.url,
                    allowedOrigins: [], scopes: destination.capability.scopes,
                    ttlSeconds: min(3600, max(60, destination.capability.sessionToken?.ttlSeconds ?? 300)),
                    provider: model.client)
            }
            .alert("需在后续版本授权", isPresented: $highRiskMessage) {
                Button("知道了", role: .cancel) {}
            } message: {
                Text("此操作需要二次确认和设备签名。当前版本不会执行该操作。")
            }
            .confirmationDialog(
                pendingAction?.action.confirmation?.title ?? "确认操作",
                isPresented: Binding(get: { pendingAction != nil }, set: { if !$0 { pendingAction = nil } }),
                titleVisibility: .visible
            ) {
                if let pendingAction {
                    Button(pendingAction.action.label) {
                        let selection = pendingAction
                        self.pendingAction = nil
                        execute(selection)
                    }
                }
                Button("取消", role: .cancel) { pendingAction = nil }
            } message: {
                Text(pendingAction?.action.confirmation?.body ?? "确认后将向网关提交此操作。")
            }
            .confirmationDialog(
                "在外部浏览器打开？",
                isPresented: Binding(get: { externalURL != nil }, set: { if !$0 { externalURL = nil } }),
                titleVisibility: .visible
            ) {
                if let externalURL {
                    Button("打开浏览器") {
                        openURL(externalURL)
                        self.externalURL = nil
                    }
                }
                Button("取消", role: .cancel) { externalURL = nil }
            }
        }
    }

    private var settings: some View {
        NavigationStack {
            Form {
                Section("数据来源") {
                    Toggle("使用示例数据", isOn: $useMock)
                    if !useMock {
                        if usesPairedGateway {
                            Text(baseURL).font(.footnote.monospaced())
                            Text("此网关来自已确认的配对，并保留二维码证书固定。要更换网关，请重新配对。")
                                .font(.footnote).foregroundStyle(.secondary)
                        } else {
                            TextField("网关 Base URL", text: $baseURL)
                                .autocorrectionDisabled()
                        #if DEBUG
                            Toggle("允许本地 HTTP（仅开发）", isOn: $allowLocalHTTP)
                        #endif
                        }
                        Text("真实网关尚未提供动态界面接口。当前入口用于连接准备就绪的网关；此处不保存访问令牌。")
                            .font(.footnote).foregroundStyle(.secondary)
                    }
                }
            }
            .navigationTitle("网关设置")
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("应用") {
                        showSettings = false
                        Task {
                            if !useMock, let paired = try? matrixService.confirmedGatewayClient() {
                                await model.configureConfirmedGateway(paired)
                            } else {
                                await model.configure(mock: useMock, baseURL: baseURL, allowLocalHTTP: allowLocalHTTP)
                            }
                        }
                    }
                }
                ToolbarItem(placement: .cancellationAction) { Button("取消") { showSettings = false } }
            }
        }
        .frame(minWidth: 320, minHeight: 300)
    }

    private func activateConfirmedGatewayIfAvailable() async {
        guard model.isMock, let paired = try? matrixService.confirmedGatewayClient() else { return }
        baseURL = paired.baseURL.absoluteString
        useMock = false
        usesPairedGateway = true
        await model.configureConfirmedGateway(paired)
    }

    private func moduleTitle(_ module: String) -> String {
        [
            "core": "核心", "role": "角色", "memory": "记忆", "social": "社交", "notify": "通知", "bridge": "桥接",
            "town": "小镇",
        ][module] ?? "其他"
    }

    private func entryIcon(_ capability: GatewayCapability) -> String {
        switch capability.entry.type {
        case "web": return "globe"
        case "card_list": return "rectangle.stack"
        default: return "bubble.left.and.bubble.right"
        }
    }

    private func open(_ capability: GatewayCapability) {
        switch capability.entry.type {
        case "native":
            if capability.id == "chat.direct", capability.entry.deepLink == "botville://chat" { onOpenChat() }
        case "card_list": Task { await model.select(capability) }
        case "web":
            if let url = capability.entry.webURL {
                webDestination = WebDestination(capability: capability, url: url)
            }
        default: break
        }
    }

    private func choose(card: GatewayCard, action: GatewayCardAction) {
        switch GatewayActionSelectionPolicy.decision(for: card, action: action) {
        case .ignore:
            return
        case .requireUnavailableAuthorization:
            // C3.1 owns biometric confirmation, challenge and Secure Enclave signing.
            Task {
                do {
                    _ = try await UnavailableHighRiskAuthorizer().authorize(
                        cardID: card.id, actionID: action.id, revision: card.revision ?? 1)
                } catch { highRiskMessage = true }
            }
        case .requireConfirmation:
            pendingAction = ActionSelection(card: card, action: action)
        case .perform:
            execute(ActionSelection(card: card, action: action))
        }
    }

    private func execute(_ selection: ActionSelection) {
        let action = selection.action
        if action.kind == "open_url" {
            guard let url = action.url, url.scheme?.lowercased() == "https", url.user == nil,
                url.password == nil
            else { return }
            if action.target == "in_app" {
                guard let capability = visibleCapabilities.first(where: { $0.id == action.capabilityID }),
                    let entryURL = capability.entry.webURL,
                    ModuleWebOrigin(url: entryURL) == ModuleWebOrigin(url: url)
                else {
                    model.notice = "无法为此链接建立模块会话。"
                    return
                }
                webDestination = WebDestination(capability: capability, url: url)
            } else {
                externalURL = url
            }
        } else {
            Task { await model.perform(card: selection.card, action: action) }
        }
    }
}

private struct WebDestination: Identifiable {
    let id = UUID()
    let capability: GatewayCapability
    let url: URL
}
private struct ActionSelection {
    let card: GatewayCard
    let action: GatewayCardAction
}

private struct DynamicCardView: View {
    let card: GatewayCard
    let isBusy: Bool
    let onAction: (GatewayCardAction) -> Void
    private let knownTypes = ["notification", "approval", "moment", "system", "reminder", "result"]
    private var fallbackOnly: Bool {
        !GatewayCardSchema.supports(card.cardSchemaVersion) || !knownTypes.contains(card.type)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            if !fallbackOnly {
                Text(card.title).font(.headline)
                if let subtitle = card.subtitle { Text(subtitle).font(.caption).foregroundStyle(.secondary) }
                ForEach(Array(card.renderableBlocks.prefix(50).enumerated()), id: \.offset) { _, block in
                    DynamicBlockView(block: block)
                }
            }
            if fallbackOnly || card.hasUnknownBlocks || card.renderableBlocks.isEmpty {
                Text(card.fallbackText).font(.body)
            }
            if !fallbackOnly {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 110))], alignment: .leading) {
                    ForEach(Array(card.actions.filter(\.isKnownKind).prefix(10)), id: \.id) { action in
                        Button(action.label) { onAction(action) }
                            .buttonStyle(.bordered)
                            .tint(
                                action.style == "danger"
                                    ? .red : action.style == "primary" ? .accentColor : .secondary
                            )
                            .disabled(isBusy || !card.isActionable || action.disabledReason != nil)
                    }
                }
                if isBusy { ProgressView("正在提交…") }
                if !card.isActionable { Text("此卡片当前不可操作").font(.caption).foregroundStyle(.secondary) }
            }
        }
        .padding(18).frame(maxWidth: .infinity, alignment: .leading)
        .background(.background, in: RoundedRectangle(cornerRadius: 16))
        .overlay(RoundedRectangle(cornerRadius: 16).stroke(.quaternary))
    }
}

private struct DynamicBlockView: View {
    let block: GatewayCardBlock

    @ViewBuilder var body: some View {
        switch block.type {
        case "text": Text(String((block.text ?? "").prefix(4000))).foregroundStyle(semanticColor)
        case "code":
            Text(String((block.text ?? "").prefix(4000)))
                .font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                .padding(10).frame(maxWidth: .infinity, alignment: .leading)
                .background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 8))
        case "divider": Divider()
        case "key_value", "list":
            ForEach(Array((block.items ?? []).prefix(50).enumerated()), id: \.offset) { _, item in
                HStack(alignment: .top) {
                    if block.type == "list" { Text("•") }
                    Text(item.label).foregroundStyle(.secondary)
                    Spacer()
                    Text(item.value ?? "").fontWeight(item.emphasis == true ? .semibold : .regular)
                }.font(.subheadline)
            }
        case "progress":
            if let maximum = block.max, maximum > 0, let value = block.value, value.isFinite, maximum.isFinite
            {
                ProgressView(value: min(max(0, value), maximum), total: maximum) { Text(block.label ?? "进度") }
            } else {
                Text(block.label ?? "进度暂不可用").foregroundStyle(.secondary)
            }
        case "image":
            if let url = block.url, url.scheme?.lowercased() == "https" {
                AsyncImage(url: url) { phase in
                    if let image = phase.image {
                        image.resizable().scaledToFit().accessibilityLabel(block.alt ?? "图片")
                    } else {
                        Text(block.alt ?? "图片暂不可用").foregroundStyle(.secondary)
                    }
                }.frame(maxHeight: 240)
            } else {
                Text(block.alt ?? "图片暂不可用").foregroundStyle(.secondary)
            }
        default: EmptyView()
        }
    }

    private var semanticColor: Color {
        switch block.level {
        case "info": return .blue
        case "success": return .green
        case "warning": return .orange
        case "danger": return .red
        default: return .primary
        }
    }
}
