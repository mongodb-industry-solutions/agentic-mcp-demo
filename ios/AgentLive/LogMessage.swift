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
        let (text, color) = applyColor(to: clean)
        return LogMessage(text: text, color: color)
    }

    private static func stripANSI(_ s: String) -> String {
        s.replacingOccurrences(of: #"\x1B\[[0-9;]*[mGKHFJ]"#,
                               with: "", options: .regularExpression)
    }

    // Whitelist-only color palette. Names are case-folded to uppercase before
    // lookup; only ASCII letters/digits are accepted in the marker (see
    // extractColorMarker), so no path here is reachable with attacker-controlled
    // identifiers — the marker either matches a literal case in this switch or
    // is ignored. Keep this list short and explicit; do NOT replace with a
    // reflection-based lookup or accept user-supplied hex codes.
    private static func namedColor(for name: String) -> Color? {
        switch name {
        case "RED":          return Color(hex: "FF4D6A")
        case "GREEN":        return Color(hex: "00FF88")
        case "BLUE":         return Color(hex: "4D9FFF")
        case "CYAN":         return Color(hex: "00D4FF")
        case "YELLOW":       return Color(hex: "FFD700")
        case "ORANGE":       return Color(hex: "FF9F1C")
        case "PURPLE":       return Color(hex: "C77DFF")
        case "PINK":         return Color(hex: "FF8FAB")
        case "WHITE":        return Color(hex: "FFFFFF")
        case "GRAY", "GREY": return Color(hex: "808080")
        default:             return nil
        }
    }

    // Returns (text, color). If the string starts with a well-formed [_NAME_]
    // marker, the marker is stripped from the visible text and the matching
    // palette color is applied. Otherwise the original tag mapping is used.
    private static func applyColor(to s: String) -> (String, Color) {
        if let (name, end) = extractColorMarker(s), let color = namedColor(for: name) {
            var rest = s[end...]
            while rest.first == " " { rest = rest.dropFirst() }
            return (String(rest), color)
        }
        return (s, tagColor(s))
    }

    // Hardened marker extractor.
    //   • Must be at the literal start of the string (no embedded markers).
    //   • Name is 2..16 chars, ASCII uppercase letters or digits only.
    //   • Fails closed on any deviation — no partial matches, no fallback.
    private static func extractColorMarker(_ s: String) -> (name: String, end: String.Index)? {
        guard s.hasPrefix("[_") else { return nil }
        let nameStart = s.index(s.startIndex, offsetBy: 2)
        // Cap scan at 16 name chars + the trailing "_]" so a missing terminator
        // can't drag us through an arbitrarily long line.
        let scanEnd = s.index(nameStart, offsetBy: 18, limitedBy: s.endIndex) ?? s.endIndex
        var i = nameStart
        var name = ""
        while i < scanEnd {
            let c = s[i]
            if c == "_" {
                let after = s.index(after: i)
                guard after < s.endIndex, s[after] == "]" else { return nil }
                guard name.count >= 2 else { return nil }
                return (name, s.index(after: after))
            }
            guard c.isASCII, c.isLetter || c.isNumber else { return nil }
            name.append(c.uppercased())
            i = s.index(after: i)
            if name.count > 16 { return nil }
        }
        return nil
    }

    private static func tagColor(_ s: String) -> Color {
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
