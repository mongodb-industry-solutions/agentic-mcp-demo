import SwiftUI
import WatchKit

struct ContentView: View {
    @StateObject private var client = SSEClient()
    @StateObject private var session = ExtendedSession()

    var body: some View {
        VStack(spacing: 0) {
            header
            logView
        }
        .background(Color(hex: "0A0A0A"))
        .onAppear  { client.connect(); session.start() }
        .onDisappear { client.disconnect(); session.stop() }
    }

    private var header: some View {
        HStack {
            Circle()
                .fill(client.isConnected ? Color(hex: "00FF88") : Color(hex: "FF4D6A"))
                .frame(width: 6, height: 6)
            Text(client.isConnected ? "live" : "offline")
                .font(.system(size: 9, weight: .medium, design: .monospaced))
                .foregroundColor(.white.opacity(0.5))
            Spacer()
            Button(action: { client.clear() }) {
                Image(systemName: "trash")
                    .font(.system(size: 9))
                    .foregroundColor(.white.opacity(0.35))
            }
            .buttonStyle(.plain)
        }
        .padding(.horizontal, 6)
        .padding(.vertical, 4)
        .background(Color(hex: "111111"))
    }

    private var logView: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 1) {
                    ForEach(client.messages) { msg in
                        Text(msg.text)
                            .font(.system(size: 9, design: .monospaced))
                            .foregroundColor(msg.color)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .fixedSize(horizontal: false, vertical: true)
                            .id(msg.id)
                    }
                    Color.clear.frame(height: 1).id("bottom")
                }
                .padding(.horizontal, 6)
                .padding(.vertical, 4)
            }
            .onChange(of: client.messages.count) {
                proxy.scrollTo("bottom", anchor: .bottom)
            }
        }
    }
}

@MainActor
class ExtendedSession: NSObject, ObservableObject, WKExtendedRuntimeSessionDelegate {
    private var session: WKExtendedRuntimeSession?

    func start() {
        guard session == nil else { return }
        let s = WKExtendedRuntimeSession()
        s.delegate = self
        s.start()
        session = s
    }

    func stop() {
        session?.invalidate()
        session = nil
    }

    nonisolated func extendedRuntimeSessionDidStart(_ session: WKExtendedRuntimeSession) {}
    nonisolated func extendedRuntimeSessionWillExpire(_ session: WKExtendedRuntimeSession) {}
    nonisolated func extendedRuntimeSession(_ session: WKExtendedRuntimeSession,
                                            didInvalidateWith reason: WKExtendedRuntimeSessionInvalidationReason,
                                            error: Error?) {}
}
