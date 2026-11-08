import XCTest

final class StepExecutor {
  static let snapshotLimit = 800
  /// Every driver build is cached outside the checkout it was built from, so the host cannot infer
  /// what this one understands. An op it does not know is silently IGNORED, which is why the host
  /// must ask before it sends one.
  static let features = ["labelAnyOf", "labelContainsAnyOf"]

  private let issues: IssueRecorder
  private let channel: Channel
  private let request: BatchRequest
  let registry: AppRegistry

  init(issues: IssueRecorder, channel: Channel, request: BatchRequest, registry: AppRegistry) {
    self.issues = issues
    self.channel = channel
    self.request = request
    self.registry = registry
  }

  func run(_ step: Step, index: Int) throws -> StepResult {
    let started = Channel.milliseconds()
    let outcome = try self.perform(step)
    try self.requireNoIssues()
    return StepResult(
      index: index,
      op: step.op,
      ms: Channel.milliseconds() - started,
      ok: true,
      skipped: outcome.skipped,
      info: outcome.info,
      children: outcome.children
    )
  }

  func perform(_ step: Step) throws -> StepOutcome {
    switch step.op {
    case "ping":
      return StepOutcome(info: "features=" + StepExecutor.features.joined(separator: ","))
    case "sleep":
      Thread.sleep(forTimeInterval: Double(step.ms ?? 100) / 1000)
      return StepOutcome()
    case "launch":
      let bundleId = try self.bundleId(for: step)
      let app = self.registry.app(for: bundleId)
      app.launchArguments = step.args ?? []
      app.launch()
      self.registry.markCurrent(bundleId)
      guard app.wait(for: .runningForeground, timeout: self.seconds(step, fallback: 20)) else {
        throw StepFailure.failed(reason: "timedOut", detail: "\(bundleId) never foregrounded")
      }
      return StepOutcome(info: "state=\(app.state.rawValue)")
    case "activate":
      let bundleId = try self.bundleId(for: step)
      let app = self.registry.app(for: bundleId)
      app.activate()
      self.registry.markCurrent(bundleId)
      guard app.wait(for: .runningForeground, timeout: self.seconds(step, fallback: 20)) else {
        throw StepFailure.failed(reason: "timedOut", detail: "\(bundleId) never foregrounded")
      }
      return StepOutcome(info: "state=\(app.state.rawValue)")
    case "terminate":
      let bundleId = try self.bundleId(for: step)
      self.registry.app(for: bundleId).terminate()
      return StepOutcome()
    case "appState":
      let bundleId = try self.bundleId(for: step)
      let app = self.registry.app(for: bundleId)
      let wanted = StepExecutor.state(step.state)
      guard app.wait(for: wanted, timeout: self.seconds(step, fallback: 5)) else {
        throw StepFailure.failed(
          reason: "timedOut",
          detail: "\(bundleId) is state=\(app.state.rawValue), wanted \(step.state ?? "runningForeground")"
        )
      }
      if wanted == .runningForeground { self.registry.markCurrent(bundleId) }
      return StepOutcome(info: "state=\(app.state.rawValue)")
    case "tap", "doubleTap", "longPress":
      let (app, element) = try self.target(step, fallbackTimeoutMs: 4000, requireHittable: true)
      _ = app
      switch step.op {
      case "doubleTap": element.doubleTap()
      case "longPress": element.press(forDuration: Double(step.durationMs ?? 1000) / 1000)
      default: element.tap()
      }
      return StepOutcome()
    case "pinch", "rotate":
      // Two-finger gestures: `value` is the scale (pinch) or angle in radians (rotate).
      let (_, element) = try self.target(step, fallbackTimeoutMs: 4000, requireHittable: true)
      guard let amount = step.value.flatMap(Double.init) else {
        throw StepFailure.failed(reason: "badStep", detail: "\(step.op) needs a numeric value")
      }
      if step.op == "pinch" {
        element.pinch(withScale: CGFloat(amount), velocity: amount >= 1 ? 1 : -1)
      } else {
        element.rotate(CGFloat(amount), withVelocity: amount >= 0 ? 1 : -1)
      }
      return StepOutcome()
    case "tapIfPresent":
      guard let selector = step.sel else {
        throw StepFailure.failed(reason: "badStep", detail: "tapIfPresent needs a sel")
      }
      let app = try self.app(for: step, selector: selector)
      guard let found = SelectorResolver.resolve(
        selector,
        in: app,
        timeoutMs: self.timeout(step, fallback: 800),
        requireHittable: true
      ) else {
        return StepOutcome(skipped: true, info: "absent")
      }
      found.element.tap()
      return StepOutcome(info: found.info)
    case "tapAt":
      let app = try self.app(for: step, selector: step.sel)
      let base: XCUIElement
      if let selector = step.sel {
        // Deliberately NOT hittable-gated: the row whose centre is a dead label is exactly the
        // case a normalized offset exists to reach (review C5).
        guard let found = SelectorResolver.resolve(
          selector,
          in: app,
          timeoutMs: self.timeout(step, fallback: 4000),
          requireHittable: false
        ) else {
          throw StepFailure.failed(reason: "notFound", detail: Describe.selector(selector))
        }
        base = found.element
      } else {
        base = app
      }
      let point = base.coordinate(withNormalizedOffset: CGVector(dx: step.dx ?? 0.5, dy: step.dy ?? 0.5))
      // Custom keyboards vend unlabeled controls whose only long-press affordance is a coordinate,
      // so `tapAt` doubles as the press primitive when `durationMs` is given.
      if let duration = step.durationMs {
        point.press(forDuration: Double(duration) / 1000)
      } else {
        point.tap()
      }
      return StepOutcome()
    case "scrollTo":
      return try self.scrollTo(step)
    case "setSwitch":
      return try self.setSwitch(step)
    case "adjust":
      guard let value = step.value else {
        throw StepFailure.failed(reason: "badStep", detail: "adjust needs a value")
      }
      let (_, element) = try self.target(step, fallbackTimeoutMs: 4000, requireHittable: true)
      element.adjust(toPickerWheelValue: value)
      return StepOutcome(info: "value=\(element.value.map { "\($0)" } ?? "")")
    case "assertValue":
      guard let selector = step.sel, let wanted = step.equals else {
        throw StepFailure.failed(reason: "badStep", detail: "assertValue needs a sel and equals")
      }
      let app = try self.app(for: step, selector: selector)
      // Same guard as `assert`: a value read from a backgrounded app answers from a stale tree and
      // takes tens of seconds doing it, so a precondition must read "not satisfied" immediately.
      guard app.state == .runningForeground else {
        throw StepFailure.failed(
          reason: "hostNotForeground",
          detail: "\(self.registry.bundleId(of: app)) is state=\(app.state.rawValue)"
        )
      }
      let valued = Selector(
        app: selector.app,
        type: selector.type,
        id: selector.id,
        idPrefix: selector.idPrefix,
        label: selector.label,
        labelContains: selector.labelContains,
        labelAnyOf: selector.labelAnyOf,
        labelContainsAnyOf: selector.labelContainsAnyOf,
        valueEquals: wanted,
        index: selector.index,
        descendant: selector.descendant
      )
      if SelectorResolver.resolve(
        valued,
        in: app,
        timeoutMs: self.timeout(step, fallback: 2000),
        requireHittable: false
      ) != nil {
        return StepOutcome(info: "value=\(wanted)")
      }
      let observed = SelectorResolver.resolve(selector, in: app, timeoutMs: 0, requireHittable: false)
      let actual = observed?.element.value.map { "\($0)" } ?? "<no match>"
      throw StepFailure.failed(
        reason: "notFound",
        detail: "\(Describe.selector(selector)) value=\(actual) wanted=\(wanted)"
      )
    case "type":
      guard let text = step.text else {
        throw StepFailure.failed(reason: "badStep", detail: "type needs text")
      }
      let app = try self.app(for: step, selector: step.sel)
      let field: XCUIElement
      if let selector = step.sel {
        guard let found = SelectorResolver.resolve(
          selector,
          in: app,
          timeoutMs: self.timeout(step, fallback: 4000),
          requireHittable: true
        ) else {
          throw StepFailure.failed(reason: "notFound", detail: Describe.selector(selector))
        }
        field = found.element
      } else {
        guard let focused = Typer.focusedInput(in: app) else {
          throw StepFailure.failed(reason: "notFound", detail: "no input holds keyboard focus")
        }
        field = focused
      }
      return StepOutcome(info: Typer.type(text, into: field))
    case "typeKeys":
      guard let ids = step.ids, !ids.isEmpty else {
        throw StepFailure.failed(reason: "badStep", detail: "typeKeys needs ids")
      }
      let app = try self.app(for: step, selector: step.sel)
      let info = try Typer.tapKeys(
        ids,
        type: step.sel?.type,
        in: app,
        timeoutMs: self.timeout(step, fallback: 4000)
      )
      return StepOutcome(info: info)
    case "screenshot":
      // NEVER a bare `XCUIApplication()`: this bundle has no target app, so that instance has no
      // process behind it and taking its screenshot kills the long-lived session (review C13).
      let image: XCUIScreenshot
      if let bundleId = step.app ?? step.sel?.app {
        let app = self.registry.app(for: bundleId)
        image = app.state == .runningForeground ? app.screenshot() : XCUIScreen.main.screenshot()
      } else {
        image = XCUIScreen.main.screenshot()
      }
      let url = self.channel.directory.appendingPathComponent("shot-\(Channel.milliseconds()).png")
      try? image.pngRepresentation.write(to: url, options: .atomic)
      return StepOutcome(info: "path=\(url.path)")
    case "group":
      return try GroupExecutor(executor: self).group(step)
    case "repeatUntil":
      return try GroupExecutor(executor: self).repeatUntil(step)
    case "firstMatch":
      return try GroupExecutor(executor: self).firstMatch(step)
    case "swipe":
      let app = try self.app(for: step, selector: step.sel)
      let target: XCUIElement
      if let selector = step.sel {
        guard let found = SelectorResolver.resolve(
          selector,
          in: app,
          timeoutMs: self.timeout(step, fallback: 2000),
          requireHittable: true
        ) else {
          throw StepFailure.failed(reason: "notFound", detail: Describe.selector(selector))
        }
        target = found.element
      } else {
        target = app
      }
      switch step.direction ?? "up" {
      case "down": target.swipeDown()
      case "left": target.swipeLeft()
      case "right": target.swipeRight()
      default: target.swipeUp()
      }
      return StepOutcome()
    case "waitFor", "assert":
      return try self.assertStep(step)
    case "snapshot":
      let apps: [(String, XCUIApplication)]
      if let bundleId = step.app ?? step.sel?.app {
        apps = [(bundleId, self.registry.app(for: bundleId))]
      } else {
        apps = self.registry.running()
      }
      let lines = Snapshot.lines(for: apps, limit: StepExecutor.snapshotLimit)
      return StepOutcome(info: lines.joined(separator: "\n"))
    case "orientation":
      let orientations: [String: UIDeviceOrientation] = [
        "portrait": .portrait, "portraitUpsideDown": .portraitUpsideDown,
        "landscapeLeft": .landscapeLeft, "landscapeRight": .landscapeRight,
      ]
      guard let value = step.value, let orientation = orientations[value] else {
        throw StepFailure.failed(reason: "badStep", detail: "orientation needs a value in \(orientations.keys.sorted())")
      }
      XCUIDevice.shared.orientation = orientation
      return StepOutcome(info: "orientation=\(value)")
    case "screen":
      let springboard = self.registry.app(for: "com.apple.springboard")
      let frame = springboard.frame
      guard frame.width > 0, frame.height > 0 else {
        throw StepFailure.failed(reason: "noScreen", detail: "springboard reports an empty frame")
      }
      let size: [String: Double] = ["w": Double(frame.width), "h": Double(frame.height)]
      guard let data = try? JSONSerialization.data(withJSONObject: size),
            let line = String(data: data, encoding: .utf8) else {
        throw StepFailure.failed(reason: "noScreen", detail: "could not encode the frame")
      }
      return StepOutcome(info: line)
    default:
      throw StepFailure.failed(reason: "unknownOp", detail: step.op)
    }
  }

