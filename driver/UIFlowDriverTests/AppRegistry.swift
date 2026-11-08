import XCTest

/// XCUITest queries are only coherent on a reused `XCUIApplication` instance, and a snapshot must
/// cover every app the session has touched — a keyboard extension's elements are vended by the
/// HOST process, not by the app that owns the extension (review C7).
final class AppRegistry {
  private var apps: [String: XCUIApplication] = [:]
  private(set) var current: String?

  func app(for bundleId: String) -> XCUIApplication {
    if let existing = self.apps[bundleId] { return existing }
    let app = XCUIApplication(bundleIdentifier: bundleId)
    self.apps[bundleId] = app
    return app
  }

  func markCurrent(_ bundleId: String) {
    self.current = bundleId
    _ = self.app(for: bundleId)
  }

  func all() -> [(String, XCUIApplication)] {
    self.apps.keys.sorted().map { ($0, self.apps[$0]!) }
  }

  /// Snapshotting an app that is not on screen blocks until XCUITest gives up — a failure reply
  /// once took 92 s because a backgrounded app was still in the registry. Only live apps are worth
  /// describing, and `current` is kept even if it is mid-transition so a reply is never empty.
  func running() -> [(String, XCUIApplication)] {
    let live = self.all().filter { $0.1.state == .runningForeground }
    if !live.isEmpty { return live }
    guard let bundleId = self.current else { return [] }
    return [(bundleId, self.app(for: bundleId))]
  }

  func resolve(_ requested: String?, defaultApp: String?) throws -> XCUIApplication {
    if let bundleId = requested, bundleId != "frontmost", !bundleId.isEmpty {
      return self.app(for: bundleId)
    }
    if let bundleId = self.current ?? defaultApp {
      return self.app(for: bundleId)
    }
    throw StepFailure.failed(reason: "noApp", detail: "no app: pass sel.app, step.app or defaultApp")
  }

  func bundleId(of app: XCUIApplication) -> String {
    self.apps.first { $0.value === app }?.key ?? (self.current ?? "")
  }
}
