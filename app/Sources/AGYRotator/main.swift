import SwiftUI
import WebKit
import AppKit
import Darwin

struct Config: Codable, Equatable, Sendable {
    var enabled: Bool
    var poll_seconds: Int
    var time_enabled: Bool
    var hourly_minutes: [Int]
    var usage_enabled: Bool
    var remaining_below: Int
    var family: String
    var cooldown_seconds: Int
    var mode: String
    var windows: [String]
    var participants: [String]
    var countdown_seconds: Int
    var manual_confirm: Bool
    var close_app: Bool
    var notify: Bool
}
struct Bucket: Decodable, Sendable {
    let remaining_percent: Double?
    let reset_at: String?
}
struct Group: Decodable, Sendable {
    let family: String
    let windows: [String: Bucket]
}
struct Profile: Decodable, Sendable {
    let label: String
    let email: String?
    let status: String
    let updated_at: String?
    let error: String?
    let groups: [Group]
}
struct Pending: Decodable, Sendable {
    let reason: String
    let state: String
    let deadline: Double?
    let target: String?
}
struct Event: Decodable, Sendable {
    let time: String; let kind: String; let selected: String?; let error: String?
    let source: String?; let project: String?; let summary: String?; let enabled: Bool?
}
struct CodexWatch: Decodable, Sendable {
    let thread: String; let project: String; let status: String
    let started_at: String?; let finished_at: String?; let summary: String?
}
struct Snapshot: Decodable, Sendable {
    let active: String?
    let profiles: [String: Profile]
    let config: Config
    let revision: Int
    let pending: Pending?
    let refreshing: Bool
    let switching: Bool
    let events: [Event]
    let config_error: String?
    let next_scheduled_at: String?
    let cancelled_until: Double
    let last_refresh_finished: String?
    let codex: CodexState?
    let codex_watches: [CodexWatch]?
    let codex_events: [Event]?
    let gemini_events: [Event]?
    let activity: [String: Activity]?
    let warmup: [String: Bool]?
}
struct Activity: Decodable, Sendable { let working: Bool? }

struct CodexWindow: Decodable, Sendable {
    let remaining_percent: Double?
    let reset_at: Double?
}
struct CodexRow: Decodable, Sendable {
    let status: String
    let plan: String?
    let email: String?
    let five: CodexWindow?
    let weekly: CodexWindow?
    enum CodingKeys: String, CodingKey { case status, plan, email, weekly, five = "5h" }
}
struct CodexState: Decodable, Sendable {
    let profiles: [String: CodexRow]
    let active: String?
    let updated_at: String?
    let error: String?
    let switching: Bool
    let auto: Bool
    let autocontinue: Bool?
}
struct RPCError: Decodable, Error { let message: String }
struct Envelope<T: Decodable>: Decodable { let result: T?; let error: RPCError? }
struct Accepted: Decodable, Sendable { let accepted: Bool }
enum ClientError: Error { case unavailable, malformed }

enum API {
    static func request(_ method: String, params: [String: Any] = [:]) throws -> Data {
        try JSONSerialization.data(withJSONObject: ["jsonrpc": "2.0", "id": 1, "method": method, "params": params])
    }
    static func call<T: Decodable>(_ request: Data, as: T.Type) throws -> T {
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { throw ClientError.unavailable }
        defer { Darwin.close(fd) }
        var one: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, socklen_t(MemoryLayout.size(ofValue: one)))
        var timeout = timeval(tv_sec: 90, tv_usec: 0)
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout.size(ofValue: timeout)))
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, socklen_t(MemoryLayout.size(ofValue: timeout)))
        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
        let path = Array((NSHomeDirectory() + "/.agy-rotator/rotatord.sock").utf8) + [0]
        guard path.count <= MemoryLayout.size(ofValue: address.sun_path) else { throw ClientError.unavailable }
        withUnsafeMutableBytes(of: &address.sun_path) { buffer in
            buffer.copyBytes(from: path)
        }
        let rc = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard rc == 0 else { throw ClientError.unavailable }
        let outgoing = request + Data([10])
        try outgoing.withUnsafeBytes { buffer in
            var sent = 0
            while sent < buffer.count {
                let count = Darwin.write(fd, buffer.baseAddress!.advanced(by: sent), buffer.count - sent)
                guard count > 0 else { throw ClientError.unavailable }
                sent += count
            }
        }
        var incoming = Data()
        var buffer = [UInt8](repeating: 0, count: 8192)
        while incoming.count <= 1_048_576 {
            let count = Darwin.read(fd, &buffer, buffer.count)
            guard count > 0 else { throw ClientError.unavailable }
            incoming.append(contentsOf: buffer.prefix(count))
            if incoming.contains(10) { break }
        }
        guard incoming.count <= 1_048_576 else { throw ClientError.malformed }
        let response = try JSONDecoder().decode(Envelope<T>.self, from: incoming)
        if let error = response.error { throw error }
        guard let result = response.result else { throw ClientError.malformed }
        return result
    }
}

