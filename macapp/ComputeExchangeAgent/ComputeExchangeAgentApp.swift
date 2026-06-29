//  ComputeExchangeAgentApp.swift
//  Menu-bar supplier app entry point. A `MenuBarExtra` whose label reflects live
//  agent state and whose content is the status + controls panel.
//
//  This is the SwiftUI shell around the Rust `cx-agent` binary (see
//  AgentController). It owns the Sparkle updater, the first-run consent gate, and
//  the locally-observed earnings history that backs the trust sparkline.

import SwiftUI

@main
struct ComputeExchangeAgentApp: App {
    @StateObject private var controller = AgentController()
    @StateObject private var updater = UpdaterController()

    var body: some Scene {
        MenuBarExtra {
            MenuContentView(controller: controller, updater: updater)
        } label: {
            // The label shows the agent's live state as an SF Symbol + a terse
            // earnings figure, so the operator sees status without opening the menu.
            Label(menuTitle, systemImage: controller.status.state.symbol)
        }
        .menuBarExtraStyle(.window)   // a rich popover, not a plain menu
    }

    private var menuTitle: String {
        let s = controller.status
        if s.state == .running { return String(format: "$%.2f", s.todayEarningsUsd) }
        return ""   // symbol-only when idle/offline keeps the menu bar tidy
    }
}
