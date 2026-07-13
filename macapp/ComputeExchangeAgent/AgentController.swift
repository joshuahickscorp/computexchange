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
import EnrollmentCore

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

    /// The bundled macOS seatbelt profile (cx-agent.sb) that sandboxes the cx-agent
    /// CHILD process (Security Posture 8->9, docs/internal/CREED_AND_PATH_TO_TEN.md).
    /// Shipped as an app-bundle resource; proven by
    /// macapp/ComputeExchangeAgent/sandbox-profile-test.sh. Absent in a bare dev
    /// checkout with no assembled bundle — the launch path then records that the
    /// child ran UNSANDBOXED rather than pretending otherwise.
    static var sandboxProfile: URL? {
        Bundle.main.url(forResource: "cx-agent", withExtension: "sb")
    }

    /// The model cache root the sandbox profile scopes writes to. Mirrors the agent's
    /// own resolution (agent/src/models.rs / status.rs): $CX_MODEL_CACHE, else
    /// $HF_HOME, else ~/.cache/huggingface. The launcher passes this as the profile's
    /// MODELCACHE param so hf-hub downloads stay allowed.
    static var modelCacheDir: URL {
        let env = ProcessInfo.processInfo.environment
        if let c = env["CX_MODEL_CACHE"], !c.isEmpty {
            return URL(fileURLWithPath: c, isDirectory: true)
        }
        if let hf = env["HF_HOME"], !hf.isEmpty {
            return URL(fileURLWithPath: hf, isDirectory: true)
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".cache/huggingface", isDirectory: true)
    }

    /// Absolute path to `sandbox-exec` (a fixed OS location).
    static let sandboxExec = "/usr/bin/sandbox-exec"

    /// Env marker set on the child when THIS launcher has already applied the seatbelt
    /// profile, so the agent's own startup self-re-exec
    /// (agent/src/main.rs `reexec_under_sandbox_if_needed`, which keys off the same
    /// name) becomes a no-op and does not wrap the process a second time. Must match the
    /// Rust `CX_SANDBOXED_ENV` constant exactly.
    static let sandboxedEnvKey = "CX_SANDBOXED"
    /// The distributed app is a production launch path: if neither the launcher nor
    /// the agent can apply the seatbelt profile, the child must stop instead of
    /// accepting buyer work unsandboxed. Must match the Rust constant exactly.
    static let requireSandboxEnvKey = "CX_REQUIRE_SANDBOX"
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
    /// Whether the CURRENTLY-launched (or last-launched) cx-agent child is running
    /// under the macOS seatbelt sandbox (cx-agent.sb). Honest: false when the profile
    /// couldn't be resolved (a bare dev build with no assembled bundle) so the UI /
    /// consent copy never claims a protection that isn't active. The menu-bar app can
    /// surface this to the operator.
    @Published private(set) var sandboxActive = false

    private var timer: Timer?
    private var process: Process?

    /// Consent gate: the agent may not launch (and so may not earn) until the
    /// operator has accepted the first-run terms. Injected so the same store backs
    /// both the onboarding sheet and this gate.
    let consent: ConsentStore
    /// Rolling, locally-observed earnings series for the trust sparkline.
    let earningsHistory: EarningsHistory
    /// Supplier-machine enrollment. Its Keychain token + verified receipt are a
    /// second hard launch gate, independent of the consent gate above.
    let enrollment: EnrollmentStore

    var canStart: Bool {
        consent.granted && enrollment.isReady && launchedPID == nil
    }

    init(
        consent: ConsentStore? = nil,
        earningsHistory: EarningsHistory? = nil,
        enrollment: EnrollmentStore? = nil
    ) {
        // Build the stores inside the (main-actor) init body. They are @MainActor
        // types, so their initializers can only run here, not as default arguments
        // (which Swift evaluates in a nonisolated context).
        self.consent = consent ?? ConsentStore()
        self.earningsHistory = earningsHistory ?? EarningsHistory()
        self.enrollment = enrollment ?? EnrollmentStore.live(
            dataDirectory: AgentPaths.dataDir,
            configFile: AgentPaths.configFile
        )
        loadPrefs()
        refresh()
        // Poll the status file. A file-presence/heartbeat check every 3s is cheap
        // and robust; a DispatchSource file watcher could be layered on later but
        // polling alone is enough and never misses an atomic-rename write.
        timer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
        // NOTE on thermal signal (Supplier onboarding & safety 6→7): this app does
        // NOT observe ProcessInfo.thermalState itself. The Rust agent
        // (agent/src/hardware.rs `read_thermal_pressure`) reads it directly,
        // in-process, via its own NSProcessInfo FFI call every poll cycle — no
        // menu-bar-app round trip needed. This app only DISPLAYS the thermal_state
        // the agent reports back in status.json.
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
        guard enrollment.isReady else {
            lastLaunchError = "Enrollment required: verify this Mac with a worker token before starting the agent."
            return
        }
        let credentials: EnrollmentCredentials
        do {
            credentials = try enrollment.launchCredentials()
        } catch {
            lastLaunchError = "Enrollment is incomplete or the Keychain credential is unavailable. Open Enrollment and retry or reset."
            return
        }
        guard let bin = AgentPaths.bundledBinary else {
            lastLaunchError = "cx-agent binary not found in the app bundle. In dev, run the Rust agent directly (cargo run -p cx-agent)."
            return
        }
        do {
            try FileManager.default.createDirectory(at: AgentPaths.dataDir, withIntermediateDirectories: true)
            // The sandbox profile scopes writes to the model cache; create it up front
            // so hf-hub's first download has an existing, allowed target directory.
            try FileManager.default.createDirectory(at: AgentPaths.modelCacheDir, withIntermediateDirectories: true)
        } catch {
            lastLaunchError = "Could not create data dir: \(error.localizedDescription)"
            return
        }
        let p = Process()
        let agentArgs = ["run", "--config", AgentPaths.configFile.path]
        // Sandbox the cx-agent CHILD under the shipped macOS seatbelt profile
        // (cx-agent.sb) when it's available — the Security Posture 8->9 boundary that
        // contains a malicious buyer payload's filesystem blast radius (proven by
        // macapp/ComputeExchangeAgent/sandbox-profile-test.sh). When the profile can't
        // be resolved, we launch the child directly only so its own self-wrapper gets
        // one final chance. CX_REQUIRE_SANDBOX=1 below makes that child fail closed if
        // it still cannot apply the profile; it can never process buyer work unwrapped.
        if let argv = sandboxWrappedLaunch(binary: bin, agentArgs: agentArgs) {
            p.executableURL = URL(fileURLWithPath: AgentPaths.sandboxExec)
            p.arguments = argv
            sandboxActive = true
        } else {
            p.executableURL = bin
            p.arguments = agentArgs
            sandboxActive = false
        }
        // Point the agent at the operator-prefs overlay (Atlas F7 / item 25). The agent
        // merges this sidecar over agent.toml, so the menu toggles ACTUALLY control it —
        // they are no longer decorative. (The agent also auto-discovers this conventional
        // path next to the config; setting the env var makes the contract explicit.)
        var env = ProcessInfo.processInfo.environment
        env["CX_AGENT_PREFS"] = AgentPaths.prefsFile.path
        env["CX_STATUS_PATH"] = AgentPaths.statusFile.path
        env["CX_CONTROL_URL"] = credentials.controlURL.absoluteString
        env[AgentPaths.requireSandboxEnvKey] = "1"
        // The raw secret exists at rest only in Keychain. The Rust agent already
        // supports this environment override, so it never appears in agent.toml,
        // argv, UserDefaults, UI text, or app logs.
        env["CX_WORKER_TOKEN"] = credentials.workerToken
        // When WE applied the seatbelt profile (via sandbox-exec above), tell the agent
        // it is already sandboxed so its own startup self-re-exec
        // (agent/src/main.rs, reexec_under_sandbox_if_needed) is a no-op instead of
        // wrapping the child a SECOND time. The Rust side keys off exactly this marker to
        // break the re-exec loop; the two launch paths cooperate through it. When we could
        // NOT apply the profile (sandboxActive=false), we deliberately leave the marker
        // UNSET so the binary's own self-re-exec still gets a chance to contain a direct
        // launch — belt and suspenders, never a double no-op.
        if sandboxActive {
            env[AgentPaths.sandboxedEnvKey] = "1"
        }
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
            // Process has copied the environment into the launched child. Drop the
            // app-side copy immediately rather than retaining the raw token for the
            // full child lifetime.
            p.environment = nil
            env["CX_WORKER_TOKEN"] = nil
            process = p
            launchedPID = p.processIdentifier
        } catch {
            p.environment = nil
            env["CX_WORKER_TOKEN"] = nil
            lastLaunchError = "Failed to launch agent: \(error.localizedDescription)"
        }
    }

    /// Build the `sandbox-exec` argv that runs cx-agent under the shipped seatbelt
    /// profile (cx-agent.sb), or return nil when the sandbox cannot be applied — in
    /// which case the caller launches the agent directly and records sandboxActive=false.
    ///
    /// The profile (Security Posture 8->9) contains the child's filesystem blast radius:
    /// writes are confined to the model cache + data dir + temp, and reads of the
    /// operator's SSH keys / keychain / documents are denied — proven by
    /// macapp/ComputeExchangeAgent/sandbox-profile-test.sh. The `-D KEY=VALUE` params
    /// below are exactly the ones the profile references.
    ///
    /// Returns nil (→ unsandboxed launch, honestly flagged) when either the profile
    /// resource or `sandbox-exec` is absent, so a bare dev build without an assembled
    /// bundle still runs rather than failing to launch — the app never claims a
    /// protection it isn't applying.
    private func sandboxWrappedLaunch(binary: URL, agentArgs: [String]) -> [String]? {
        guard let profile = AgentPaths.sandboxProfile else { return nil }
        guard FileManager.default.isExecutableFile(atPath: AgentPaths.sandboxExec) else { return nil }
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let tmp = ProcessInfo.processInfo.environment["TMPDIR"] ?? "/private/var/folders"
        var argv = [
            "-f", profile.path,
            "-D", "HOME=\(home)",
            "-D", "MODELCACHE=\(AgentPaths.modelCacheDir.path)",
            "-D", "DATADIR=\(AgentPaths.dataDir.path)",
            "-D", "TMPDIR=\(tmp)",
            binary.path,
        ]
        argv.append(contentsOf: agentArgs)
        return argv
    }

    /// Stop an agent we launched. (An agent started elsewhere · e.g. a LaunchAgent
    /// · is not ours to kill; we only manage our own child.)
    func stopAgent() {
        process?.terminate()
        process = nil
        launchedPID = nil
        sandboxActive = false
        refresh()
    }

    /// Stop first, then remove the Keychain credential, app-managed base config,
    /// and non-secret enrollment receipt. This is local removal, not server-side
    /// revocation; the enrollment UI states that boundary explicitly.
    func resetEnrollment() {
        stopAgent()
        if !enrollment.reset() {
            lastLaunchError = enrollment.message
        } else {
            lastLaunchError = nil
        }
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