/// Accumulates one trackpad gesture; a class so every scroll event does not re-render the view.
@MainActor final class Model: ObservableObject {
    @Published var snapshot: Snapshot?
    @Published var error = ""
    @Published var acting = false
    @Published var connected = false
    private var loading = false

    func describe(_ error: Error) -> String {
        if let rpc = error as? RPCError { return rpc.message }
        return "未能連接背景服務，請執行專案 scripts/install.sh。"
    }
    func load() async {
        guard !loading else { return }
        loading = true
        defer { loading = false }
        do {
            let request = try API.request("status")
            snapshot = try await Task.detached { try API.call(request, as: Snapshot.self) }.value
            connected = true
            error = ""
        } catch { connected = false; self.error = describe(error) }
    }
    func enabled(_ value: Bool) async {
        acting = true
        defer { acting = false }
        do {
            let request = try API.request("enabled.set", params: ["enabled": value])
            snapshot = try await Task.detached { try API.call(request, as: Snapshot.self) }.value
        } catch { self.error = describe(error) }
    }
    func refresh() async {
        do {
            let request = try API.request("refresh")
            _ = try await Task.detached { try API.call(request, as: Accepted.self) }.value
            await load()
        } catch { self.error = describe(error) }
    }
}

// Native Liquid Glass on macOS 26+, material fallback on older systems.

// Same soft blue/lilac field as the web console, so the glass has something to refract.
extension ISO8601DateFormatter {
    static let withFraction: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()
}

// Working / idle chip for the live account; unknown shows nothing.
struct WebConsole: NSViewRepresentable {
    static let url = URL(string: "http://127.0.0.1:3082/agy/")!
    @Binding var failed: Bool
    let reload: Int

    func makeCoordinator() -> Coordinator { Coordinator(failed: $failed) }
    func makeNSView(context: Context) -> WKWebView {
        let view = WKWebView(frame: .zero, configuration: WKWebViewConfiguration())
        view.navigationDelegate = context.coordinator
        view.uiDelegate = context.coordinator
        view.setValue(false, forKey: "drawsBackground")
        context.coordinator.lastReload = reload
        view.load(URLRequest(url: Self.url))
        return view
    }
    func updateNSView(_ view: WKWebView, context: Context) {
        guard context.coordinator.lastReload != reload else { return }
        context.coordinator.lastReload = reload
        view.load(URLRequest(url: Self.url))
    }

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
        let failed: Binding<Bool>
        var lastReload = 0
        init(failed: Binding<Bool>) { self.failed = failed }
        /// Sign-in pages (OpenAI, Google) and any other site open in the default browser, not in this window.
        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            if let url = action.request.url, url.host != "127.0.0.1", ["http", "https"].contains(url.scheme ?? "") {
                NSWorkspace.shared.open(url)
                decisionHandler(.cancel)
                return
            }
            decisionHandler(.allow)
        }
        func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                     for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
            if let url = action.request.url { NSWorkspace.shared.open(url) }
            return nil
        }
        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) { failed.wrappedValue = false }
        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) { failed.wrappedValue = true }
        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
            failed.wrappedValue = true
        }
    }
}

/// Menu bar panel: every account's 5 h / weekly bars at a glance, same colours as the phone console.
struct UsageBar: View {
    let value: Double?
    var color: Color {
        guard let v = value else { return .gray }
        return v <= 0 ? .gray : v < 20 ? .red : v < 50 ? .orange : .green
    }
    var body: some View {
        GeometryReader { g in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.primary.opacity(0.10))
                Capsule().fill(color).frame(width: g.size.width * CGFloat(max(0, min(100, value ?? 0))) / 100)
            }
        }.frame(height: 5)
    }
}

