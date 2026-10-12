import SwiftUI

@main
struct MayaApp: App {
    @State private var model = MonitorModel()

    var body: some Scene {
        WindowGroup {
            Group {
                if model.isConnected {
                    DashboardView(model: model)
                } else {
                    ConnectView(model: model)
                }
            }
            .preferredColorScheme(.dark)
            .tint(Theme.accent)
        }
    }
}
