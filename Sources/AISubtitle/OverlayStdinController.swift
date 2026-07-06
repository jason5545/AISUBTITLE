import AppKit
import Foundation

final class OverlayStdinController {
    private let overlay = OverlayWindowController()
    private var ipcServer: OverlayIPCServer?
    private let parseQueue = DispatchQueue(label: "ai.subtitle.overlay.stdin")
    private var buffer = Data()

    func start() {
        overlay.show()
        overlay.showStatus("Waiting for terminal subtitles")
        ipcServer = OverlayIPCServer { [weak self] line in
            guard let self else {
                return
            }
            OverlayMessageRenderer.render(line, overlay: self.overlay)
        }
        ipcServer?.start()

        FileHandle.standardInput.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else {
                handle.readabilityHandler = nil
                DispatchQueue.main.async {
                    NSApp.terminate(nil)
                }
                return
            }
            self?.append(data)
        }
    }

    func stop() {
        FileHandle.standardInput.readabilityHandler = nil
        ipcServer?.stop()
        ipcServer = nil
    }

    private func append(_ data: Data) {
        parseQueue.async { [weak self] in
            guard let self else {
                return
            }
            self.buffer.append(data)
            self.emitLines()
        }
    }

    private func emitLines() {
        let newline = Data([0x0A])
        while let range = buffer.range(of: newline) {
            let lineData = buffer.subdata(in: buffer.startIndex..<range.lowerBound)
            buffer.removeSubrange(buffer.startIndex..<range.upperBound)
            handle(lineData)
        }
    }

    private func handle(_ data: Data) {
        guard let line = String(data: data, encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines),
            !line.isEmpty else {
            return
        }

        OverlayMessageRenderer.render(line, overlay: overlay)
    }
}
