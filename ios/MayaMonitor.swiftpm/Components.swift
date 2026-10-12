import SwiftUI

// The reusable pieces of the design. Each matches a component in the Figma
// file's "Components" board (iOS column) so design and code stay one-to-one.

/// Rounded surface with an optional small-caps title.
struct Card<Content: View>: View {
    let title: String?
    @ViewBuilder var content: Content

    init(_ title: String? = nil, @ViewBuilder content: () -> Content) {
        self.title = title
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let title {
                Text(title.uppercased())
                    .font(.caption.weight(.semibold))
                    .tracking(0.7)
                    .foregroundStyle(Theme.textSecondary)
            }
            content
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.surface, in: RoundedRectangle(cornerRadius: Theme.cardRadius, style: .continuous))
    }
}

/// "iOS/Metric row": a label on the left, a value on the right.
struct MetricRow: View {
    let label: String
    let value: String
    var tint: Color = Theme.textPrimary

    var body: some View {
        HStack {
            Text(label).foregroundStyle(Theme.textSecondary)
            Spacer(minLength: 12)
            Text(value).fontWeight(.medium).foregroundStyle(tint)
                .lineLimit(1).truncationMode(.middle)
        }
        .font(.subheadline)
        .padding(.vertical, 4)
        .accessibilityElement(children: .combine)
    }
}

/// "iOS/Trade row": direction badge, setup, net PnL (green win / red loss).
struct TradeRow: View {
    let trade: Trade

    var body: some View {
        HStack(spacing: 10) {
            Text(trade.direction.uppercased())
                .font(.caption2.weight(.bold))
                .foregroundStyle(Theme.textSecondary)
                .padding(.horizontal, 6).padding(.vertical, 2)
                .background(Theme.raised, in: RoundedRectangle(cornerRadius: 6, style: .continuous))
            Text(trade.setupName)
                .font(.footnote)
                .foregroundStyle(Theme.textPrimary)
                .lineLimit(1).truncationMode(.tail)
            Spacer(minLength: 8)
            Text(Format.signedMoney(trade.pnlUsdt))
                .font(.footnote.weight(.semibold))
                .foregroundStyle(trade.pnlUsdt >= 0 ? Theme.positive : Theme.negative)
        }
        .padding(.vertical, 6)
    }
}

/// "iOS/Scan row": coin, setup direction, net R:R after fees, and why it was
/// traded or skipped.
struct ScanRow: View {
    let entry: ScanEntry

    var body: some View {
        HStack(spacing: 8) {
            Text(entry.coin).fontWeight(.bold).frame(width: 48, alignment: .leading)
            Text(entry.direction ?? "–").foregroundStyle(Theme.textSecondary)
                .frame(width: 44, alignment: .leading)
            Text(entry.setup == nil ? "" : String(format: "%.2f", entry.netRr))
                .fontWeight(.medium).frame(width: 40, alignment: .trailing)
            Text(entry.isTrade ? "TRADE" : entry.status)
                .fontWeight(entry.isTrade ? .bold : .regular)
                .foregroundStyle(entry.isTrade ? Theme.positive : Theme.textSecondary)
                .lineLimit(1).truncationMode(.tail)
            Spacer(minLength: 0)
        }
        .font(.footnote)
        .foregroundStyle(Theme.textPrimary)
        .padding(.vertical, 5)
    }
}

/// "iOS/State badge": running / cooldown / halted.
struct StateBadge: View {
    let state: AgentState

    private var color: Color {
        switch state {
        case .running: Theme.positive
        case .cooldown: Theme.warning
        case .halted: Theme.negative
        }
    }

    var body: some View {
        HStack(spacing: 6) {
            Circle().fill(color).frame(width: 8, height: 8)
            Text(state.rawValue.uppercased())
                .font(.caption2.weight(.bold))
                .foregroundStyle(color)
        }
        .padding(.leading, 10).padding(.trailing, 12).padding(.vertical, 5)
        .background(Theme.raised, in: Capsule())
        .accessibilityLabel("Agent \(state.rawValue)")
    }
}

/// Full-width capsule button used for Connect (filled) and Halt (tinted).
struct CapsuleButton: View {
    let title: String
    var color: Color = Theme.accent
    var tinted = false
    var isBusy = false
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 8) {
                if isBusy { ProgressView().tint(tinted ? color : .white) }
                Text(title).font(.body.weight(.semibold))
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 16)
            .foregroundStyle(tinted ? color : Theme.textPrimary)
            .background(tinted ? color.opacity(0.16) : color, in: Capsule())
        }
        .buttonStyle(.plain)
        .disabled(isBusy)
    }
}
