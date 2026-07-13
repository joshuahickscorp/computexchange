import SwiftUI
import EnrollmentCore
import AppKit

struct EnrollmentView: View {
    @ObservedObject var enrollment: EnrollmentStore
    var onReset: () -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var controlURL = EnrollmentStore.defaultControlURL
    @State private var enrollmentBundle = ""
    @State private var confirmingReset = false

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 10) {
                Image(systemName: enrollment.isReady ? "checkmark.shield.fill" : "link.badge.plus")
                    .font(.title)
                    .foregroundStyle(enrollment.isReady ? .green : Color.accentColor)
                VStack(alignment: .leading, spacing: 2) {
                    Text(enrollment.isReady ? "This Mac is connected" : "Connect this supplier Mac")
                        .font(.headline)
                    Text(enrollment.isReady
                         ? "The stored credential passed an authenticated server check."
                         : "No Terminal or buyer password is required in this app.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            Divider()

            if enrollment.isReady, let record = enrollment.record {
                enrolledContent(record)
            } else {
                enrollmentForm
            }

            if let message = enrollment.message {
                Label(message, systemImage: enrollment.isReady ? "checkmark.circle" : "info.circle")
                    .font(.caption)
                    .foregroundStyle(enrollment.isReady ? Color.secondary : Color.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Divider()

            HStack {
                Button("Close", role: .cancel) { dismiss() }
                Spacer()
                if enrollment.isReady {
                    Button("Recheck") {
                        Task { await enrollment.reverify() }
                    }
                    .disabled(enrollment.isWorking)
                }
            }
        }
        .padding(20)
        .frame(width: 480)
        .confirmationDialog(
            "Reset enrollment on this Mac?",
            isPresented: $confirmingReset,
            titleVisibility: .visible
        ) {
            Button("Reset local enrollment", role: .destructive) { onReset() }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This stops the agent and removes the Keychain token plus app-managed config. It does not revoke the server token; revoke it from the supplier account when retiring this Mac.")
        }
    }

    private var enrollmentForm: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Approve this Mac from your authenticated supplier account. The app never asks for or stores your buyer password.")
                .font(.callout)
                .fixedSize(horizontal: false, vertical: true)

            VStack(alignment: .leading, spacing: 5) {
                Text("Control-plane URL").font(.caption).foregroundStyle(.secondary)
                TextField("https://computexchange.net", text: $controlURL)
                    .textFieldStyle(.roundedBorder)
                    .disableAutocorrection(true)
            }

            VStack(alignment: .leading, spacing: 5) {
                Text("1. Public device request").font(.caption).foregroundStyle(.secondary)
                if let request = enrollment.deviceRequest {
                    Text(request)
                        .font(.caption2.monospaced())
                        .lineLimit(3)
                        .textSelection(.enabled)
                        .padding(7)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(Color.secondary.opacity(0.08), in: RoundedRectangle(cornerRadius: 6))
                    HStack {
                        Button("Copy device request") {
                            NSPasteboard.general.clearContents()
                            NSPasteboard.general.setString(request, forType: .string)
                        }
                        Button("Regenerate for this origin") {
                            _ = enrollment.prepareDeviceRequest(controlURL: controlURL)
                        }
                    }
                    .buttonStyle(.bordered)
                } else {
                    Button("Create device request") {
                        _ = enrollment.prepareDeviceRequest(controlURL: controlURL)
                    }
                    .buttonStyle(.bordered)
                    .disabled(enrollment.isWorking || controlURL.isEmpty)
                }
                Text("This request contains only the origin, audience, request id, and this Mac's public key. Its private key stays protected in Keychain.")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }

            VStack(alignment: .leading, spacing: 5) {
                Text("2. Account approval bundle").font(.caption).foregroundStyle(.secondary)
                SecureField("cxeb2_…", text: $enrollmentBundle)
                    .textFieldStyle(.roundedBorder)
                    .disableAutocorrection(true)
                Text("Paste the one-time approval returned by the authenticated account. The app verifies it targets this pending key, exchanges it once, and probes the returned credential before saving anything.")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }

            if enrollment.needsRepair {
                Button("Reset incomplete enrollment", role: .destructive) {
                    confirmingReset = true
                }
                .buttonStyle(.bordered)
            } else {
                Button {
                    let submittedBundle = enrollmentBundle
                    enrollmentBundle = "" // Minimize how long the short-lived code remains in SwiftUI state.
                    Task {
                        _ = await enrollment.enroll(bundle: submittedBundle)
                    }
                } label: {
                    if enrollment.isWorking {
                        ProgressView().controlSize(.small)
                    } else {
                        Label("Exchange, verify, and save", systemImage: "checkmark.shield")
                    }
                }
                .keyboardShortcut(.defaultAction)
                .buttonStyle(.borderedProminent)
                .disabled(enrollment.isWorking || enrollment.deviceRequest == nil || enrollmentBundle.isEmpty)
            }

            Text("HTTPS is required in release builds. Redirects are rejected. Regular agent requests still use the returned bearer token; enrollment proof does not make those requests device-bound.")
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
    }

    private func enrolledContent(_ record: EnrollmentRecord) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            LabeledContent("Control plane", value: record.controlURLString)
            LabeledContent("Verified", value: record.verifiedAt.formatted(date: .abbreviated, time: .shortened))
            LabeledContent("Credential", value: "Stored in Keychain")

            Text("Before uninstalling, use Reset enrollment below. Deleting the app alone does not remove Keychain items. Local reset does not revoke the server credential.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            Button("Reset enrollment", role: .destructive) {
                confirmingReset = true
            }
            .buttonStyle(.bordered)
            .disabled(enrollment.isWorking)
        }
    }
}
