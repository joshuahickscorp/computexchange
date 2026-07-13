//  Consent.swift
//  First-run consent / onboarding. The operator must explicitly accept what runs
//  on their machine BEFORE the agent is allowed to accept any work. Consent is
//  persisted (UserDefaults) and gates the launch path in AgentController.
//
//  Honesty note (BLACKHOLE): consent is a hard gate, not a cosmetic banner. Until
//  `ConsentStore.granted` is true, AgentController.startAgent() refuses to launch
//  the agent and surfaces why. We never start earning before the operator has seen
//  and accepted the terms below.

import Foundation
import Combine

/// Persisted record of the operator's consent. Versioned so a future material
/// change to the terms (e.g. a different revenue split) can require re-consent by
/// bumping `currentVersion`.
struct ConsentRecord: Codable, Equatable {
    var version: Int
    var acceptedAt: TimeInterval

    // Bumped 3→4 because the network disclosure was materially corrected: model
    // files come from approved model hosts (currently Hugging Face), and job I/O may
    // use object storage. Version 3's "control plane only" sentence was false. A
    // supplier must see the real outbound boundary rather than inheriting stale
    // consent. (The prior 2→3 bump covered the seatbelt sandbox change.)
    static let currentVersion = 4
}

/// The fixed terms presented at first run. Kept here (not hard-coded in the view)
/// so the same numbers drive both the copy and any future agreement export.
enum ConsentTerms {
    /// Supplier's share of buyer revenue. The platform keeps the remainder.
    static let supplierSharePct = 90
    static let platformSharePct = 10

    /// What actually runs and the default protections, stated plainly. These match
    /// the agent's real defaults (OperatorPrefs / agent config): power-only,
    /// memory headroom, quiet hours, dynamic throttling.
    ///
    /// Honesty note (docs/internal/CREED_AND_PATH_TO_TEN.md, Security Posture 8→9):
    /// the sandbox line is now ACCURATE about a real, enforced protection. The
    /// menu-bar app launches the cx-agent child under a macOS seatbelt sandbox
    /// (macapp/ComputeExchangeAgent/cx-agent.sb, applied via sandbox-exec in
    /// AgentController.sandboxWrappedLaunch): the child's filesystem writes are
    /// confined to the model cache + the agent's own data dir + temp, and it CANNOT
    /// read the operator's SSH keys, cloud credentials, keychain, or Documents. That
    /// containment is proven by sandbox-profile-test.sh and gated in CI. What the
    /// copy deliberately does NOT claim: the seatbelt profile does not yet restrict
    /// the child's network to only the control/storage hosts (macOS seatbelt can't
    /// filter by hostname from a profile — that half is a named follow-up in
    /// docs/SECURITY.md). The line below states exactly what IS enforced without
    /// overclaiming the part that isn't; the memory/thermal/quiet-hours/power lines
    /// that follow describe the other real, enforced protections.
    static let points: [String] = [
        "Computexchange runs a compute agent (cx-agent) on this Mac that performs paid inference jobs for buyers when your machine is idle and eligible. Both this menu-bar app and the cx-agent process it launches run inside a macOS sandbox: the compute agent's file access is confined to its model cache and its own working folder, and it cannot read your SSH keys, passwords/keychain, or your Documents. (The sandbox does not yet restrict which servers the agent can reach — that protection is in progress and described in our security notes.)",
        "You keep \(supplierSharePct)% of what each job earns; Computexchange keeps \(platformSharePct)%.",
        "Resource limits: the agent reserves memory headroom for you and pauses new work under memory pressure or high thermals (dynamic throttling, backed by macOS's own thermal-pressure signal) · it backs off before it would slow you down.",
        "Power: by default the agent only works while on AC power and pauses on battery.",
        "Quiet hours: you can set hours when the agent never accepts work.",
        "You can stop the agent at any time from this menu; stopping is immediate.",
        "The agent makes outbound network connections to the Computexchange control plane, job object storage, and approved model hosts (currently Hugging Face for model downloads). It does not use your microphone, camera, or location.",
    ]
}

/// Owns the persisted consent state. The app reads `granted` to decide whether the
/// onboarding sheet must be shown and whether the agent may launch.
@MainActor
final class ConsentStore: ObservableObject {
    @Published private(set) var record: ConsentRecord?

    static let defaultsKey = "cx.consent"

    init() {
        if let data = UserDefaults.standard.data(forKey: Self.defaultsKey),
           let decoded = try? JSONDecoder().decode(ConsentRecord.self, from: data) {
            record = decoded
        }
    }

    /// True only when a stored consent exists AND matches the current terms version.
    var granted: Bool {
        guard let r = record else { return false }
        return r.version == ConsentRecord.currentVersion
    }

    /// Human date of when consent was accepted, for the trust panel.
    var acceptedDateString: String? {
        guard let r = record else { return nil }
        let f = DateFormatter()
        f.dateStyle = .medium
        f.timeStyle = .short
        return f.string(from: Date(timeIntervalSince1970: r.acceptedAt))
    }

    func accept() {
        let r = ConsentRecord(version: ConsentRecord.currentVersion,
                              acceptedAt: Date().timeIntervalSince1970)
        record = r
        if let data = try? JSONEncoder().encode(r) {
            UserDefaults.standard.set(data, forKey: Self.defaultsKey)
        }
    }

    /// Withdraw consent (used by a "revoke" affordance). Clears the record so the
    /// agent is gated again until the operator re-accepts.
    func revoke() {
        record = nil
        UserDefaults.standard.removeObject(forKey: Self.defaultsKey)
    }
}
