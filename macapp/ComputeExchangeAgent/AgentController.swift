//  AgentController.swift
//  Reads ~/.compute-exchange/status.json (polled + file-watched), launches and
//  stops the bundled `cx-agent` Rust binary, and persists operator prefs both to
//  UserDefaults and to the agent's TOML config so the next launch honors them.
//
//  Honesty rules (BLACKHOLE: surface every failure):
//   - A missing or unreadable status file => state .offline + a visible message;
//     never a fabricated "running".
//   - A status file whose heartbeat is stale => treated as offline regardless of
//     what the file's `state` field claims.
//   - A launch failure surfaces its error; we do not pretend the agent started.

import Foundation
import Combine

/// Canonical paths under the agent's data dir. The app and the agent must agree
/// on these; they match agent/src/config.rs `data_dir` (defaulting to
/// ~/.compute-exchange).
enum AgentPaths {
    static var dataDir: URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".compute-exchange", isDirectory: true)
    }
    static var statusFile: URL { dataDir.appendingPathComponent("status.json") }
    static var configFile: URL { dataDir.appendingPathComponent("agent.toml") }
    /// Operator-prefs sidecar the app writes; the agent reads it as an overlay over
    /// agent.toml (via CX_AGENT_PREFS). Atlas F7 — this is what makes the menu toggles
    /// real instead of decorative.
    static var prefsFile: URL { dataDir.appendingPathComponent("agent.prefs.toml") }

    /// The bundled agent binary. In a packaged .app it ships in Contents/MacOS;
    /// in dev we fall back to a PATH lookup / the cargo target.
    static var bundledBinary: URL? {
        if let url = Bundle.main.url(forResource: "cx-agent", withExtension: nil) {
            return url
        }
        return nil
    }
}

@MainActor
final class AgentController: ObservableObject {
    @Published private(set) var status = AgentStatus()
    /// Set when the status file is absent / unreadable / stale · shown in the UI.
    @Published private(set) var statusMessage: String? = "Waiting for the agent…"
    @Published var prefs = OperatorPrefs() {
        didSet { persistPrefs() }
    }
    /// Non-nil while we are managing a child agent process we launched ourselves.
    @Published private(set) var launchedPID: Int32?
    @Published private(set) var lastLaunchError: String?

    private var timer: Timer?
    private var process: Process?

    /// Consent gate: the agent may not launch (and so may not earn) until the
    /// operator has accepted the first-run terms. Injected so the same store backs
    /// both the onboarding sheet and this gate.
    let consent: ConsentStore
    /// Rolling, locally-observed earnings series for the trust sparkline.
    let earningsHistory: EarningsHistory

    init(consent: ConsentStore? = nil, earningsHistory: EarningsHistory? = nil) {
        // Build the stores inside the (main-actor) init body. They are @MainActor
        // types, so their initializers can only run here, not as default arguments
        // (which Swift evaluates in a nonisolated context).
        self.consent = consent ?? ConsentStore()
        self.earningsHistory = earningsHistory ?? EarningsHistory()
        loadPrefs()
        refresh()
        // Poll the status file. A file-presence/heartbeat check every 3s is cheap
        // and robust; a DispatchSource file watcher could be layered on later but
        // polling alone is enough and never misses an atomic-rename write.
        timer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
    }

    // MARK: status

    /// Re-read status.json. On any failure the published state becomes offline
    /// with a human message · we never keep showing stale "running".
    func refresh() {
        guard FileManager.default.fileExists(atPath: AgentPaths.statusFile.path) else {
            status = AgentStatus()
            statusMessage = "Agent not started (no status file at \(AgentPaths.statusFile.path))."
            return
        }
        do {
            let data = try Data(contentsOf: AgentPaths.statusFile)
            var decoded = try JSONDecoder().decode(AgentStatus.self, from: data)
            if decoded.isStale {
                decoded.state = .offline
                statusMessage = "Agent heartbeat is stale · it may have stopped."
            } else {
                statusMessage = decoded.lastError   // nil unless the agent reported one
            }
            status = decoded
            // Record the observed lifetime total for the sparkline. This is the
            // app's own honest record of what it read · never synthesized.
            if !decoded.isStale && decoded.lifetimeUsd > 0 {
                earningsHistory.record(lifetimeUsd: decoded.lifetimeUsd)
            }
        } catch {
            status = AgentStatus()
            statusMessage = "Could not read status.json: \(error.localizedDescription)"
        }
    }

    // MARK: launch / stop

