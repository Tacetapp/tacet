import AVFoundation
import CoreMedia
import Foundation
import ScreenCaptureKit

/// Manages ScreenCaptureKit streams for audio and screen capture.
@available(macOS 13.0, *)
final class CaptureManager: NSObject, SCStreamDelegate, SCStreamOutput {

    private var stream: SCStream?
    private var screenCaptureInterval: Double = 10.0
    private var lastFrameTime: CFAbsoluteTime = 0
    private var captureSystemAudio = true
    private var captureMic = true
    private var targetAppBundleId: String?
    private var isCapturing = false

    // Audio resampling: ScreenCaptureKit outputs 48kHz, whisper needs 16kHz
    private let targetSampleRate: Double = 16000.0

    // Cached AVAudioConverter for system audio resampling (proper anti-aliased resampling)
    private var systemAudioConverter: AVAudioConverter?
    private var systemAudioSourceFormat: AVAudioFormat?
    private let targetAudioFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16000.0, channels: 1, interleaved: false)!

    // Mic capture via AVAudioEngine
    private var audioEngine: AVAudioEngine?

    // Logging counters
    private var audioChunkCount = 0
    private var frameCount = 0

    /// Start capturing with the given configuration.
    func start(
        systemAudio: Bool = true,
        mic: Bool = true,
        screenInterval: Double = 10.0,
        screenTarget: String? = nil,  // "display" or "app"
        screenAppBundle: String? = nil
    ) async throws {
        captureSystemAudio = systemAudio
        captureMic = mic
        screenCaptureInterval = screenInterval
        targetAppBundleId = screenAppBundle

        log("Getting available content from ScreenCaptureKit...")
        // Get available content
        let availableContent = try await SCShareableContent.excludingDesktopWindows(
            false, onScreenWindowsOnly: true
        )
        log("Found \(availableContent.displays.count) displays, \(availableContent.applications.count) apps")

        // Report available apps
        let apps = availableContent.applications.map { app in
            ["name": app.applicationName, "bundle_id": app.bundleIdentifier]
        }
        sendStatus([
            "type": "apps_available",
            "apps": apps,
            "displays": availableContent.displays.count
        ])

        // Determine content filter
        let filter: SCContentFilter
        if let bundleId = screenAppBundle,
           let app = availableContent.applications.first(where: { $0.bundleIdentifier == bundleId }),
           let display = availableContent.displays.first {
            // Capture specific app by excluding all other apps from the display
            let excludedApps = availableContent.applications.filter { $0.bundleIdentifier != bundleId }
            filter = SCContentFilter(display: display, excludingApplications: excludedApps, exceptingWindows: [])
            sendStatus(["type": "target", "mode": "app", "app": app.applicationName, "bundle_id": bundleId])
        } else {
            // Capture entire primary display
            guard let display = availableContent.displays.first else {
                sendStatus(["type": "error", "message": "No displays found"])
                return
            }
            filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])
            sendStatus(["type": "target", "mode": "display", "display_id": display.displayID])
        }

        // Configure stream
        let config = SCStreamConfiguration()

        // Audio configuration
        config.capturesAudio = systemAudio
        config.sampleRate = 48000  // ScreenCaptureKit native rate
        config.channelCount = 1

        // Screen capture configuration
        config.width = 1920
        config.height = 1080
        config.minimumFrameInterval = CMTime(value: Int64(screenInterval), timescale: 1)
        config.pixelFormat = kCVPixelFormatType_32BGRA
        config.showsCursor = false

        // Create and start stream
        let stream = SCStream(filter: filter, configuration: config, delegate: self)
        self.stream = stream

        // Add output handlers
        try stream.addStreamOutput(self, type: .screen, sampleHandlerQueue: .global(qos: .userInteractive))
        if systemAudio {
            try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: .global(qos: .userInteractive))
        }

        log("Starting SCStream capture...")
        try await stream.startCapture()
        isCapturing = true
        log("SCStream capture active")

        // Start mic capture if requested
        if mic {
            log("Starting mic capture...")
            startMicCapture()
        }

        sendStatus(["type": "started", "system_audio": systemAudio, "mic": mic, "screen_interval": screenInterval])
    }

    /// Stop all capture.
    func stop() async {
        isCapturing = false
        if let stream = stream {
            try? await stream.stopCapture()
            self.stream = nil
        }
        stopMicCapture()
        sendStatus(["type": "stopped"])
    }

    // MARK: - SCStreamOutput

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard isCapturing else { return }

        switch type {
        case .screen:
            handleScreenFrame(sampleBuffer)
        case .audio:
            handleAudioBuffer(sampleBuffer)
        case .microphone:
            handleAudioBuffer(sampleBuffer)
        @unknown default:
            break
        }
    }

    // MARK: - SCStreamDelegate

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        sendStatus(["type": "error", "message": "Stream stopped: \(error.localizedDescription)"])
        isCapturing = false
    }

    // MARK: - Audio Processing

    private func handleAudioBuffer(_ sampleBuffer: CMSampleBuffer) {
        guard let formatDesc = sampleBuffer.formatDescription,
              let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(formatDesc) else {
            return
        }

        let sourceSampleRate = asbd.pointee.mSampleRate

        // Get raw audio data
        guard let blockBuffer = CMSampleBufferGetDataBuffer(sampleBuffer) else { return }
        var length = 0
        var dataPointer: UnsafeMutablePointer<Int8>?
        let status = CMBlockBufferGetDataPointer(blockBuffer, atOffset: 0, lengthAtOffsetOut: nil, totalLengthOut: &length, dataPointerOut: &dataPointer)
        guard status == kCMBlockBufferNoErr, let ptr = dataPointer else { return }

        // Already at target rate — send directly
        if sourceSampleRate == targetSampleRate {
            let data = Data(bytes: ptr, count: length)
            audioChunkCount += 1
            if audioChunkCount == 1 {
                log("First audio chunk: \(data.count) bytes (native \(sourceSampleRate)Hz)")
            }
            sendAudio(data)
            return
        }

        // Need to resample — use AVAudioConverter for proper anti-aliased resampling
        guard let sourceFormat = AVAudioFormat(
            commonFormat: asbd.pointee.mBitsPerChannel == 32 ? .pcmFormatFloat32 : .pcmFormatInt16,
            sampleRate: sourceSampleRate,
            channels: AVAudioChannelCount(asbd.pointee.mChannelsPerFrame),
            interleaved: asbd.pointee.mChannelsPerFrame == 1  // mono is same either way
        ) else { return }

        // Create/recreate converter if source format changed
        if systemAudioConverter == nil || systemAudioSourceFormat != sourceFormat {
            systemAudioConverter = AVAudioConverter(from: sourceFormat, to: targetAudioFormat)
            systemAudioSourceFormat = sourceFormat
            if systemAudioConverter == nil {
                log("Failed to create audio converter \(sourceSampleRate)Hz → \(targetSampleRate)Hz")
                return
            }
            log("Created AVAudioConverter: \(sourceSampleRate)Hz → \(targetSampleRate)Hz")
        }

        let sampleCount = CMSampleBufferGetNumSamples(sampleBuffer)
        guard sampleCount > 0 else { return }

        // Create source buffer and copy raw audio into it
        guard let sourceBuffer = AVAudioPCMBuffer(
            pcmFormat: sourceFormat,
            frameCapacity: AVAudioFrameCount(sampleCount)
        ) else { return }
        sourceBuffer.frameLength = AVAudioFrameCount(sampleCount)

        // Copy raw bytes into the source buffer's audio data
        if asbd.pointee.mBitsPerChannel == 32, let channelData = sourceBuffer.floatChannelData {
            memcpy(channelData[0], ptr, min(length, sampleCount * MemoryLayout<Float>.size))
        } else if let channelData = sourceBuffer.int16ChannelData {
            memcpy(channelData[0], ptr, min(length, sampleCount * MemoryLayout<Int16>.size))
        } else {
            return
        }

        // Create target buffer
        let targetFrameCount = AVAudioFrameCount(Double(sampleCount) * targetSampleRate / sourceSampleRate)
        guard targetFrameCount > 0,
              let targetBuffer = AVAudioPCMBuffer(
                  pcmFormat: targetAudioFormat,
                  frameCapacity: targetFrameCount
              ) else { return }

        // Convert with proper anti-aliased resampling
        var error: NSError?
        var consumed = false
        systemAudioConverter!.convert(to: targetBuffer, error: &error) { _, outStatus in
            if consumed {
                outStatus.pointee = .noDataNow
                return nil
            }
            consumed = true
            outStatus.pointee = .haveData
            return sourceBuffer
        }

        guard error == nil, let channelData = targetBuffer.floatChannelData else {
            if let error = error {
                log("Audio conversion error: \(error.localizedDescription)")
            }
            return
        }

        let outputData = Data(bytes: channelData[0], count: Int(targetBuffer.frameLength) * MemoryLayout<Float>.size)

        audioChunkCount += 1
        if audioChunkCount == 1 {
            log("First audio chunk: \(outputData.count) bytes (AVAudioConverter \(sourceSampleRate)→\(targetSampleRate)Hz)")
        } else if audioChunkCount % 200 == 0 {
            log("Audio chunks sent: \(audioChunkCount)")
        }
        sendAudio(outputData)
    }

    // MARK: - Mic Capture

    private func startMicCapture() {
        let engine = AVAudioEngine()
        let inputNode = engine.inputNode
        let format = inputNode.outputFormat(forBus: 0)

        // Install tap to get mic audio
        let targetFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: targetSampleRate, channels: 1, interleaved: false)!

        // We'll convert in the tap handler
        inputNode.installTap(onBus: 0, bufferSize: 8192, format: format) { [weak self] buffer, _ in
            guard let self = self, self.isCapturing else { return }

            // Convert to 16kHz mono float32
            guard let converter = AVAudioConverter(from: format, to: targetFormat) else { return }
            let frameCount = AVAudioFrameCount(Double(buffer.frameLength) * self.targetSampleRate / format.sampleRate)
            guard let convertedBuffer = AVAudioPCMBuffer(pcmFormat: targetFormat, frameCapacity: frameCount) else { return }

            var error: NSError?
            var consumed = false
            converter.convert(to: convertedBuffer, error: &error) { _, outStatus in
                if consumed {
                    outStatus.pointee = .noDataNow
                    return nil
                }
                consumed = true
                outStatus.pointee = .haveData
                return buffer
            }

            if error == nil, let channelData = convertedBuffer.floatChannelData {
                let data = Data(bytes: channelData[0], count: Int(convertedBuffer.frameLength) * MemoryLayout<Float>.size)
                sendMicAudio(data)
            }
        }

        do {
            try engine.start()
            self.audioEngine = engine
            sendStatus(["type": "mic_started"])
        } catch {
            sendStatus(["type": "error", "message": "Mic start failed: \(error.localizedDescription)"])
        }
    }

    private func stopMicCapture() {
        audioEngine?.inputNode.removeTap(onBus: 0)
        audioEngine?.stop()
        audioEngine = nil
    }

    // MARK: - Screen Frame Processing

    private func handleScreenFrame(_ sampleBuffer: CMSampleBuffer) {
        let now = CFAbsoluteTimeGetCurrent()
        // Respect the configured interval (SCK may deliver faster than requested)
        guard now - lastFrameTime >= screenCaptureInterval else { return }
        lastFrameTime = now

        guard let imageBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }

        CVPixelBufferLockBaseAddress(imageBuffer, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(imageBuffer, .readOnly) }

        let width = CVPixelBufferGetWidth(imageBuffer)
        let height = CVPixelBufferGetHeight(imageBuffer)
        let bytesPerRow = CVPixelBufferGetBytesPerRow(imageBuffer)
        guard let baseAddress = CVPixelBufferGetBaseAddress(imageBuffer) else { return }

        let colorSpace = CGColorSpace(name: CGColorSpace.sRGB)!
        guard let context = CGContext(
            data: baseAddress,
            width: width,
            height: height,
            bitsPerComponent: 8,
            bytesPerRow: bytesPerRow,
            space: colorSpace,
            bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue
        ) else { return }

        guard let cgImage = context.makeImage() else { return }

        // Encode as JPEG
        let mutableData = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(mutableData, "public.jpeg" as CFString, 1, nil) else { return }
        CGImageDestinationAddImage(destination, cgImage, [kCGImageDestinationLossyCompressionQuality: 0.7] as CFDictionary)
        guard CGImageDestinationFinalize(destination) else { return }

        sendFrame(mutableData as Data)
    }
}
