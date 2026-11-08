import Foundation

enum StepFailure: Error {
  case failed(reason: String, detail: String)
}
