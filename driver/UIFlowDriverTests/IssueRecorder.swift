import Foundation
import XCTest

/// XCUITest reports a failed tap or an unfocused `typeText` as an `XCTIssue`, not as a Swift error,
/// so without this the driver answers `ok: true` while the run is already poisoned (review C8).
final class IssueRecorder: NSObject, XCTestObservation {
  private let lock = NSLock()
  private var buffer: [String] = []

  var issues: [String] {
    self.lock.lock()
    defer { self.lock.unlock() }
    return self.buffer
  }

  func drain() -> [String] {
    self.lock.lock()
    defer { self.lock.unlock() }
    let out = self.buffer
    self.buffer = []
    return out
  }

  func testCase(_: XCTestCase, didRecord issue: XCTIssue) {
    self.lock.lock()
    self.buffer.append(issue.compactDescription)
    self.lock.unlock()
  }
}
