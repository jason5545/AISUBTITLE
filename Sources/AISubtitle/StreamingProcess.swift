import Foundation

final class StreamingProcess {
    var onStdoutLine: ((String) -> Void)?
    var onStderrLine: ((String) -> Void)?
    var onTermination: ((Int32) -> Void)?

    private let name: String
    private let command: CommandSpec
    private let workingDirectory: URL
    private let parseQueue: DispatchQueue
    private let writeLock = NSLock()

    private var process: Process?
    private var stdinPipe: Pipe?
    private var stdoutBuffer = Data()
    private var stderrBuffer = Data()

    init(name: String, command: CommandSpec, workingDirectory: URL) {
        self.name = name
        self.command = command
        self.workingDirectory = workingDirectory
        self.parseQueue = DispatchQueue(label: "ai.subtitle.process.\(name).parse")
    }

    var isRunning: Bool {
        process?.isRunning == true
    }

    func start() throws {
        guard process == nil else {
            throw AppError.processAlreadyRunning(name)
        }

        let process = try command.makeProcess(workingDirectory: workingDirectory)
        let stdinPipe = Pipe()
        let stdoutPipe = Pipe()
        let stderrPipe = Pipe()

        process.standardInput = stdinPipe
        process.standardOutput = stdoutPipe
        process.standardError = stderrPipe

        stdoutPipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else {
                return
            }
            self?.appendStdout(data)
        }

        stderrPipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else {
                return
            }
            self?.appendStderr(data)
        }

        process.terminationHandler = { [weak self] process in
            self?.flushBuffers()
            self?.onTermination?(process.terminationStatus)
        }

        try process.run()
        self.process = process
        self.stdinPipe = stdinPipe
    }

    func send(_ data: Data) {
        guard isRunning, !data.isEmpty, let stdinPipe else {
            return
        }

        writeLock.lock()
        defer { writeLock.unlock() }
        stdinPipe.fileHandleForWriting.write(data)
    }

    func sendLine(_ line: String) {
        guard let data = (line + "\n").data(using: .utf8) else {
            return
        }
        send(data)
    }

    func stop() {
        stdoutBuffer.removeAll()
        stderrBuffer.removeAll()

        if let stdinPipe {
            try? stdinPipe.fileHandleForWriting.close()
        }

        process?.standardOutput = nil
        process?.standardError = nil

        if let process, process.isRunning {
            process.terminate()
        }

        process = nil
        stdinPipe = nil
    }

    private func appendStdout(_ data: Data) {
        parseQueue.async { [weak self] in
            guard let self else {
                return
            }
            self.stdoutBuffer.append(data)
            self.emitLines(from: &self.stdoutBuffer, callback: self.onStdoutLine)
        }
    }

    private func appendStderr(_ data: Data) {
        parseQueue.async { [weak self] in
            guard let self else {
                return
            }
            self.stderrBuffer.append(data)
            self.emitLines(from: &self.stderrBuffer, callback: self.onStderrLine)
        }
    }

    private func flushBuffers() {
        parseQueue.async { [weak self] in
            guard let self else {
                return
            }
            self.emitRemaining(from: &self.stdoutBuffer, callback: self.onStdoutLine)
            self.emitRemaining(from: &self.stderrBuffer, callback: self.onStderrLine)
        }
    }

    private func emitLines(from buffer: inout Data, callback: ((String) -> Void)?) {
        let newline = Data([0x0A])
        while let range = buffer.range(of: newline) {
            let lineData = buffer.subdata(in: buffer.startIndex..<range.lowerBound)
            buffer.removeSubrange(buffer.startIndex..<range.upperBound)
            emit(lineData, callback: callback)
        }
    }

    private func emitRemaining(from buffer: inout Data, callback: ((String) -> Void)?) {
        guard !buffer.isEmpty else {
            return
        }
        let data = buffer
        buffer.removeAll()
        emit(data, callback: callback)
    }

    private func emit(_ data: Data, callback: ((String) -> Void)?) {
        guard let line = String(data: data, encoding: .utf8)?
            .trimmingCharacters(in: CharacterSet(charactersIn: "\r")) else {
            return
        }
        callback?(line)
    }
}
