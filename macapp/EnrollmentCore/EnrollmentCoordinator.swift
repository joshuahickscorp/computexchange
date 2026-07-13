import Foundation

public enum EnrollmentCoordinatorError: Error, Equatable, LocalizedError {
    case existingEnrollmentOrConfiguration
    case incompleteEnrollment
    case persistenceFailed
    case rollbackFailed
    case resetFailed

    public var errorDescription: String? {
        switch self {
        case .existingEnrollmentOrConfiguration:
            return "Enrollment data or an existing agent config is already present. Reset enrollment before replacing it."
        case .incompleteEnrollment:
            return "Enrollment is incomplete. Open enrollment and verify this Mac again."
        case .persistenceFailed:
            return "The verified enrollment could not be saved securely. No agent was started."
        case .rollbackFailed:
            return "Enrollment failed and cleanup was incomplete. Use Reset Enrollment before trying again."
        case .resetFailed:
            return "Enrollment could not be fully reset. The agent remains gated; retry reset before enrolling again."
        }
    }
}

public final class EnrollmentCoordinator {
    private let tokenStore: WorkerTokenStoring
    private let configStore: EnrollmentConfigStoring
    private let recordStore: EnrollmentRecordStoring
    private let pendingRequestStore: EnrollmentPendingRequestStoring
    private let probe: EnrollmentProbing
    private let bundleExchange: EnrollmentBundleExchanging?
    private let dataDirectory: URL
    private let now: () -> Date

    public init(
        tokenStore: WorkerTokenStoring,
        configStore: EnrollmentConfigStoring,
        recordStore: EnrollmentRecordStoring,
        pendingRequestStore: EnrollmentPendingRequestStoring,
        probe: EnrollmentProbing,
        bundleExchange: EnrollmentBundleExchanging? = nil,
        dataDirectory: URL,
        now: @escaping () -> Date = Date.init
    ) {
        self.tokenStore = tokenStore
        self.configStore = configStore
        self.recordStore = recordStore
        self.pendingRequestStore = pendingRequestStore
        self.probe = probe
        self.bundleExchange = bundleExchange
        self.dataDirectory = dataDirectory
        self.now = now
    }

    public func snapshot() throws -> EnrollmentSnapshot {
        EnrollmentSnapshot(
            record: try recordStore.loadRecord(),
            hasWorkerToken: try tokenStore.containsToken(),
            hasConfiguration: try configStore.configurationExists()
        )
    }

    /// Verify before persisting anything. Once verified, persist the non-secret
    /// config, then the Keychain token, then the non-secret record. Any failure
    /// removes everything written by this attempt so no half-enrolled launch exists.
    public func enroll(
        controlURL rawControlURL: String,
        workerToken rawWorkerToken: String,
        allowInsecureLoopback: Bool = false
    ) async throws -> EnrollmentRecord {
        let input = try EnrollmentValidator.validate(
            controlURL: rawControlURL,
            workerToken: rawWorkerToken,
            allowInsecureLoopback: allowInsecureLoopback
        )
        guard try recordStore.loadRecord() == nil,
              try !tokenStore.containsToken(),
              try !configStore.configurationExists() else {
            throw EnrollmentCoordinatorError.existingEnrollmentOrConfiguration
        }

        return try await verifyAndPersist(
            controlURL: input.controlURL,
            workerToken: input.workerToken
        )
    }

    /// Create the public, copyable request that the authenticated account surface
    /// approves. The persistent private key remains in Keychain.
    public func makeDeviceRequest(
        controlURL: String,
        allowInsecureLoopback: Bool = false
    ) throws -> String {
        guard let bundleExchange else { throw EnrollmentExchangeError.unavailable }
        guard try recordStore.loadRecord() == nil,
              try !tokenStore.containsToken(),
              try !configStore.configurationExists() else {
            throw EnrollmentCoordinatorError.existingEnrollmentOrConfiguration
        }
        let encoded = try bundleExchange.makeDeviceRequest(
            controlURL: controlURL,
            allowInsecureLoopback: allowInsecureLoopback
        )
        let request = try EnrollmentDeviceRequest.decode(
            encoded,
            allowInsecureLoopback: allowInsecureLoopback
        )
        guard let normalizedURL = URL(string: request.controlOrigin),
              let publicKey = EnrollmentProtocol.base64URLDecode(request.devicePublicKey) else {
            throw EnrollmentBundleError.invalidSchema
        }
        try pendingRequestStore.savePendingRequest(EnrollmentPendingRequest(
            encodedRequest: encoded,
            controlURL: normalizedURL,
            requestID: request.requestID,
            deviceFingerprint: try EnrollmentProtocol.deviceFingerprint(publicKey: publicKey)
        ))
        return encoded
    }

    /// Return only a self-consistent persisted request. A corrupt or partially
    /// rewritten record is not displayed as an approvable ceremony.
    public func pendingDeviceRequest(
        allowInsecureLoopback: Bool = false
    ) throws -> String? {
        try validatedPendingRequest(allowInsecureLoopback: allowInsecureLoopback)?.encodedRequest
    }

