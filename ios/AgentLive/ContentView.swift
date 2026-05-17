import SwiftUI

struct ContentView: View {
    @StateObject private var client = SSEClient()
    @Environment(\.scenePhase) private var scenePhase
    @AppStorage("sseEndpoint") private var savedEndpoint: String = ""
    @State private var showEndpointSheet = false

    var body: some View {
        ZStack {
            Color(hex: "0A0A0A").ignoresSafeArea()
            VStack(spacing: 0) {
                header
                logView
            }
        }
        .preferredColorScheme(.dark)
        .onAppear {
            if savedEndpoint.isEmpty {
                showEndpointSheet = true
            } else {
                client.connect()
            }
        }
        .onDisappear { client.disconnect() }
        .onChange(of: scenePhase, perform: { phase in
            switch phase {
            case .background: client.didEnterBackground()
            case .active:     client.didEnterForeground()
            default: break
            }
        })
        .sheet(isPresented: $showEndpointSheet) {
            EndpointSheet(savedEndpoint: $savedEndpoint) {
                client.reconnect()
            }
            .interactiveDismissDisabled(savedEndpoint.isEmpty)
        }
    }

    // ── Header ────────────────────────────────────────────────────────────
    private var header: some View {
        HStack {
            HStack(spacing: 5) {
                Circle()
                    .fill(client.isConnected ? Color(hex: "00FF88") : Color(hex: "FF4D6A"))
                    .frame(width: 8, height: 8)
                Text(client.isConnected ? "live" : "offline")
                    .font(.system(size: 11, weight: .medium, design: .monospaced))
                    .foregroundColor(.white.opacity(0.5))
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            Text("🧠 Agent Live")
                .font(.system(size: 13, weight: .bold, design: .monospaced))
                .foregroundColor(Color(hex: "00FF88"))

            HStack(spacing: 12) {
                Button {
                    showEndpointSheet = true
                } label: {
                    Image(systemName: "link")
                        .font(.system(size: 11))
                        .foregroundColor(.white.opacity(0.35))
                }
                Button("clear") { client.clear() }
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundColor(.white.opacity(0.35))
            }
            .frame(maxWidth: .infinity, alignment: .trailing)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .background(Color(hex: "111111"))
        .overlay(
            Rectangle().frame(height: 1).foregroundColor(.white.opacity(0.08)),
            alignment: .bottom
        )
    }

    // ── Log ───────────────────────────────────────────────────────────────
    private var logView: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 2) {
                    ForEach(client.messages) { msg in
                        Text(msg.text)
                            .font(.system(size: 11, design: .monospaced))
                            .foregroundColor(msg.color)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .id(msg.id)
                    }
                    // Invisible anchor at the bottom for auto-scroll
                    Color.clear.frame(height: 1).id("bottom")
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 8)
            }
            .onChange(of: client.messages.count) { _ in
                withAnimation(.easeOut(duration: 0.15)) {
                    proxy.scrollTo("bottom")
                }
            }
        }
    }
}

#Preview {
    ContentView()
}
