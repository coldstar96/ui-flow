import XCTest

/// Composites. Every one of them reports its inner `StepResult`s in `children` on BOTH the success
/// and the failure path, and a failure carries the path down to the failing leaf — without that,
/// host-side timing has no sample to learn from and repair has no index to patch (review C2).
///
/// Child index space, fixed here and relied on by both: a group's `steps` occupy 0 ..< steps.count,
/// its `verifies` continue from steps.count, and a `reassert` result carries index -1 because it is
/// not a step of the record.
final class GroupExecutor {
  private static let focusMovingOps: Set<String> = [
    "tap", "doubleTap", "tapAt", "tapIfPresent", "longPress", "pinch", "rotate", "launch", "activate",
  ]

  private let executor: StepExecutor

  init(executor: StepExecutor) {
    self.executor = executor
  }

  func group(_ step: Step) throws -> StepOutcome {
    let name = step.name ?? "group"
    if self.holds(step.satisfiedWhen) {
      return StepOutcome(skipped: true, info: "name=\(name) satisfied")
    }
    var children: [StepResult] = []
    let steps = step.steps ?? []
    for (index, inner) in steps.enumerated() {
      try self.append(inner, index: index, into: &children)
      if let reassert = step.reassert, GroupExecutor.focusMovingOps.contains(inner.op) {
        for check in reassert {
          try self.append(check, index: -1, into: &children)
        }
      }
    }
    for (offset, verify) in (step.verifies ?? []).enumerated() {
      try self.append(verify, index: steps.count + offset, into: &children)
    }
    return StepOutcome(info: "name=\(name) ran=\(children.count)", children: children)
  }

  /// One tap on the globe advances to whichever keyboard is next, not to the one wanted, so the
  /// step is re-resolved and retried up to `maxCycles` (a keyboard-switch loop).
  func repeatUntil(_ step: Step) throws -> StepOutcome {
    guard let until = step.until, !until.isEmpty else {
      throw StepFailure.failed(reason: "badStep", detail: "repeatUntil needs an until")
    }
    let maxCycles = step.maxCycles ?? 6
    let deadline = Date().addingTimeInterval(Double(self.executor.timeout(step, fallback: 30000)) / 1000)
    var children: [StepResult] = []
    var cycles = 0
    while true {
      if self.holds(until) {
        return StepOutcome(info: "cycles=\(cycles)", children: children)
      }
      if cycles >= maxCycles || Date() > deadline { break }
      for (index, inner) in (step.steps ?? []).enumerated() {
        try self.append(inner, index: index, into: &children)
      }
      cycles += 1
    }
    throw NestedStepFailure(
      reason: "timedOut",
      detail: "until never held, cycles=\(cycles)",
      path: [],
      children: children
    )
  }

  /// One batch probing several candidates, so a repair costs one round trip rather than one per
  /// candidate — and each probe gets an explicit budget instead of inheriting a learned one.
  func firstMatch(_ step: Step) throws -> StepOutcome {
    guard let selectors = step.sels, !selectors.isEmpty else {
      throw StepFailure.failed(reason: "badStep", detail: "firstMatch needs sels")
    }
    let timeoutMs = step.timeoutMs ?? 500
    for (index, selector) in selectors.enumerated() {
      let app = try self.executor.registry.resolve(
        selector.app ?? step.app,
        defaultApp: self.executor.defaultApp
      )
      if let found = SelectorResolver.resolve(
        selector,
        in: app,
        timeoutMs: timeoutMs,
        requireHittable: step.hittable ?? false
      ) {
        return StepOutcome(info: "index=\(index) \(found.info)")
      }
    }
    return StepOutcome(info: "index=-1")
  }

  /// ONE-SHOT, never a polling loop: eight groups polling 1500 ms each is 12 s of preconditions
  /// before any work happens (review finding 9).
  func holds(_ checks: [Step]?) -> Bool {
    guard let checks, !checks.isEmpty else { return false }
    for check in checks {
      do {
        _ = try self.executor.perform(check.withTimeoutMs(0))
      } catch {
        self.executor.clearIssues()
        return false
      }
    }
    self.executor.clearIssues()
    return true
  }

  private func append(_ step: Step, index: Int, into children: inout [StepResult]) throws {
    let started = Channel.milliseconds()
    do {
      children.append(try self.executor.run(step, index: index))
    } catch {
      children.append(
        StepResult(
          index: index,
          op: step.op,
          ms: Channel.milliseconds() - started,
          ok: false,
          skipped: nil,
          info: nil,
          children: (error as? NestedStepFailure)?.children
        )
      )
      throw GroupExecutor.nest(error, index: index, children: children)
    }
  }

  private static func nest(_ error: Error, index: Int, children: [StepResult]) -> NestedStepFailure {
    if let nested = error as? NestedStepFailure {
      return NestedStepFailure(
        reason: nested.reason,
        detail: nested.detail,
        path: [index] + nested.path,
        children: children
      )
    }
    if case let StepFailure.failed(reason, detail) = error {
      return NestedStepFailure(reason: reason, detail: detail, path: [index], children: children)
    }
    return NestedStepFailure(reason: "badStep", detail: "\(error)", path: [index], children: children)
  }
}
