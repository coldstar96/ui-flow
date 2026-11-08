import Foundation

/// A reference type only because `descendant` makes it recursive, which a Swift `struct` cannot be.
/// The JSON shape is unchanged: a nested object under `descendant`.
final class Selector: Codable {
  let app: String?
  let type: String?
  let id: String?
  let idPrefix: String?
  let label: String?
  let labelContains: String?
  let labelAnyOf: [String]?
  let labelContainsAnyOf: [String]?
  let valueEquals: String?
  let index: Int?
  let descendant: Selector?

  init(
    app: String? = nil,
    type: String? = nil,
    id: String? = nil,
    idPrefix: String? = nil,
    label: String? = nil,
    labelContains: String? = nil,
    labelAnyOf: [String]? = nil,
    labelContainsAnyOf: [String]? = nil,
    valueEquals: String? = nil,
    index: Int? = nil,
    descendant: Selector? = nil
  ) {
    self.app = app
    self.type = type
    self.id = id
    self.idPrefix = idPrefix
    self.label = label
    self.labelContains = labelContains
    self.labelAnyOf = labelAnyOf
    self.labelContainsAnyOf = labelContainsAnyOf
    self.valueEquals = valueEquals
    self.index = index
    self.descendant = descendant
  }
}
