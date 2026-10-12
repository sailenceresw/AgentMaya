import Foundation

// Decoding models for the agent's monitoring API (maya/api/server.py).
//
// Field names follow the JSON exactly (snake_case, converted by the decoder).
// Almost everything is optional on purpose: `run` is just `{"trades": 0}`
// before the first trade, `position` is null when flat, `profit_factor` is
// null when nothing has lost yet, and `scan` exists only when the scanner
// agent is running. A missing field must never stop the dashboard decoding.

struct Envelope<T: Decodable>: Decodable {
    let ok: Bool
    let data: T?
    let error: String?
}

struct Snapshot: Decodable {
    let config: Config
    let account: Account
    let position: Position?
    let run: RunStats
    let lifetime: RunStats?
    let candlesProcessed: Int?
    let scan: [ScanEntry]?
}

struct Config: Decodable {
    let mode: String
    let venue: String
    let symbol: String
    let timeframe: String
    let leverage: Int
    let agent: String?
    let roundTripFeePct: Double?
    let minNetRr: Double?

    /// "paper · hyperliquid · scanner · 1m · 20x"
    var summary: String {
        let subject = agent == "scanner" ? "scanner" : symbol
        return "\(mode) · \(venue) · \(subject) · \(timeframe) · \(leverage)x"
    }
}

struct Account: Decodable {
    let equity: Double
    let startEquity: Double
    let pnlUsdt: Double
    let pnlPct: Double
    let state: String
    let haltReasons: [String]
    let openPositions: Int
    let tradesToday: Int
    let consecutiveLosses: Int

    var agentState: AgentState { AgentState(rawValue: state) ?? .running }
}

enum AgentState: String {
    case running, cooldown, halted
}

struct Position: Decodable {
    let symbol: String
    let direction: String
    let setup: String
    let entryPrice: Double
    let stopLoss: Double
    let takeProfit: Double
    let quantity: Double
    let notional: Double
    let leverage: Int
    let movedToBreakeven: Bool
}

struct RunStats: Decodable {
    let trades: Int
    let winRate: Double?
    let totalPnlUsdt: Double?
    let totalFees: Double?
    /// null when there are no losing trades yet (the API sends null, not inf).
    let profitFactor: Double?
}

struct ScanEntry: Decodable, Identifiable {
    let coin: String
    let setup: String?
    let direction: String?
    let netRr: Double
    let feeSharePct: Double?
    let desk: String
    let status: String

    var id: String { coin }
    var isTrade: Bool { status == "trade" }
}

struct Trade: Decodable, Identifiable {
    let setupName: String
    let direction: String
    let pnlUsdt: Double
    let result: String
    let exitReason: String?
    // Double, not Int: Swift refuses to decode 1.5 as Int, and one odd row
    // must not blank the whole trades list.
    let timestampExit: Double

    var id: String { "\(Int64(timestampExit))-\(setupName)" }
}

enum API {
    static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()
}
