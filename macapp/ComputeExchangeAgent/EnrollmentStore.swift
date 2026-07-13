import Foundation
import Combine
import EnrollmentCore

@MainActor
final class EnrollmentStore: ObservableObject {
    static let defaultControlURL = "https://computexchange.net"

    @Published private(set) var snapshot = EnrollmentSnapshot(
        record: nil,
        hasWorkerToken: false,
        hasConfiguration: false
    )
    @Published private(set) var isWorking = false
    @Published private(set) var message: String?
    @Published private(set) var deviceRequest: String?

    private let coordinator: EnrollmentCoordinator

    var isReady: Bool { snapshot.isReady }
    var record: EnrollmentRecord? { snapshot.record }
    var needsRepair: Bool {
        !isReady && (snapshot.record != nil || snapshot.hasWorkerToken || snapshot.hasConfiguration)
    }

    init(coordinator: EnrollmentCoordinator) {
        self.coordinator = coordinator
        reload()
    }

    static func live(dataDirectory: URL, configFile: URL) -> EnrollmentStore {
        let tokenStore = KeychainWorkerTokenStore()
        let configStore = FileEnrollmentConfigStore(url: configFile)
        let recordStore = UserDefaultsEnrollmentRecordStore()
        let pendingRequestStore = UserDefaultsEnrollmentPendingRequestStore()
        #if DEBUG
        let allowInsecureLoopback = true
        #else
        let allowInsecureLoopback = false
        #endif
        let coordinator = EnrollmentCoordinator(
            tokenStore: tokenStore,
            configStore: configStore,
            recordStore: recordStore,
            pendingRequestStore: pendingRequestStore,
            probe: EnrollmentProbeClient(),
            bundleExchange: EnrollmentExchangeClient(
                allowInsecureLoopback: allowInsecureLoopback
            ),
            dataDirectory: dataDirectory
        )
        return EnrollmentStore(coordinator: coordinator)
    }

    @discardableResult
    func prepareDeviceRequest(controlURL: String) -> String? {
        guard !isWorking else {
            message = "Another enrollment operation is already in progress."
            return nil
        }
        isWorking = true
        defer { isWorking = false }
        do {
            #if DEBUG
            let allowInsecureLoopback = true
            #else
            let allowInsecureLoopback = false
            #endif
            let request = try coordinator.makeDeviceRequest(
                controlURL: controlURL,
                allowInsecureLoopback: allowInsecureLoopback
            )
            deviceRequest = request
            message = "Copy this public device request into your authenticated supplier account."
            return request
        } catch {
            deviceRequest = nil
            message = error.localizedDescription
            return nil
        }
    }

    @discardableResult
    func enroll(bundle: String) async -> Bool {
        guard !isWorking else {
            message = "Another enrollment operation is already in progress."
            return false
        }
        isWorking = true
        message = "Exchanging the one-time approval and checking the returned credential…"
        defer { isWorking = false }
        do {
            #if DEBUG
            let allowInsecureLoopback = true
            #else
            let allowInsecureLoopback = false
            #endif
            _ = try await coordinator.enroll(
                bundle: bundle,
                allowInsecureLoopback: allowInsecureLoopback
            )
            deviceRequest = nil
            reload()
            message = "This Mac is authenticated and ready to start after consent."
            return true
        } catch {
            reload()
            message = error.localizedDescription
            return false
        }
    }

    @discardableResult
    func enroll(controlURL: String, workerToken: String) async -> Bool {
        guard !isWorking else {
            message = "Another enrollment operation is already in progress."
            return false
        }
        isWorking = true
        message = "Checking the server and worker token…"
        defer { isWorking = false }
        do {
            #if DEBUG
            let allowInsecureLoopback = true
            #else
            let allowInsecureLoopback = false
            #endif
            _ = try await coordinator.enroll(
                controlURL: controlURL,
                workerToken: workerToken,
                allowInsecureLoopback: allowInsecureLoopback
            )
            reload()
            message = "This Mac is authenticated and ready to start after consent."
            return true
        } catch {
            reload()
            message = error.localizedDescription
            return false
        }
    }

    @discardableResult
    func reverify() async -> Bool {
        guard !isWorking else {
            message = "Another enrollment operation is already in progress."
            return false
        }
        isWorking = true
        message = "Rechecking the stored enrollment…"
        defer { isWorking = false }
        do {
            _ = try await coordinator.reverify()
            reload()
            message = "The stored worker token is still accepted."
            return true
        } catch {
            reload()
            message = error.localizedDescription
            return false
        }
    }

    @discardableResult
    func reset() -> Bool {
        guard !isWorking else {
            message = "Another enrollment operation is already in progress."
            return false
        }
        isWorking = true
        defer { isWorking = false }
        do {
            try coordinator.reset()
            deviceRequest = nil
            reload()
            message = "Local enrollment removed. Revoke the worker token in your supplier account if this Mac is being retired."
            return true
        } catch {
            reload()
            message = error.localizedDescription
            return false
        }
    }

    func launchCredentials() throws -> EnrollmentCredentials {
        try coordinator.launchCredentials()
    }

    func reload() {
        do {
            snapshot = try coordinator.snapshot()
            #if DEBUG
            let allowInsecureLoopback = true
            #else
            let allowInsecureLoopback = false
            #endif
            deviceRequest = try coordinator.pendingDeviceRequest(
                allowInsecureLoopback: allowInsecureLoopback
            )
            if needsRepair {
                message = "Enrollment is incomplete. Reset it before trying again; existing configs are never overwritten."
            }
        } catch {
            snapshot = EnrollmentSnapshot(
                record: nil,
                hasWorkerToken: false,
                hasConfiguration: false
            )
            deviceRequest = nil
            message = "Could not inspect enrollment securely. The agent remains gated."
        }
    }
}
