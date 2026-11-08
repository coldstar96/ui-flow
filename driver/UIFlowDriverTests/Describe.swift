import Foundation

enum Describe {
  static func selector(_ selector: Selector) -> String {
    var parts: [String] = []
    if let app = selector.app { parts.append("app=\(app)") }
    if let type = selector.type { parts.append("type=\(type)") }
    if let id = selector.id { parts.append("id=\(id)") }
    if let prefix = selector.idPrefix { parts.append("idPrefix=\(prefix)") }
    if let label = selector.label { parts.append("label=\(label)") }
    if let fragment = selector.labelContains { parts.append("labelContains=\(fragment)") }
    if let options = selector.labelAnyOf { parts.append("labelAnyOf=\(options.joined(separator: "|"))") }
    if let options = selector.labelContainsAnyOf {
      parts.append("labelContainsAnyOf=\(options.joined(separator: "|"))")
    }
    if let value = selector.valueEquals { parts.append("valueEquals=\(value)") }
    if let index = selector.index { parts.append("index=\(index)") }
    if let inner = selector.descendant { parts.append("descendant{\(self.selector(inner))}") }
    return parts.joined(separator: " ")
  }
}
