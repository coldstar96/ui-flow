import XCTest

enum SelectorResolver {
  private static let candidateCap = 40

  /// Iterates to the first hittable match, never a fixed index: an off-screen cached view carries
  /// the same identifiers and taps on it land on a `{-1, -1}` hit point (review C4, `KeyGrid`).
  static func resolve(
    _ selector: Selector,
    in app: XCUIApplication,
    timeoutMs: Int,
    requireHittable: Bool
  ) -> Resolution? {
    self.resolve(selector, scope: app, timeoutMs: timeoutMs, requireHittable: requireHittable)
  }

  static func resolve(
    _ selector: Selector,
    scope: XCUIElementTypeQueryProvider,
    timeoutMs: Int,
    requireHittable: Bool
  ) -> Resolution? {
    let deadline = Date().addingTimeInterval(Double(max(0, timeoutMs)) / 1000)
    let untyped = ElementTypes.isUntyped(selector.type)
    repeat {
      let started = Date()
      if let found = self.probe(selector, scope: scope, requireHittable: requireHittable) { return found }
      // Paced so the poll does not starve the rebuild it is waiting on: the kit runs the identical
      // loop at 250 ms in eleven places. A bare `.any` query is never repeated at all.
      if untyped { return nil }
      let queryMs = Date().timeIntervalSince(started)
      let pace = queryMs > 0.1 ? 0.25 : max(0.02, queryMs)
      if Date().addingTimeInterval(pace) > deadline { break }
      Thread.sleep(forTimeInterval: pace)
    } while Date() < deadline
    return nil
  }

  static func firstHittable(in query: XCUIElementQuery, timeoutMs: Int) -> XCUIElement? {
    let deadline = Date().addingTimeInterval(Double(max(0, timeoutMs)) / 1000)
    repeat {
      let started = Date()
      let count = query.count
      for index in 0 ..< min(count, self.candidateCap) {
        let candidate = query.element(boundBy: index)
        if candidate.exists, candidate.isHittable { return candidate }
      }
      let queryMs = Date().timeIntervalSince(started)
      let pace = queryMs > 0.1 ? 0.25 : max(0.02, queryMs)
      if Date().addingTimeInterval(pace) > deadline { break }
      Thread.sleep(forTimeInterval: pace)
    } while Date() < deadline
    return nil
  }

  static func count(_ selector: Selector, scope: XCUIElementTypeQueryProvider) -> Int {
    self.query(selector, scope: scope).count
  }

  static func element(
    _ selector: Selector,
    in app: XCUIApplication,
    timeoutMs: Int,
    requireHittable: Bool
  ) -> XCUIElement? {
    self.resolve(selector, in: app, timeoutMs: timeoutMs, requireHittable: requireHittable)?.element
  }

  /// ONE-SHOT. A precondition asks "is this true now"; polling it costs 1.5 s per group before any
  /// work happens (review finding 9).
  static func matches(_ selector: Selector, in app: XCUIApplication) -> Bool {
    self.probe(selector, scope: app, requireHittable: false) != nil
  }

  static func absent(_ selector: Selector, in app: XCUIApplication, timeoutMs: Int) -> Bool {
    let deadline = Date().addingTimeInterval(Double(max(0, timeoutMs)) / 1000)
    repeat {
      let started = Date()
      if self.probe(selector, scope: app, requireHittable: false) == nil { return true }
      let queryMs = Date().timeIntervalSince(started)
      let pace = queryMs > 0.1 ? 0.25 : max(0.02, queryMs)
      if Date().addingTimeInterval(pace) > deadline { break }
      Thread.sleep(forTimeInterval: pace)
    } while Date() < deadline
    return self.probe(selector, scope: app, requireHittable: false) == nil
  }

  static func probe(
    _ selector: Selector,
    scope: XCUIElementTypeQueryProvider,
    requireHittable: Bool
  ) -> Resolution? {
    if let inner = selector.descendant {
      guard let outer = self.probe(selector.stripped(), scope: scope, requireHittable: false) else { return nil }
      return self.probe(inner, scope: outer.element, requireHittable: requireHittable)
    }
    let query = self.query(selector, scope: scope)
    let count = query.count
    guard count > 0 else { return nil }
    var qualifying = 0
    for index in 0 ..< min(count, self.candidateCap) {
      let candidate = query.element(boundBy: index)
      guard candidate.exists else { continue }
      if let wanted = selector.valueEquals, !self.valueMatches(candidate, wanted) { continue }
      if requireHittable, !candidate.isHittable { continue }
      if let ordinal = selector.index {
        if qualifying == ordinal { return Resolution(element: candidate, index: index, candidates: count) }
        qualifying += 1
        continue
      }
      return Resolution(element: candidate, index: index, candidates: count)
    }
    return nil
  }

  /// `value` is a `String` for most controls and a `Bool` for a switch, and the two must never
  /// disagree — reading only one is how a Full Access grant gets toggled back off (review C5).
  static func valueMatches(_ element: XCUIElement, _ wanted: String) -> Bool {
    guard let value = element.value else { return wanted.isEmpty }
    if let text = value as? String { return text == wanted }
    if let flag = value as? Bool { return (flag ? "1" : "0") == wanted || "\(flag)" == wanted }
    if let number = value as? NSNumber { return number.stringValue == wanted }
    return "\(value)" == wanted
  }

  static func query(_ selector: Selector, scope: XCUIElementTypeQueryProvider) -> XCUIElementQuery {
    var query = ElementTypes.query(selector.type, in: scope)
    var predicates: [NSPredicate] = []
    if let id = selector.id { predicates.append(NSPredicate(format: "identifier == %@", id)) }
    if let prefix = selector.idPrefix { predicates.append(NSPredicate(format: "identifier BEGINSWITH %@", prefix)) }
    if let label = selector.label { predicates.append(NSPredicate(format: "label == %@", label)) }
    if let fragment = selector.labelContains {
      predicates.append(NSPredicate(format: "label CONTAINS[c] %@", fragment))
    }
    if let options = selector.labelAnyOf, !options.isEmpty {
      predicates.append(NSCompoundPredicate(orPredicateWithSubpredicates:
        options.map { NSPredicate(format: "label == %@", $0) }))
    }
    if let options = selector.labelContainsAnyOf, !options.isEmpty {
      predicates.append(NSCompoundPredicate(orPredicateWithSubpredicates:
        options.map { NSPredicate(format: "label CONTAINS[c] %@", $0) }))
    }
    if !predicates.isEmpty {
      query = query.matching(NSCompoundPredicate(andPredicateWithSubpredicates: predicates))
    }
    return query
  }
}

private extension Selector {
  func stripped() -> Selector {
    Selector(
      app: self.app,
      type: self.type,
      id: self.id,
      idPrefix: self.idPrefix,
      label: self.label,
      labelContains: self.labelContains,
      labelAnyOf: self.labelAnyOf,
      labelContainsAnyOf: self.labelContainsAnyOf,
      valueEquals: self.valueEquals,
      index: self.index,
      descendant: nil
    )
  }
}
