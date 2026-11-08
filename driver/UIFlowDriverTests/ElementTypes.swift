import XCTest

enum ElementTypes {
  static func query(_ type: String?, in scope: XCUIElementTypeQueryProvider) -> XCUIElementQuery {
    switch type {
    case "button": return scope.buttons
    case "cell": return scope.cells
    case "staticText": return scope.staticTexts
    case "switch": return scope.switches
    case "textField": return scope.textFields
    case "secureTextField": return scope.secureTextFields
    case "searchField": return scope.searchFields
    case "textView": return scope.textViews
    case "key": return scope.keys
    case "keyboard": return scope.keyboards
    case "image": return scope.images
    case "pickerWheel": return scope.pickerWheels
    case "picker": return scope.pickers
    case "slider": return scope.sliders
    case "alert": return scope.alerts
    case "navigationBar": return scope.navigationBars
    case "tabBar": return scope.tabBars
    case "table": return scope.tables
    case "collectionView": return scope.collectionViews
    case "scrollView": return scope.scrollViews
    case "window": return scope.windows
    case "other": return scope.otherElements
    case "link": return scope.links
    case "toggle": return scope.switches
    default:
      if let element = scope as? XCUIElement { return element.descendants(matching: .any) }
      if let query = scope as? XCUIElementQuery { return query.descendants(matching: .any) }
      return scope.otherElements
    }
  }

  /// `.any` walks the whole hierarchy and is the most expensive query available, so it is never
  /// polled — a selector without a concrete type gets one probe and no repeat.
  static func isUntyped(_ type: String?) -> Bool {
    guard let type else { return true }
    return type.isEmpty || type == "any"
  }

  static func name(_ type: XCUIElement.ElementType) -> String {
    switch type {
    case .button: return "button"
    case .cell: return "cell"
    case .staticText: return "staticText"
    case .switch: return "switch"
    case .textField: return "textField"
    case .secureTextField: return "secureTextField"
    case .searchField: return "searchField"
    case .textView: return "textView"
    case .key: return "key"
    case .keyboard: return "keyboard"
    case .image: return "image"
    case .pickerWheel: return "pickerWheel"
    case .picker: return "picker"
    case .slider: return "slider"
    case .alert: return "alert"
    case .navigationBar: return "navigationBar"
    case .tabBar: return "tabBar"
    case .table: return "table"
    case .collectionView: return "collectionView"
    case .scrollView: return "scrollView"
    case .window: return "window"
    case .other: return "other"
    case .link: return "link"
    case .application: return "application"
    case .toolbar: return "toolbar"
    default: return "type\(type.rawValue)"
    }
  }
}
