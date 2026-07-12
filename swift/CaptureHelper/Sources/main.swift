/// CaptureHelper — ScreenCaptureKit-based audio and screen capture for Tacet.
///
/// Communicates with Python backend via binary framing protocol on stdin/stdout.
/// See Protocol.swift for message format details.

import Foundation
import ScreenCaptureKit

/// Log to stderr (stdout is reserved for the binary protocol).
func log(_ message: String) {
    FileHandle.standardError.write("[\(ProcessInfo.processInfo.processName)] \(message)\n".data(using: .utf8)!)
}

if #available(macOS 13.0, *) {
    let manager = CaptureManager()
    log("CaptureHelper started")
    sendStatus(["type": "ready", "version": "1.0.0"])

    // Read stdin commands on a background thread (readLine() blocks)
    DispatchQueue.global(qos: .userInitiated).async {
        log("Listening for commands on stdin")
        while let command = readCommand() {
            guard let cmd = command["command"] as? String else {
                log("Received command without 'command' key, skipping")
                continue
            }
            log("Received command: \(cmd)")

            switch cmd {
            case "start":
                let systemAudio = command["system_audio"] as? Bool ?? true
                let mic = command["mic"] as? Bool ?? true
                let screenInterval = command["screen_interval"] as? Double ?? 10.0
                let screenTarget = command["screen_target"] as? String
                let screenAppBundle = command["screen_app_bundle"] as? String

                let sem = DispatchSemaphore(value: 0)
                Task {
                    do {
                        try await manager.start(
                            systemAudio: systemAudio,
                            mic: mic,
                            screenInterval: screenInterval,
                            screenTarget: screenTarget,
                            screenAppBundle: screenAppBundle
                        )
                        log("Capture started successfully")
                    } catch {
                        log("Start failed: \(error.localizedDescription)")
                        sendStatus(["type": "error", "message": "Start failed: \(error.localizedDescription)"])
                    }
                    sem.signal()
                }
                sem.wait()

            case "stop":
                let sem = DispatchSemaphore(value: 0)
                Task {
                    await manager.stop()
                    log("Capture stopped")
                    sem.signal()
                }
                sem.wait()

            case "enumerate":
                Task {
                    do {
                        let content = try await SCShareableContent.excludingDesktopWindows(
                            false, onScreenWindowsOnly: true
                        )
                        let apps = content.applications.map { app in
                            ["name": app.applicationName, "bundle_id": app.bundleIdentifier]
                        }
                        sendStatus([
                            "type": "apps_available",
                            "apps": apps,
                            "displays": content.displays.count
                        ])
                    } catch {
                        sendStatus(["type": "error", "message": "Enumerate failed: \(error.localizedDescription)"])
                    }
                }

            default:
                sendStatus(["type": "error", "message": "Unknown command: \(cmd)"])
            }
        }

        // stdin closed — shut down
        log("stdin closed, shutting down")
        Task {
            await manager.stop()
            exit(0)
        }
    }

    // Keep the process alive — needed for ScreenCaptureKit callbacks
    // and Swift concurrency. dispatchMain() never returns.
    dispatchMain()
} else {
    sendStatus(["type": "error", "message": "macOS 13.0 or later is required"])
    exit(1)
}
