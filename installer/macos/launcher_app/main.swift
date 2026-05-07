// Media Search Agent — macOS menu bar launcher
//
// A native Swift NSStatusItem app that manages the MSA backend process and
// provides a persistent menu bar icon. Replaces the former Platypus-based
// launcher.sh approach.
//
// Compile (done by build.sh — no Xcode project required):
//   swiftc -framework AppKit -framework Foundation \
//     -target arm64-apple-macos12.0 -O \
//     -o MediaSearchAgent \
//     installer/macos/launcher_app/main.swift
//
// Requires macOS 12.0+ (matches LSMinimumSystemVersion in Info.plist).

import AppKit
import Foundation

// ── Path resolution ───────────────────────────────────────────────────────────
//
// App code always lives in Contents/Resources/ (Bundle.main.resourcePath):
//   shell bundle  — ~/Applications/MediaSearchAgent.app/Contents/Resources/
//   .pkg install  — /Applications/MediaSearchAgent.app/Contents/Resources/
//
// The venv is also inside Contents/Resources/.venv for the shell bundle.
// For .pkg installs that placed the venv in Application Support, a sidecar
// file (msa-paths.env) can override any individual path.

private struct MSAPaths {
    let msaRoot:          String   // app code dir (scripts/, src/, bin/)
    let startSH:          String
    let stopSH:           String
    let venvBin:          String   // .venv/bin/ — msa CLI lives here
    let venvDir:          String   // .venv/ root
    let configPath:       String
    let dataDir:          String   // Application Support / user data root
    let cacheDir:         String
    let logDir:           String
    let launchAgentLabel: String
    let launchAgentPlist: String
}

private func loadPaths() -> MSAPaths {
    let resourcePath = Bundle.main.resourcePath
        ?? (NSHomeDirectory() + "/Applications/MediaSearchAgent.app/Contents/Resources")
    let home = NSHomeDirectory()
    let appSupport = home + "/Library/Application Support/MediaSearchAgent"
    let label = "ai.openara.mediasearchagent"

    // Parse optional sidecar — present only when a legacy layout needs overrides.
    var sidecar: [String: String] = [:]
    let envFile = resourcePath + "/msa-paths.env"
    if let content = try? String(contentsOfFile: envFile, encoding: .utf8) {
        for line in content.components(separatedBy: .newlines) {
            let t = line.trimmingCharacters(in: .whitespaces)
            guard !t.isEmpty, !t.hasPrefix("#"),
                  let eq = t.firstIndex(of: "=") else { continue }
            sidecar[String(t[t.startIndex ..< eq])] = String(t[t.index(after: eq)...])
        }
    }

    let msaRoot = sidecar["MSA_ROOT"] ?? resourcePath

    // Shell bundle: venv is inside Contents/Resources/.venv.
    // .pkg (legacy): venv may be in Application Support — detect by presence.
    let bundleVenv = msaRoot + "/.venv"
    let defaultVenvDir = FileManager.default.fileExists(atPath: bundleVenv + "/bin/python")
        ? bundleVenv
        : (appSupport + "/.venv")
    let venvDir    = sidecar["MSA_VENV_DIR"]           ?? defaultVenvDir
    let configPath = sidecar["MSA_CONFIG_PATH"]        ?? (appSupport + "/config.yaml")
    let dataDir    = sidecar["MSA_DATA_DIR"]           ?? appSupport
    let cacheDir   = sidecar["MSA_CACHE_DIR"]          ?? (home + "/Library/Caches/MediaSearchAgent")
    let logDir     = sidecar["MSA_LOG_DIR"]            ?? (home + "/Library/Logs/MediaSearchAgent")
    let labelStr   = sidecar["MSA_LAUNCH_AGENT_LABEL"] ?? label

    return MSAPaths(
        msaRoot:          msaRoot,
        startSH:          msaRoot + "/scripts/start.sh",
        stopSH:           msaRoot + "/scripts/stop.sh",
        venvBin:          venvDir + "/bin",
        venvDir:          venvDir,
        configPath:       configPath,
        dataDir:          dataDir,
        cacheDir:         cacheDir,
        logDir:           logDir,
        launchAgentLabel: labelStr,
        launchAgentPlist: home + "/Library/LaunchAgents/" + labelStr + ".plist"
    )
}

private let paths = loadPaths()

private func resolveUninstallScript() -> String? {
    let candidates = [
        paths.msaRoot + "/uninstall.sh",
        paths.msaRoot + "/installer/macos/uninstaller.sh",
    ]

    for candidate in candidates where FileManager.default.fileExists(atPath: candidate) {
        return candidate
    }

    return nil
}

// ── Port resolution ────────────────────────────────────────────────────────────

