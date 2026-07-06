import AppKit
import Darwin
import Foundation

enum OverlayIPC {
    static var socketPath: String {
        "/tmp/aisubtitle-overlay-\(getuid()).sock"
    }
}

enum OverlayMessageRenderer {
    static func render(_ line: String, overlay: OverlayWindowController) {
        guard let payload = parsePayload(line) else {
            overlay.showSubtitle(line, source: "terminal", usage: nil)
            return
        }

        let type = string(payload["type"])?.lowercased()
        let status = string(payload["status"])
        let text = string(payload["text"])
            ?? string(payload["translation"])
            ?? string(payload["result"])
        let source = string(payload["source"])
            ?? string(payload["context_title"])
            ?? string(payload["media_title"])
        let usage = string(payload["usage_display"])
            ?? string((payload["usage"] as? [String: Any])?["display"])

        if type == "status", let status {
            overlay.showStatus(status)
            return
        }

        if let text, !text.isEmpty {
            overlay.showSubtitle(text, source: source, usage: usage)
            if let status, !status.isEmpty {
                overlay.showStatus(status)
            }
        } else if let status, !status.isEmpty {
            overlay.showStatus(status)
        }
    }

    static func isStopStatus(_ line: String) -> Bool {
        guard let payload = parsePayload(line),
              let status = string(payload["status"])?.lowercased() else {
            return false
        }
        return status == "stopped"
    }

    private static func parsePayload(_ line: String) -> [String: Any]? {
        guard let data = line.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data),
              let payload = object as? [String: Any] else {
            return nil
        }
        return payload
    }

    private static func string(_ value: Any?) -> String? {
        guard let value else {
            return nil
        }
        if let value = value as? String {
            let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
            return trimmed.isEmpty ? nil : trimmed
        }
        return "\(value)"
    }
}

final class OverlayIPCServer {
    private let socketPath: String
    private let onLine: (String) -> Void
    private let acceptQueue = DispatchQueue(label: "ai.subtitle.overlay.ipc.accept")
    private let clientQueue = DispatchQueue(label: "ai.subtitle.overlay.ipc.client", attributes: .concurrent)
    private let stateLock = NSLock()
    private var serverFD: Int32 = -1
    private var running = false

    init(socketPath: String = OverlayIPC.socketPath, onLine: @escaping (String) -> Void) {
        self.socketPath = socketPath
        self.onLine = onLine
    }

    deinit {
        stop()
    }

    func start() {
        stateLock.lock()
        if running {
            stateLock.unlock()
            return
        }
        stateLock.unlock()

        acceptQueue.async { [weak self] in
            self?.startListening()
        }
    }

    func stop() {
        stateLock.lock()
        running = false
        let fd = serverFD
        serverFD = -1
        stateLock.unlock()

        if fd >= 0 {
            Darwin.close(fd)
            unlink(socketPath)
        }
    }

    private func startListening() {
        if Self.canConnect(to: socketPath) {
            return
        }
        unlink(socketPath)

        let fd = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else {
            return
        }

        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        let pathBytes = Array(socketPath.utf8)
        guard pathBytes.count + 1 < MemoryLayout.size(ofValue: address.sun_path) else {
            Darwin.close(fd)
            return
        }

        withUnsafeMutableBytes(of: &address.sun_path) { buffer in
            buffer.initializeMemory(as: UInt8.self, repeating: 0)
            buffer.copyBytes(from: pathBytes)
        }

        let bindStatus = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { socketAddress in
                Darwin.bind(
                    fd,
                    socketAddress,
                    socklen_t(MemoryLayout<sa_family_t>.size + pathBytes.count + 1)
                )
            }
        }
        guard bindStatus == 0 else {
            Darwin.close(fd)
            return
        }

        guard Darwin.listen(fd, 8) == 0 else {
            Darwin.close(fd)
            unlink(socketPath)
            return
        }

        stateLock.lock()
        serverFD = fd
        running = true
        stateLock.unlock()

        acceptLoop(fd)
    }

    private func acceptLoop(_ fd: Int32) {
        while isRunning {
            let clientFD = Darwin.accept(fd, nil, nil)
            if clientFD < 0 {
                if errno == EINTR {
                    continue
                }
                break
            }
            clientQueue.async { [weak self] in
                self?.readClient(clientFD)
            }
        }
    }

    private var isRunning: Bool {
        stateLock.lock()
        defer { stateLock.unlock() }
        return running
    }

    private func readClient(_ fd: Int32) {
        defer {
            Darwin.close(fd)
        }

        var buffer = Data()
        var chunk = [UInt8](repeating: 0, count: 4096)

        while true {
            let count = Darwin.read(fd, &chunk, chunk.count)
            if count <= 0 {
                return
            }

            buffer.append(chunk, count: count)
            emitLines(from: &buffer)
        }
    }

    private func emitLines(from buffer: inout Data) {
        let newline = Data([0x0A])
        while let range = buffer.range(of: newline) {
            let lineData = buffer.subdata(in: buffer.startIndex..<range.lowerBound)
            buffer.removeSubrange(buffer.startIndex..<range.upperBound)
            guard let line = String(data: lineData, encoding: .utf8)?
                .trimmingCharacters(in: .whitespacesAndNewlines),
                !line.isEmpty else {
                continue
            }
            onLine(line)
        }
    }

    private static func canConnect(to path: String) -> Bool {
        let fd = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else {
            return false
        }
        defer {
            Darwin.close(fd)
        }

        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        let pathBytes = Array(path.utf8)
        guard pathBytes.count + 1 < MemoryLayout.size(ofValue: address.sun_path) else {
            return false
        }

        withUnsafeMutableBytes(of: &address.sun_path) { buffer in
            buffer.initializeMemory(as: UInt8.self, repeating: 0)
            buffer.copyBytes(from: pathBytes)
        }

        let status = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { socketAddress in
                Darwin.connect(
                    fd,
                    socketAddress,
                    socklen_t(MemoryLayout<sa_family_t>.size + pathBytes.count + 1)
                )
            }
        }
        return status == 0
    }
}
