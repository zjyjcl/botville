// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import BotvilleCore
import Combine
import SwiftUI

#if canImport(WebKit)
    import WebKit

    /// Sheet-ready session host. `GatewayClient` can conform to
    /// `ModuleWebSessionProviding`; the view deliberately receives no code or
    /// cookie value. It makes one session request initially and again only after a
    /// trusted page requests `botville.session.renew`.
    public struct ModuleWebSheet: View {
        public let capabilityID: String
        public let url: URL
        public let allowedOrigins: [String]
        public let scopes: [String]
        public let ttlSeconds: Int
        public let provider: any ModuleWebSessionProviding

        @Environment(\.dismiss) private var dismiss
        @Environment(\.openURL) private var openURL
        @State private var session: ModuleWebSession?
        @State private var errorText: String?
        @State private var externalURL: URL?
        @State private var mockCookie: ModuleWebCookie?
        @State private var mockHTML: String?
        @State private var now = Date()
        @State private var renewing = false

        public init(
            capabilityID: String,
            url: URL,
            allowedOrigins: [String],
            scopes: [String] = [],
            ttlSeconds: Int = 300,
            provider: any ModuleWebSessionProviding
        ) {
            self.capabilityID = capabilityID
            self.url = url
            self.allowedOrigins = allowedOrigins
            self.scopes = scopes
            self.ttlSeconds = ttlSeconds
            self.provider = provider
        }

        public var body: some View {
            NavigationStack {
                Group {
                    if let session, session.isValid(at: now) {
                        ModuleWebView(
                            session: session,
                            mockCookie: mockCookie,
                            mockHTML: mockHTML,
                            onExternalURL: { externalURL = $0 },
                            onRenew: requestSession
                        )
                        .id(session.expiresAt)
                    } else if session != nil {
                        VStack {
                            ContentUnavailableView(
                                "网页会话已过期",
                                systemImage: "clock.badge.exclamationmark",
                                description: Text("请续期后继续使用模块网页。")
                            )
                            Button("续期") { requestSession() }.buttonStyle(.borderedProminent)
                        }
                    } else if let errorText {
                        ContentUnavailableView(
                            "无法打开模块网页",
                            systemImage: "safari",
                            description: Text(errorText)
                        )
                    } else {
                        ProgressView("正在创建短时网页会话…")
                    }
                }
                .navigationTitle("模块网页")
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) {
                        Button("关闭") { dismiss() }
                    }
                }
            }
            .task {
                if session == nil { requestSession() }
            }
            .onReceive(Timer.publish(every: 1, on: .main, in: .common).autoconnect()) { now = $0 }
            .alert(
                "将在外部浏览器打开",
                isPresented: Binding(get: { externalURL != nil }, set: { if !$0 { externalURL = nil } })
            ) {
                Button("取消", role: .cancel) { externalURL = nil }
                Button("打开") {
                    if let externalURL { openURL(externalURL) }
                    externalURL = nil
                }
            } message: {
                Text("该链接不在模块允许的站点范围内。")
            }
        }

        private func requestSession() {
            guard !renewing else { return }
            renewing = true
            Task {
                defer { renewing = false }
                do {
                    // Omitting an empty allow-list lets the gateway apply its
                    // contract default (the requested URL's origin); do not send an
                    // empty JSON whitelist.
                    let grant = try await provider.createModuleWebSession(
                        capabilityID: capabilityID,
                        url: url,
                        allowedOrigins: allowedOrigins.isEmpty ? nil : allowedOrigins,
                        scopes: scopes.isEmpty ? nil : scopes,
                        ttlSeconds: ttlSeconds
                    )
                    let fresh = ModuleWebSession(grant: grant)
                    guard fresh.isValid() else {
                        throw ModuleWebScreenError.expiredSession
                    }
                    if let mockProvider = provider as? any MockModuleWebSessionProviding {
                        if let client = provider as? any GatewayClient,
                            let code = fresh.oneTimeCode
                        {
                            _ = try await client.exchangeSessionCookie(
                                capabilityID: fresh.capabilityID,
                                code: code
                            )
                        }
                        let exchange = MockModuleWebCookieExchange()
                        try await exchange.exchange(launchURL: fresh.launchURL, for: fresh)
                        mockCookie = await exchange.storedCookies().first
                        mockHTML = mockProvider.mockModuleWebHTML(for: fresh)
                    } else {
                        mockCookie = nil
                        mockHTML = nil
                    }
                    now = Date()
                    session = fresh
                    errorText = nil
                } catch {
                    session = nil
                    errorText = "短时网页会话未创建。请返回后重试。"
                }
            }
        }
    }

    private enum ModuleWebScreenError: Error {
        case expiredSession
    }

    /// Compatibility spelling for callers created before the sheet host was named.
    public typealias ModuleWebSessionScreen = ModuleWebSheet

    /// Platform WebKit wrapper. Navigation outside `allowed_origins` is cancelled;
    /// the host view can present the resulting URL in the system browser.
    #if os(iOS)
        import UIKit
        public struct ModuleWebView: UIViewRepresentable {
            public let session: ModuleWebSession
            public let mockCookie: ModuleWebCookie?
            public let mockHTML: String?
            public let onExternalURL: (URL) -> Void
            public let onRenew: () -> Void

            public func makeCoordinator() -> Coordinator {
                Coordinator(
                    session: session, mockCookie: mockCookie, mockHTML: mockHTML,
                    onExternalURL: onExternalURL, onRenew: onRenew)
            }

            public func makeUIView(context: Context) -> WKWebView { context.coordinator.webView() }

            public func updateUIView(_ view: WKWebView, context: Context) {
                context.coordinator.loadIfNeeded(in: view)
            }

            public static func dismantleUIView(_ view: WKWebView, coordinator: Coordinator) {
                coordinator.tearDown(view)
            }
        }
    #elseif os(macOS)
        import AppKit
        public struct ModuleWebView: NSViewRepresentable {
            public let session: ModuleWebSession
            public let mockCookie: ModuleWebCookie?
            public let mockHTML: String?
            public let onExternalURL: (URL) -> Void
            public let onRenew: () -> Void

            public func makeCoordinator() -> Coordinator {
                Coordinator(
                    session: session, mockCookie: mockCookie, mockHTML: mockHTML,
                    onExternalURL: onExternalURL, onRenew: onRenew)
            }

            public func makeNSView(context: Context) -> WKWebView { context.coordinator.webView() }

            public func updateNSView(_ view: WKWebView, context: Context) {
                context.coordinator.loadIfNeeded(in: view)
            }

            public static func dismantleNSView(_ view: WKWebView, coordinator: Coordinator) {
                coordinator.tearDown(view)
            }
        }
    #endif

    public final class Coordinator: NSObject, WKNavigationDelegate, WKScriptMessageHandler {
        private let session: ModuleWebSession
        private let mockCookie: ModuleWebCookie?
        private let mockHTML: String?
        private let onExternalURL: (URL) -> Void
        private let onRenew: () -> Void
        private var hasLoaded = false

        init(
            session: ModuleWebSession,
            mockCookie: ModuleWebCookie?,
            mockHTML: String?,
            onExternalURL: @escaping (URL) -> Void,
            onRenew: @escaping () -> Void
        ) {
            self.session = session
            self.mockCookie = mockCookie
            self.mockHTML = mockHTML
            self.onExternalURL = onExternalURL
            self.onRenew = onRenew
        }

        func webView() -> WKWebView {
            let configuration = WKWebViewConfiguration()
            configuration.websiteDataStore = .nonPersistent()
            configuration.userContentController.add(self, name: "botville.session.renew")
            let webView = WKWebView(frame: .zero, configuration: configuration)
            webView.navigationDelegate = self
            loadIfNeeded(in: webView)
            return webView
        }

        func tearDown(_ webView: WKWebView) {
            webView.stopLoading()
            webView.navigationDelegate = nil
            webView.configuration.userContentController.removeScriptMessageHandler(
                forName: "botville.session.renew"
            )
        }

        func loadIfNeeded(in webView: WKWebView) {
            guard !hasLoaded, session.isValid() else { return }
            hasLoaded = true
            if let mockCookie, let mockHTML {
                let store = WebKitModuleWebCookieStore(
                    store: webView.configuration.websiteDataStore.httpCookieStore)
                Task { @MainActor in
                    await store.store(mockCookie)
                    webView.loadHTMLString(mockHTML, baseURL: session.launchURL)
                }
            } else {
                webView.load(URLRequest(url: session.launchURL))
            }
        }

        public func webView(
            _ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
            decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
        ) {
            guard session.isValid(), let url = action.request.url else {
                decisionHandler(.cancel)
                return
            }
            switch session.navigationPolicy.decision(for: url) {
            case .allow: decisionHandler(.allow)
            case .openExternally:
                if action.navigationType == .linkActivated { onExternalURL(url) }
                decisionHandler(.cancel)
            case .deny:
                decisionHandler(.cancel)
            }
        }

        public func userContentController(
            _ userContentController: WKUserContentController, didReceive message: WKScriptMessage
        ) {
            let policy = ModuleWebBridgePolicy(navigationPolicy: session.navigationPolicy)
            guard message.name == "botville.session.renew", session.isValid(),
                policy.accepts(
                    eventBody: "botville.session.renew", frameURL: message.frameInfo.request.url,
                    isMainFrame: message.frameInfo.isMainFrame) == .renew
            else { return }
            onRenew()
        }
    }
#endif
