# Maya — iOS monitor

A native SwiftUI app for watching a running Maya agent from an iPhone: equity
and state, the open position, this run's net PnL and fees, recent trades, the
scanner's last scan, and a **Halt** button. Like the Android app it cannot
open, resume or close trades — halting stops new trades only; close an open
position on the exchange.

`MayaMonitor.swiftpm` is an *app package*: one folder that both Xcode and
Swift Playgrounds open directly, with no `.xcodeproj` to keep in sync.

## Build

- **Xcode 15 or newer:** File → Open… → `ios/MayaMonitor.swiftpm`, pick your
  iPhone, set a team under Signing & Capabilities, Run.
- **Swift Playgrounds 4.4+ (iPad or Mac):** open the same folder and press Run.

Requires iOS 17 (it uses `@Observable`).

## Connect

1. Start the agent with the API on your LAN (see `docs/RUNNING.md`,
   "Watching from your phone"):
   `MAYA_API_ENABLED=true MAYA_API_HOST=0.0.0.0 MAYA_API_TOKEN=… python -m maya`
2. In the app enter the machine's **IP address** (e.g. `192.168.1.24`), port
   `8787` and the token, then Connect. The token is checked before the
   dashboard opens and is stored in the Keychain (this device only).
3. Allow **Local Network** access when iOS asks — without it every request
   fails. You can change it later in Settings → Privacy & Security → Local
   Network.

Use the IP address rather than a hostname: App Transport Security blocks
plain HTTP to ordinary host names, and Apple documents IP addresses and
`.local` names as exempt. That exemption is read from Apple's docs, not tested
on a device here; if iOS refuses, the app says it was blocked by App Transport
Security.

## Files

| File | What |
|---|---|
| `MayaApp.swift` | Entry point: Connect screen until connected, then the dashboard |
| `ConnectView.swift` / `DashboardView.swift` | The two screens from the Figma design |
| `Components.swift` | Card, metric/trade/scan rows, state badge, capsule button |
| `MonitorModel.swift` | Settings, 3 s polling while visible, halt |
| `APIClient.swift` | `/api/status`, `/api/trades`, `/api/halt`; Keychain token store |
| `Models.swift` | Decoding models — pinned to the API by `tests/test_app_contract.py` |
| `Theme.swift` | Colour tokens (shared with Android and Figma) and number formatting |

Not verified in this repository's CI: there is no Xcode there, so the app has
not been compiled or run on a device by CI. Build it once before relying on it.
