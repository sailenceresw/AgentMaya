import Foundation
import Observation

/// App state: connection settings, the latest snapshot, and the poll loop.
@MainActor
@Observable
final class MonitorModel {
    // Connection settings. Host and port are not secret; the token is (Keychain).
    var host: String = UserDefaults.standard.string(forKey: "host") ?? ""
    var port: String = UserDefaults.standard.string(forKey: "port") ?? "8787"
    var token: String = TokenStore.load()

    var isConnected = false
    var isConnecting = false
    var connectError: String?

    var snapshot: Snapshot?
    var trades: [Trade] = []
    var lastUpdated: Date?
    var pollError: String?
    var isHalting = false
    var haltError: String?

    /// Same cadence as the Android app.
    @ObservationIgnored private let pollInterval: Duration = .seconds(3)
    @ObservationIgnored private var pollTask: Task<Void, Never>?

    init() {
        isConnected = !host.isEmpty && !token.isEmpty
    }

    private var client: APIClient {
        APIClient(host: host.trimmingCharacters(in: .whitespaces),
                  port: port.trimmingCharacters(in: .whitespaces),
                  token: token.trimmingCharacters(in: .whitespaces))
    }

    /// Validates the address *and* the token before showing the dashboard,
    /// so a typo reports itself here instead of as an empty screen.
    func connect() async {
        guard !host.isEmpty, !token.isEmpty else {
            connectError = "Host and token are both required."
            return
        }
        isConnecting = true
        connectError = nil
        defer { isConnecting = false }
        do {
            snapshot = try await client.status()
            UserDefaults.standard.set(host, forKey: "host")
            UserDefaults.standard.set(port, forKey: "port")
            TokenStore.save(token)
            isConnected = true
            lastUpdated = .now
            startPolling()
        } catch {
            connectError = "Could not reach the agent at \(host):\(port).\n\n"
                + error.localizedDescription
                + "\n\nCheck the agent is running with MAYA_API_HOST=0.0.0.0 and that both devices are on the same Wi-Fi."
        }
    }

    func disconnect() {
        stopPolling()
        isConnected = false
    }

    /// Polls only while the dashboard is on screen (the view starts and stops
    /// this), so nothing runs in the background draining the battery.
    func startPolling() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh()
                try? await Task.sleep(for: self?.pollInterval ?? .seconds(3))
            }
        }
    }

    func stopPolling() {
        pollTask?.cancel()
        pollTask = nil
    }

    func refresh() async {
        do {
            async let status = client.status()
            async let recent = client.trades(limit: 6)
            snapshot = try await status
            trades = try await recent
            lastUpdated = .now
            pollError = nil
        } catch {
            pollError = error.localizedDescription
        }
    }

    func halt() async {
        isHalting = true
        defer { isHalting = false }
        do {
            try await client.halt()
            await refresh()
        } catch {
            haltError = error.localizedDescription
        }
    }
}
