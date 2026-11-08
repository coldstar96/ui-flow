import XCTest

/// ONE `app.snapshot()` per app, walked in process. The v1 shape — seven full-hierarchy queries
/// plus five attribute round trips per element, against a measured 108 ms per query — turned the
/// hottest primitive into seconds.
enum Snapshot {
  /// Longer than this, an unidentified staticText is user content, not a label.
  static let proseCap = 40
  static let labelCap = 120

  static func lines(for apps: [(String, XCUIApplication)], limit: Int) -> [String] {
    var out: [String] = []
    for (bundleId, app) in apps {
      guard out.count < limit else { break }
      guard let root = try? app.snapshot() else { continue }
      self.walk(root, app: bundleId, parent: nil, into: &out, limit: limit)
    }
    return out
  }

  private static func walk(
    _ node: XCUIElementSnapshot,
    app: String,
    parent: CGRect?,
    into out: inout [String],
    limit: Int
  ) {
    guard out.count < limit else { return }
    if !self.isPassThrough(node, parent: parent), let line = self.line(node, app: app) {
      out.append(line)
    }
    for child in node.children {
      guard out.count < limit else { return }
      self.walk(child, app: app, parent: node.frame, into: &out, limit: limit)
    }
  }

  /// A container that carries no identifier, label or value and exactly covers its parent tells a
  /// selector nothing. Unlabeled elements with a frame of their own are kept: a custom-drawn key
  /// with no accessibility label is precisely what coordinate targeting needs.
  private static func isPassThrough(_ node: XCUIElementSnapshot, parent: CGRect?) -> Bool {
    guard node.elementType == .other || node.elementType == .window else { return false }
    guard node.identifier.isEmpty, node.label.isEmpty, node.value == nil else { return false }
    if node.frame.width == 0 || node.frame.height == 0 { return true }
    guard let parent else { return false }
    return node.frame == parent
  }

  private static func line(_ node: XCUIElementSnapshot, app: String) -> String? {
    let type = ElementTypes.name(node.elementType)
    if type == "application" { return nil }
    let frame = node.frame
    var object: [String: Any] = [
      "app": app,
      "type": type,
      "id": node.identifier,
      "frame": [
        self.round(frame.origin.x), self.round(frame.origin.y),
        self.round(frame.size.width), self.round(frame.size.height),
      ],
    ]
    var truncated = false
    // On-screen PROSE is the app's own content — clipboard and chat text would be harvested
    // wholesale and auto-persisted by repair (review finding 18). But a short unidentified label
    // is a control glyph or UI copy, and on an app with no identifiers it is the only handle
    // there is: redacting it blinds both label targeting and keyMap learning. Redact by length.
    if !node.label.isEmpty {
      if type == "staticText", node.identifier.isEmpty, node.label.count > self.proseCap {
        object["labelLen"] = node.label.count
      } else if node.label.count > self.labelCap {
        object["label"] = String(node.label.prefix(self.labelCap))
        truncated = true
      } else {
        object["label"] = node.label
      }
    }
    if let value = node.value {
      if type == "secureTextField" {
        object["valueLen"] = "\(value)".count
      } else {
        let text = "\(value)"
        if text.count > self.labelCap {
          object["value"] = String(text.prefix(self.labelCap))
          truncated = true
        } else if !text.isEmpty {
          object["value"] = text
        }
      }
    }
    object["truncated"] = truncated
    guard let data = try? JSONSerialization.data(withJSONObject: object),
          let line = String(data: data, encoding: .utf8) else { return nil }
    return line
  }

  private static func round(_ value: CGFloat) -> Double {
    (Double(value) * 10).rounded() / 10
  }
}
