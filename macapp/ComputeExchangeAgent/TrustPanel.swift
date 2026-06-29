//  TrustPanel.swift
//  The "trust" section of the popover: an earnings sparkline, payout proof
//  (last/next + payout-readiness), and a verification badge.
//
//  Honesty rules (BLACKHOLE) baked into every subview here:
//   - Sparkline: drawn ONLY from the app's own observed history (EarningsHistory).
//     With fewer than two points it shows "Not enough history yet", never a faked
//     line.
//   - Payout proof: shows last/next payout ONLY when status.json carried real
//     timestamps; payout readiness mirrors the control plane's
//     configured/connected/payouts_enabled, and when payouts are not yet enabled it
//     says so plainly instead of implying money is on the way.
//   - Verification badge: GREEN only when honeypot/redundancy counts actually back
//     it (label "verified"); otherwise an honest amber/neutral state. We never show
//     a badge we cannot back with counts.

import SwiftUI

struct TrustPanel: View {
    @ObservedObject var controller: AgentController
    @ObservedObject var history: EarningsHistory

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            sparklineSection
            payoutSection
            verificationSection
        }
    }

    // MARK: earnings sparkline

    private var sparklineSection: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text("Earnings trend").font(.caption2).foregroundStyle(.secondary)
                Spacer()
                Text(String(format: "lifetime $%.2f", controller.status.lifetimeUsd))
                    .font(.caption.monospacedDigit()).foregroundStyle(.secondary)
            }
            if history.hasTrend {
                Sparkline(values: history.series)
                    .frame(height: 30)
                    .accessibilityLabel("Lifetime earnings trend")
            } else {
                Text("Not enough history yet · the trend appears once the agent has run for a while.")
                    .font(.caption2).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(height: 30, alignment: .leading)
            }
        }
    }

    // MARK: payout proof

    private var payoutSection: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Payouts").font(.caption2).foregroundStyle(.secondary)
            payoutReadiness
            HStack(spacing: 16) {
                payoutStat("Last payout", lastPayoutText)
                payoutStat("Next payout", nextPayoutText)
            }
        }
    }

    /// Readiness line, mirroring GET /v1/worker/connect/status. Honest about the
    /// boundary: if the control plane has no Stripe key, or the supplier has not
    /// connected an account, we say exactly that.
    @ViewBuilder private var payoutReadiness: some View {
        let configured = controller.status.payoutsConfigured
        let connected = controller.status.payoutsConnected
        let enabled = controller.status.payoutsEnabled
        if configured == nil && connected == nil && enabled == nil {
            badgeLine(symbol: "questionmark.circle", color: .secondary,
                      text: "Payout status unknown · the agent has not reported it yet.")
        } else if configured == false {
            badgeLine(symbol: "exclamationmark.triangle.fill", color: .orange,
                      text: "Payouts not configured on the control plane yet.")
        } else if connected == false {
            badgeLine(symbol: "link.badge.plus", color: .orange,
                      text: "Connect a payout account to get paid.")
        } else if enabled == true {
            badgeLine(symbol: "checkmark.seal.fill", color: .green,
                      text: "Payout account active · earnings can be paid out.")
        } else {
            badgeLine(symbol: "clock.fill", color: .orange,
                      text: "Payout account linked; activation pending.")
        }
    }

    private var lastPayoutText: String {
        guard let at = controller.status.lastPayoutAt else { return "None yet" }
        let when = relativeDate(at)
        if let amt = controller.status.lastPayoutUsd {
            return String(format: "$%.2f · %@", amt, when)
        }
        return when
    }

    private var nextPayoutText: String {
        guard let at = controller.status.nextPayoutAt else { return "Not scheduled" }
        return relativeDate(at)
    }

    // MARK: verification badge

    @ViewBuilder private var verificationSection: some View {
        let passed = controller.status.honeypotsPassed
        let failed = controller.status.honeypotsFailed
        let label = controller.status.verificationLabel
        VStack(alignment: .leading, spacing: 4) {
            Text("Verification").font(.caption2).foregroundStyle(.secondary)
            if passed == nil && failed == nil && label == nil {
                badgeLine(symbol: "shield", color: .secondary,
                          text: "No verification results reported yet.")
            } else {
                let p = passed ?? 0
                let f = failed ?? 0
                // GREEN only when checks ran, none failed, and the derived label is a
                // real "verified". Any failure, or an unverified label, downgrades it.
                let healthy = f == 0 && p > 0 && (label == "verified" || label == "honeypot-checked")
                badgeLine(
                    symbol: healthy ? "checkmark.shield.fill" : (f > 0 ? "xmark.shield.fill" : "shield"),
                    color: healthy ? .green : (f > 0 ? .red : .secondary),
                    text: verificationText(passed: p, failed: f, label: label)
                )
            }
        }
    }

    private func verificationText(passed: Int, failed: Int, label: String?) -> String {
        if failed > 0 {
            return "\(failed) honeypot check\(failed == 1 ? "" : "s") FAILED · reputation at risk (\(passed) passed)."
        }
        if passed > 0 {
            let l = label.map { " · \($0)" } ?? ""
            return "\(passed) honeypot check\(passed == 1 ? "" : "s") passed\(l)."
        }
        return "No checks have run yet."
    }

    // MARK: shared bits

    private func payoutStat(_ key: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(key).font(.caption2).foregroundStyle(.secondary)
            Text(value).font(.caption.monospacedDigit())
        }
    }

    private func badgeLine(symbol: String, color: Color, text: String) -> some View {
        Label(text, systemImage: symbol)
            .font(.caption)
            .foregroundStyle(color)
            .fixedSize(horizontal: false, vertical: true)
    }

    private func relativeDate(_ ts: TimeInterval) -> String {
        let f = RelativeDateTimeFormatter()
        f.unitsStyle = .abbreviated
        return f.localizedString(for: Date(timeIntervalSince1970: ts), relativeTo: Date())
    }
}

/// A minimal sparkline. Plots `values` (oldest→newest) as a normalized polyline.
/// Pure SwiftUI Shape · no external charting dependency. Renders nothing
/// meaningful for <2 points (callers gate on EarningsHistory.hasTrend).
struct Sparkline: View {
    let values: [Double]

    var body: some View {
        GeometryReader { geo in
            let path = makePath(in: geo.size)
            ZStack {
                path.stroke(Color.accentColor, style: StrokeStyle(lineWidth: 1.5, lineJoin: .round))
                // Soft fill under the line for a finished look.
                path.fill(.clear)
            }
        }
    }

    private func makePath(in size: CGSize) -> Path {
        var p = Path()
        guard values.count >= 2 else { return p }
        let minV = values.min() ?? 0
        let maxV = values.max() ?? 1
        let span = max(maxV - minV, 0.0001)   // avoid divide-by-zero on a flat line
        let stepX = size.width / CGFloat(values.count - 1)
        for (i, v) in values.enumerated() {
            let x = CGFloat(i) * stepX
            // Invert y: higher value => higher on screen.
            let y = size.height - CGFloat((v - minV) / span) * size.height
            if i == 0 { p.move(to: CGPoint(x: x, y: y)) }
            else { p.addLine(to: CGPoint(x: x, y: y)) }
        }
        return p
    }
}
