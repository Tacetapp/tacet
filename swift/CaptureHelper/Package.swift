// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "CaptureHelper",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "CaptureHelper",
            path: "Sources",
            linkerSettings: [
                .linkedFramework("ScreenCaptureKit"),
                .linkedFramework("CoreMedia"),
                .linkedFramework("AVFoundation"),
                .linkedFramework("CoreAudio"),
                .linkedFramework("CoreGraphics"),
                .linkedFramework("ImageIO"),
            ]
        ),
    ]
)
