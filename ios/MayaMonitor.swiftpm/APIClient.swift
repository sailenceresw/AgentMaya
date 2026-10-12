import Foundation
import Security

/// Talks to a running Maya agent's monitoring API over the local network.
///
/// Read-mostly by design, like the Android app: the only state-changing call
/// is `halt()`, which moves the agent to a safer state. There is no resume and
/// no way to open a trade from a phone.
struct APIClient {
    let host: String
    let port: String
    let token: String

    /// Short: the phone is on a radio and a stalled request must not wedge the poll.
    private static let timeout: TimeInterval = 5

    var baseURL: URL? { URL(string: "http://\(host):\(port)") }

    func status() async throws -> Snapshot {
        try await get("/api/status")
    }

    func trades(limit: Int = 6) async throws -> [Trade] {
        try await get("/api/trades?limit=\(limit)")
    }

    func halt(reason: String = "halted from iOS app") async throws {
        let body = try JSONSerialization.data(withJSONObject: ["reason": reason])
        let _: Snapshot = try await send("/api/halt", method: "POST", body: body)
    }

    // MARK: - transport

    private func get<T: Decodable>(_ path: String) async throws -> T {
        try await send(path, method: "GET", body: nil)
    }

    private func send<T: Decodable>(_ path: String, method: String, body: Data?) async throws -> T {
        guard let base = baseURL, let url = URL(string: path, relativeTo: base) else {
            throw MonitorError.badAddress
        }
        var request = URLRequest(url: url, timeoutInterval: Self.timeout)
        request.httpMethod = method
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        if let body {
            request.httpBody = body
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }

        let result: (Data, URLResponse)
        do {
            result = try await URLSession.shared.data(for: request)
        } catch let error as URLError {
            throw MonitorError.network(error)
        }
        let (data, response) = result

        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        if code == 401 { throw MonitorError.unauthorized }
        guard (200..<300).contains(code) else {
            throw MonitorError.http(code, String(data: data, encoding: .utf8) ?? "")
        }
        let envelope = try API.decoder.decode(Envelope<T>.self, from: data)
        guard envelope.ok, let payload = envelope.data else {
            throw MonitorError.http(code, envelope.error ?? "empty response")
        }
        return payload
    }
}

enum MonitorError: LocalizedError {
    case badAddress
    case unauthorized
    case network(URLError)
    case http(Int, String)

    /// Plain-language messages, the same wording as the Android app.
    var errorDescription: String? {
        switch self {
        case .badAddress:
            return "That host or port is not a valid address."
        case .unauthorized:
            return "Unauthorized — the API token is wrong."
        case .network(let error):
            switch error.code {
            case .cannotConnectToHost, .networkConnectionLost:
                return "Connection refused — is the agent running?"
            case .timedOut:
                return "Timed out — check the IP address."
            case .notConnectedToInternet:
                return "No network — connect to the same Wi-Fi as the agent."
            case .appTransportSecurityRequiresSecureConnection:
                return "iOS blocked the plain-HTTP connection. Connect by IP address (e.g. 192.168.1.24) rather than a hostname."
            default:
                return error.localizedDescription
            }
        case .http(let code, let body):
            return "HTTP \(code): \(body.prefix(160))"
        }
    }
}

/// The API token is a credential that can halt a trading system, so it lives
/// in the Keychain rather than in UserDefaults.
enum TokenStore {
    private static let service = "com.maya.monitor.api-token"

    static func load() -> String {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data else { return "" }
        return String(data: data, encoding: .utf8) ?? ""
    }

    static func save(_ token: String) {
        let base: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
        ]
        SecItemDelete(base as CFDictionary)
        var add = base
        add[kSecValueData as String] = Data(token.utf8)
        add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        SecItemAdd(add as CFDictionary, nil)
    }
}
