import Darwin
import Foundation
import OSLog

final class HeliumTabContextProvider {
    // Invariant: All Apple Events in this file must run on the refresh queue via
    // the timeout-guarded subprocess; never on the caller's thread.
    private let cacheDuration: TimeInterval = 5.0
    private let scriptDeadline: TimeInterval = 3.0
    private let cooldownDuration: TimeInterval = 60.0
    private let refreshQueue = DispatchQueue(label: "aisubtitle.helium-context")
    private let logger = Logger(subsystem: "com.jasonchien.AISubtitle", category: "HeliumTabContextProvider")
    private let lock = NSLock()

    private var cachedContext: BrowserContext?
    private var cachedAt: Date = .distantPast
    private var lastAttemptAt: Date = .distantPast
    private var isRefreshing = false
    private var audibleHint: TabHint?
    private var consecutiveTimeouts = 0
    private var activeOnlyUntil: Date = .distantPast

    func currentContext() -> BrowserContext? {
        let now = Date()
        var context: BrowserContext?
        var refreshRequest: RefreshRequest?

        lock.lock()
        context = cachedContext

        let cacheIsStale = now.timeIntervalSince(cachedAt) >= cacheDuration
        let retryWindowOpen = now.timeIntervalSince(lastAttemptAt) >= cacheDuration
        if cacheIsStale && retryWindowOpen && !isRefreshing {
            let mode: RefreshMode = now < activeOnlyUntil ? .activeOnly : .full
            refreshRequest = RefreshRequest(mode: mode, hint: audibleHint)
            isRefreshing = true
            lastAttemptAt = now
        }
        lock.unlock()

        if let refreshRequest {
            refreshQueue.async { [weak self] in
                self?.refresh(request: refreshRequest)
            }
        }

        return context
    }

    private func refresh(request: RefreshRequest) {
        let startedAt = Date()
        logger.info("context-refresh-start mode=\(request.mode.rawValue, privacy: .public)")
        defer {
            markRefreshFinished()
        }

        let runResult = runOsaScript(
            script: script(for: request.mode),
            arguments: scriptArguments(for: request),
            deadline: scriptDeadline
        )

        switch runResult {
        case .success(let output):
            guard output.terminationStatus == 0 else {
                noteRefreshError("status=\(output.terminationStatus) stderr=\(trimmedDiagnostic(output.stderr))")
                return
            }

            guard let result = parseRefreshOutput(output.stdout, previousHint: request.hint) else {
                noteRefreshError("empty or invalid osascript output")
                return
            }

            noteRefreshSuccess(result)
            let elapsed = Date().timeIntervalSince(startedAt)
            let elapsedText = formatSeconds(elapsed)
            let source = result.context.source
            let hasURL = !result.context.url.isEmpty
            let hintHit = result.hintHit
            logger.info("context-refresh-done elapsed=\(elapsedText, privacy: .public)s source=\(source, privacy: .public) has_url=\(hasURL, privacy: .public) hint_hit=\(hintHit, privacy: .public)")

        case .timeout(let elapsed):
            noteRefreshTimeout(elapsed: elapsed)

        case .launchError(let message):
            noteRefreshError(message)
        }
    }

    private func markRefreshFinished() {
        lock.lock()
        isRefreshing = false
        lock.unlock()
    }

    private func noteRefreshSuccess(_ result: RefreshResult) {
        lock.lock()
        cachedContext = result.context
        cachedAt = Date()
        if let hint = result.hint {
            audibleHint = hint
        }
        consecutiveTimeouts = 0
        lock.unlock()
    }

    private func noteRefreshTimeout(elapsed: TimeInterval) {
        let elapsedText = formatSeconds(elapsed)
        logger.info("context-refresh-timeout elapsed=\(elapsedText, privacy: .public)s")

        var shouldLogDegrade = false
        lock.lock()
        consecutiveTimeouts += 1
        if consecutiveTimeouts >= 3 && Date() >= activeOnlyUntil {
            activeOnlyUntil = Date().addingTimeInterval(cooldownDuration)
            consecutiveTimeouts = 0
            shouldLogDegrade = true
        }
        lock.unlock()

        if shouldLogDegrade {
            logger.info("context-degrade cooldown=\(Int(self.cooldownDuration), privacy: .public)s")
        }
    }

    private func noteRefreshError(_ message: String) {
        logger.info("context-refresh-error \(message.prefix(500), privacy: .public)")
    }

    private func scriptArguments(for request: RefreshRequest) -> [String] {
        guard request.mode == .full, let hint = request.hint else {
            return []
        }

        return [String(hint.windowIndex), String(hint.tabIndex)]
    }

    private func script(for mode: RefreshMode) -> String {
        switch mode {
        case .full:
            return fullContextScript
        case .activeOnly:
            return activeOnlyScript
        }
    }

