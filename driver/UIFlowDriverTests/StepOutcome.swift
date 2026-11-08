import Foundation

struct StepOutcome {
  var skipped: Bool?
  var info: String?
  var children: [StepResult]?

  init(skipped: Bool? = nil, info: String? = nil, children: [StepResult]? = nil) {
    self.skipped = skipped
    self.info = info
    self.children = children
  }
}
