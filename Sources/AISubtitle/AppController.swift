import AppKit
import Foundation

final class AppController: NSObject {
    private let overlay = OverlayWindowController()
    private let workingDirectory: URL
    private var config: AppConfig?
    private var captureService: AudioCaptureService?
    private var pipeline: TranscriptionPipeline?
    private let heliumContextProvider = HeliumTabContextProvider()
    private var statusItem: NSStatusItem?
    private var isRunning = false

    override init() {
        workingDirectory = Self.resolveWorkingDirectory()
        super.init()
        setupStatusItem()
    }

    private static func resolveWorkingDirectory() -> URL {
        if let path = ProcessInfo.processInfo.environment["AISUBTITLE_WORKDIR"], !path.isEmpty {
            return URL(fileURLWithPath: path)
        }

        let current = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
        if FileManager.default.fileExists(atPath: current.appendingPathComponent("scripts/codex-translate-lines.sh").path) {
            return current
        }

        return URL(fileURLWithPath: "/Users/jianruicheng/GitHub/AISUBTITLE")
    }

    func start() {
        overlay.show()
        Task {
            await startAsync()
        }
    }

    func stop() {
        Task {
            await stopAsync()
        }
    }

    private func startAsync() async {
        guard !isRunning else {
            return
        }

        do {
            overlay.showStatus("Loading config")
            let loaded = try AppConfig.load(arguments: CommandLine.arguments, workingDirectory: workingDirectory)
            config = loaded.config
            overlay.showStatus("Config: \(loaded.source)")

            let pipeline = TranscriptionPipeline(
                config: loaded.config,
                workingDirectory: workingDirectory,
                browserContextProvider: { [weak self] in
                    self?.heliumContextProvider.currentContext()
                }
            )
            pipeline.onSubtitle = { [weak self] text, source, usage in
                self?.overlay.showSubtitle(text, source: source, usage: usage)
            }
            pipeline.onStatus = { [weak self] status in
                self?.overlay.showStatus(status)
            }
            try pipeline.start()

            let captureService = AudioCaptureService()
            captureService.onPCM = { [weak pipeline] data in
                pipeline?.acceptPCM(data)
            }
            captureService.onStatus = { [weak self] status in
                self?.overlay.showStatus(status)
            }

            overlay.showStatus("Looking for Helium")
            try await captureService.start(config: loaded.config)

            self.pipeline = pipeline
            self.captureService = captureService
            self.isRunning = true
            updateMenu()
        } catch {
            overlay.showStatus(error.localizedDescription)
            pipeline?.stop()
            pipeline = nil
            captureService = nil
            isRunning = false
            updateMenu()
        }
    }

    private func stopAsync() async {
        guard isRunning else {
            return
        }

        overlay.showStatus("Stopping")
        await captureService?.stop()
        pipeline?.stop()
        captureService = nil
        pipeline = nil
        isRunning = false
        overlay.showStatus("Stopped")
        updateMenu()
    }

    private func setupStatusItem() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.title = "AI字幕"
        statusItem = item
        updateMenu()
    }

    private func updateMenu() {
        DispatchQueue.main.async {
            let menu = NSMenu()
            let startStopTitle = self.isRunning ? "Stop" : "Start"
            menu.addItem(NSMenuItem(title: startStopTitle, action: #selector(self.toggleRunning), keyEquivalent: ""))
            menu.addItem(NSMenuItem.separator())
            menu.addItem(NSMenuItem(title: "Quit", action: #selector(self.quit), keyEquivalent: "q"))
            menu.items.forEach { $0.target = self }
            self.statusItem?.menu = menu
        }
    }

    @objc private func toggleRunning() {
        if isRunning {
            stop()
        } else {
            start()
        }
    }

    @objc private func quit() {
        Task {
            await stopAsync()
            DispatchQueue.main.async {
                NSApp.terminate(nil)
            }
        }
    }
}
