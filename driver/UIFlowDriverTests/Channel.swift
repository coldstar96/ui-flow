import Foundation

final class Channel {
  private static let journalCapBytes = 4_000_000

  let directory: URL
  private var lastBeatMs = 0
  private var lastSeq = -1

  init(directory: String) {
    self.directory = URL(fileURLWithPath: directory, isDirectory: true)
    try? FileManager.default.createDirectory(at: self.directory, withIntermediateDirectories: true)
  }

  var path: String { self.directory.path }

  static func milliseconds() -> Int { Int(Date().timeIntervalSince1970 * 1000) }

  func writeReady() {
    let payload: [String: Any] = [
      "startedAt": Channel.milliseconds(),
      "pid": Int(ProcessInfo.processInfo.processIdentifier),
      "channel": self.path,
    ]
    let data = (try? JSONSerialization.data(withJSONObject: payload)) ?? Data("{}".utf8)
    self.writeAtomically(data, to: "ready")
  }

  func beat() {
    let now = Channel.milliseconds()
    guard now - self.lastBeatMs >= 1000 else { return }
    self.lastBeatMs = now
    self.writeAtomically(Data("\(now)".utf8), to: "heartbeat")
  }

  /// The app container `tmp` outlives the runner — it is migrated intact across reinstalls — so
  /// every file here is the previous session's until proven otherwise. An unanswered `quit` left in
  /// `cmd.json` would be read as this session's first command and end it in milliseconds, and a
  /// carried-over `ready` would let a host adopt this channel before it is being served.
  /// Retiring `ready` here is what makes its reappearance in `writeReady()` a real start barrier.
  func discardPriorExchange() {
    for name in ["cmd.json", "reply.json", "ready", "heartbeat"] {
      try? FileManager.default.removeItem(at: self.directory.appendingPathComponent(name))
    }
  }

  func readRequest() -> Result<BatchRequest, Error>? {
    let url = self.directory.appendingPathComponent("cmd.json")
    guard let data = try? Data(contentsOf: url) else { return nil }
    try? FileManager.default.removeItem(at: url)
    do {
      let request = try JSONDecoder().decode(BatchRequest.self, from: data)
      guard request.seq > self.lastSeq else { return nil }
      self.lastSeq = request.seq
      return .success(request)
    } catch {
      return .failure(ChannelError.badRequest(seq: Channel.lenientSeq(data), detail: "\(error)"))
    }
  }

  func write(reply: BatchReply) {
    guard let data = try? JSONEncoder().encode(reply) else { return }
    self.writeAtomically(data, to: "reply.json")
  }

  func appendJournal(_ line: String) {
    let url = self.directory.appendingPathComponent("journal.ndjson")
    let manager = FileManager.default
    if let size = (try? manager.attributesOfItem(atPath: url.path)[.size]) as? Int,
       size > Channel.journalCapBytes {
      let rolled = self.directory.appendingPathComponent("journal.1.ndjson")
      try? manager.removeItem(at: rolled)
      try? manager.moveItem(at: url, to: rolled)
    }
    let payload = Data((line + "\n").utf8)
    guard let handle = try? FileHandle(forWritingTo: url) else {
      try? payload.write(to: url, options: .atomic)
      return
    }
    defer { try? handle.close() }
    _ = try? handle.seekToEnd()
    try? handle.write(contentsOf: payload)
  }

  /// Never `removeItem` then `moveItem`: the gap is a window where the host reads `heartbeat` as
  /// missing on a healthy session and forks a duplicate driver (review C10).
  private func writeAtomically(_ data: Data, to name: String) {
    try? data.write(to: self.directory.appendingPathComponent(name), options: .atomic)
  }

  private static func lenientSeq(_ data: Data) -> Int {
    guard let text = String(data: data, encoding: .utf8) else { return -1 }
    guard let range = text.range(of: "\"seq\"") else { return -1 }
    let rest = text[range.upperBound...].drop { $0 == ":" || $0 == " " }
    let digits = rest.prefix { $0.isNumber || $0 == "-" }
    return Int(digits) ?? -1
  }
}
