import Foundation

struct BatchReply: Codable {
  let seq: Int
  let ok: Bool
  let results: [StepResult]
  let error: StepError?
  let elapsedMs: Int
}
