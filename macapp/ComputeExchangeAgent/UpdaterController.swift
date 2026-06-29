//  UpdaterController.swift
//  Sparkle auto-update wiring for the menu-bar app. We use Sparkle's
//  `SPUStandardUpdaterController`, which owns an `SPUUpdater` configured from the
//  bundle's Info.plist (SUFeedURL + SUPublicEDKey) and drives the standard
//  user-driven update UI (background check, "Update available" prompt, download,
//  install-on-quit).
//
//  Honesty note (BLACKHOLE): Sparkle ONLY installs an update whose appcast entry
//  carries a valid EdDSA (ed25519) signature matching the SUPublicEDKey embedded in
//  this app's Info.plist. There is no code path here that bypasses that check; an
//  unsigned or mis-signed update is rejected by Sparkle itself. The owner generates
//  the key pair once with Sparkle's `generate_keys` tool and pastes the PUBLIC key
//  into Info.plist (see macapp/README.md). Without that key Sparkle refuses to
//  update rather than silently installing anything · exactly the honest boundary.

import Foundation
import SwiftUI
import Sparkle

/// Wraps Sparkle's standard updater so SwiftUI can observe "can check for updates"
/// and trigger a manual check from the menu. `startingUpdater: true` begins the
/// scheduled background checks immediately at launch.
@MainActor
final class UpdaterController: ObservableObject {
    private let controller: SPUStandardUpdaterController

    /// Mirrors `updater.canCheckForUpdates` so the menu item can disable itself
    /// while a check is already in flight.
    @Published var canCheckForUpdates = false

    /// True once Sparkle has a usable feed URL. When false (no SUFeedURL in
    /// Info.plist, e.g. an unconfigured dev build) we say so in the UI rather than
    /// offering a check that cannot work.
    @Published var feedConfigured = false

    init() {
        // userDriver: nil => Sparkle's built-in standard UI. updaterDelegate: nil
        // => default behavior driven entirely by Info.plist keys.
        controller = SPUStandardUpdaterController(
            startingUpdater: true,
            updaterDelegate: nil,
            userDriverDelegate: nil
        )
        canCheckForUpdates = controller.updater.canCheckForUpdates
        feedConfigured = (controller.updater.feedURL != nil)

        // Keep the published flag in sync with Sparkle's own state via KVO.
        observation = controller.updater.observe(\.canCheckForUpdates, options: [.initial, .new]) { [weak self] updater, _ in
            Task { @MainActor in self?.canCheckForUpdates = updater.canCheckForUpdates }
        }
    }

    private var observation: NSKeyValueObservation?

    /// User-initiated update check (the "Check for Updates…" menu item). Shows the
    /// standard Sparkle UI: up-to-date, update-available, or an explicit error.
    func checkForUpdates() {
        controller.updater.checkForUpdates()
    }

    /// The current feed URL, for display/diagnostics. Nil when unconfigured.
    var feedURLString: String? { controller.updater.feedURL?.absoluteString }
}