  func assertStep(_ step: Step) throws -> StepOutcome {
    guard let selector = step.sel else {
      throw StepFailure.failed(reason: "badStep", detail: "\(step.op) needs a sel")
    }
    let app = try self.app(for: step, selector: selector)
    // A query against a backgrounded app comes back empty, not as an error, so `present: false`
    // would pass for the wrong reason and silently skip a group that had to run (review C-design 8).
    guard app.state == .runningForeground else {
      throw StepFailure.failed(
        reason: "hostNotForeground",
        detail: "\(self.registry.bundleId(of: app)) is state=\(app.state.rawValue)"
      )
    }
    let timeoutMs = self.timeout(step, fallback: step.op == "assert" ? 0 : 2000)
    if step.present == false {
      guard SelectorResolver.absent(selector, in: app, timeoutMs: timeoutMs) else {
        throw StepFailure.failed(reason: "stillPresent", detail: Describe.selector(selector))
      }
      return StepOutcome(info: "absent")
    }
    guard let found = SelectorResolver.resolve(
      selector,
      in: app,
      timeoutMs: timeoutMs,
      requireHittable: step.hittable ?? false
    ) else {
      throw StepFailure.failed(reason: "notFound", detail: Describe.selector(selector))
    }
    return StepOutcome(info: found.info)
  }

