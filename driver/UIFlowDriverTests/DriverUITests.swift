import XCTest

final class DriverUITests: XCTestCase {
  func testServeSession() {
    self.continueAfterFailure = true
    let recorder = IssueRecorder()
    XCTestObservationCenter.shared.addTestObserver(recorder)
    defer { XCTestObservationCenter.shared.removeTestObserver(recorder) }
    let session = DriverSession.fromEnvironment(issues: recorder)
    // Announce only once the channel is clean, so a host that acts on the announcement can never
    // have its first request purged out from under it.
    session.prepareChannel()
    print("UIFLOW_CHANNEL=\(session.channelPath)")
    session.serve()
    print("UIFLOW_SESSION_ENDED")
  }
}
