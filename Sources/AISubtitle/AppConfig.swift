import Foundation

private let standardExecutableSearchPath = [
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin"
]

struct CommandSpec: Decodable {
    var argv: [String]?
    var shell: String?
    var environment: [String: String]?

    var isEmpty: Bool {
        (argv?.isEmpty ?? true) && (shell?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ?? true)
    }

    func makeProcess(workingDirectory: URL?) throws -> Process {
        let process = Process()

        if let argv, let executable = argv.first, !executable.isEmpty {
            if executable.hasPrefix("/") {
                process.executableURL = URL(fileURLWithPath: executable)
            } else if executable.contains("/") {
                process.executableURL = workingDirectory?.appendingPathComponent(executable).standardizedFileURL
                    ?? URL(fileURLWithPath: executable)
            } else {
                process.executableURL = URL(fileURLWithPath: executable)
            }
            process.arguments = Array(argv.dropFirst())
        } else if let shell, !shell.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            process.executableURL = URL(fileURLWithPath: "/bin/zsh")
            process.arguments = ["-lc", shell]
        } else {
            throw AppError.invalidCommand("Command must provide either argv or shell.")
        }

        if let workingDirectory {
            process.currentDirectoryURL = workingDirectory
        }

        var mergedEnvironment = ProcessInfo.processInfo.environment
        environment?.forEach { key, value in
            mergedEnvironment[key] = value
        }
        mergedEnvironment["PATH"] = normalizedExecutableSearchPath(mergedEnvironment["PATH"])
        process.environment = mergedEnvironment
        return process
    }

    private func normalizedExecutableSearchPath(_ currentValue: String?) -> String {
        var paths = standardExecutableSearchPath
        for path in currentValue?.split(separator: ":").map(String.init) ?? [] {
            if !paths.contains(path) {
                paths.append(path)
            }
        }
        return paths.joined(separator: ":")
    }
}

struct AppConfig: Decodable {
    var targetApplicationNames: [String]
    var targetBundleIdentifiers: [String]
    var audioSampleRate: Int
    var audioChannelCount: Int
    var showChineseSource: Bool
    var translatePartialResults: Bool
    var asrCommand: CommandSpec
    var translatorCommand: CommandSpec

    static func load(arguments: [String], workingDirectory: URL) throws -> LoadedConfig {
        let explicitConfigPath = value(after: "--config", in: arguments)
            ?? ProcessInfo.processInfo.environment["AISUBTITLE_CONFIG"]

        if let explicitConfigPath {
            let url = URL(fileURLWithPath: explicitConfigPath, relativeTo: workingDirectory).standardizedFileURL
            return LoadedConfig(config: try decode(url), source: url.path)
        }

        let localConfig = workingDirectory.appendingPathComponent("config.json")
        if FileManager.default.fileExists(atPath: localConfig.path) {
            return LoadedConfig(config: try decode(localConfig), source: localConfig.path)
        }

        return LoadedConfig(config: defaultReal(workingDirectory: workingDirectory), source: "built-in real defaults")
    }

    private static func decode(_ url: URL) throws -> AppConfig {
        let data = try Data(contentsOf: url)
        return try JSONDecoder().decode(AppConfig.self, from: data)
    }

    private static func defaultReal(workingDirectory: URL) -> AppConfig {
        AppConfig(
            targetApplicationNames: ["Helium"],
            targetBundleIdentifiers: ["net.imput.helium", "app.helium", "com.imput.helium"],
            audioSampleRate: 16_000,
            audioChannelCount: 1,
            showChineseSource: false,
            translatePartialResults: false,
            asrCommand: CommandSpec(
                argv: [
                    workingDirectory.appendingPathComponent(".build/debug/qwen3-asr-stdin").path,
                    "--model-dir",
                    NSHomeDirectory() + "/Library/Application Support/com.jasonchien.Voco/Qwen3Models/mlx-community_Qwen3-ASR-1.7B-8bit",
                    "--language",
                    "auto",
                    "--min-segment-seconds",
                    "1.1",
                    "--max-segment-seconds",
                    "3.2",
                    "--silence-seconds",
                    "0.28"
                ],
                shell: nil,
                environment: nil
            ),
            translatorCommand: CommandSpec(
                argv: [workingDirectory.appendingPathComponent("scripts/codex-translate-lines.sh").path],
                shell: nil,
                environment: nil
            )
        )
    }

    private static func value(after flag: String, in arguments: [String]) -> String? {
        guard let index = arguments.firstIndex(of: flag) else {
            return nil
        }
        let nextIndex = arguments.index(after: index)
        guard nextIndex < arguments.endIndex else {
            return nil
        }
        return arguments[nextIndex]
    }
}

struct LoadedConfig {
    var config: AppConfig
    var source: String
}

enum AppError: Error, LocalizedError {
    case invalidCommand(String)
    case processAlreadyRunning(String)
    case processNotRunning(String)
    case targetApplicationNotFound(names: [String], bundleIdentifiers: [String], available: [String])
    case noDisplayAvailable
    case audioFormatUnavailable
    case audioConversionFailed(String)

    var errorDescription: String? {
        switch self {
        case .invalidCommand(let message):
            return message
        case .processAlreadyRunning(let name):
            return "\(name) is already running."
        case .processNotRunning(let name):
            return "\(name) is not running."
        case let .targetApplicationNotFound(names, bundleIdentifiers, available):
            return "找不到目標 App。names=\(names.joined(separator: ",")) bundleIDs=\(bundleIdentifiers.joined(separator: ",")) available=\(available.joined(separator: ", "))"
        case .noDisplayAvailable:
            return "ScreenCaptureKit did not return any display."
        case .audioFormatUnavailable:
            return "Could not read audio format from ScreenCaptureKit sample buffer."
        case .audioConversionFailed(let message):
            return "Audio conversion failed: \(message)"
        }
    }
}