  private func target(
    _ step: Step,
    fallbackTimeoutMs: Int,
    requireHittable: Bool
  ) throws -> (XCUIApplication, XCUIElement) {
    guard let selector = step.sel else {
      throw StepFailure.failed(reason: "badStep", detail: "\(step.op) needs a sel")
    }
    let app = try self.app(for: step, selector: selector)
    guard let found = SelectorResolver.resolve(
      selector,
      in: app,
      timeoutMs: self.timeout(step, fallback: fallbackTimeoutMs),
      requireHittable: requireHittable
    ) else {
      throw StepFailure.failed(reason: "notFound", detail: Describe.selector(selector))
    }
    return (app, found.element)
  }

  /// Bounded by swipe COUNT, as the kit is, never by a wall clock — and it gives up as soon as two
  /// swipes leave the match set unchanged (review finding 22).
  private func scrollTo(_ step: Step) throws -> StepOutcome {
    guard let selector = step.sel else {
      throw StepFailure.failed(reason: "badStep", detail: "scrollTo needs a sel")
    }
    let app = try self.app(for: step, selector: selector)
    let maxSwipes = step.maxSwipes ?? 8
    let container = SelectorResolver.probe(Selector(type: "table"), scope: app, requireHittable: true)?.element
      ?? SelectorResolver.probe(Selector(type: "collectionView"), scope: app, requireHittable: true)?.element
      ?? SelectorResolver.probe(Selector(type: "scrollView"), scope: app, requireHittable: true)?.element
      ?? app
    var lastCount = -1
    var stalled = 0
    for swipe in 0 ... maxSwipes {
      if let found = SelectorResolver.probe(selector, scope: app, requireHittable: true) {
        return StepOutcome(info: "swipes=\(swipe) \(found.info)")
      }
      let count = SelectorResolver.count(selector, scope: app)
      stalled = count == lastCount ? stalled + 1 : 0
      lastCount = count
      if stalled >= 2 {
        throw StepFailure.failed(
          reason: "notFound",
          detail: "\(Describe.selector(selector)) — two swipes left the match set at \(count)"
        )
      }
      if swipe == maxSwipes { break }
      if step.direction == "down" { container.swipeDown() } else { container.swipeUp() }
    }
    throw StepFailure.failed(
      reason: "notFound",
      detail: "\(Describe.selector(selector)) after \(maxSwipes) swipes"
    )
  }

