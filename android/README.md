# Maya — Android monitor

A dependency-free Android app (plain `Activity`, `HttpURLConnection`,
`org.json`) for watching a running Maya agent: equity and state, the open
position, this run's net PnL and fees, recent trades, the scanner's last scan,
and a **Halt** button. It cannot open, resume or close trades; halting stops
new trades only.

## Build

Open `android/` in Android Studio and Run, or with Gradle 8.11+ and the
Android SDK installed:

```bash
cd android && gradle :app:assembleDebug
adb install app/build/outputs/apk/debug/app-debug.apk
```

Android 7.0+ (minSdk 24). It is a debug build for sideloading onto your own
phone; a Play Store release would need your own signing key.

## Connect

Start the agent with `MAYA_API_ENABLED=true MAYA_API_HOST=0.0.0.0` and a
`MAYA_API_TOKEN` (see `docs/RUNNING.md`, "Watching from your phone"), then
enter the machine's LAN IP, port `8787` and the token. Connect checks the
token before saving anything. Traffic is plain HTTP, so only use it on a
network you trust.

The screens follow the same design and colour tokens as the iOS app
(`res/values/colors.xml` ↔ `ios/MayaMonitor.swiftpm/Theme.swift`), and
`tests/test_app_contract.py` fails if the app reads a field the API no longer
sends. Not compiled with the real Android SDK in this repository's CI.
