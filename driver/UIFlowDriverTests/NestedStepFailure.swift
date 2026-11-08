import Foundation

/// A failure raised inside a composite step, carrying the path from that step down to the failing
/// leaf so the reply can report `failedPath` (review C2) — without it neither timing nor repair
/// has an index to work with.
struct NestedStepFailure: Error {
  let reason: String
  let detail: String
  let path: [Int]
  let children: [StepResult]
}
