import AppKit
import Foundation

/// Registers the two background services (the daemon and the web console) as LaunchAgents, so an app installed from
/// the .dmg needs no install script. The rotator bundled in the app is copied to a fixed folder under Application
/// Support and the services run that copy: moving or deleting the app never leaves them pointing at a missing path.
/// It runs at every launch; a new build (another BUILD stamp) replaces the copy and restarts both services.
enum Services {
    enum Outcome: Equatable { case ready, notBundled, notInstalled, needsTools, otherCopyRunning, failed(String) }

    static let home = FileManager.default.homeDirectoryForCurrentUser.path
    static let state = home + "/.agy-rotator"
    static let support = home + "/Library/Application Support/Account Rotator"
    static let labels = ["io.account-rotator.daemon", "io.account-rotator.web"]
    static let logLimit = 5_000_000  // bytes; a larger log is emptied at launch
    static var domain: String { "gui/\(getuid())" }

    @discardableResult
    static func run(_ path: String, _ args: [String]) -> (status: Int32, out: String) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: path)
        p.arguments = args
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = FileHandle.nullDevice
        do { try p.run() } catch { return (-1, "") }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        return (p.terminationStatus, String(decoding: data, as: UTF8.self))
    }

    static func loaded(_ label: String) -> Bool { run("/bin/launchctl", ["print", "\(domain)/\(label)"]).status == 0 }

    static func plist(label: String, root: String, arguments: [String], log: String, build: String) -> [String: Any] {
        [
            "Label": label,
            "ProgramArguments": ["/usr/bin/python3"] + arguments,
            "WorkingDirectory": root,
            "RunAtLoad": true,
            "KeepAlive": true,
            "ThrottleInterval": 10,
            "StandardOutPath": "\(state)/\(log).log",
            "StandardErrorPath": "\(state)/\(log)-error.log",
            // agy / codex / claude are found wherever their installers put them
            "EnvironmentVariables": ["PATH": "\(home)/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                                     "ACCOUNT_ROTATOR_BUILD": build],  // a new build changes the plist: services restart
            "Umask": 0o077,
        ]
    }

    static func stamp(_ folder: String) -> String? {
        (try? String(contentsOfFile: folder + "/BUILD", encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    static func trimLogs() {
        for name in ["daemon", "daemon-error", "web", "web-error"] {
            let path = "\(state)/\(name).log"
            // Truncated in place: launchd keeps the file open (append mode), so a replaced file would stay empty.
            if let size = (try? FileManager.default.attributesOfItem(atPath: path))?[.size] as? Int, size > logLimit,
               let handle = FileHandle(forWritingAtPath: path) {
                try? handle.truncate(atOffset: 0)
                try? handle.close()
            }
        }
    }

    static func portBusy() -> Bool {
        !run("/usr/sbin/lsof", ["-nP", "-iTCP:3082", "-sTCP:LISTEN", "-t"]).out.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    /// Unload a service and wait until its process is gone (it still holds its lock / port 3082 for a moment), so
    /// the next one does not start into a busy port and wait out launchd's 10 s restart throttle.
    static func stop(_ label: String) {
        guard loaded(label) else { return }
        run("/bin/launchctl", ["bootout", "\(domain)/\(label)"])
        for _ in 0..<40 {
            if !loaded(label) && !(label == labels[1] && portBusy()) { return }
            usleep(250_000)
        }
    }

    static func ensure() -> Outcome {
        let fm = FileManager.default
        guard let bundled = Bundle.main.resourceURL?.appendingPathComponent("rotator").path,
              fm.fileExists(atPath: bundled + "/agy-rotator") else { return .notBundled }
        // Opened straight from the .dmg, or from the quarantine copy macOS makes of an app that was not moved.
        let app = Bundle.main.bundlePath
        if app.hasPrefix("/Volumes/") || app.contains("/AppTranslocation/") { return .notInstalled }
        // /usr/bin/python3 is only a stub until the Command Line Tools are installed.
        guard run("/usr/bin/xcode-select", ["-p"]).status == 0 else { return .needsTools }
        // Another copy (an older install) already serves the console: two rotators would fight over the accounts.
        if portBusy() && !loaded(labels[1]) { return .otherCopyRunning }
        try? fm.createDirectory(atPath: state, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        trimLogs()
        var build = stamp(bundled) ?? (Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "0")
        let root = support + "/rotator"
        var problem: String?
        if stamp(root) != build || !fm.fileExists(atPath: root + "/agy-rotator") {
            // Copy this build next to the installed one first; only then stop the services and swap it in.
            let incoming = support + "/rotator.incoming"
            try? fm.createDirectory(atPath: support, withIntermediateDirectories: true)
            try? fm.removeItem(atPath: incoming)
            if (try? fm.copyItem(atPath: bundled, toPath: incoming)) != nil {
                labels.forEach(stop)
                try? fm.removeItem(atPath: root)
                if (try? fm.moveItem(atPath: incoming, toPath: root)) == nil { problem = "未能安裝新版本嘅程式。" }
            } else {
                problem = "未能抄新版本嘅程式去「Application Support」。"
            }
            if problem != nil {
                // Keep running the version already installed, if there is one.
                guard let installed = stamp(root), fm.fileExists(atPath: root + "/agy-rotator") else {
                    return .failed((problem ?? "") + "背景服務未能啟動。")
                }
                build = installed
            }
        }
        let agents = home + "/Library/LaunchAgents"
        try? fm.createDirectory(atPath: agents, withIntermediateDirectories: true)
        let wanted = [
            plist(label: labels[0], root: root, arguments: [root + "/agy-rotator", "daemon"], log: "daemon", build: build),
            plist(label: labels[1], root: root, arguments: [root + "/web/server.py"], log: "web", build: build),
        ]
        for doc in wanted {
            let label = doc["Label"] as! String
            let path = "\(agents)/\(label).plist"
            guard let data = try? PropertyListSerialization.data(fromPropertyList: doc, format: .xml, options: 0) else {
                return .failed("未能建立背景服務 \(label) 嘅設定。")
            }
            let same = (try? Data(contentsOf: URL(fileURLWithPath: path))) == data
            if same && loaded(label) { continue }
            do {
                try data.write(to: URL(fileURLWithPath: path), options: .atomic)
                try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path)
            } catch { return .failed("未能寫入背景服務 \(label) 嘅設定。") }
            stop(label)
            if run("/bin/launchctl", ["bootstrap", domain, path]).status != 0 { return .failed("未能啟動背景服務 \(label)。") }
        }
        if let problem { return .failed(problem + "暫時繼續用緊舊版本。") }
        return .ready
    }

    /// Called once at launch: registers the services off the main thread and explains anything the user has to do.
    static func start() {
        DispatchQueue.global(qos: .userInitiated).async {
            let outcome = ensure()
            DispatchQueue.main.async { explain(outcome) }
        }
    }

    @MainActor static func explain(_ outcome: Outcome) {
        switch outcome {
        case .ready, .notBundled:
            return
        case .notInstalled:
            let alert = NSAlert()
            alert.messageText = "請先將 Account Rotator 拖入「應用程式」"
            alert.informativeText = "而家係直接喺安裝檔（.dmg）入面打開。請將 Account Rotator 拖去 Applications（應用程式）folder，再喺嗰度打開。"
            NSApp.activate(ignoringOtherApps: true)
            alert.runModal()
        case .needsTools:
            let alert = NSAlert()
            alert.messageText = "需要先安裝 Command Line Tools"
            alert.informativeText = "Account Rotator 用 macOS 嘅 Command Line Tools（python3）喺背景運行。撳「安裝」，跟住 macOS 嘅指示裝完，再開一次 Account Rotator。"
            alert.addButton(withTitle: "安裝")
            alert.addButton(withTitle: "稍後")
            NSApp.activate(ignoringOtherApps: true)
            if alert.runModal() == .alertFirstButtonReturn { run("/usr/bin/xcode-select", ["--install"]) }
        case .otherCopyRunning:
            let alert = NSAlert()
            alert.messageText = "另一個 Account Rotator 已經喺度運行"
            alert.informativeText = "本機 127.0.0.1:3082 已經俾另一個版本用緊。兩個同時運行會爭住轉帳號，所以呢個版本冇啟動背景服務。請先停咗舊嗰個，再開一次。"
            NSApp.activate(ignoringOtherApps: true)
            alert.runModal()
        case .failed(let message):
            let alert = NSAlert()
            alert.messageText = "背景服務出咗問題"
            alert.informativeText = message + "請結束再打開一次 Account Rotator；如果仍然失敗，睇 ~/.agy-rotator 入面嘅 log。"
            NSApp.activate(ignoringOtherApps: true)
            alert.runModal()
        }
    }
}
