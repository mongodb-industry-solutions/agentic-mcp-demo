import Foundation

@MainActor
class SSEClient: ObservableObject {
    @Published var messages: [LogMessage] = []
    @Published var isConnected = false

    private let url = URL(string: "https://notify.bjjl.dev/receive")!
    private var streamTask: Task<Void, Never>?

    func connect() {
        streamTask?.cancel()
        streamTask = Task { await self.stream() }
    }

    func disconnect() {
        streamTask?.cancel()
        isConnected = false
    }

    func clear() {
        messages = []
    }

    private func stream() async {
        while !Task.isCancelled {
            isConnected = false
            do {
                var request = URLRequest(url: url)
                request.timeoutInterval = .infinity
                let (bytes, _) = try await URLSession.shared.bytes(for: request)
                isConnected = true

                // Buffer raw bytes and split on 0x0A — decode each line as UTF-8
                // so that multi-byte characters (emoji, ✓, ○, …) are preserved.
                var lineBuffer = Data()
                for try await byte in bytes {
                    if Task.isCancelled { return }
                    if byte == UInt8(ascii: "\n") {
                        let line = String(data: lineBuffer, encoding: .utf8) ?? ""
                        lineBuffer = Data()
                        if let msg = LogMessage.parse(line: line) {
                            messages.append(msg)
                            if messages.count > 1000 {
                                messages.removeFirst(messages.count - 1000)
                            }
                        }
                    } else {
                        lineBuffer.append(byte)
                    }
                }
            } catch {
                isConnected = false
                if Task.isCancelled { return }
                try? await Task.sleep(nanoseconds: 3_000_000_000)
            }
        }
    }
}
