import Foundation

final class TranscriptionPipeline {
    var onSubtitle: ((String, String?, String?) -> Void)?
    var onStatus: ((String) -> Void)?

    private let softStaleLagSeconds: TimeInterval = 4.5
    private let hardStaleLagSeconds: TimeInterval = 8.0
    private let maximumAllowedSegmentsBehind = 3
    private let config: AppConfig
    private let workingDirectory: URL
    private let browserContextProvider: (() -> BrowserContext?)?
    private let systemOutputVolumeProvider = SystemOutputVolumeProvider()
    private let stateLock = NSLock()
    private var asrProcess: StreamingProcess?
    private var translatorProcess: StreamingProcess?
    private var nextTranslationID = 0
    private var latestSubmittedTranslationID = 0
    private var submittedAtByTranslationID: [Int: TimeInterval] = [:]

    init(
        config: AppConfig,
        workingDirectory: URL,
        browserContextProvider: (() -> BrowserContext?)? = nil
    ) {
        self.config = config
        self.workingDirectory = workingDirectory
        self.browserContextProvider = browserContextProvider
    }

    func start() throws {
        let asrProcess = StreamingProcess(name: "asr", command: config.asrCommand, workingDirectory: workingDirectory)
        let translatorProcess = StreamingProcess(name: "translator", command: config.translatorCommand, workingDirectory: workingDirectory)

        asrProcess.onStdoutLine = { [weak self] line in
            self?.handleASRLine(line)
        }
        asrProcess.onStderrLine = { [weak self] line in
            self?.onStatus?("ASR: \(line)")
        }
        asrProcess.onTermination = { [weak self] status in
            self?.onStatus?("ASR stopped: \(status)")
        }

        translatorProcess.onStdoutLine = { [weak self] line in
            self?.handleTranslatorLine(line)
        }
        translatorProcess.onStderrLine = { [weak self] line in
            self?.onStatus?("Translator: \(line)")
        }
        translatorProcess.onTermination = { [weak self] status in
            self?.onStatus?("Translator stopped: \(status)")
        }

        do {
            try translatorProcess.start()
            try asrProcess.start()
        } catch {
            asrProcess.stop()
            translatorProcess.stop()
            throw error
        }

        self.asrProcess = asrProcess
        self.translatorProcess = translatorProcess
    }

    func acceptPCM(_ data: Data) {
        asrProcess?.send(data)
    }

    func stop() {
        asrProcess?.stop()
        translatorProcess?.stop()
        asrProcess = nil
        translatorProcess = nil
    }

    private func handleASRLine(_ line: String) {
        guard let event = TranscriptEvent.parse(line) else {
            return
        }

        guard event.isFinal || config.translatePartialResults else {
            return
        }

        let shouldTranslate = LanguageDecision.shouldTranslate(event)
        let direct = !shouldTranslate && (config.showChineseSource || systemOutputVolumeProvider.isMutedOrSilent())

        if !shouldTranslate && !direct {
            onStatus?("Chinese source skipped")
            return
        }

        let issuedAt = Date().timeIntervalSince1970
        let id = allocateTranslationID(issuedAt: issuedAt)
        let browserContext = browserContextProvider?()

        if direct {
            onStatus?("Direct Chinese \(event.language ?? "unknown")")
        } else {
            onStatus?("Translating \(event.language ?? "unknown")")
        }

        translatorProcess?.sendLine(
            event.jsonLine(
                id: id,
                issuedAt: issuedAt,
                browserContext: browserContext,
                direct: direct
            )
        )
    }

    private func handleTranslatorLine(_ line: String) {
        guard let event = TranslationEvent.parse(line) else {
            return
        }
        let now = Date().timeIntervalSince1970
        if let id = event.id, shouldDropTranslation(id: id, now: now) {
            return
        }
        noteSubtitleDisplayed(id: event.id)
        onSubtitle?(event.text, "zh-Hant", event.usageDisplay)
    }

    private func allocateTranslationID(issuedAt: TimeInterval) -> Int {
        stateLock.lock()
        defer { stateLock.unlock() }

        nextTranslationID += 1
        latestSubmittedTranslationID = nextTranslationID
        submittedAtByTranslationID[nextTranslationID] = issuedAt
        pruneSubmissionTimes(keepingIDsAfter: latestSubmittedTranslationID - 24)
        return nextTranslationID
    }

    private func shouldDropTranslation(id: Int, now: TimeInterval) -> Bool {
        stateLock.lock()
        defer { stateLock.unlock() }

        guard let issuedAt = submittedAtByTranslationID[id] else {
            return false
        }

        let lagSeconds = max(0, now - issuedAt)
        let segmentsBehind = latestSubmittedTranslationID - id

        if lagSeconds >= hardStaleLagSeconds {
            return true
        }

        return segmentsBehind > maximumAllowedSegmentsBehind
            && lagSeconds >= softStaleLagSeconds
    }

    private func noteSubtitleDisplayed(id: Int?) {
        guard let id else {
            return
        }

        stateLock.lock()
        defer { stateLock.unlock() }

        pruneSubmissionTimes(keepingIDsAfter: id - 4)
    }

    private func pruneSubmissionTimes(keepingIDsAfter minimumID: Int) {
        submittedAtByTranslationID = submittedAtByTranslationID.filter { id, _ in
            id >= minimumID
        }
    }
}

private final class SystemOutputVolumeProvider {
    private let cacheDuration: TimeInterval = 0.5
    private let lock = NSLock()
    private var cachedIsMutedOrSilent = false
    private var cachedAt: Date = .distantPast

    func isMutedOrSilent() -> Bool {
        let now = Date()

        lock.lock()
        if now.timeIntervalSince(cachedAt) < cacheDuration {
            let value = cachedIsMutedOrSilent
            lock.unlock()
            return value
        }
        lock.unlock()

        let value = readIsMutedOrSilent()

        lock.lock()
        cachedAt = now
        cachedIsMutedOrSilent = value
        lock.unlock()

        return value
    }

    private func readIsMutedOrSilent() -> Bool {
        let source = """
        set volumeSettings to get volume settings
        set isMuted to output muted of volumeSettings
        set outputLevel to output volume of volumeSettings
        if isMuted or outputLevel is 0 then return "1"
        return "0"
        """

        guard let script = NSAppleScript(source: source) else {
            return false
        }

        var error: NSDictionary?
        let descriptor = script.executeAndReturnError(&error)
        guard error == nil, let value = descriptor.stringValue else {
            return false
        }

        return value == "1"
    }
}