    /// Exchange a short-lived account approval, then authenticate the returned
    /// bearer against the probe before any local persistence. A consumed code may
    /// leave a server-side credential if networking is interrupted after exchange;
    /// locally, however, no token/config/record survives a failed or cancelled flow.
    public func enroll(
        bundle rawBundle: String,
        allowInsecureLoopback: Bool = false
    ) async throws -> EnrollmentRecord {
        guard let bundleExchange else { throw EnrollmentExchangeError.unavailable }
        let bundle = try EnrollmentBundle.decode(
            rawBundle.trimmingCharacters(in: .whitespacesAndNewlines),
            allowInsecureLoopback: allowInsecureLoopback
        )
        guard let controlURL = bundle.controlURL else {
            throw EnrollmentBundleError.invalidSchema
        }
        guard try recordStore.loadRecord() == nil,
              try !tokenStore.containsToken(),
              try !configStore.configurationExists() else {
            throw EnrollmentCoordinatorError.existingEnrollmentOrConfiguration
        }
        guard let pending = try validatedPendingRequest(
            allowInsecureLoopback: allowInsecureLoopback
        ), pending.controlURLString == bundle.controlOrigin,
              pending.requestID == bundle.requestID,
              pending.deviceFingerprint == bundle.deviceFingerprint else {
            throw EnrollmentBundleError.requestMismatch
        }
        let credential = try await bundleExchange.exchange(bundle)
        try Task.checkCancellation()
        return try await verifyAndPersist(
            controlURL: controlURL,
            workerToken: credential.workerToken
        )
    }

    private func verifyAndPersist(
        controlURL: URL,
        workerToken: String
    ) async throws -> EnrollmentRecord {
        let status = try await probe.verify(
            controlURL: controlURL,
            workerToken: workerToken
        )
        try Task.checkCancellation()

        let config = AgentBootstrapConfig(
            controlURL: controlURL,
            dataDirectory: dataDirectory
        ).renderTOML()
        let record = EnrollmentRecord(
            controlURL: controlURL,
            verifiedAt: now(),
            probeStatus: status
        )

        var wroteConfig = false
        var wroteToken = false
        var attemptedRecord = false
        do {
            try Task.checkCancellation()
            try configStore.writeNewConfiguration(config)
            wroteConfig = true
            try Task.checkCancellation()
            try tokenStore.saveToken(workerToken)
            wroteToken = true
            try Task.checkCancellation()
            attemptedRecord = true
            try recordStore.saveRecord(record)
            try Task.checkCancellation()
            // Delete the non-secret pending request only after every launch
            // prerequisite is durable. If deletion fails, roll back the newly
            // persisted enrollment and leave the ceremony visibly incomplete.
            try pendingRequestStore.deletePendingRequest()
            return record
        } catch {
            let clean = rollbackEnrollmentAttempt(
                removeToken: wroteToken,
                removeConfig: wroteConfig,
                removeRecord: attemptedRecord
            )
            if error is CancellationError, clean {
                throw error
            }
            throw clean ? EnrollmentCoordinatorError.persistenceFailed : EnrollmentCoordinatorError.rollbackFailed
        }
    }

    public func reverify() async throws -> EnrollmentRecord {
        guard let record = try recordStore.loadRecord(),
              let controlURL = record.controlURL,
              let token = try tokenStore.loadToken(),
              try configStore.configurationExists() else {
            throw EnrollmentCoordinatorError.incompleteEnrollment
        }
        let status = try await probe.verify(controlURL: controlURL, workerToken: token)
        let updated = EnrollmentRecord(
            controlURL: controlURL,
            verifiedAt: now(),
            probeStatus: status
        )
        do {
            try recordStore.saveRecord(updated)
            return updated
        } catch {
            throw EnrollmentCoordinatorError.persistenceFailed
        }
    }

    public func launchCredentials() throws -> EnrollmentCredentials {
        guard let record = try recordStore.loadRecord(),
              let controlURL = record.controlURL,
              let token = try tokenStore.loadToken(),
              try configStore.configurationExists() else {
            throw EnrollmentCoordinatorError.incompleteEnrollment
        }
        return EnrollmentCredentials(controlURL: controlURL, workerToken: token)
    }

    /// Fail closed: remove the Keychain credential first. If a later file/defaults
    /// deletion fails, launch still cannot authenticate and the retained record keeps
    /// the UI in a repairable incomplete state rather than pretending reset succeeded.
    public func reset() throws {
        do {
            try tokenStore.deleteToken()
            try bundleExchange?.deleteDeviceKey()
            try configStore.deleteManagedConfiguration()
            try recordStore.deleteRecord()
            try pendingRequestStore.deletePendingRequest()
        } catch {
            throw EnrollmentCoordinatorError.resetFailed
        }
    }

    @discardableResult
    private func rollbackEnrollmentAttempt(
        removeToken: Bool,
        removeConfig: Bool,
        removeRecord: Bool
    ) -> Bool {
        var clean = true
        if removeToken {
            do { try tokenStore.deleteToken() } catch { clean = false }
        }
        if removeConfig {
            do { try configStore.deleteManagedConfiguration() } catch { clean = false }
        }
        if removeRecord {
            do { try recordStore.deleteRecord() } catch { clean = false }
        }
        return clean
    }

    private func validatedPendingRequest(
        allowInsecureLoopback: Bool
    ) throws -> EnrollmentPendingRequest? {
        guard let pending = try pendingRequestStore.loadPendingRequest() else { return nil }
        guard pending.version == EnrollmentProtocol.version,
              let controlURL = pending.controlURL,
              controlURL.absoluteString == pending.controlURLString else {
            throw EnrollmentBundleError.invalidSchema
        }
        let decoded = try EnrollmentDeviceRequest.decode(
            pending.encodedRequest,
            allowInsecureLoopback: allowInsecureLoopback
        )
        guard decoded.controlOrigin == pending.controlURLString,
              decoded.requestID == pending.requestID,
              let publicKey = EnrollmentProtocol.base64URLDecode(decoded.devicePublicKey),
              try EnrollmentProtocol.deviceFingerprint(publicKey: publicKey) == pending.deviceFingerprint else {
            throw EnrollmentBundleError.requestMismatch
        }
        return pending
    }
}
