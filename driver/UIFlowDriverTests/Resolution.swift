import XCTest

struct Resolution {
  let element: XCUIElement
  let index: Int
  let candidates: Int

  var info: String { "matches=\(self.candidates) picked=\(self.index)" }
}
