/// Binary framing protocol for communication with Python backend.
///
/// Stdout messages (helper -> Python):
///   [1 byte: type] [4 bytes: length, big-endian] [N bytes: payload]
///   Types: 0x01 = system audio (PCM float32 16kHz mono), 0x02 = screen frame (JPEG), 0x03 = status (JSON), 0x04 = mic audio (PCM float32 16kHz mono)
///
/// Stdin commands (Python -> helper):
///   One JSON object per line (newline-delimited)

import Foundation

enum MessageType: UInt8 {
    case audio = 0x01
    case screenFrame = 0x02
    case status = 0x03
    case micAudio = 0x04
}

/// Serial queue protecting stdout writes from concurrent dispatch queues.
private let stdoutQueue = DispatchQueue(label: "capture.stdout")

/// Write a framed message to stdout (thread-safe).
func sendMessage(type: MessageType, payload: Data) {
    var header = Data(capacity: 5)
    header.append(type.rawValue)
    var length = UInt32(payload.count).bigEndian
    header.append(Data(bytes: &length, count: 4))

    stdoutQueue.sync {
        let stdout = FileHandle.standardOutput
        stdout.write(header)
        stdout.write(payload)
    }
}

/// Send a JSON status message.
func sendStatus(_ dict: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: dict) {
        sendMessage(type: .status, payload: data)
    }
}

/// Send system audio PCM data.
func sendAudio(_ data: Data) {
    sendMessage(type: .audio, payload: data)
}

/// Send microphone audio PCM data.
func sendMicAudio(_ data: Data) {
    sendMessage(type: .micAudio, payload: data)
}

/// Send a screen frame as JPEG.
func sendFrame(_ data: Data) {
    sendMessage(type: .screenFrame, payload: data)
}

/// Read newline-delimited JSON commands from stdin.
func readCommand() -> [String: Any]? {
    guard let line = readLine(strippingNewline: true), !line.isEmpty else {
        return nil
    }
    guard let data = line.data(using: .utf8),
          let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
        return nil
    }
    return json
}
