//  ConsentView.swift
//  First-run onboarding sheet. Shown automatically before the agent can accept any
//  work, and re-shown if the terms version changes. Spells out what runs on the
//  machine, the resource limits, quiet hours, and the 90/10 split, then requires an
//  explicit accept. Declining leaves the agent gated (it will not earn).

import SwiftUI

struct ConsentView: View {
    @ObservedObject var consent: ConsentStore
    /// Called after the operator accepts, so the host can dismiss the sheet.
    var onAccept: () -> Void
    /// Called when the operator declines / closes without accepting.
    var onDecline: () -> Void

    @State private var checkedUnderstand = false

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 10) {
                Image(systemName: "bolt.shield.fill")
                    .font(.title)
                    .foregroundStyle(Color.accentColor)
                VStack(alignment: .leading, spacing: 1) {
                    Text("Before Computexchange earns on this Mac")
                        .font(.headline)
                    Text("Please review what runs and your protections.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }

            Divider()

            VStack(alignment: .leading, spacing: 10) {
                ForEach(Array(ConsentTerms.points.enumerated()), id: \.offset) { _, point in
                    HStack(alignment: .top, spacing: 8) {
                        Image(systemName: "checkmark.circle.fill")
                            .foregroundStyle(.green)
                            .font(.callout)
                        Text(point)
                            .font(.callout)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }

            // The headline split, restated as a prominent badge so it can't be missed.
            HStack {
                Spacer()
                VStack(spacing: 2) {
                    Text("\(ConsentTerms.supplierSharePct)% you · \(ConsentTerms.platformSharePct)% platform")
                        .font(.headline.monospacedDigit())
                    Text("Your share of every job").font(.caption2).foregroundStyle(.secondary)
                }
                .padding(.vertical, 8).padding(.horizontal, 14)
                .background(Color.accentColor.opacity(0.12), in: RoundedRectangle(cornerRadius: 10))
                Spacer()
            }

            Toggle(isOn: $checkedUnderstand) {
                Text("I understand what runs on my Mac and the \(ConsentTerms.supplierSharePct)/\(ConsentTerms.platformSharePct) split.")
                    .font(.callout)
            }
            .toggleStyle(.checkbox)

            Divider()

            HStack {
                Button("Not now", role: .cancel) { onDecline() }
                Spacer()
                Button {
                    consent.accept()
                    onAccept()
                } label: {
                    Text("Accept and continue").bold()
                }
                .keyboardShortcut(.defaultAction)
                .disabled(!checkedUnderstand)
                .buttonStyle(.borderedProminent)
            }
        }
        .padding(20)
        .frame(width: 460)
    }
}
