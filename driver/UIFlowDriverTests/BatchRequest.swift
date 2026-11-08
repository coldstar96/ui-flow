import Foundation

struct BatchRequest: Codable {
  let seq: Int
  let pace: Double?
  let defaultApp: String?
  let steps: [Step]
}
