// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import BotvilleCore
import Foundation
import SwiftUI

#if os(iOS)
import AVFoundation
import UIKit
#elseif os(macOS)
import AppKit
import CoreImage
import UniformTypeIdentifiers
import Vision
#endif

/// Keeps pairing material in view state only.  In particular, neither the QR
/// URI nor its one-time code is sent to UserDefaults, the pasteboard, or logs.
@MainActor
struct PairingFlowView: View {
    @ObservedObject var matrixService: MatrixService
    @Binding var isPresented: Bool
    @State private var scanned: PairingQRCode?
    @State private var pastedURI = ""
    @State private var errorMessage: String?
    @State private var isRedeeming = false
    @State private var username = ""
    @State private var password = ""
    @State private var response: PairingRedeemResponse?
    @State private var mockCompleted = false
    @State private var retryUntil: Date?
    @State private var retryForbidden = false
    @State private var generation = 0
    @State private var flowTask: Task<Void, Never>?

    init(matrixService: MatrixService, isPresented: Binding<Bool>, initialURI: String? = nil) {
        self.matrixService = matrixService
        self._isPresented = isPresented
        // Resolve a debug handoff before the first render so capture never
        // asks for camera access while opening a confirmation preview.
        if let initialURI {
            do { self._scanned = State(initialValue: try PairingQRCode.parse(initialURI)) }
            catch { self._errorMessage = State(initialValue: error.localizedDescription) }
        }
    }

    private var isDebugMock: Bool {
#if DEBUG
        let arguments = ProcessInfo.processInfo.arguments
        return arguments.contains("-pairing-mock") || arguments.contains("-pair-confirm")
#else
        false
#endif
    }

