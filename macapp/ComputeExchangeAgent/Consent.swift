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

    static let currentVersion = 1
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
    static let points: [String] = [
        "Computexchange runs a sandboxed compute agent (cx-agent) on this Mac that performs paid inference jobs for buyers when your machine is idle and eligible.",
        "You keep \(supplierSharePct)% of what each job earns; Computexchange keeps \(platformSharePct)%.",
        "Resource limits: the agent reserves memory headroom for you and pauses new work under memory pressure or high thermals (dynamic throttling) · it backs off before it would slow you down.",
        "Power: by default the agent only works while on AC power and pauses on battery.",
        "Quiet hours: you can set hours when the agent never accepts work.",
        "You can stop the agent at any time from this menu; stopping is immediate.",
        "The agent makes outbound network connections to the Computexchange control plane only. It does not use your microphone, camera, or location.",
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
