import CoreMedia
import Foundation
import ScreenCaptureKit

final class AudioCaptureService: NSObject, SCStreamOutput, SCStreamDelegate {
    var onPCM: ((Data) -> Void)?
    var onStatus: ((String) -> Void)?

    private var stream: SCStream?
    private var converter: PCMConverter?
    private let sampleQueue = DispatchQueue(label: "ai.subtitle.capture.audio")
    private let targetLookupTimeout: TimeInterval = 8.0
    private let targetLookupRetryDelayNanoseconds: UInt64 = 300_000_000

    func start(config: AppConfig) async throws {
        let (content, target) = try await waitForTargetApplication(config: config)
        let display = displayForTargetApplication(target, in: content) ?? content.displays.first

        guard let display else {
            throw AppError.noDisplayAvailable
        }

        let filter = SCContentFilter(display: display, including: [target], exceptingWindows: [])
        let configuration = SCStreamConfiguration()
        configuration.capturesAudio = true
        configuration.excludesCurrentProcessAudio = true
        configuration.sampleRate = config.audioSampleRate
        configuration.channelCount = config.audioChannelCount
        configuration.width = 2
        configuration.height = 2
        configuration.minimumFrameInterval = CMTime(value: 1, timescale: 5)
        configuration.showsCursor = false

        converter = try PCMConverter(sampleRate: config.audioSampleRate, channelCount: config.audioChannelCount)

        let stream = SCStream(filter: filter, configuration: configuration, delegate: self)
        try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: sampleQueue)
        try await stream.startCapture()

        self.stream = stream
        onStatus?("Capturing \(target.applicationName)")
    }

    func stop() async {
        guard let stream else {
            return
        }

        try? await stream.stopCapture()
        self.stream = nil
        self.converter = nil
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, CMSampleBufferDataIsReady(sampleBuffer), let converter else {
            return
        }

        do {
            let pcm = try converter.convert(sampleBuffer)
            if !pcm.isEmpty {
                onPCM?(pcm)
            }
        } catch {
            onStatus?("Audio: \(error.localizedDescription)")
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        onStatus?("Capture stopped: \(error.localizedDescription)")
    }

    private func waitForTargetApplication(config: AppConfig) async throws -> (SCShareableContent, SCRunningApplication) {
        let startedAt = Date()

        while true {
            let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
            if let target = findTargetApplication(in: content, config: config) {
                return (content, target)
            }

            let elapsed = Date().timeIntervalSince(startedAt)
            if elapsed >= targetLookupTimeout {
                throw targetApplicationNotFoundError(in: content, config: config)
            }

            let targetName = config.targetApplicationNames.first ?? "target app"
            onStatus?("Waiting for \(targetName)")
            try await Task.sleep(nanoseconds: targetLookupRetryDelayNanoseconds)
        }
    }

    private func findTargetApplication(in content: SCShareableContent, config: AppConfig) -> SCRunningApplication? {
        let normalizedBundleIDs = Set(config.targetBundleIdentifiers.map { $0.lowercased() })
        let normalizedNames = config.targetApplicationNames.map { $0.lowercased() }

        if let bundleMatch = content.applications.first(where: { app in
            normalizedBundleIDs.contains(app.bundleIdentifier.lowercased())
        }) {
            return bundleMatch
        }

        if let nameMatch = content.applications.first(where: { app in
            let name = app.applicationName.lowercased()
            return normalizedNames.contains(where: { name.contains($0) })
                || app.bundleIdentifier.lowercased().contains("helium")
        }) {
            return nameMatch
        }

        return nil
    }

    private func targetApplicationNotFoundError(in content: SCShareableContent, config: AppConfig) -> AppError {
        let available = content.applications
            .map { "\($0.applicationName)(\($0.bundleIdentifier))" }
            .sorted()

        return AppError.targetApplicationNotFound(
            names: config.targetApplicationNames,
            bundleIdentifiers: config.targetBundleIdentifiers,
            available: available
        )
    }

    private func displayForTargetApplication(_ app: SCRunningApplication, in content: SCShareableContent) -> SCDisplay? {
        let window = content.windows
            .filter { $0.owningApplication?.processID == app.processID }
            .max { left, right in
                (left.frame.width * left.frame.height) < (right.frame.width * right.frame.height)
            }

        guard let window else {
            return nil
        }

        let windowCenter = CGPoint(x: window.frame.midX, y: window.frame.midY)
        return content.displays.first(where: { display in
            display.frame.contains(windowCenter)
        })
    }
}
