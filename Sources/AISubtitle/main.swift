import AppKit

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var controller: AppController?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let controller = AppController()
        self.controller = controller
        controller.start()
    }

    func applicationWillTerminate(_ notification: Notification) {
        controller?.stop()
    }
}

final class OverlayStdinAppDelegate: NSObject, NSApplicationDelegate {
    private var controller: OverlayStdinController?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let controller = OverlayStdinController()
        self.controller = controller
        controller.start()
    }

    func applicationWillTerminate(_ notification: Notification) {
        controller?.stop()
    }
}

let app = NSApplication.shared
let delegate: NSApplicationDelegate = CommandLine.arguments.contains("--overlay-stdin")
    ? OverlayStdinAppDelegate()
    : AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
