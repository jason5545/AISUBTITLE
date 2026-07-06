import Foundation

enum AppIdentifiers {
    static let subsystem = "com.jasonchien.AISubtitle"
    static let bundleID = "com.jasonchien.Voco"

    static var appSupportDirectory: URL {
        FileManager.default
            .urls(for: .applicationSupportDirectory, in: .userDomainMask)
            .first!
            .appendingPathComponent(bundleID, isDirectory: true)
    }
}
