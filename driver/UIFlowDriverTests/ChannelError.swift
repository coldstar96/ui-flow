import Foundation

enum ChannelError: Error {
  case badRequest(seq: Int, detail: String)
}
