import XCTest

enum Typer {
  /// One `typeText` at HID rate, into an element that must already hold keyboard focus. Never a
  /// bare `app.typeText` into nothing: XCUITest records that as an issue, not as an error, so it
  /// would report success while poisoning the run (review C8).
  static func type(_ text: String, into element: XCUIElement) -> String {
    let started = Channel.milliseconds()
    element.typeText(text)
    return self.rate(characters: text.count, sinceMs: started)
  }

  static func focusedInput(in app: XCUIApplication) -> XCUIElement? {
    for query in [app.textFields, app.searchFields, app.textViews, app.secureTextFields] {
      let count = query.count
      for index in 0 ..< min(count, 12) {
        let candidate = query.element(boundBy: index)
        guard candidate.exists else { continue }
        if (candidate.value(forKey: "hasKeyboardFocus") as? Bool) == true { return candidate }
      }
    }
    return nil
  }

  /// The type-scoped base query is built ONCE and re-`matching`ed per key: rebuilding it per
  /// character cannot meet the 300 CPM gate (review finding 27).
  static func tapKeys(
    _ ids: [String],
    type: String?,
    in app: XCUIApplication,
    timeoutMs: Int
  ) throws -> String {
    let started = Channel.milliseconds()
    let base = ElementTypes.query(type ?? "button", in: app)
    for id in ids {
      // Stock keys carry a label and no identifier, and the label case flips with the shift
      // state, so a key id is matched against either attribute, case-insensitively.
      let matches = base.matching(NSPredicate(format: "identifier == %@ OR label ==[c] %@", id, id))
      guard let key = SelectorResolver.firstHittable(in: matches, timeoutMs: timeoutMs) else {
        throw StepFailure.failed(reason: "notFound", detail: "no hittable key '\(id)'")
      }
      key.tap()
    }
    return self.rate(characters: ids.count, sinceMs: started)
  }

  private static func rate(characters: Int, sinceMs: Int) -> String {
    let elapsed = max(1, Channel.milliseconds() - sinceMs)
    let cpm = Int((Double(characters) * 60000 / Double(elapsed)).rounded())
    return "cpm=\(cpm) chars=\(characters) ms=\(elapsed)"
  }
}
