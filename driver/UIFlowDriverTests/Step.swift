import Foundation

struct Step: Codable {
  let op: String
  let sel: Selector?
  let app: String?
  let args: [String]?
  let text: String?
  let ids: [String]?
  let present: Bool?
  let hittable: Bool?
  let equals: String?
  let value: String?
  let state: String?
  let dx: Double?
  let dy: Double?
  let timeoutMs: Int?
  let direction: String?
  let durationMs: Int?
  let on: Bool?
  let ms: Int?
  let maxCycles: Int?
  let maxSwipes: Int?
  let name: String?
  let sels: [Selector]?
  let satisfiedWhen: [Step]?
  let steps: [Step]?
  let verifies: [Step]?
  let reassert: [Step]?
  let until: [Step]?
}

extension Step {
  /// A precondition asks "is this true now", so every check is re-timed to a single probe.
  func withTimeoutMs(_ ms: Int) -> Step {
    Step(
      op: self.op,
      sel: self.sel,
      app: self.app,
      args: self.args,
      text: self.text,
      ids: self.ids,
      present: self.present,
      hittable: self.hittable,
      equals: self.equals,
      value: self.value,
      state: self.state,
      dx: self.dx,
      dy: self.dy,
      timeoutMs: ms,
      direction: self.direction,
      durationMs: self.durationMs,
      on: self.on,
      ms: self.ms,
      maxCycles: self.maxCycles,
      maxSwipes: self.maxSwipes,
      name: self.name,
      sels: self.sels,
      satisfiedWhen: self.satisfiedWhen,
      steps: self.steps,
      verifies: self.verifies,
      reassert: self.reassert,
      until: self.until
    )
  }
}
