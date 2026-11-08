import Foundation

struct StepResult: Codable {
  let index: Int
  let op: String
  let ms: Int
  let ok: Bool
  var skipped: Bool?
  var info: String?
  var children: [StepResult]?
}