  /// The outer row is a dead label; the real control is the descendant switch, and the same
  /// element is read and tapped so the two can never disagree and toggle the setting back off.
  private func setSwitch(_ step: Step) throws -> StepOutcome {
    guard let selector = step.sel else {
      throw StepFailure.failed(reason: "badStep", detail: "setSwitch needs a sel")
    }
    let wanted = step.on ?? true
    let app = try self.app(for: step, selector: selector)
    let timeoutMs = self.timeout(step, fallback: 4000)
    guard let row = SelectorResolver.resolve(selector, in: app, timeoutMs: timeoutMs, requireHittable: false) else {
      throw StepFailure.failed(reason: "notFound", detail: Describe.selector(selector))
    }
    let toggle = SelectorResolver.probe(Selector(type: "switch"), scope: row.element, requireHittable: false)?
      .element ?? row.element
    if StepExecutor.isOn(toggle) == wanted {
      return StepOutcome(skipped: true, info: "already=\(wanted)")
    }
    if toggle.isHittable {
      toggle.tap()
    } else {
      row.element.coordinate(withNormalizedOffset: CGVector(dx: 0.95, dy: 0.5)).tap()
    }
    let deadline = Date().addingTimeInterval(Double(timeoutMs) / 1000)
    repeat {
      if StepExecutor.isOn(toggle) == wanted { return StepOutcome(info: "on=\(wanted)") }
      Thread.sleep(forTimeInterval: 0.25)
    } while Date() < deadline
    throw StepFailure.failed(
      reason: "timedOut",
      detail: "\(Describe.selector(selector)) stayed \(StepExecutor.isOn(toggle) ? "on" : "off")"
    )
  }

  /// A switch reports its state as a `String` on some controls and a `Bool` on others.
  static func isOn(_ element: XCUIElement) -> Bool {
    guard element.exists, let value = element.value else { return false }
    if let text = value as? String { return text == "1" || text.lowercased() == "true" }
    if let flag = value as? Bool { return flag }
    if let number = value as? NSNumber { return number.boolValue }
    return false
  }

