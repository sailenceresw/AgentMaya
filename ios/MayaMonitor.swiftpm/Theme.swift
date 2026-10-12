import SwiftUI

/// The Maya colour tokens — the same values as the "Maya" variable collection
/// in the Figma file and the Android app, so the two platforms read alike.
enum Theme {
    static let canvas = Color(hex: 0x0B0F14)
    static let surface = Color(hex: 0x141B24)
    static let raised = Color(hex: 0x1B2430)
    static let border = Color(hex: 0x223041)
    static let textPrimary = Color(hex: 0xE6EDF3)
    static let textSecondary = Color(hex: 0x8B9BB0)
    static let textHint = Color(hex: 0x5C6B7F)
    static let positive = Color(hex: 0x3FB950)
    static let negative = Color(hex: 0xF85149)
    static let warning = Color(hex: 0xD29922)
    static let accent = Color(hex: 0x2F81F7)

    /// Card corner radius from the design.
    static let cardRadius: CGFloat = 20
}

extension Color {
    init(hex: UInt32) {
        self.init(
            .sRGB,
            red: Double((hex >> 16) & 0xFF) / 255,
            green: Double((hex >> 8) & 0xFF) / 255,
            blue: Double(hex & 0xFF) / 255,
            opacity: 1
        )
    }
}

/// Number formatting shared by every screen, matching the Android app:
/// money with a sign and two decimals, prices with thousands separators.
enum Format {
    private static let moneyFormatter: NumberFormatter = {
        let f = NumberFormatter()
        f.numberStyle = .decimal
        f.minimumFractionDigits = 2
        f.maximumFractionDigits = 2
        f.locale = Locale(identifier: "en_US")
        return f
    }()

    private static func grouped(_ value: Double, decimals: Int) -> String {
        let f = NumberFormatter()
        f.numberStyle = .decimal
        f.minimumFractionDigits = decimals
        f.maximumFractionDigits = decimals
        f.locale = Locale(identifier: "en_US")
        return f.string(from: NSNumber(value: value)) ?? String(format: "%.\(decimals)f", value)
    }

    /// `$1,036.42` / `-$4.12`
    static func money(_ value: Double) -> String {
        let body = moneyFormatter.string(from: NSNumber(value: abs(value))) ?? String(format: "%.2f", abs(value))
        return (value < 0 ? "-$" : "$") + body
    }

    /// `+$36.42` / `-$4.12` — for PnL, where the sign is the point.
    static func signedMoney(_ value: Double) -> String {
        value > 0 ? "+" + money(value) : money(value)
    }

    /// Prices: more decimals for cheap coins so a $0.0123 stop doesn't read as $0.01.
    static func price(_ value: Double) -> String {
        let decimals = value >= 1000 ? 1 : value >= 1 ? 2 : 5
        return grouped(value, decimals: decimals)
    }

    static func percent(_ value: Double) -> String {
        String(format: "%.1f%%", value)
    }
}
