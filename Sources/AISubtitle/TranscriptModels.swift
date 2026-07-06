import Foundation

struct BrowserContext {
    var url: String
    var title: String?
    var source: String
}

struct TranscriptEvent {
    var text: String
    var language: String?
    var isFinal: Bool

    static func parse(_ line: String) -> TranscriptEvent? {
        let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            return nil
        }

        guard let data = trimmed.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data),
              let dictionary = object as? [String: Any] else {
            return TranscriptEvent(text: trimmed, language: nil, isFinal: true)
        }

        let text = (dictionary["text"] as? String)
            ?? (dictionary["transcript"] as? String)
            ?? (dictionary["result"] as? String)
            ?? ""

        let language = (dictionary["language"] as? String)
            ?? (dictionary["lang"] as? String)
            ?? (dictionary["source_language"] as? String)

        let isFinal = (dictionary["is_final"] as? Bool)
            ?? (dictionary["isFinal"] as? Bool)
            ?? (dictionary["final"] as? Bool)
            ?? true

        let cleanText = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleanText.isEmpty else {
            return nil
        }

        return TranscriptEvent(text: cleanText, language: language, isFinal: isFinal)
    }

    func jsonLine(
        targetLanguage: String = "zh-Hant-TW",
        id: Int? = nil,
        issuedAt: TimeInterval? = nil,
        browserContext: BrowserContext? = nil
    ) -> String {
        var object: [String: Any] = [
            "text": text,
            "is_final": isFinal,
            "target_language": targetLanguage
        ]
        if let language {
            object["language"] = language
        }
        if let id {
            object["id"] = id
        }
        if let issuedAt {
            object["issued_at"] = issuedAt
        }
        if let browserContext {
            object["context_url"] = browserContext.url
            object["context_source"] = browserContext.source
            if let title = browserContext.title, !title.isEmpty {
                object["context_title"] = title
            }
        }

        guard let data = try? JSONSerialization.data(withJSONObject: object),
              let line = String(data: data, encoding: .utf8) else {
            return text
        }
        return line
    }
}

struct TranslationEvent {
    var text: String
    var id: Int?
    var usageDisplay: String?

    static func parse(_ line: String) -> TranslationEvent? {
        let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            return nil
        }

        guard let data = trimmed.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data),
              let dictionary = object as? [String: Any] else {
            return TranslationEvent(text: trimmed, id: nil, usageDisplay: nil)
        }

        let text = (dictionary["text"] as? String)
            ?? (dictionary["translation"] as? String)
            ?? (dictionary["result"] as? String)
            ?? ""
        let id = (dictionary["id"] as? Int)
            ?? (dictionary["source_id"] as? Int)
            ?? (dictionary["sequence"] as? Int)
        let usageDictionary = dictionary["usage"] as? [String: Any]
        let usageDisplay = (dictionary["usage_display"] as? String)
            ?? (usageDictionary?["display"] as? String)

        let cleanText = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleanText.isEmpty else {
            return nil
        }

        return TranslationEvent(text: cleanText, id: id, usageDisplay: usageDisplay)
    }
}

enum LanguageDecision {
    static func shouldTranslate(_ event: TranscriptEvent) -> Bool {
        if let language = event.language?.lowercased(), !language.isEmpty {
            return !isChineseLanguageCode(language)
        }

        return !looksLikeChineseText(event.text)
    }

    private static func isChineseLanguageCode(_ language: String) -> Bool {
        language == "zh"
            || language.hasPrefix("zh-")
            || language == "cmn"
            || language.hasPrefix("cmn-")
            || language == "yue"
            || language.hasPrefix("yue-")
            || language == "chinese"
            || language == "mandarin"
            || language == "traditionalchinese"
            || language == "simplifiedchinese"
            || language == "traditional chinese"
            || language == "simplified chinese"
    }

    private static func looksLikeChineseText(_ text: String) -> Bool {
        var hanCount = 0
        var latinCount = 0
        var kanaOrHangulCount = 0

        for scalar in text.unicodeScalars {
            switch scalar.value {
            case 0x4E00...0x9FFF, 0x3400...0x4DBF:
                hanCount += 1
            case 0x0041...0x005A, 0x0061...0x007A:
                latinCount += 1
            case 0x3040...0x30FF, 0xAC00...0xD7AF:
                kanaOrHangulCount += 1
            default:
                continue
            }
        }

        return hanCount > 0 && latinCount == 0 && kanaOrHangulCount == 0
    }
}
