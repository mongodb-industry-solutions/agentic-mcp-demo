import SwiftUI

struct LogMessage: Identifiable {
    let id = UUID()
    let text: String
    let color: Color

    static func parse(line: String) -> LogMessage? {
        guard line.hasPrefix("data: ") else { return nil }
        let raw = String(line.dropFirst(6))
        let clean = stripANSI(raw)
        guard !clean.trimmingCharacters(in: .whitespaces).isEmpty else { return nil }
        return LogMessage(text: clean, color: colorFor(clean))
    }

    private static func stripANSI(_ s: String) -> String {
        s.replacingOccurrences(of: #"\x1B\[[0-9;]*[mGKHFJ]"#,
                               with: "", options: .regularExpression)
    }

    private static func colorFor(_ s: String) -> Color {
        if s.contains("[QUERY]")     { return Color(hex: "FFD700") }
        if s.contains("[RESULT]")    { return Color(hex: "00FF88") }
        if s.contains("[AGENT]")     { return Color(hex: "00D4FF") }
        if s.contains("[BOOTSTRAP]") { return Color(hex: "4D9FFF") }
        if s.contains("[ERROR]")     { return Color(hex: "FF4D6A") }
        return .white
    }
}

extension Color {
    init(hex: String) {
        let v = UInt64(hex, radix: 16) ?? 0
        let r = Double((v >> 16) & 0xFF) / 255
        let g = Double((v >>  8) & 0xFF) / 255
        let b = Double( v        & 0xFF) / 255
        self.init(red: r, green: g, blue: b)
    }
}
