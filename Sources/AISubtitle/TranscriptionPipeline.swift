import Foundation
import OSLog

final class TranscriptionPipeline {
    var onSubtitle: ((String, String?, String?) -> Void)?
    var onStatus: ((String) -> Void)?

    private let softStaleLagSeconds: TimeInterval = 2.0
    private let hardStaleLagSeconds: TimeInterval = 5.0
    private let maximumAllowedSegmentsBehind = 1
    private let config: AppConfig
    private let workingDirectory: URL
    private let browserContextProvider: (() -> BrowserContext?)?
    private let logger = Logger(subsystem: "com.jasonchien.AISubtitle", category: "TranscriptionPipeline")
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
            logger.info("asr-unparsed line_chars=\(line.count, privacy: .public)")
            return
        }

        guard event.isFinal || config.translatePartialResults else {
            logger.info("asr-skip-partial lang=\(event.language ?? "unknown", privacy: .public) text_chars=\(event.text.count, privacy: .public)")
            return
        }

        let shouldTranslate = LanguageDecision.shouldTranslate(event)
        let direct = !shouldTranslate && (config.showChineseSource || systemOutputVolumeProvider.isMutedOrSilent())

        if !shouldTranslate && !direct {
            onStatus?("Chinese source skipped")
            logger.info("asr-skip-chinese lang=\(event.language ?? "unknown", privacy: .public) text_chars=\(event.text.count, privacy: .public)")
            return
        }

        let issuedAt = Date().timeIntervalSince1970
        let id = allocateTranslationID(issuedAt: issuedAt)
        let contextStartedAt = Date().timeIntervalSince1970
        let browserContext = browserContextProvider?()
        let contextElapsed = Date().timeIntervalSince1970 - contextStartedAt
        logger.info("context-provider id=\(id, privacy: .public) elapsed=\(self.formatSeconds(contextElapsed), privacy: .public)s source=\(browserContext?.source ?? "none", privacy: .public) has_url=\((browserContext?.url.isEmpty == false), privacy: .public)")

        if direct {
            onStatus?("Direct Chinese \(event.language ?? "unknown")")
        } else {
            onStatus?("Translating \(event.language ?? "unknown")")
        }

        let mode = direct ? "direct" : "translate"
        let preSubmitLag = Date().timeIntervalSince1970 - issuedAt
        logger.info("submit id=\(id, privacy: .public) mode=\(mode, privacy: .public) lang=\(event.language ?? "unknown", privacy: .public) final=\(event.isFinal, privacy: .public) pre_submit_lag=\(self.formatSeconds(preSubmitLag), privacy: .public)s text_chars=\(event.text.count, privacy: .public)")

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
            logger.info("translator-unparsed line_chars=\(line.count, privacy: .public)")
            return
        }
        let now = Date().timeIntervalSince1970
        let lag = submissionLag(id: event.id, now: now)
        logger.info("translator-recv id=\(event.id ?? -1, privacy: .public) lag=\(self.formatSeconds(lag), privacy: .public)s usage=\(event.usageDisplay ?? "none", privacy: .public) text_chars=\(event.text.count, privacy: .public)")
        if let id = event.id, let dropReason = dropTranslationReason(id: id, now: now) {
            logger.info("translator-drop id=\(id, privacy: .public) reason=\(dropReason, privacy: .public)")
            return
        }
        noteSubtitleDisplayed(id: event.id)
        logger.info("subtitle-display id=\(event.id ?? -1, privacy: .public) lag=\(self.formatSeconds(lag), privacy: .public)s")
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

    private func submissionLag(id: Int?, now: TimeInterval) -> TimeInterval? {
        guard let id else {
            return nil
        }

        stateLock.lock()
        defer { stateLock.unlock() }

        guard let issuedAt = submittedAtByTranslationID[id] else {
            return nil
        }

        return max(0, now - issuedAt)
    }

    private func dropTranslationReason(id: Int, now: TimeInterval) -> String? {
        stateLock.lock()
        defer { stateLock.unlock() }

        guard let issuedAt = submittedAtByTranslationID[id] else {
            return nil
        }

        let lagSeconds = max(0, now - issuedAt)
        let segmentsBehind = latestSubmittedTranslationID - id

        if lagSeconds >= hardStaleLagSeconds {
            return "hard-stale lag=\(formatSeconds(lagSeconds))s behind=\(segmentsBehind)"
        }

        if segmentsBehind > maximumAllowedSegmentsBehind
            && lagSeconds >= softStaleLagSeconds {
            return "soft-stale lag=\(formatSeconds(lagSeconds))s behind=\(segmentsBehind)"
        }

        return nil
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

    private func formatSeconds(_ value: TimeInterval?) -> String {
        guard let value else {
            return "na"
        }
        return String(format: "%.3f", value)
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
