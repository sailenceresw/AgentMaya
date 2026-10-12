// swift-tools-version: 5.9

// An iOS app package: open this folder in Xcode 15+ (File ▸ Open) or in Swift
// Playgrounds 4.4+ on a Mac/iPad. No .xcodeproj is needed; Xcode builds and
// runs it on a simulator or a device directly.

import PackageDescription
import AppleProductTypes

let package = Package(
    name: "MayaMonitor",
    platforms: [
        .iOS("17.0")
    ],
    products: [
        .iOSApplication(
            name: "Maya",
            targets: ["AppModule"],
            bundleIdentifier: "com.maya.monitor",
            teamIdentifier: "",
            displayVersion: "1.0",
            bundleVersion: "1",
            appIcon: .placeholder(icon: .chart),
            accentColor: .presetColor(.blue),
            supportedDeviceFamilies: [
                .phone
            ],
            supportedInterfaceOrientations: [
                .portrait
            ],
            capabilities: [
                // The agent runs on a machine on the same Wi-Fi; iOS asks the
                // user before an app may talk to devices on the local network.
                .localNetwork(purposeString: "Maya connects to your trading agent on this network.")
            ],
            appCategory: .finance
        )
    ],
    targets: [
        .executableTarget(
            name: "AppModule",
            path: "."
        )
    ]
)