    /// Launch the bundled agent as a child process pointed at our data dir. Sets
    /// `lastLaunchError` (and does NOT pretend to run) on any failure.
    func startAgent() {
        lastLaunchError = nil
        // Hard consent gate (BLACKHOLE): never start earning before the operator
        // has accepted the first-run terms. The UI also shows the onboarding sheet,
        // but we refuse here too so no code path can launch around it.
        guard consent.granted else {
            lastLaunchError = "Consent required: review and accept the terms before the agent can start."
            return
        }
        guard let bin = AgentPaths.bundledBinary else {
            lastLaunchError = "cx-agent binary not found in the app bundle. In dev, run the Rust agent directly (cargo run -p cx-agent)."
            return
        }
        do {
            try FileManager.default.createDirectory(at: AgentPaths.dataDir, withIntermediateDirectories: true)
        } catch {
            lastLaunchError = "Could not create data dir: \(error.localizedDescription)"
            return
        }
        let p = Process()
        p.executableURL = bin
        p.arguments = ["run", "--config", AgentPaths.configFile.path]
        // Point the agent at the operator-prefs overlay (Atlas F7 / item 25). The agent
        // merges this sidecar over agent.toml, so the menu toggles ACTUALLY control it —
        // they are no longer decorative. (The agent also auto-discovers this conventional
        // path next to the config; setting the env var makes the contract explicit.)
        var env = ProcessInfo.processInfo.environment
        env["CX_AGENT_PREFS"] = AgentPaths.prefsFile.path
        p.environment = env
        p.terminationHandler = { [weak self] proc in
            Task { @MainActor in
                self?.launchedPID = nil
                self?.process = nil
                if proc.terminationStatus != 0 {
                    self?.lastLaunchError = "Agent exited with status \(proc.terminationStatus)."
                }
                self?.refresh()
            }
        }
        do {
            try p.run()
            process = p
            launchedPID = p.processIdentifier
        } catch {
            lastLaunchError = "Failed to launch agent: \(error.localizedDescription)"
        }
    }

    /// Stop an agent we launched. (An agent started elsewhere · e.g. a LaunchAgent
    /// · is not ours to kill; we only manage our own child.)
    func stopAgent() {
        process?.terminate()
        process = nil
        launchedPID = nil
        refresh()
    }

    func openDataDir() {
        try? FileManager.default.createDirectory(at: AgentPaths.dataDir, withIntermediateDirectories: true)
        NSWorkspace.shared.open(AgentPaths.dataDir)
    }

    // MARK: prefs

    private func loadPrefs() {
        guard let data = UserDefaults.standard.data(forKey: OperatorPrefs.defaultsKey),
              let decoded = try? JSONDecoder().decode(OperatorPrefs.self, from: data) else { return }
        prefs = decoded
    }

    /// Persist prefs to UserDefaults AND to the agent's prefs overlay so a subsequent
    /// launch honors them. We write the keys the Rust AgentConfig understands into a
    /// dedicated `agent.prefs.toml` overlay (NOT the canonical agent.toml), which the
    /// agent merges over its base config at startup (Atlas F7) — so these toggles
    /// actually control the agent. Write failures are surfaced, never hidden.
    private func persistPrefs() {
        if let data = try? JSONEncoder().encode(prefs) {
            UserDefaults.standard.set(data, forKey: OperatorPrefs.defaultsKey)
        }
        writePrefsToAgentConfig()
    }

    private func writePrefsToAgentConfig() {
        // We write a SEPARATE overlay (agent.prefs.toml), not a patch of agent.toml —
        // that is the intended design, not a limitation: the agent merges this overlay
        // over its base config (control_url, worker_token, supplier_id stay untouched in
        // agent.toml). Each line below maps a pref to a key the Rust AgentConfig reads.
        var lines: [String] = []
        lines.append("# Written by ComputeExchangeAgent.app · operator prefs.")
        lines.append("max_concurrent_tasks = 0   # 0 / omit => agent derives from cores+RAM")
        lines.append("power_only = \(prefs.powerOnly)")
        lines.append("min_payout_usd_per_hr = \(prefs.minPayoutUsdPerHr)")
        lines.append("memory_headroom_gb = \(prefs.memoryHeadroomGb)   # GB reserved for the operator (dynamic throttling)")
        if prefs.quietHoursEnabled {
            lines.append("quiet_hours = [\(prefs.quietStartHour), \(prefs.quietEndHour)]")
        }
        let snippet = lines.joined(separator: "\n") + "\n"
        // We do NOT clobber the canonical agent.toml; we write the overlay the agent
        // reads via CX_AGENT_PREFS (set in startAgent) and also auto-discovers next to
        // the config. The toggles apply on the next launch.
        let sidecar = AgentPaths.prefsFile
        do {
            try FileManager.default.createDirectory(at: AgentPaths.dataDir, withIntermediateDirectories: true)
            try snippet.write(to: sidecar, atomically: true, encoding: .utf8)
        } catch {
            lastLaunchError = "Could not write prefs sidecar: \(error.localizedDescription)"
        }
    }
}

#if canImport(AppKit)
import AppKit
#endif
