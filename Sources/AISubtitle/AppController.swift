import AppKit
import Foundation

final class AppController: NSObject {
    private let overlay = OverlayWindowController()
    private let workingDirectory: URL
    private var config: AppConfig?
    private var captureService: AudioCaptureService?
    private var pipeline: TranscriptionPipeline?
    private var overlayIPCServer: OverlayIPCServer?
    private let heliumContextProvider = HeliumTabContextProvider()
    private var statusItem: NSStatusItem?
    private var isRunning = false
    private var externalOverlayActive = false
    private var shouldResumeAfterSystemWake = false
    private var wakeRestartTask: Task<Void, Never>?
    private let wakeRestartDelayNanoseconds: UInt64 = 1_500_000_000

    override init() {
        workingDirectory = Self.resolveWorkingDirectory()
        super.init()
        setupStatusItem()
        setupOverlayIPC()
        setupPowerNotifications()
    }

    deinit {
        wakeRestartTask?.cancel()
        NSWorkspace.shared.notificationCenter.removeObserver(self)
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

        var startingCaptureService: AudioCaptureService?
        var startingPipeline: TranscriptionPipeline?

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
                guard self?.externalOverlayActive != true else {
                    return
                }
                self?.overlay.showSubtitle(text, source: source, usage: usage)
            }
            pipeline.onStatus = { [weak self] status in
                guard self?.externalOverlayActive != true else {
                    return
                }
                self?.overlay.showStatus(status)
            }
            startingPipeline = pipeline

            let captureService = AudioCaptureService()
            captureService.onPCM = { [weak pipeline] data in
                pipeline?.acceptPCM(data)
            }
            captureService.onStatus = { [weak self] status in
                guard self?.externalOverlayActive != true else {
                    return
                }
                self?.overlay.showStatus(status)
            }
            startingCaptureService = captureService

            overlay.showStatus("Looking for Helium")
            try await captureService.start(config: loaded.config)

            overlay.showStatus("Starting subtitle pipeline")
            try pipeline.start()

            self.pipeline = pipeline
            self.captureService = captureService
            self.isRunning = true
            updateMenu()
        } catch {
            overlay.showStatus(error.localizedDescription)
            startingPipeline?.stop()
            await startingCaptureService?.stop()
            pipeline = nil
            captureService = nil
            isRunning = false
            updateMenu()
        }
    }

    private func stopAsync(finalStatus: String = "Stopped") async {
        guard isRunning else {
            return
        }

        overlay.showStatus("Stopping")
        await captureService?.stop()
        pipeline?.stop()
        captureService = nil
        pipeline = nil
        isRunning = false
        overlay.showStatus(finalStatus)
        updateMenu()
    }

    private func setupStatusItem() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.title = "AI字幕"
        statusItem = item
        updateMenu()
    }

    private func setupOverlayIPC() {
        overlayIPCServer = OverlayIPCServer { [weak self] line in
            self?.handleOverlayIPC(line)
        }
        overlayIPCServer?.start()
    }

    private func setupPowerNotifications() {
        let notificationCenter = NSWorkspace.shared.notificationCenter
        notificationCenter.addObserver(
            self,
            selector: #selector(systemWillSleep),
            name: NSWorkspace.willSleepNotification,
            object: nil
        )
        notificationCenter.addObserver(
            self,
            selector: #selector(systemDidWake),
            name: NSWorkspace.didWakeNotification,
            object: nil
        )
    }

    private func handleOverlayIPC(_ line: String) {
        externalOverlayActive = !OverlayMessageRenderer.isStopStatus(line)
        OverlayMessageRenderer.render(line, overlay: overlay)
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

    @objc private func systemWillSleep() {
        shouldResumeAfterSystemWake = isRunning || captureService != nil || pipeline != nil
        guard shouldResumeAfterSystemWake else {
            return
        }

        wakeRestartTask?.cancel()
        overlay.showStatus("System sleep: pausing capture")
        Task {
            await stopAsync(finalStatus: "Paused for system sleep")
        }
    }

    @objc private func systemDidWake() {
        let shouldRestart = shouldResumeAfterSystemWake || isRunning || captureService != nil || pipeline != nil
        shouldResumeAfterSystemWake = false
        guard shouldRestart else {
            return
        }

        wakeRestartTask?.cancel()
        wakeRestartTask = Task { [weak self] in
            await self?.restartAfterSystemWake()
        }
    }

    private func restartAfterSystemWake() async {
        overlay.showStatus("System wake: recapturing Helium")
        await stopAsync(finalStatus: "Reconnecting after wake")

        do {
            try await Task.sleep(nanoseconds: wakeRestartDelayNanoseconds)
        } catch {
            return
        }

        await startAsync()
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
