import Foundation

struct StepError: Codable {
  let reason: String
  let detail: String
  let failedPath: [Int]
  let snapshot: [String]
}
