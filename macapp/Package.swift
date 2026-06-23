// swift-tools-version:5.9
//
// Buildable SwiftPM package for the menu-bar supplier app, so the sources compile
// locally with `swift build --package-path macapp` (no Xcode project needed). This
// is the local, buildable part of the agent app; producing a SIGNED, NOTARIZED
// `.app` bundle (with Info.plist LSUIElement + the entitlements alongside) requires
// an Apple Developer ID and is the external release step — see macapp/README.md.
import PackageDescription

let package = Package(
    name: "ComputeExchangeAgent",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "ComputeExchangeAgent",
            path: "ComputeExchangeAgent",
            exclude: ["Info.plist", "ComputeExchangeAgent.entitlements"]
        )
    ]
)
