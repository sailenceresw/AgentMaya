import SwiftUI

/// "iOS · Dashboard" in the design: equity and state, the open position, this
/// run's stats (fees broken out), recent trades, the last scan, and Halt.
struct DashboardView: View {
    @Bindable var model: MonitorModel
    @State private var confirmHalt = false
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        ScrollView {
            VStack(spacing: 12) {
                header
                if let snap = model.snapshot {
                    equityCard(snap)
                    positionCard(snap.position)
                    runCard(snap)
                    tradesCard
                    if let scan = snap.scan { scanCard(scan, config: snap.config) }
                    haltSection(snap.account)
                } else {
                    ProgressView().tint(Theme.textSecondary).padding(.top, 80)
                }
                footer
            }
            .padding(.horizontal, 16)
            .padding(.bottom, 16)
        }
        .background(Theme.canvas)
        .refreshable { await model.refresh() }
        // Poll only while this screen is visible and the app is in front.
        .onAppear { model.startPolling() }
        .onDisappear { model.stopPolling() }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { model.startPolling() } else { model.stopPolling() }
        }
        .alert("Halt trading?", isPresented: $confirmHalt) {
            Button("Cancel", role: .cancel) {}
            Button("Halt", role: .destructive) { Task { await model.halt() } }
        } message: {
            Text("The agent will stop opening new positions.\n\nThis does NOT close an open position — do that on the exchange.")
        }
        .alert("Halt failed", isPresented: Binding(
            get: { model.haltError != nil },
            set: { if !$0 { model.haltError = nil } })
        ) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(model.haltError ?? "")
        }
    }

    // MARK: - sections

    private var header: some View {
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text("Maya").font(.largeTitle.weight(.bold)).foregroundStyle(Theme.textPrimary)
                Text(model.snapshot?.config.summary ?? "connecting…")
                    .font(.footnote).foregroundStyle(Theme.textSecondary)
            }
            Spacer()
            Button { model.disconnect() } label: {
                Image(systemName: "gearshape")
                    .font(.title3)
                    .foregroundStyle(Theme.textPrimary)
                    .frame(width: 44, height: 44)
                    .background(Theme.surface, in: Circle())
            }
            .accessibilityLabel("Connection settings")
        }
        .padding(.top, 8)
        .padding(.bottom, 4)
    }

    private func equityCard(_ snap: Snapshot) -> some View {
        let acc = snap.account
        let pnlColor = acc.pnlUsdt > 0 ? Theme.positive : acc.pnlUsdt < 0 ? Theme.negative : Theme.textSecondary
        return Card("Equity") {
            HStack {
                Text(Format.money(acc.equity))
                    .font(.system(size: 34, weight: .bold))
                    .foregroundStyle(Theme.textPrimary)
                    .minimumScaleFactor(0.6).lineLimit(1)
                Spacer()
                StateBadge(state: acc.agentState)
            }
            Text("\(Format.signedMoney(acc.pnlUsdt))  (\(String(format: "%+.2f", acc.pnlPct))%) this run")
                .font(.subheadline.weight(.medium))
                .foregroundStyle(pnlColor)
            if !acc.haltReasons.isEmpty {
                Text(acc.haltReasons.joined(separator: " · "))
                    .font(.footnote).foregroundStyle(Theme.negative)
            }
        }
    }

    @ViewBuilder
    private func positionCard(_ pos: Position?) -> some View {
        Card("Open position") {
            if let pos {
                Text("\(pos.direction.uppercased())  \(pos.symbol) · \(pos.leverage)x")
                    .font(.headline).foregroundStyle(Theme.textPrimary)
                MetricRow(label: "Entry", value: Format.price(pos.entryPrice))
                MetricRow(label: pos.movedToBreakeven ? "Stop (breakeven)" : "Stop",
                          value: Format.price(pos.stopLoss), tint: Theme.negative)
                MetricRow(label: "Target", value: Format.price(pos.takeProfit), tint: Theme.positive)
                MetricRow(label: "Size", value: "\(String(format: "%.5g", pos.quantity)) · \(Format.money(pos.notional))")
                MetricRow(label: "Setup", value: pos.setup)
            } else {
                Text("Flat — no open position").foregroundStyle(Theme.textSecondary)
            }
        }
    }

    private func runCard(_ snap: Snapshot) -> some View {
        let run = snap.run, acc = snap.account
        return Card("This run") {
            MetricRow(label: "Trades", value: "\(run.trades)")
            if run.trades > 0 {
                MetricRow(label: "Win rate", value: Format.percent(run.winRate ?? 0))
                let pnl = run.totalPnlUsdt ?? 0
                MetricRow(label: "PnL (net)", value: Format.signedMoney(pnl),
                          tint: pnl >= 0 ? Theme.positive : Theme.negative)
                MetricRow(label: "Fees paid", value: Format.money(run.totalFees ?? 0), tint: Theme.warning)
                MetricRow(label: "Profit factor",
                          value: run.profitFactor.map { String(format: "%.2f", $0) } ?? "∞ (no losses)")
            }
            MetricRow(label: "Trades today", value: "\(acc.tradesToday)")
            MetricRow(label: "Loss streak", value: "\(acc.consecutiveLosses)")
        }
    }

    private var tradesCard: some View {
        Card("Recent trades") {
            if model.trades.isEmpty {
                Text("None yet").foregroundStyle(Theme.textSecondary)
            } else {
                ForEach(model.trades) { TradeRow(trade: $0) }
            }
        }
    }

    private func scanCard(_ scan: [ScanEntry], config: Config) -> some View {
        Card("Last scan") {
            if let fee = config.roundTripFeePct, let rr = config.minNetRr {
                Text("Fees \(String(format: "%.2f", fee))% round trip · needs net R:R ≥ \(String(format: "%.1f", rr))")
                    .font(.caption).foregroundStyle(Theme.textHint)
            }
            ForEach(scan.prefix(10)) { ScanRow(entry: $0) }
        }
    }

    private func haltSection(_ acc: Account) -> some View {
        let halted = acc.agentState == .halted
        return VStack(spacing: 8) {
            CapsuleButton(title: halted ? "System halted" : (model.isHalting ? "Halting…" : "Halt trading"),
                          color: Theme.negative, tinted: true, isBusy: model.isHalting) {
                confirmHalt = true
            }
            .disabled(halted)
            .opacity(halted ? 0.5 : 1)
            Text("Halting stops new trades. It does not close an open position — do that on the exchange.")
                .font(.footnote).foregroundStyle(Theme.textSecondary)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var footer: some View {
        Group {
            if let error = model.pollError {
                Text(error).foregroundStyle(Theme.warning)
            } else if let updated = model.lastUpdated {
                Text("Updated \(updated.formatted(date: .omitted, time: .standard)) · \(model.snapshot?.candlesProcessed ?? 0) candles")
                    .foregroundStyle(Theme.textHint)
            }
        }
        .font(.caption)
        .multilineTextAlignment(.center)
        .padding(.top, 4)
    }
}