private func resolveAPIPort() -> Int {
    guard let content = try? String(contentsOfFile: paths.configPath, encoding: .utf8) else {
        return 8000
    }
    var inAPISection = false
    for line in content.components(separatedBy: .newlines) {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        if trimmed.hasPrefix("api:") { inAPISection = true; continue }
        if inAPISection {
            if !line.hasPrefix(" ") && !line.hasPrefix("\t") && !trimmed.isEmpty { break }
            if trimmed.hasPrefix("port:") {
                let value = trimmed.dropFirst(5).trimmingCharacters(in: .whitespaces)
                if let port = Int(value) { return port }
            }
        }
    }
    return 8000
}

private let kAPIPort     = resolveAPIPort()
private let kPollSeconds = 5.0
private let kReadyTimeout = 90
private let kHealthURL   = "http://localhost:\(kAPIPort)/health"
private let kLaunchURL   = "http://localhost:\(kAPIPort)/?launch=1"
private let kAppVersion  = (Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String)
    ?? (Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String)
    ?? "unknown"

// ── AppDelegate ────────────────────────────────────────────────────────────────

final class AppDelegate: NSObject, NSApplicationDelegate {

    private var statusItem: NSStatusItem!
    private var pollTimer: Timer?
    private var isRunning = false

    private var statusLabel: NSMenuItem!
    private var stopItem: NSMenuItem!
    private var loginItem: NSMenuItem!

    // MARK: – Lifecycle

