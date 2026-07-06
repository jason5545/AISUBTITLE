import Foundation

final class HeliumTabContextProvider {
    private let cacheDuration: TimeInterval = 1.0
    private let lock = NSLock()
    private var cachedContext: BrowserContext?
    private var cachedAt: Date = .distantPast

    func currentContext() -> BrowserContext? {
        let now = Date()

        lock.lock()
        if now.timeIntervalSince(cachedAt) < cacheDuration {
            let context = cachedContext
            lock.unlock()
            return context
        }
        lock.unlock()

        let context = readActiveTabContext()

        lock.lock()
        cachedAt = now
        cachedContext = context
        lock.unlock()

        return context
    }

    private func readActiveTabContext() -> BrowserContext? {
        let source = """
        tell application "Helium"
            if (count of windows) = 0 then return ""
            set mediaScript to "(() => { const media = Array.from(document.querySelectorAll('audio,video')); return media.some((element) => !element.paused && !element.ended && !element.muted && element.volume > 0 && element.readyState > 1) ? '1' : '0'; })()"
            set javascriptUnavailable to false

            repeat with windowIndex from 1 to (count of windows)
                repeat with tabIndex from 1 to (count of tabs of window windowIndex)
                    set targetTab to tab tabIndex of window windowIndex
                    try
                        set playingState to execute targetTab javascript mediaScript
                        if playingState as text is "1" then
                            return (URL of targetTab) & linefeed & (title of targetTab) & linefeed & "audible"
                        end if
                    on error errorMessage number errorNumber
                        if errorNumber is 12 then
                            set javascriptUnavailable to true
                            exit repeat
                        end if
                    end try
                end repeat
                if javascriptUnavailable then exit repeat
            end repeat

            set targetTab to active tab of window 1
            return (URL of targetTab) & linefeed & (title of targetTab) & linefeed & "active-fallback"
        end tell
        """

        guard let script = NSAppleScript(source: source) else {
            return nil
        }

        var error: NSDictionary?
        let descriptor = script.executeAndReturnError(&error)
        guard error == nil, let value = descriptor.stringValue else {
            return nil
        }

        let parts = value.split(separator: "\n", omittingEmptySubsequences: false)
        let url = parts.first.map(String.init)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        guard !url.isEmpty else {
            return nil
        }

        let title = parts.dropFirst().first.map(String.init)?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let contextSource = parts.dropFirst(2).first.map(String.init)?
            .trimmingCharacters(in: .whitespacesAndNewlines)

        let normalizedSource: String
        if let contextSource, !contextSource.isEmpty {
            normalizedSource = contextSource
        } else {
            normalizedSource = "unknown"
        }

        return BrowserContext(
            url: url,
            title: title?.isEmpty == false ? title : nil,
            source: normalizedSource
        )
    }
}
