//  MenuContentView.swift
//  The popover shown from the menu-bar icon: live status, earnings, thermal +
//  cache telemetry, the operator toggles (active / quiet-hours / power-only /
//  min-payout), and the launch / stop / open-data-dir actions.

import SwiftUI

struct MenuContentView: View {
    @ObservedObject var controller: AgentController

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            header
            Divider()
            statusGrid
            throttleBanner
            if let job = controller.status.currentJob {
                currentJobView(job)
            }
            if let msg = controller.statusMessage {
                Label(msg, systemImage: "exclamationmark.triangle")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let err = controller.lastLaunchError {
                Label(err, systemImage: "xmark.octagon")
                    .font(.caption)
                    .foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Divider()
            controls
            Divider()
            actions
        }
        .padding(14)
        .frame(width: 320)
    }

    // MARK: header

    private var header: some View {
        HStack {
            Image(systemName: controller.status.state.symbol)
                .foregroundStyle(controller.status.state == .running ? Color.accentColor : .secondary)
            VStack(alignment: .leading, spacing: 1) {
                Text("Computexchange").font(.headline)
                Text(controller.status.state.label).font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            Text(controller.status.agentVersion)
                .font(.caption2).foregroundStyle(.secondary)
        }
    }

    // MARK: status

    private var statusGrid: some View {
        Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 6) {
            GridRow {
                stat("Today", String(format: "$%.2f", controller.status.todayEarningsUsd))
                stat("Balance", String(format: "$%.2f", controller.status.balanceUsd))
            }
            GridRow {
                stat("Thermal", controller.status.thermalState.label)
                stat("Model cache", controller.status.modelCacheHuman)
            }
            GridRow {
                stat("GPU temp", controller.status.gpuTempC.map { String(format: "%.0f°C", $0) } ?? "—")
                stat("CPU", String(format: "%.0f%%", controller.status.cpuPct))
            }
            GridRow {
                stat("Effective mem", controller.status.effectiveMemoryGb > 0
                    ? String(format: "%.0f GB", controller.status.effectiveMemoryGb) : "—")
                stat("Headroom", controller.status.reservedHeadroomGb > 0
                    ? String(format: "%.0f GB", controller.status.reservedHeadroomGb) : "—")
            }
        }
    }

    /// A memory-throttle banner: shown only when the agent has paused new work for
    /// memory pressure, surfacing the agent's own reason verbatim (never faked).
    private var throttleBanner: some View {
        Group {
            if controller.status.throttled {
                Label(controller.status.throttleReason ?? "Paused — memory pressure",
                      systemImage: "exclamationmark.triangle.fill")
                    .font(.caption)
                    .foregroundStyle(Color.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func stat(_ key: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(key).font(.caption2).foregroundStyle(.secondary)
            Text(value).font(.callout.monospacedDigit())
        }
    }

    private func currentJobView(_ job: CurrentJob) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("Current job").font(.caption2).foregroundStyle(.secondary)
            HStack {
                Text(job.jobType).font(.callout).bold()
                Spacer()
                Text(job.jobId.prefix(8) + "…").font(.caption.monospaced()).foregroundStyle(.secondary)
            }
        }
    }

    // MARK: controls

    private var controls: some View {
        VStack(alignment: .leading, spacing: 8) {
            Toggle("Active (accept work)", isOn: $controller.prefs.active)
            Toggle("Pause on battery (power-only)", isOn: $controller.prefs.powerOnly)
            Toggle("Quiet hours", isOn: $controller.prefs.quietHoursEnabled)
            if controller.prefs.quietHoursEnabled {
                HStack {
                    Text("From").font(.caption).foregroundStyle(.secondary)
                    Stepper(value: $controller.prefs.quietStartHour, in: 0...23) {
                        Text(String(format: "%02d:00", controller.prefs.quietStartHour)).monospacedDigit()
                    }
                }
                HStack {
                    Text("To").font(.caption).foregroundStyle(.secondary)
                    Stepper(value: $controller.prefs.quietEndHour, in: 0...23) {
                        Text(String(format: "%02d:00", controller.prefs.quietEndHour)).monospacedDigit()
                    }
                }
            }
            HStack {
                Text("Min payout").font(.caption).foregroundStyle(.secondary)
                Spacer()
                Text(String(format: "$%.2f/hr", controller.prefs.minPayoutUsdPerHr)).monospacedDigit()
            }
            Slider(value: $controller.prefs.minPayoutUsdPerHr, in: 0...1, step: 0.01)
            HStack {
                Text("Memory headroom").font(.caption).foregroundStyle(.secondary)
                Spacer()
                Stepper(value: $controller.prefs.memoryHeadroomGb, in: 0...64, step: 2) {
                    Text(String(format: "%.0f GB", controller.prefs.memoryHeadroomGb)).monospacedDigit()
                }
            }
        }
        .toggleStyle(.switch)
    }

    // MARK: actions

    private var actions: some View {
        VStack(spacing: 6) {
            HStack {
                if controller.launchedPID != nil {
                    Button(role: .destructive) { controller.stopAgent() } label: {
                        Label("Stop agent", systemImage: "stop.fill").frame(maxWidth: .infinity)
                    }
                } else {
                    Button { controller.startAgent() } label: {
                        Label("Start agent", systemImage: "play.fill").frame(maxWidth: .infinity)
                    }
                }
            }
            Button { controller.openDataDir() } label: {
                Label("Open data dir", systemImage: "folder").frame(maxWidth: .infinity)
            }
            Button { NSApplication.shared.terminate(nil) } label: {
                Label("Quit", systemImage: "power").frame(maxWidth: .infinity)
            }
        }
        .buttonStyle(.bordered)
    }
}
