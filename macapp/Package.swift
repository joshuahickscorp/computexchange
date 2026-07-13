// swift-tools-version:5.9
//
// Buildable SwiftPM package for the menu-bar supplier app, so the sources compile
// locally with `swift build --package-path macapp` (no Xcode project needed). This
// is the local, buildable part of the agent app; producing a SIGNED, NOTARIZED
// `.app` bundle (with Info.plist LSUIElement + the entitlements alongside) is the
// external release step driven by macapp/sign-notarize.sh · see macapp/README.md.
//
// Auto-update is provided by Sparkle (https://github.com/sparkle-project/Sparkle),
// pulled in as a Swift Package Manager dependency below. Sparkle ships an EdDSA
// (ed25519) signing scheme for the appcast; the owner generates the key pair once
// and embeds the PUBLIC key in Info.plist (SUPublicEDKey). See macapp/README.md.
import PackageDescription

let package = Package(
    name: "ComputeExchangeAgent",
    platforms: [.macOS(.v13)],
    dependencies: [
        // Pinned to a 2.x release line. Sparkle 2 is the current, sandbox-friendly
        // generation and supports EdDSA appcast signatures + a stable SPM product.
        .package(url: "https://github.com/sparkle-project/Sparkle", from: "2.5.0"),
    ],
    targets: [
        .target(
            name: "EnrollmentCore",
            path: "EnrollmentCore"
        ),
        .executableTarget(
            name: "ComputeExchangeAgent",
            dependencies: [
                "EnrollmentCore",
                .product(name: "Sparkle", package: "Sparkle"),
            ],
            path: "ComputeExchangeAgent",
            // Non-Swift files that live alongside the sources but aren't compiled:
            // the bundle Info.plist + entitlements, the seatbelt sandbox profile
            // (cx-agent.sb — embedded into the .app by assemble-app.sh, not SwiftPM),
            // and its proof harness (sandbox-profile-test.sh). Excluded so `swift
            // build` doesn't treat them as sources or emit unhandled-file warnings.
            exclude: [
                "Info.plist",
                "ComputeExchangeAgent.entitlements",
                "cx-agent.sb",
                "sandbox-profile-test.sh",
            ]
        ),
        .testTarget(
            name: "EnrollmentCoreTests",
            dependencies: ["EnrollmentCore"],
            path: "Tests/EnrollmentCoreTests"
        )
    ]
)
