import Foundation

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
        DispatchQueue.main.async { self.isConnected = false }
    }

    func clear() {
        DispatchQueue.main.async { self.messages = [] }
    }

    private func stream() async {
        while !Task.isCancelled {
            DispatchQueue.main.async { self.isConnected = false }
            do {
                var request = URLRequest(url: url)
                request.timeoutInterval = .infinity
                let (bytes, _) = try await URLSession.shared.bytes(for: request)
                DispatchQueue.main.async { self.isConnected = true }

                var buffer = ""
                for try await byte in bytes {
                    if Task.isCancelled { return }
                    let ch = Character(UnicodeScalar(byte))
                    if ch == "\n" {
                        if let msg = LogMessage.parse(line: buffer) {
                            let captured = msg
                            DispatchQueue.main.async {
                                self.messages.append(captured)
                                if self.messages.count > 1000 {
                                    self.messages.removeFirst(self.messages.count - 1000)
                                }
                            }
                        }
                        buffer = ""
                    } else {
                        buffer.append(ch)
                    }
                }
            } catch {
                DispatchQueue.main.async { self.isConnected = false }
                if Task.isCancelled { return }
                try? await Task.sleep(nanoseconds: 3_000_000_000)
            }
        }
    }
}