    private func runOsaScript(
        script: String,
        arguments: [String],
        deadline: TimeInterval
    ) -> OsaScriptRunResult {
        let startedAt = Date()
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        process.arguments = ["-e", script] + arguments

        let stdoutPipe = Pipe()
        let stderrPipe = Pipe()
        process.standardOutput = stdoutPipe
        process.standardError = stderrPipe

        let semaphore = DispatchSemaphore(value: 0)
        process.terminationHandler = { _ in
            semaphore.signal()
        }

        do {
            try process.run()
        } catch {
            return .launchError("launch failed: \(error.localizedDescription)")
        }

        let waitResult = semaphore.wait(timeout: .now() + deadline)
        if waitResult == .timedOut {
            Darwin.kill(process.processIdentifier, SIGKILL)
            _ = semaphore.wait(timeout: .now() + 0.5)
            closeReadHandle(stdoutPipe.fileHandleForReading)
            closeReadHandle(stderrPipe.fileHandleForReading)
            return .timeout(Date().timeIntervalSince(startedAt))
        }

        let stdout = String(
            data: stdoutPipe.fileHandleForReading.readDataToEndOfFile(),
            encoding: .utf8
        ) ?? ""
        let stderr = String(
            data: stderrPipe.fileHandleForReading.readDataToEndOfFile(),
            encoding: .utf8
        ) ?? ""

        return .success(
            OsaScriptOutput(
                stdout: stdout,
                stderr: stderr,
                terminationStatus: process.terminationStatus
            )
        )
    }

    private func closeReadHandle(_ handle: FileHandle) {
        try? handle.close()
    }

    private func parseRefreshOutput(_ rawOutput: String, previousHint: TabHint?) -> RefreshResult? {
        let parts = rawOutput
            .trimmingCharacters(in: CharacterSet(charactersIn: "\r\n"))
            .split(separator: "\n", omittingEmptySubsequences: false)
            .map { String($0).trimmingCharacters(in: .whitespacesAndNewlines) }

        guard let url = parts[safe: 0], !url.isEmpty else {
            return nil
        }

        let title = parts[safe: 1]
        let source = parts[safe: 2]?.isEmpty == false ? parts[2] : "unknown"
        let hint = parseHint(windowIndex: parts[safe: 3], tabIndex: parts[safe: 4])
        let hintHit = source == "audible" && hint != nil && hint == previousHint

        return RefreshResult(
            context: BrowserContext(
                url: url,
                title: title?.isEmpty == false ? title : nil,
                source: source
            ),
            hint: hint,
            hintHit: hintHit
        )
    }

    private func parseHint(windowIndex: String?, tabIndex: String?) -> TabHint? {
        guard let windowIndex,
              let tabIndex,
              let window = Int(windowIndex),
              let tab = Int(tabIndex),
              window > 0,
              tab > 0 else {
            return nil
        }

        return TabHint(windowIndex: window, tabIndex: tab)
    }

    private func trimmedDiagnostic(_ value: String) -> String {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? "none" : String(trimmed.prefix(500))
    }

    private func formatSeconds(_ value: TimeInterval) -> String {
        String(format: "%.3f", value)
    }

    private let fullContextScript = """
    on run argv
        set hintedWindowIndex to 0
        set hintedTabIndex to 0
        if (count of argv) >= 2 then
            try
                set hintedWindowIndex to item 1 of argv as integer
                set hintedTabIndex to item 2 of argv as integer
            end try
        end if

        with timeout of 2 seconds
            tell application "Helium"
                if (count of windows) = 0 then return ""
                set mediaScript to "(() => { const media = Array.from(document.querySelectorAll('audio,video')); return media.some((element) => !element.paused && !element.ended && !element.muted && element.volume > 0 && element.readyState > 1) ? '1' : '0'; })()"

                if hintedWindowIndex > 0 and hintedTabIndex > 0 and hintedWindowIndex <= (count of windows) then
                    set hintedWindow to window hintedWindowIndex
                    if hintedTabIndex <= (count of tabs of hintedWindow) then
                        set targetTab to tab hintedTabIndex of hintedWindow
                        try
                            set playingState to execute targetTab javascript mediaScript
                            if playingState as text is "1" then
                                return (URL of targetTab) & linefeed & (title of targetTab) & linefeed & "audible" & linefeed & (hintedWindowIndex as text) & linefeed & (hintedTabIndex as text)
                            end if
                        end try
                    end if
                end if

                repeat with windowIndex from 1 to (count of windows)
                    repeat with tabIndex from 1 to (count of tabs of window windowIndex)
                        set targetTab to tab tabIndex of window windowIndex
                        try
                            set playingState to execute targetTab javascript mediaScript
                            if playingState as text is "1" then
                                return (URL of targetTab) & linefeed & (title of targetTab) & linefeed & "audible" & linefeed & (windowIndex as text) & linefeed & (tabIndex as text)
                            end if
                        end try
                    end repeat
                end repeat

                set targetTab to active tab of window 1
                return (URL of targetTab) & linefeed & (title of targetTab) & linefeed & "active"
            end tell
        end timeout
    end run
    """

    private let activeOnlyScript = """
    on run argv
        with timeout of 2 seconds
            tell application "Helium"
                if (count of windows) = 0 then return ""
                set targetTab to active tab of window 1
                return (URL of targetTab) & linefeed & (title of targetTab) & linefeed & "active"
            end tell
        end timeout
    end run
    """
}

private enum RefreshMode: String {
    case full
    case activeOnly = "active-only"
}

private struct RefreshRequest {
    var mode: RefreshMode
    var hint: TabHint?
}

private struct TabHint: Equatable {
    var windowIndex: Int
    var tabIndex: Int
}

private struct RefreshResult {
    var context: BrowserContext
    var hint: TabHint?
    var hintHit: Bool
}

private struct OsaScriptOutput {
    var stdout: String
    var stderr: String
    var terminationStatus: Int32
}

private enum OsaScriptRunResult {
    case success(OsaScriptOutput)
    case timeout(TimeInterval)
    case launchError(String)
}

private extension Array {
    subscript(safe index: Int) -> Element? {
        indices.contains(index) ? self[index] : nil
    }
}
