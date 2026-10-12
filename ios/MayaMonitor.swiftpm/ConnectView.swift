import SwiftUI

/// "iOS · Connect" in the design: large title, an inset grouped list for the
/// agent's address and token, setup instructions, and a Connect button.
struct ConnectView: View {
    @Bindable var model: MonitorModel
    @FocusState private var focused: Field?

    private enum Field { case host, port, token }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Connect to Maya")
                .font(.largeTitle.weight(.bold))
                .foregroundStyle(Theme.textPrimary)
                .padding(.top, 24)
            Text("Watch your trading agent from your phone. Both devices must be on the same Wi-Fi.")
                .foregroundStyle(Theme.textSecondary)

            VStack(spacing: 0) {
                row("Host") {
                    TextField("192.168.1.24", text: $model.host)
                        .keyboardType(.numbersAndPunctuation)
                        .focused($focused, equals: .host)
                }
                divider
                row("Port") {
                    TextField("8787", text: $model.port)
                        .keyboardType(.numberPad)
                        .focused($focused, equals: .port)
                }
                divider
                row("API token") {
                    SecureField("MAYA_API_TOKEN", text: $model.token)
                        .focused($focused, equals: .token)
                }
            }
            .background(Theme.surface, in: RoundedRectangle(cornerRadius: Theme.cardRadius, style: .continuous))
            .padding(.top, 8)

            Text("Start the agent with MAYA_API_ENABLED=true and MAYA_API_HOST=0.0.0.0. The token is MAYA_API_TOKEN from its .env.")
                .font(.footnote)
                .foregroundStyle(Theme.textSecondary)

            if let error = model.connectError {
                Text(error)
                    .font(.footnote)
                    .foregroundStyle(Theme.negative)
                    .padding(.top, 4)
            }

            Spacer()

            CapsuleButton(title: "Connect", isBusy: model.isConnecting) {
                focused = nil
                Task { await model.connect() }
            }
            .padding(.bottom, 8)
        }
        .padding(.horizontal, 16)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(Theme.canvas)
        .autocorrectionDisabled()
        .textInputAutocapitalization(.never)
    }

    private var divider: some View {
        Rectangle().fill(Theme.border).frame(height: 0.5).padding(.leading, 16)
    }

    private func row<Field: View>(_ label: String, @ViewBuilder field: () -> Field) -> some View {
        HStack(spacing: 12) {
            Text(label).foregroundStyle(Theme.textPrimary)
            field()
                .multilineTextAlignment(.trailing)
                .foregroundStyle(Theme.textSecondary)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 14)
    }
}