struct AccountLine: View {
    let label: String
    let email: String?
    let five: Double?
    let weekly: Double?
    let active: Bool
    let working: Bool?
    var spent: Bool { (weekly ?? 1) <= 0 }
    func meter(_ title: String, _ value: Double?) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 4) {
                Text(title).font(.system(size: 9)).foregroundStyle(.secondary)
                Spacer(minLength: 0)
                Text(value.map { "\(Int($0.rounded()))%" } ?? "—").font(.system(size: 11, weight: .semibold).monospacedDigit())
            }
            UsageBar(value: value)
        }.frame(width: 78)
    }
    var body: some View {
        HStack(spacing: 10) {
            Circle().fill(active ? Color.green : Color.clear).overlay(Circle().stroke(Color.primary.opacity(active ? 0 : 0.2)))
                .frame(width: 8, height: 8).shadow(color: active ? .green.opacity(0.7) : .clear, radius: 3)
            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 5) {
                    Text(label).font(.system(size: 12, weight: .semibold))
                    if active, let working {
                        Text(working ? "工作中" : "閒置").font(.system(size: 9, weight: .semibold))
                            .foregroundStyle(working ? Color.red : Color.blue)
                    } else if spent {
                        Text("每週已用盡").font(.system(size: 9)).foregroundStyle(.secondary)
                    }
                }
                Text(email ?? "").font(.system(size: 9)).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
            }
            Spacer(minLength: 4)
            meter("5h", five)
            meter("每週", weekly)
        }
        .opacity(spent && !active ? 0.5 : 1)
        .padding(.vertical, 5).padding(.horizontal, 8)
        .background(active ? Color.green.opacity(0.10) : Color.clear, in: RoundedRectangle(cornerRadius: 8))
    }
}

struct MenuPanel: View {
    @ObservedObject var model: Model
    let openConsole: () -> Void
    func section<Content: View>(_ title: String, @ViewBuilder _ rows: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title).font(.system(size: 11, weight: .bold)).foregroundStyle(.secondary).padding(.leading, 8)
            rows()
        }
    }
    var body: some View {
        let s = model.snapshot
        let family = s?.config.family ?? "gemini"
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Account Rotator").font(.system(size: 13, weight: .bold))
                Spacer()
                Circle().fill(model.connected ? Color.green : Color.orange).frame(width: 7, height: 7)
                Text(model.connected ? "已連接" : "連線中斷").font(.system(size: 10)).foregroundStyle(.secondary)
            }
            section("Antigravity") {
                ForEach((s?.profiles ?? [:]).keys.sorted(), id: \.self) { label in
                    let p = s?.profiles[label]
                    let w = p?.groups.first { $0.family == family }?.windows
                    AccountLine(label: label.replacingOccurrences(of: "GEMINI_", with: ""), email: p?.email,
                                five: w?["5h"]?.remaining_percent, weekly: w?["weekly"]?.remaining_percent,
                                active: label == s?.active, working: s?.activity?["gemini"]?.working)
                }
            }
            section("Codex") {
                ForEach((s?.codex?.profiles ?? [:]).keys.sorted(), id: \.self) { label in
                    let r = s?.codex?.profiles[label]
                    AccountLine(label: label.replacingOccurrences(of: "CODEX_", with: ""), email: r?.email,
                                five: r?.five?.remaining_percent, weekly: r?.weekly?.remaining_percent,
                                active: label == s?.codex?.active, working: s?.activity?["codex"]?.working)
                }
            }
            Divider()
            HStack {
                Text("自動輪轉（Antigravity）").font(.system(size: 12))
                Spacer()
                Toggle("", isOn: Binding(get: { s?.config.enabled ?? false },
                                         set: { value in Task { await model.enabled(value) } }))
                    .toggleStyle(.switch).labelsHidden().controlSize(.small).disabled(!model.connected || model.acting)
            }
            HStack {
                Button("開啟控制台", action: openConsole).keyboardShortcut("o")
                Button("更新用量") { Task { await model.refresh() } }
                Spacer()
                Button("結束") { NSApp.terminate(nil) }.keyboardShortcut("q")
            }.controlSize(.small)
        }
        .padding(14).frame(width: 380)
    }
}

/// Opens the window at the phone-like size every time (macOS would otherwise restore an old, cramped frame).
struct WindowSizer: NSViewRepresentable {
    func makeNSView(context: Context) -> NSView {
        let view = NSView()
        DispatchQueue.main.async {
            guard let window = view.window else { return }
            window.isRestorable = false
            let visible = (window.screen ?? NSScreen.main)?.visibleFrame.height ?? 940
            window.setContentSize(NSSize(width: 460, height: min(940, visible - 40)))
            window.center()
        }
        return view
    }
    func updateNSView(_ view: NSView, context: Context) {}
}

/// `--snapshot-web PATH`: render the console offscreen at window size and save a PNG (works with the screen locked).
enum WebSnapshot {
    static func run(to path: String) -> Never {
        _ = NSApplication.shared
        let view = WKWebView(frame: NSRect(x: 0, y: 0, width: 460, height: 940))
        view.setValue(false, forKey: "drawsBackground")
        let window = NSWindow(contentRect: view.frame, styleMask: [.borderless], backing: .buffered, defer: false)
        window.contentView = view
        view.load(URLRequest(url: WebConsole.url))
        RunLoop.main.run(until: Date().addingTimeInterval(4))
        var done = false
        view.takeSnapshot(with: nil) { image, _ in
            if let tiff = image?.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
               let png = rep.representation(using: .png, properties: [:]) {
                try? png.write(to: URL(fileURLWithPath: path))
                print("snapshot: \(path)")
            } else { print("snapshot: FAIL") }
            done = true
        }
        while !done { RunLoop.main.run(until: Date().addingTimeInterval(0.1)) }
        Darwin.exit(0)
    }
}