  static func state(_ name: String?) -> XCUIApplication.State {
    switch name {
    case "notRunning": return .notRunning
    case "runningBackground": return .runningBackground
    case "runningBackgroundSuspended": return .runningBackgroundSuspended
    case "unknown": return .unknown
    default: return .runningForeground
    }
  }

  func app(for step: Step, selector: Selector?) throws -> XCUIApplication {
    try self.registry.resolve(selector?.app ?? step.app, defaultApp: self.request.defaultApp)
  }

  func bundleId(for step: Step) throws -> String {
    if let bundleId = step.app ?? step.sel?.app, !bundleId.isEmpty, bundleId != "frontmost" {
      return bundleId
    }
    if let bundleId = self.request.defaultApp ?? self.registry.current { return bundleId }
    throw StepFailure.failed(reason: "noApp", detail: "\(step.op) needs an app bundle id")
  }

  func timeout(_ step: Step, fallback: Int) -> Int {
    Int(Double(step.timeoutMs ?? fallback) * (self.request.pace ?? 1))
  }

  func seconds(_ step: Step, fallback: Int) -> TimeInterval {
    Double(self.timeout(step, fallback: fallback * 1000)) / 1000
  }

  var defaultApp: String? { self.request.defaultApp }

  /// A precondition probe that fails is not a defect, so the issues it recorded must not poison
  /// the next real step.
  func clearIssues() {
    _ = self.issues.drain()
  }

  func requireNoIssues() throws {
    let recorded = self.issues.drain()
    guard recorded.isEmpty else {
      throw StepFailure.failed(reason: "xctestFailure", detail: recorded.joined(separator: " | "))
    }
  }

  func stepError(_ error: Error, failedPath: [Int]) -> StepError {
    if let nested = error as? NestedStepFailure {
      return StepError(
        reason: nested.reason,
        detail: nested.detail,
        failedPath: failedPath + nested.path,
        snapshot: self.failureSnapshot(reason: nested.reason)
      )
    }
    if case let StepFailure.failed(reason, detail) = error {
      return StepError(
        reason: reason,
        detail: detail,
        failedPath: failedPath,
        snapshot: self.failureSnapshot(reason: reason)
      )
    }
    return StepError(reason: "badStep", detail: "\(error)", failedPath: failedPath, snapshot: [])
  }

  func journalLine(step: Step, seq: Int, index: Int, ok: Bool, ms: Int) -> String {
    var entry: [String: Any] = [
      "ts": Channel.milliseconds(),
      "seq": seq,
      "index": index,
      "op": step.op,
      "ok": ok,
      "ms": ms,
    ]
    if let encoded = try? JSONEncoder().encode(self.redacted(step)),
       let object = try? JSONSerialization.jsonObject(with: encoded) {
      entry["step"] = object
    }
    guard let data = try? JSONSerialization.data(withJSONObject: entry),
          let line = String(data: data, encoding: .utf8) else { return "{}" }
    return line
  }

  /// Secrets never reach disk: a `secureTextField` target journals only the length of what was
  /// typed, so an exploration that entered a password cannot persist it into a record.
  private func redacted(_ step: Step) -> Step {
    guard step.op == "type", let text = step.text, self.isSecure(step) else { return step }
    return Step(
      op: step.op,
      sel: step.sel,
      app: step.app,
      args: step.args,
      text: nil,
      ids: step.ids,
      present: step.present,
      hittable: step.hittable,
      equals: step.equals,
      value: "textLen=\(text.count)",
      state: step.state,
      dx: step.dx,
      dy: step.dy,
      timeoutMs: step.timeoutMs,
      direction: step.direction,
      durationMs: step.durationMs,
      on: step.on,
      ms: step.ms,
      maxCycles: step.maxCycles,
      maxSwipes: step.maxSwipes,
      name: step.name,
      sels: step.sels,
      satisfiedWhen: step.satisfiedWhen,
      steps: step.steps,
      verifies: step.verifies,
      reassert: step.reassert,
      until: step.until
    )
  }

  private func isSecure(_ step: Step) -> Bool {
    step.sel?.type == "secureTextField"
  }

  /// Only a resolution failure can be diagnosed from a snapshot; `badStep` and `unknownOp` must
  /// not pay for one.
  private func failureSnapshot(reason: String) -> [String] {
    guard ["notFound", "stillPresent", "timedOut"].contains(reason) else { return [] }
    return Snapshot.lines(for: self.registry.running(), limit: StepExecutor.snapshotLimit)
  }
}
