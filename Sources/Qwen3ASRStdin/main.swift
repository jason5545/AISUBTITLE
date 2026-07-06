import Foundation

struct RunnerConfig {
    var modelDirectory: URL = URL(fileURLWithPath: NSHomeDirectory())
        .appendingPathComponent("Library/Application Support/com.jasonchien.Voco/Qwen3Models/mlx-community_Qwen3-ASR-1.7B-8bit")
    var language: String?
    var usesAudioAdapter = true
    var minSegmentSeconds: Double = 2.4
    var maxSegmentSeconds: Double = 7.5
    var silenceSeconds: Double = 0.45
    var silenceRMS: Float = 0.012
    var minSpeechRMS: Float = 0.004
    var prompt: String?

    static func parse(_ arguments: [String]) throws -> RunnerConfig {
        var config = RunnerConfig()
        var index = 1

        func requireValue(after flag: String) throws -> String {
            let next = index + 1
            guard next < arguments.count else {
                throw RunnerError.invalidArguments("Missing value after \(flag)")
            }
            return arguments[next]
        }

        while index < arguments.count {
            let flag = arguments[index]
            switch flag {
            case "--model-dir":
                config.modelDirectory = URL(fileURLWithPath: try requireValue(after: flag)).standardizedFileURL
                index += 2
            case "--language":
                let value = try requireValue(after: flag)
                config.language = value == "auto" ? nil : value
                index += 2
            case "--no-audio-adapter":
                config.usesAudioAdapter = false
                index += 1
            case "--min-segment-seconds":
                config.minSegmentSeconds = Double(try requireValue(after: flag)) ?? config.minSegmentSeconds
                index += 2
            case "--max-segment-seconds":
                config.maxSegmentSeconds = Double(try requireValue(after: flag)) ?? config.maxSegmentSeconds
                index += 2
            case "--silence-seconds":
                config.silenceSeconds = Double(try requireValue(after: flag)) ?? config.silenceSeconds
                index += 2
            case "--silence-rms":
                config.silenceRMS = Float(try requireValue(after: flag)) ?? config.silenceRMS
                index += 2
            case "--prompt":
                config.prompt = try requireValue(after: flag)
                index += 2
            case "--help", "-h":
                throw RunnerError.helpRequested
            default:
                throw RunnerError.invalidArguments("Unknown argument: \(flag)")
            }
        }

        return config
    }
}

enum RunnerError: Error, LocalizedError {
    case helpRequested
    case invalidArguments(String)
    case modelDirectoryMissing(String)

    var errorDescription: String? {
        switch self {
        case .helpRequested:
            return Self.usage
        case .invalidArguments(let message):
            return "\(message)\n\n\(Self.usage)"
        case .modelDirectoryMissing(let path):
            return "Qwen3 model directory does not exist: \(path)"
        }
    }

    static let usage = """
    qwen3-asr-stdin --model-dir PATH [--language auto|English|Japanese|Chinese]

    stdin: 16 kHz mono signed 16-bit little-endian PCM
    stdout: JSONL transcript events
    """
}

final class PCMSegmenter {
    private let sampleRate = 16_000
    private let config: RunnerConfig
    private var samples: [Float] = []

    init(config: RunnerConfig) {
        self.config = config
    }

    func appendPCM16LE(_ data: Data) -> [[Float]] {
        data.withUnsafeBytes { rawBuffer in
            guard let bytes = rawBuffer.bindMemory(to: UInt8.self).baseAddress else {
                return
            }

            var offset = 0
            while offset + 1 < data.count {
                let raw = UInt16(bytes[offset]) | (UInt16(bytes[offset + 1]) << 8)
                let value = Int16(bitPattern: raw)
                samples.append(max(-1.0, min(Float(value) / 32767.0, 1.0)))
                offset += 2
            }
        }

        return consumeReadySegments(force: false)
    }

    func finish() -> [[Float]] {
        consumeReadySegments(force: true)
    }

    private func consumeReadySegments(force: Bool) -> [[Float]] {
        var ready: [[Float]] = []
        let minSamples = Int(config.minSegmentSeconds * Double(sampleRate))
        let maxSamples = Int(config.maxSegmentSeconds * Double(sampleRate))
        let silenceSamples = Int(config.silenceSeconds * Double(sampleRate))

        while !samples.isEmpty {
            if samples.count >= maxSamples {
                ready.append(popSegment(sampleCount: maxSamples))
                continue
            }

            if samples.count >= minSamples,
               silenceSamples > 0,
               trailingRMS(sampleCount: min(silenceSamples, samples.count)) <= config.silenceRMS {
                let cut = max(0, samples.count - silenceSamples)
                if cut > 0 {
                    ready.append(popSegment(sampleCount: cut))
                }
                samples.removeFirst(min(silenceSamples, samples.count))
                continue
            }

            if force {
                ready.append(popSegment(sampleCount: samples.count))
            }

            break
        }

        return ready.filter { rms($0) >= config.minSpeechRMS }
    }

    private func popSegment(sampleCount: Int) -> [Float] {
        let count = min(sampleCount, samples.count)
        let segment = Array(samples.prefix(count))
        samples.removeFirst(count)
        return segment
    }

    private func trailingRMS(sampleCount: Int) -> Float {
        guard sampleCount > 0, samples.count >= sampleCount else {
            return .greatestFiniteMagnitude
        }
        return rms(samples.suffix(sampleCount))
    }

    private func rms<S: Sequence>(_ values: S) -> Float where S.Element == Float {
        var sum: Float = 0
        var count: Float = 0
        for value in values {
            sum += value * value
            count += 1
        }
        guard count > 0 else {
            return 0
        }
        return sqrt(sum / count)
    }
}

enum Qwen3ASRStdin {
    static func run() async throws {
        let config = try RunnerConfig.parse(CommandLine.arguments)
        guard FileManager.default.fileExists(atPath: config.modelDirectory.path) else {
            throw RunnerError.modelDirectoryMissing(config.modelDirectory.path)
        }

        let engine = Qwen3ASREngine()
        try await engine.loadModel(
            from: config.modelDirectory,
            modelSize: .large,
            usesAudioAdapter: config.usesAudioAdapter
        )

        let segmenter = PCMSegmenter(config: config)
        while let data = try FileHandle.standardInput.read(upToCount: 32_000), !data.isEmpty {
            for segment in segmenter.appendPCM16LE(data) {
                try await transcribe(segment, engine: engine, config: config)
            }
        }

        for segment in segmenter.finish() {
            try await transcribe(segment, engine: engine, config: config)
        }
    }

    private static func transcribe(_ samples: [Float], engine: Qwen3ASREngine, config: RunnerConfig) async throws {
        let result = try await engine.transcribe(
            samples: samples,
            language: config.language,
            prompt: config.prompt
        )

        let text = result.text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else {
            return
        }

        let payload: [String: Any] = [
            "text": text,
            "language": result.detectedLanguage ?? "unknown",
            "is_final": true,
            "avg_logprob": result.avgLogProb,
            "duration_seconds": Double(samples.count) / 16_000.0
        ]
        let data = try JSONSerialization.data(withJSONObject: payload)
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data([0x0A]))
    }
}

do {
    try await Qwen3ASRStdin.run()
} catch RunnerError.helpRequested {
    fputs(RunnerError.usage + "\n", stderr)
    exit(0)
} catch {
    fputs("qwen3-asr-stdin: \(error.localizedDescription)\n", stderr)
    exit(1)
}
