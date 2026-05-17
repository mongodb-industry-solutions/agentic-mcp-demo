import SwiftUI

struct EndpointSheet: View {
    @Binding var savedEndpoint: String
    var onConnect: () -> Void

    @State private var draft: String = ""
    @FocusState private var fieldFocused: Bool
    @Environment(\.dismiss) private var dismiss

    var isFirstLaunch: Bool { savedEndpoint.isEmpty }
    var isValid: Bool {
        let trimmed = draft.trimmingCharacters(in: .whitespaces)
        guard let url = URL(string: trimmed),
              let scheme = url.scheme?.lowercased(),
              scheme == "http" || scheme == "https",
              let host = url.host, !host.isEmpty
        else { return false }
        return true
    }

    var body: some View {
        NavigationView {
            ZStack {
                Color(hex: "0A0A0A").ignoresSafeArea()
                VStack(alignment: .leading, spacing: 20) {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("SSE endpoint URL")
                            .font(.system(size: 12, weight: .medium, design: .monospaced))
                            .foregroundColor(.white.opacity(0.5))
                        TextField("https://…/receive", text: $draft)
                            .textFieldStyle(.plain)
                            .focused($fieldFocused)
                            .submitLabel(.go)
                            .onSubmit { if isValid { connect() } }
                            .font(.system(size: 13, design: .monospaced))
                            .foregroundColor(.white)
                            .autocorrectionDisabled()
                            .textInputAutocapitalization(.never)
                            .keyboardType(.URL)
                            .padding(.horizontal, 10)
                            .frame(height: 44)
                            .background(Color(hex: "1A1A1A"))
                            .cornerRadius(8)
                            .overlay(
                                RoundedRectangle(cornerRadius: 8)
                                    .stroke(Color.white.opacity(0.12), lineWidth: 1)
                            )
                    }

                    Button {
                        connect()
                    } label: {
                        Text("Connect")
                            .font(.system(size: 13, weight: .semibold, design: .monospaced))
                            .foregroundColor(Color(hex: "0A0A0A"))
                            .frame(maxWidth: .infinity)
                            .frame(height: 44)
                            .background(isValid ? Color(hex: "00FF88") : Color(hex: "00FF88").opacity(0.3))
                            .cornerRadius(8)
                    }
                    .disabled(!isValid)

                    Spacer()
                }
                .padding(20)
            }
            .navigationTitle(isFirstLaunch ? "Setup" : "Endpoint")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                if !isFirstLaunch {
                    ToolbarItem(placement: .navigationBarLeading) {
                        Button("Cancel") { dismiss() }
                            .font(.system(size: 13, design: .monospaced))
                            .foregroundColor(.white.opacity(0.5))
                    }
                }
            }
            .preferredColorScheme(.dark)
        }
        .onAppear {
            draft = savedEndpoint
            // Focus on next runloop tick — synchronous from onAppear is dropped
            // mid-presentation, longer delays cause a visible re-layout flicker.
            DispatchQueue.main.async { fieldFocused = true }
        }
    }

    private func connect() {
        savedEndpoint = draft.trimmingCharacters(in: .whitespaces)
        fieldFocused = false
        onConnect()
        dismiss()
    }
}