    func applicationDidFinishLaunching(_ note: Notification) {
        NSApp.setActivationPolicy(.accessory)
        buildStatusItem()
        buildMenu()
        startPolling()

        // Run start.sh on a background thread and wait for it to exit before
        // opening the browser. start.sh kills any stale cross-install server
        // and backgrounds uvicorn, then exits (typically within a few seconds).
        // Waiting prevents waitForReadyThenOpen from seeing the old server's
        // /health 200 before it has been replaced.
        let launchURL = URL(string: kLaunchURL)
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            self?.runShellSync(paths.startSH, args: ["--no-browser"])
            DispatchQueue.main.async {
                if let url = launchURL { self?.waitForReadyThenOpen(url) }
                self?.checkHealth { [weak self] running in self?.applyStatus(running) }
            }
        }
    }

    func applicationWillTerminate(_ note: Notification) {
        pollTimer?.invalidate()
    }

    // MARK: – Status item

    private func buildStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        applyIcon(running: false)
    }

    private func applyIcon(running: Bool) {
        guard let button = statusItem.button else { return }
        let symbol = running ? "photo.stack.fill" : "photo.stack"
        button.image = NSImage(systemSymbolName: symbol,
                               accessibilityDescription: "Media Search Agent")
        button.image?.isTemplate = true
    }

    // MARK: – Menu

    private func buildMenu() {
        let menu = NSMenu()

        menu.addItem(item("Open Media Search", action: #selector(openBrowser)))
        menu.addItem(.separator())

        statusLabel = NSMenuItem(title: "○ Stopped", action: nil, keyEquivalent: "")
        statusLabel.isEnabled = false
        menu.addItem(statusLabel)
        menu.addItem(.separator())

        stopItem = item("Stop Services", action: #selector(stopServices))
        stopItem.isEnabled = false
        menu.addItem(stopItem)

        menu.addItem(item("Launch CLI", action: #selector(launchCLI)))
        menu.addItem(.separator())

        let moreMenu = NSMenu()
        moreMenu.addItem(item("View Logs", action: #selector(viewLogs)))

        loginItem = item("Start on Login", action: #selector(toggleLogin))
        loginItem.state = loginEnabled ? .on : .off
        moreMenu.addItem(loginItem)

        moreMenu.addItem(.separator())
        moreMenu.addItem(item("Uninstall…", action: #selector(uninstall)))
        moreMenu.addItem(.separator())

        let versionItem = NSMenuItem(title: "Version \(kAppVersion)", action: nil, keyEquivalent: "")
        versionItem.isEnabled = false
        moreMenu.addItem(versionItem)

        let moreItem = NSMenuItem(title: "More", action: nil, keyEquivalent: "")
        moreItem.submenu = moreMenu
        menu.addItem(moreItem)
        menu.addItem(.separator())

        menu.addItem(item("Quit", action: #selector(quit)))

        statusItem.menu = menu
    }

    private func item(_ title: String, action: Selector) -> NSMenuItem {
        let m = NSMenuItem(title: title, action: action, keyEquivalent: "")
        m.target = self
        return m
    }

    // MARK: – Health polling

    private func startPolling() {
        let t = Timer.scheduledTimer(withTimeInterval: kPollSeconds, repeats: true) { [weak self] _ in
            self?.checkHealth { running in self?.applyStatus(running) }
        }
        RunLoop.main.add(t, forMode: .common)
        pollTimer = t
    }

    private func checkHealth(completion: @escaping (Bool) -> Void) {
        guard let url = URL(string: kHealthURL) else { completion(false); return }
        var req = URLRequest(url: url)
        req.timeoutInterval = 1.5
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            let ok = (resp as? HTTPURLResponse)?.statusCode == 200
            DispatchQueue.main.async { completion(ok) }
        }.resume()
    }

    private func applyStatus(_ running: Bool) {
        isRunning = running
        applyIcon(running: running)
        statusLabel.title  = running ? "● Running — http://localhost:\(kAPIPort)" : "○ Stopped"
        stopItem.isEnabled = running
    }

    // MARK: – Menu actions

    @objc private func openBrowser() {
        guard let url = URL(string: kLaunchURL) else { return }
        if isRunning {
            NSWorkspace.shared.open(url)
            return
        }
        runShell(paths.startSH, args: ["--no-browser"])
        waitForReadyThenOpen(url)
    }

    private func waitForReadyThenOpen(_ url: URL) {
        var attempts = 0
        let t = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] timer in
            attempts += 1
            self?.checkHealth { ready in
                if ready || attempts >= kReadyTimeout {
                    timer.invalidate()
                    NSWorkspace.shared.open(url)
                }
            }
        }
        RunLoop.main.add(t, forMode: .common)
    }

    @objc private func stopServices() {
        runShell(paths.stopSH)
    }

    @objc private func viewLogs() {
        let dir = URL(fileURLWithPath: paths.logDir)
        if FileManager.default.fileExists(atPath: paths.logDir) {
            NSWorkspace.shared.open(dir)
        }
    }

    @objc private func launchCLI() {
        let cmdFile = URL(fileURLWithPath: NSTemporaryDirectory() + "msa-cli.command")
        let venvBin = paths.venvBin.replacingOccurrences(of: "\"", with: "\\\"")
        let config  = paths.configPath.replacingOccurrences(of: "\"", with: "\\\"")
        let dataDir = paths.dataDir.replacingOccurrences(of: "\"", with: "\\\"")
        try? FileManager.default.createDirectory(
            atPath: paths.dataDir, withIntermediateDirectories: true, attributes: nil)
        let script = """
            #!/bin/bash
            export PATH="\(venvBin):$PATH"
            export MSA_CONFIG_PATH="\(config)"
            cd "\(dataDir)" 2>/dev/null || true
            clear
            echo '── Media Search Agent CLI ──────────────────────'
            echo '  msa index run                — index your media'
            echo '  msa api start | stop | status'
            echo '  msa --help'
            echo '────────────────────────────────────────────────'
            exec env PATH="$PATH" MSA_CONFIG_PATH="$MSA_CONFIG_PATH" "$SHELL"
            """
        do {
            try script.write(to: cmdFile, atomically: true, encoding: .utf8)
            try FileManager.default.setAttributes(
                [.posixPermissions: 0o755], ofItemAtPath: cmdFile.path)
            NSWorkspace.shared.open(cmdFile)
        } catch { }
    }

    @objc private func uninstall() {
        let alert = NSAlert()
        alert.messageText     = "Uninstall Media Search Agent?"
        alert.informativeText = "This will stop all services, remove the app, and delete the Python environment (~500 MB).\n\nYour index, thumbnails, and config will not be removed unless you choose to delete them."
        alert.alertStyle      = .warning
        alert.addButton(withTitle: "Uninstall")
        alert.addButton(withTitle: "Cancel")
        alert.buttons[0].hasDestructiveAction = true
        alert.buttons[1].keyEquivalent        = "\r"
        alert.buttons[0].keyEquivalent        = ""

        guard alert.runModal() == .alertFirstButtonReturn else { return }

        guard let uninstallScript = resolveUninstallScript() else {
            let errorAlert = NSAlert()
            errorAlert.messageText = "Uninstall script not found"
            errorAlert.informativeText = "Media Search Agent could not find its uninstall script in this app bundle."
            errorAlert.alertStyle = .critical
            errorAlert.runModal()
            return
        }
        let cmdFile = URL(fileURLWithPath: NSTemporaryDirectory() + "msa-uninstall.command")
        let escaped = uninstallScript.replacingOccurrences(of: " ", with: "\\ ")
        let script = "#!/bin/bash\nbash \(escaped)\n"
        do {
            try script.write(to: cmdFile, atomically: true, encoding: .utf8)
            try FileManager.default.setAttributes(
                [.posixPermissions: 0o755], ofItemAtPath: cmdFile.path)
            NSWorkspace.shared.open(cmdFile)
        } catch { }

        NSApp.terminate(nil)
    }

    @objc private func toggleLogin() {
        if loginEnabled {
            disableLoginItem()
            loginItem.state = .off
        } else {
            enableLoginItem()
            loginItem.state = .on
        }
    }

    @objc private func quit() {
        runShell(paths.stopSH)
        NSApp.terminate(nil)
    }

    // MARK: – Login item (LaunchAgent)

    private var loginEnabled: Bool {
        FileManager.default.fileExists(atPath: paths.launchAgentPlist)
    }

    private func enableLoginItem() {
        // Launch the .app itself so the menu bar appears and start.sh is called
        // through the same code path as a manual double-click.
        let appBundle = Bundle.main.bundlePath
        let plist = """
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>\(paths.launchAgentLabel)</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/open</string>
        <string>-a</string>
        <string>\(appBundle)</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>StandardOutPath</key>
    <string>/tmp/msa-login.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/msa-login.log</string>
</dict>
</plist>
"""
        let agentsDir = (paths.launchAgentPlist as NSString).deletingLastPathComponent
        try? FileManager.default.createDirectory(atPath: agentsDir,
                                                 withIntermediateDirectories: true)
        try? plist.write(toFile: paths.launchAgentPlist, atomically: true, encoding: .utf8)
        launchctl("load", paths.launchAgentPlist)
    }

    private func disableLoginItem() {
        launchctl("unload", paths.launchAgentPlist)
        try? FileManager.default.removeItem(atPath: paths.launchAgentPlist)
    }

    private func launchctl(_ verb: String, _ plistPath: String) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        p.arguments = [verb, plistPath]
        p.standardOutput = FileHandle.nullDevice
        p.standardError  = FileHandle.nullDevice
        try? p.run()
    }

    // MARK: – Shell helpers

    private func makeProcess(_ script: String, args: [String]) -> Process {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/bash")
        p.arguments = [script] + args

        // Build environment: inherit the current process env (so macOS system
        // paths are present), then overlay all MSA vars that start.sh/stop.sh
        // need. Without these, start.sh falls back to wrong paths or fails.
        var env = ProcessInfo.processInfo.environment
        let binDir = paths.msaRoot + "/bin"
        let libDir = paths.msaRoot + "/lib"
        env["MSA_ROOT"]        = paths.msaRoot
        env["MSA_VENV_DIR"]    = paths.venvDir
        env["MSA_CONFIG_PATH"] = paths.configPath
        env["MSA_DATA_DIR"]    = paths.dataDir
        env["MSA_CACHE_DIR"]   = paths.cacheDir
        env["MSA_LOG_DIR"]     = paths.logDir
        // Prepend bundled tool binaries so start.sh finds exiftool/mediainfo
        let currentPath = env["PATH"] ?? "/usr/bin:/bin:/usr/local/bin"
        env["PATH"] = binDir + ":/usr/local/bin:/usr/bin:/bin:" + currentPath
        // libmediainfo.dylib lives in bin/../lib — pymediainfo needs it via ctypes
        let currentDyld = env["DYLD_LIBRARY_PATH"] ?? ""
        env["DYLD_LIBRARY_PATH"] = currentDyld.isEmpty ? libDir : libDir + ":" + currentDyld
        p.environment = env

        // Log to launcher.log so failures are diagnosable from View Logs.
        try? FileManager.default.createDirectory(
            atPath: paths.logDir, withIntermediateDirectories: true)
        let logPath = paths.logDir + "/launcher.log"
        FileManager.default.createFile(atPath: logPath, contents: nil)
        if let fh = try? FileHandle(forWritingTo: URL(fileURLWithPath: logPath)) {
            fh.seekToEndOfFile()
            p.standardOutput = fh
            p.standardError  = fh
        }

        return p
    }

    private func runShell(_ script: String, args: [String] = []) {
        do {
            try makeProcess(script, args: args).run()
        } catch {
            appendLauncherLog("Failed to run \(script): \(error)")
        }
    }

    private func runShellSync(_ script: String, args: [String] = []) {
        let p = makeProcess(script, args: args)
        do {
            try p.run()
            p.waitUntilExit()
            if p.terminationStatus != 0 {
                appendLauncherLog("\(script) exited with status \(p.terminationStatus)")
            }
        } catch {
            appendLauncherLog("Failed to run \(script): \(error)")
        }
    }

    private func appendLauncherLog(_ message: String) {
        try? FileManager.default.createDirectory(
            atPath: paths.logDir, withIntermediateDirectories: true)
        let logPath = paths.logDir + "/launcher.log"
        let line = "\(Date()) \(message)\n"
        guard let data = line.data(using: .utf8) else { return }
        FileManager.default.createFile(atPath: logPath, contents: nil)
        if let fh = try? FileHandle(forWritingTo: URL(fileURLWithPath: logPath)) {
            fh.seekToEndOfFile()
            fh.write(data)
            try? fh.close()
        }
    }
}

// ── Entry point ────────────────────────────────────────────────────────────────

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
