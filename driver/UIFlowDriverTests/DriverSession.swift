import Foundation

final class DriverSession {
  let channel: Channel
  private let issues: IssueRecorder
  private let idleSeconds: TimeInterval
  private let maxSeconds: TimeInterval
  private let registry = AppRegistry()
  private var shouldQuit = false

  init(channel: Channel, issues: IssueRecorder, idleSeconds: TimeInterval, maxSeconds: TimeInterval) {
    self.channel = channel
    self.issues = issues
    self.idleSeconds = idleSeconds
    self.maxSeconds = maxSeconds
  }

  static func fromEnvironment(issues: IssueRecorder) -> DriverSession {
    let directory = self.environment("UIFLOW_CHANNEL_DIR") ?? NSTemporaryDirectory()
    let idle = TimeInterval(self.environment("UIFLOW_IDLE_SECONDS") ?? "") ?? 1800
    let max = TimeInterval(self.environment("UIFLOW_MAX_SECONDS") ?? "") ?? 7200
    return DriverSession(
      channel: Channel(directory: directory),
      issues: issues,
      idleSeconds: idle,
      maxSeconds: max
    )
  }

  /// `xcodebuild` forwards `TEST_RUNNER_<NAME>` into the runner as `<NAME>`; some hosts pass it
  /// through unstripped, so both spellings are accepted.
  static func environment(_ name: String) -> String? {
    let environment = ProcessInfo.processInfo.environment
    if let value = environment[name], !value.isEmpty { return value }
    if let value = environment["TEST_RUNNER_" + name], !value.isEmpty { return value }
    return nil
  }

  var channelPath: String { self.channel.path }

  func prepareChannel() {
    self.channel.discardPriorExchange()
  }

  func serve() {
    self.channel.writeReady()
    let hardDeadline = Date().addingTimeInterval(self.maxSeconds)
    var idleDeadline = Date().addingTimeInterval(self.idleSeconds)
    while Date() < hardDeadline, Date() < idleDeadline, !self.shouldQuit {
      self.channel.beat()
      if let request = self.channel.readRequest() {
        idleDeadline = Date().addingTimeInterval(self.idleSeconds)
        switch request {
        case let .success(batch):
          self.channel.write(reply: self.execute(batch))
        case let .failure(error):
          self.channel.write(reply: DriverSession.badRequestReply(error))
        }
        if self.shouldQuit { break }
      }
      RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.01))
    }
  }

  private static func badRequestReply(_ error: Error) -> BatchReply {
    var seq = -1
    var detail = "\(error)"
    if case let ChannelError.badRequest(badSeq, badDetail) = error {
      seq = badSeq
      detail = badDetail
    }
    return BatchReply(
      seq: seq,
      ok: false,
      results: [],
      error: StepError(reason: "badRequest", detail: detail, failedPath: [], snapshot: []),
      elapsedMs: 0
    )
  }

  private func execute(_ request: BatchRequest) -> BatchReply {
    let started = Channel.milliseconds()
    let executor = StepExecutor(
      issues: self.issues,
      channel: self.channel,
      request: request,
      registry: self.registry
    )
    var results: [StepResult] = []
    var failure: StepError?
    for (index, step) in request.steps.enumerated() {
      // A batch can run for many seconds; without a beat here the heartbeat goes stale mid-flight
      // and the host concludes the session died while it is working perfectly well.
      self.channel.beat()
      if step.op == "quit" {
        self.shouldQuit = true
        results.append(StepResult(index: index, op: "quit", ms: 0, ok: true, skipped: nil, info: nil, children: nil))
        self.channel.appendJournal(executor.journalLine(step: step, seq: request.seq, index: index, ok: true, ms: 0))
        break
      }
      let stepStarted = Channel.milliseconds()
      do {
        let result = try executor.run(step, index: index)
        results.append(result)
        self.channel.appendJournal(
          executor.journalLine(step: step, seq: request.seq, index: index, ok: true, ms: result.ms)
        )
      } catch {
        let ms = Channel.milliseconds() - stepStarted
        results.append(
          StepResult(
            index: index,
            op: step.op,
            ms: ms,
            ok: false,
            skipped: nil,
            info: nil,
            children: (error as? NestedStepFailure)?.children
          )
        )
        self.channel.appendJournal(
          executor.journalLine(step: step, seq: request.seq, index: index, ok: false, ms: ms)
        )
        failure = executor.stepError(error, failedPath: [index])
        break
      }
    }
    return BatchReply(
      seq: request.seq,
      ok: failure == nil,
      results: results,
      error: failure,
      elapsedMs: Channel.milliseconds() - started
    )
  }
}