    var body: some View {
        NavigationStack {
            Group {
                if let scanned {
                    confirmation(scanned)
                } else {
                    capture
                }
            }
            .navigationTitle("扫码配对")
#if os(iOS)
            .navigationBarTitleDisplayMode(.inline)
#endif
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消") { dismissAndClear() }
                }
            }
        }
        .onDisappear { cancelFlow() }
    }

    @ViewBuilder private var capture: some View {
        VStack(spacing: 20) {
#if os(iOS)
            QRCodeCameraView { accept($0) } onError: { errorMessage = $0 }
                .frame(height: 280)
                .clipShape(RoundedRectangle(cornerRadius: 18))
            Text("将配对二维码放入取景框内")
                .foregroundStyle(.secondary)
#else
            Image(systemName: "qrcode.viewfinder")
                .font(.system(size: 56)).foregroundStyle(.tint)
            Text("从配对二维码图片中读取链接")
                .foregroundStyle(.secondary)
            Button("选择二维码图片") { chooseImage() }
                .buttonStyle(.borderedProminent)
#endif
            Divider().frame(maxWidth: 480)
            Text("也可粘贴配对链接").font(.headline)
            TextField("botville://pair?...", text: $pastedURI, axis: .vertical)
                .textFieldStyle(.roundedBorder)
                .autocorrectionDisabled()
#if os(iOS)
                .textInputAutocapitalization(.never)
#endif
                .frame(maxWidth: 480)
            Button("解析链接") { accept(pastedURI) }
                .buttonStyle(.bordered)
                .disabled(pastedURI.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            if let errorMessage { Text(errorMessage).foregroundStyle(.red).font(.footnote) }
        }
        .padding(28)
    }

    @ViewBuilder private func confirmation(_ qr: PairingQRCode) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                Label("确认要连接的服务器", systemImage: "checkmark.shield")
                    .font(.title3.bold())
                detail("Homeserver", qr.homeserver.absoluteString)
                detail("Gateway", qr.gateway?.absoluteString ?? "未提供")
                detail("证书指纹（SHA-256）", qr.fingerprint.map(groupedFingerprint) ?? "未提供")
                    .textSelection(.enabled)
                detail("到期时间", qr.expiresAt.formatted(.dateTime.year().month().day().hour().minute().locale(Locale(identifier: "zh_CN"))))
                Text("确认后才会使用一次性配对码。服务端仍会再次校验有效期。")
                    .font(.footnote).foregroundStyle(.secondary)

                if mockCompleted {
                    Label("模拟配对已完成", systemImage: "checkmark.circle.fill")
                        .foregroundStyle(.green)
                    Text("未连接真实 Homeserver，也未保存任何凭据。")
                        .font(.footnote).foregroundStyle(.secondary)
                } else if response?.auth.type == .registrationToken {
                    Text("创建账号").font(.headline)
                    TextField("用户名", text: $username).autocorrectionDisabled()
                    SecureField("密码", text: $password)
                    Text("用户名和密码由你选择，仅用于本次注册。")
                        .font(.footnote).foregroundStyle(.secondary)
                }
                if let response {
                    detail("兑换凭据到期", response.authExpiresAt.formatted(date: .omitted, time: .shortened))
                }
                if let errorMessage { Text(errorMessage).foregroundStyle(.red).font(.footnote) }
                if let retryUntil {
                    TimelineView(.periodic(from: .now, by: 1)) { context in
                        let seconds = max(0, Int(retryUntil.timeIntervalSince(context.date).rounded(.up)))
                        Text(seconds > 0 ? "请等待 \(seconds) 秒后重试" : "现在可以重试")
                            .font(.footnote).foregroundStyle(.secondary)
                    }
                }
                HStack {
                    Button("返回") { clearConfirmation() }.disabled(isRedeeming)
                    Spacer()
                    TimelineView(.periodic(from: .now, by: 1)) { context in
                        Button(response == nil ? "确认并配对" : "完成登录") { beginOrComplete(qr) }
                            .buttonStyle(.borderedProminent).disabled(isRedeeming || retryForbidden || (retryUntil.map { $0 > context.date } ?? false) || (response?.auth.type == .registrationToken && (username.isEmpty || password.isEmpty)))
                    }
                }
                if isRedeeming { ProgressView("正在安全配对…") }
            }
            .padding(28).frame(maxWidth: 560, alignment: .leading)
        }
    }

    private func detail(_ label: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label).font(.caption).foregroundStyle(.secondary)
            Text(value).font(.body.monospaced()).textSelection(.enabled)
        }
    }

    private func groupedFingerprint(_ data: Data) -> String {
        data.map { String(format: "%02X", $0) }.joined().chunked(every: 4).joined(separator: " ")
    }

    @discardableResult private func accept(_ value: String) -> Bool {
        do {
            scanned = try PairingQRCode.parse(value.trimmingCharacters(in: .whitespacesAndNewlines), now: Date())
            pastedURI = ""; errorMessage = nil; retryForbidden = false; retryUntil = nil
            return true
        } catch { errorMessage = error.localizedDescription; return false }
    }

    private func beginOrComplete(_ qr: PairingQRCode) {
        if let response, response.isAuthExpired() {
            errorMessage = PairingError.expired.localizedDescription
            retryForbidden = true
            return
        }
        isRedeeming = true; errorMessage = nil
        let taskGeneration = generation
        flowTask = Task {
            defer { isRedeeming = false }
            do {
                let redemption: PairingRedeemResponse
                if let response { redemption = response }
                else {
#if DEBUG
                    let pairingService: any PairingService = isDebugMock ? MockPairingService() : HTTPPairingService()
#else
                    let pairingService: any PairingService = HTTPPairingService()
#endif
                    redemption = try await pairingService.redeem(qr, deviceDisplayName: deviceDisplayName)
                    guard taskGeneration == generation, !Task.isCancelled else { return }
                    response = redemption
                    if redemption.auth.type == .registrationToken { response = redemption; return }
                }
                if isDebugMock {
#if DEBUG
                    mockCompleted = true
                    return
#endif
                }
                try await matrixService.completePairing(
                    response: redemption, qrCode: qr,
                    username: redemption.auth.type == .registrationToken ? username : nil,
                    password: redemption.auth.type == .registrationToken ? password : nil)
                guard taskGeneration == generation, !Task.isCancelled else { return }
                dismissAndClear()
            } catch is CancellationError {
                guard taskGeneration == generation else { return }
                errorMessage = "已取消配对。"
            } catch let error as PairingError {
                guard taskGeneration == generation else { return }
                errorMessage = error.localizedDescription
                if case let .rateLimited(retryAfter) = error {
                    retryUntil = Date().addingTimeInterval(retryAfter ?? 5)
                } else { retryForbidden = true }
            } catch let error as AuthenticationError {
                guard taskGeneration == generation else { return }
                errorMessage = error.localizedDescription
                if case let .rateLimited(delay) = error {
                    retryUntil = Date().addingTimeInterval(delay ?? 5)
                } else { retryForbidden = true }
            } catch {
                guard taskGeneration == generation else { return }
                errorMessage = error.localizedDescription
                retryForbidden = true
            }
        }
    }

    private var deviceDisplayName: String {
#if os(iOS)
        UIDevice.current.name
#else
        Host.current().localizedName ?? "Mac"
#endif
    }

    private func clearConfirmation() { mockCompleted = false; response = nil; scanned = nil; clearSensitiveState() }
    private func dismissAndClear() { clearSensitiveState(); isPresented = false }
    private func clearSensitiveState() { pastedURI = ""; username = ""; password = ""; response = nil; scanned = nil; mockCompleted = false; retryUntil = nil; retryForbidden = false }
    private func cancelFlow() { generation &+= 1; flowTask?.cancel(); flowTask = nil; matrixService.cancelAuthentication(); clearSensitiveState() }