struct ConsoleWindow: View {
    @State private var failed = false
    @State private var reload = 0
    var body: some View {
        ZStack {
            WebConsole(failed: $failed, reload: reload)
            if failed {
                VStack(spacing: 10) {
                    Text("連唔到控制台").font(.headline)
                    Text("本機網頁服務（127.0.0.1:3082）未回應，可能重新啟動緊。每 5 秒自動重試。")
                        .font(.caption).foregroundStyle(.secondary).multilineTextAlignment(.center)
                    Button("即刻重試") { reload += 1 }
                }.padding(24).frame(maxWidth: 320).background(.regularMaterial, in: RoundedRectangle(cornerRadius: 18))
            }
        }
        // Phone-shaped: everything fits without dragging the window bigger.
        .frame(minWidth: 420, idealWidth: 460, minHeight: 760, idealHeight: 940)
        .background(Color(red: 0.75, green: 0.85, blue: 0.93))
        .background(WindowSizer())
        .task {
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 5_000_000_000)
                if failed { reload += 1 }
            }
        }
    }
}

@main struct AGYRotatorApp: App {
    @StateObject private var model = Model()
    @Environment(\.openWindow) private var openWindow
    init() {
        if let i = CommandLine.arguments.firstIndex(of: "--snapshot-menu"), i + 1 < CommandLine.arguments.count {
            let path = CommandLine.arguments[i + 1]
            MainActor.assumeIsolated {
                _ = NSApplication.shared
                let probe = Model()
                var done = false
                Task { await probe.load(); done = true }
                while !done { RunLoop.main.run(until: Date().addingTimeInterval(0.1)) }
                let renderer = ImageRenderer(content: MenuPanel(model: probe) {}.background(Color(nsColor: .windowBackgroundColor)))
                renderer.scale = 2
                if let image = renderer.nsImage, let tiff = image.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
                   let png = rep.representation(using: .png, properties: [:]) {
                    try? png.write(to: URL(fileURLWithPath: path)); print("snapshot: \(path)")
                } else { print("snapshot: FAIL") }
            }
            Darwin.exit(0)
        }
        if let i = CommandLine.arguments.firstIndex(of: "--snapshot-web"), i + 1 < CommandLine.arguments.count {
            WebSnapshot.run(to: CommandLine.arguments[i + 1])
        }
        if CommandLine.arguments.contains("--verify-api") {
            do {
                let snapshot = try API.call(API.request("status"), as: Snapshot.self)
                print("Swift socket + schema: PASS; activity gemini=\(snapshot.activity?["gemini"]?.working.map { $0 ? "working" : "idle" } ?? "unknown") codex=\(snapshot.activity?["codex"]?.working.map { $0 ? "working" : "idle" } ?? "unknown"); codex_events=\(snapshot.codex_events?.count ?? 0) gemini_events=\(snapshot.gemini_events?.count ?? 0) watches=\(snapshot.codex_watches?.count ?? 0); profiles=\(snapshot.profiles.count); active=\(snapshot.active ?? "UNKNOWN"); auto=\(snapshot.config.enabled); codex=\(snapshot.codex?.profiles.count ?? 0) active=\(snapshot.codex?.active ?? "UNKNOWN") auto=\(snapshot.codex?.auto ?? false) emails=\(snapshot.codex?.profiles.values.filter { $0.email != nil }.count ?? 0)")
                Darwin.exit(0)
            } catch {
                print("Swift socket + schema: FAIL")
                Darwin.exit(1)
            }
        }
    }
    var body: some Scene {
        Window("Account Rotator", id: "dashboard") {
            ConsoleWindow()
        }
        .defaultSize(width: 460, height: 940)
        .windowResizability(.contentMinSize)
        MenuBarExtra {
            MenuPanel(model: model) { openWindow(id: "dashboard"); NSApp.activate(ignoringOtherApps: true) }
        } label: {
            Text("AG \(model.snapshot?.active?.replacingOccurrences(of: "GEMINI_", with: "") ?? "?") · CX \(model.snapshot?.codex?.active?.replacingOccurrences(of: "CODEX_", with: "") ?? "?")")
                .task {
                    while !Task.isCancelled {
                        await model.load()
                        try? await Task.sleep(nanoseconds: 5_000_000_000)
                    }
                }
        }
        .menuBarExtraStyle(.window)
    }
}
