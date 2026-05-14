import Foundation

@MainActor
class SSEClient: ObservableObject {
    @Published var messages: [LogMessage] = []
    @Published var isConnected = false

    private let url = URL(string: "https://notify.bjjl.dev/receive")!
    private var streamTask: Task<Void, Never>?
    private var dataTask: URLSessionDataTask?
    private var lineBuffer = Data()

    func connect() {
        streamTask?.cancel()
        streamTask = Task { await stream() }
    }

    func disconnect() {
        streamTask?.cancel()
        dataTask?.cancel()
        isConnected = false
    }

    func clear() { messages = [] }

    private func stream() async {
        while !Task.isCancelled {
            isConnected = false
            lineBuffer = Data()
            do {
                try await openConnection()
            } catch {
                isConnected = false
                if Task.isCancelled { return }
                try? await Task.sleep(nanoseconds: 3_000_000_000)
            }
        }
    }

    // Uses URLSessionDataDelegate so didReceive(response:) fires as soon as
    // HTTP headers arrive — before any body bytes — regardless of server buffering.
    private func openConnection() async throws {
        var request = URLRequest(url: url)
        request.timeoutInterval = .infinity

        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (cont: CheckedContinuation<Void, Error>) in
                var settled = false
                func settle(_ r: Result<Void, Error>) {
                    guard !settled else { return }
                    settled = true
                    cont.resume(with: r)
                }

                let delegate = _Delegate(
                    onHeaders: { [weak self] in
                        Task { @MainActor in self?.isConnected = true }
                    },
                    onData: { [weak self] data in
                        Task { @MainActor in self?.ingest(data) }
                    },
                    onDone: { settle($0) }
                )
                let session = URLSession(configuration: .default, delegate: delegate,
                                        delegateQueue: nil)
                let task = session.dataTask(with: request)
                Task { @MainActor [weak self] in self?.dataTask = task }
                task.resume()
            }
        } onCancel: {
            Task { @MainActor [weak self] in self?.dataTask?.cancel() }
        }
    }

    private func ingest(_ data: Data) {
        for byte in data {
            if byte == UInt8(ascii: "\n") {
                let line = String(data: lineBuffer, encoding: .utf8) ?? ""
                lineBuffer = Data()
                if let msg = LogMessage.parse(line: line) {
                    messages.append(msg)
                    if messages.count > 1000 { messages.removeFirst(messages.count - 1000) }
                }
            } else {
                lineBuffer.append(byte)
            }
        }
    }
}

// URLSessionDataDelegate: didReceive(response:) fires on header receipt,
// before any body data, even when the server has output buffering enabled.
private class _Delegate: NSObject, URLSessionDataDelegate {
    private let onHeaders: () -> Void
    private let onData:    (Data) -> Void
    private let onDone:    (Result<Void, Error>) -> Void

    init(onHeaders: @escaping () -> Void,
         onData:    @escaping (Data) -> Void,
         onDone:    @escaping (Result<Void, Error>) -> Void) {
        self.onHeaders = onHeaders
        self.onData    = onData
        self.onDone    = onDone
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask,
                    didReceive response: URLResponse,
                    completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        onHeaders()
        completionHandler(.allow)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        // Also fire onHeaders on first data — covers servers/proxies that buffer
        // response headers until the first body chunk arrives.
        onHeaders()
        onData(data)
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        if let e = error { onDone(.failure(e)) } else { onDone(.success(())) }
    }
}