#if os(macOS)
    private func chooseImage() {
        let panel = NSOpenPanel(); panel.allowedContentTypes = [.image]; panel.allowsMultipleSelection = false
        guard panel.runModal() == .OK, let url = panel.url else { return }
        guard let image = NSImage(contentsOf: url), let tiff = image.tiffRepresentation,
              let ciImage = CIImage(data: tiff) else { errorMessage = "无法读取该图片。"; return }
        let request = VNDetectBarcodesRequest()
        let handler = VNImageRequestHandler(ciImage: ciImage)
        do {
            try handler.perform([request])
            guard let payload = request.results?.first?.payloadStringValue else { errorMessage = "图片中没有可识别的二维码。"; return }
            accept(payload)
        } catch { errorMessage = "无法识别该二维码图片。" }
    }
#endif
}

private extension String {
    func chunked(every size: Int) -> [String] {
        stride(from: 0, to: count, by: size).map { start in
            let lower = index(startIndex, offsetBy: start)
            let upper = index(lower, offsetBy: min(size, count - start))
            return String(self[lower..<upper])
        }
    }
}

#if os(iOS)
private struct QRCodeCameraView: UIViewControllerRepresentable {
    let onCode: (String) -> Bool
    let onError: (String) -> Void
    func makeUIViewController(context: Context) -> QRCodeCameraController {
        let controller = QRCodeCameraController(); controller.onCode = onCode; controller.onError = onError; return controller
    }
    func updateUIViewController(_ uiViewController: QRCodeCameraController, context: Context) {}
}

private final class QRCodeCameraController: UIViewController, AVCaptureMetadataOutputObjectsDelegate {
    var onCode: ((String) -> Bool)?; var onError: ((String) -> Void)?
    private let session = AVCaptureSession()
    override func viewDidLoad() {
        super.viewDidLoad()
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized: configure()
        case .notDetermined: AVCaptureDevice.requestAccess(for: .video) { [weak self] granted in DispatchQueue.main.async { granted ? self?.configure() : self?.onError?("需要相机权限才能扫码；你也可以粘贴配对链接。") } }
        default: onError?("未授予相机权限；你也可以粘贴配对链接。")
        }
    }
    private func configure() {
        guard session.inputs.isEmpty, let device = AVCaptureDevice.default(for: .video), let input = try? AVCaptureDeviceInput(device: device), session.canAddInput(input) else { onError?("无法启动相机；你也可以粘贴配对链接。"); return }
        session.addInput(input); let output = AVCaptureMetadataOutput(); guard session.canAddOutput(output) else { return }; session.addOutput(output)
        output.setMetadataObjectsDelegate(self, queue: .main); output.metadataObjectTypes = [.qr]
        let preview = AVCaptureVideoPreviewLayer(session: session); preview.videoGravity = .resizeAspectFill; preview.frame = view.bounds; view.layer.addSublayer(preview)
        DispatchQueue.global(qos: .userInitiated).async { self.session.startRunning() }
    }
    override func viewDidLayoutSubviews() { super.viewDidLayoutSubviews(); view.layer.sublayers?.compactMap { $0 as? AVCaptureVideoPreviewLayer }.first?.frame = view.bounds }
    func metadataOutput(_ output: AVCaptureMetadataOutput, didOutput metadataObjects: [AVMetadataObject], from connection: AVCaptureConnection) { guard let value = (metadataObjects.first as? AVMetadataMachineReadableCodeObject)?.stringValue else { return }; if onCode?(value) == true { session.stopRunning() } }
    deinit { session.stopRunning() }
}
#endif
