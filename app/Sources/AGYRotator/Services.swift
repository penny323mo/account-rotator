import AppKit
import Foundation

/// Registers the two background services (the daemon and the web console) as LaunchAgents that run the copy bundled
/// in this app, so an app installed from the .dmg needs no install script. It runs at every launch: an app that was
/// moved rewrites the paths, an unchanged one is left alone.
enum Services {
    enum Outcome: Equatable { case ready, notBundled, needsTools, otherCopyRunning, failed(String) }

    static let home = FileManager.default.homeDirectoryForCurrentUser.path
    static let state = home + "/.agy-rotator"
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

    static func plist(label: String, root: String, arguments: [String], log: String) -> [String: Any] {
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
            "EnvironmentVariables": ["PATH": "\(home)/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"],
            "Umask": 0o077,
        ]
    }

    static func ensure() -> Outcome {
        guard let resources = Bundle.main.resourceURL?.appendingPathComponent("rotator").path,
              FileManager.default.fileExists(atPath: resources + "/agy-rotator") else { return .notBundled }
        // /usr/bin/python3 is only a stub until the Command Line Tools are installed.
        guard run("/usr/bin/xcode-select", ["-p"]).status == 0 else { return .needsTools }
        // Another copy (an older install) already serves the console: two rotators would fight over the accounts.
        let listening = !run("/usr/sbin/lsof", ["-nP", "-iTCP:3082", "-sTCP:LISTEN", "-t"]).out
            .trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        if listening && !loaded("io.account-rotator.web") { return .otherCopyRunning }
        try? FileManager.default.createDirectory(atPath: state, withIntermediateDirectories: true,
                                                 attributes: [.posixPermissions: 0o700])
        let agents = home + "/Library/LaunchAgents"
        try? FileManager.default.createDirectory(atPath: agents, withIntermediateDirectories: true)
        let wanted = [
            plist(label: "io.account-rotator.daemon", root: resources, arguments: [resources + "/agy-rotator", "daemon"], log: "daemon"),
            plist(label: "io.account-rotator.web", root: resources, arguments: [resources + "/web/server.py"], log: "web"),
        ]
        for doc in wanted {
            let label = doc["Label"] as! String
            let path = "\(agents)/\(label).plist"
            guard let data = try? PropertyListSerialization.data(fromPropertyList: doc, format: .xml, options: 0) else {
                return .failed(label)
            }
            let same = (try? Data(contentsOf: URL(fileURLWithPath: path))) == data
            if same && loaded(label) { continue }
            do {
                try data.write(to: URL(fileURLWithPath: path), options: .atomic)
                try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path)
            } catch { return .failed(label) }
            if loaded(label) { run("/bin/launchctl", ["bootout", "\(domain)/\(label)"]) }
            if run("/bin/launchctl", ["bootstrap", domain, path]).status != 0 { return .failed(label) }
        }
        return .ready
    }

    /// Called once at launch: registers the services and explains anything the user has to do.
    @MainActor static func start() {
        switch ensure() {
        case .ready, .notBundled:
            return
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
        case .failed(let label):
            let alert = NSAlert()
            alert.messageText = "未能啟動背景服務"
            alert.informativeText = "登記 \(label) 失敗。請再開一次 Account Rotator；如果仍然失敗，睇 ~/.agy-rotator 入面嘅 log。"
            NSApp.activate(ignoringOtherApps: true)
            alert.runModal()
        }
    }
}
