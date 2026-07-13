import XCTest
@testable import EnrollmentCore

private enum FakeFailure: Error { case requested }

private final class MemoryTokenStore: WorkerTokenStoring {
    var token: String?
    var failSave = false
    var failDelete = false

    func containsToken() throws -> Bool { token != nil }
    func loadToken() throws -> String? { token }
    func saveToken(_ token: String) throws {
        if failSave { throw FakeFailure.requested }
        self.token = token
    }
    func deleteToken() throws {
        if failDelete { throw FakeFailure.requested }
        token = nil
    }
}

private final class MemoryConfigStore: EnrollmentConfigStoring {
    var contents: String?
    var failWrite = false
    var failDelete = false

    func configurationExists() throws -> Bool { contents != nil }
    func writeNewConfiguration(_ contents: String) throws {
        if failWrite { throw FakeFailure.requested }
        guard self.contents == nil else { throw FakeFailure.requested }
        self.contents = contents
    }
    func deleteManagedConfiguration() throws {
        if failDelete { throw FakeFailure.requested }
        contents = nil
    }
}

private final class MemoryRecordStore: EnrollmentRecordStoring {
    var record: EnrollmentRecord?
    var failSave = false
    var cancelSave = false
    var failDelete = false

    func loadRecord() throws -> EnrollmentRecord? { record }
    func saveRecord(_ record: EnrollmentRecord) throws {
        if cancelSave { throw CancellationError() }
        if failSave { throw FakeFailure.requested }
        self.record = record
    }
    func deleteRecord() throws {
        if failDelete { throw FakeFailure.requested }
        record = nil
    }
}

private final class MemoryPendingRequestStore: EnrollmentPendingRequestStoring {
    var request: EnrollmentPendingRequest?
    var failDelete = false

    func loadPendingRequest() throws -> EnrollmentPendingRequest? { request }
    func savePendingRequest(_ request: EnrollmentPendingRequest) throws { self.request = request }
    func deletePendingRequest() throws {
        if failDelete { throw FakeFailure.requested }
        request = nil
    }
}

private final class StubBundleExchange: EnrollmentBundleExchanging {
    let credential: EnrollmentExchangeCredential
    var exchangeError: Error?
    var exchangeCalls = 0
    var deleteKeyCalls = 0
    var requestedBundle: EnrollmentBundle?

    init(token: String) {
        credential = EnrollmentExchangeCredential(
            credentialID: UUID(uuidString: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")!,
            workerID: UUID(uuidString: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")!,
            supplierID: UUID(uuidString: "cccccccc-cccc-4ccc-8ccc-cccccccccccc")!,
            workerToken: token,
            deviceFingerprint: "p256:sha256:" + String(repeating: "a", count: 64),
            credentialVersion: 1,
            rotated: false
        )
    }

    func makeDeviceRequest(controlURL: String, allowInsecureLoopback: Bool) throws -> String {
        let normalized = try EnrollmentValidator.normalizedControlURL(
            controlURL,
            allowInsecureLoopback: allowInsecureLoopback
        )
        let publicKey = try XCTUnwrap(EnrollmentProtocol.base64URLDecode(
            "BGsX0fLhLEJH-Lzm5WOkQPJ3A32BLeszoPShOUXYmMKWT-NC4v4af5uO5-tKfA-eFivOM1drMV7Oy7ZAaDe_UfU"
        ))
        return try EnrollmentDeviceRequest(
            controlURL: normalized,
            publicKeyX963: publicKey
        ).encoded()
    }

    func exchange(_ bundle: EnrollmentBundle) async throws -> EnrollmentExchangeCredential {
        exchangeCalls += 1
        requestedBundle = bundle
        if let exchangeError { throw exchangeError }
        return credential
    }

    func deleteDeviceKey() throws { deleteKeyCalls += 1 }
}

private final class StubProbe: EnrollmentProbing {
    var status = EnrollmentProbeStatus(configured: true, connected: false, payoutsEnabled: false)
    var error: Error?
    var calls = 0

    func verify(controlURL: URL, workerToken: String) async throws -> EnrollmentProbeStatus {
        calls += 1
        if let error { throw error }
        return status
    }
}

final class EnrollmentCoordinatorTests: XCTestCase {
    private let validToken = "cxw_" + String(repeating: "C", count: 43)
    private let fixedDate = Date(timeIntervalSince1970: 1_750_000_000)

    private func makeCoordinator(
        tokenStore: MemoryTokenStore,
        configStore: MemoryConfigStore,
        recordStore: MemoryRecordStore,
        pendingRequestStore: MemoryPendingRequestStore = MemoryPendingRequestStore(),
        probe: StubProbe,
        bundleExchange: EnrollmentBundleExchanging? = nil
    ) -> EnrollmentCoordinator {
        EnrollmentCoordinator(
            tokenStore: tokenStore,
            configStore: configStore,
            recordStore: recordStore,
            pendingRequestStore: pendingRequestStore,
            probe: probe,
            bundleExchange: bundleExchange,
            dataDirectory: URL(fileURLWithPath: "/tmp/cx-enrollment-test"),
            now: { self.fixedDate }
        )
    }

    private func validBundle(
        controlOrigin: String = "https://control.example.test"
    ) throws -> String {
        let publicKey = try XCTUnwrap(EnrollmentProtocol.base64URLDecode(
            "BGsX0fLhLEJH-Lzm5WOkQPJ3A32BLeszoPShOUXYmMKWT-NC4v4af5uO5-tKfA-eFivOM1drMV7Oy7ZAaDe_UfU"
        ))
        return try EnrollmentBundle(
            controlURL: URL(string: controlOrigin)!,
            accountID: UUID(uuidString: "12345678-1234-4abc-8def-1234567890ab")!,
            enrollmentCode: "cxe_" + EnrollmentProtocol.base64URLEncode(Data(repeating: 7, count: 32)),
            requestID: EnrollmentProtocol.requestID(publicKey: publicKey),
            deviceFingerprint: EnrollmentProtocol.deviceFingerprint(publicKey: publicKey)
        ).encoded()
    }

    func testSuccessfulEnrollmentPersistsOnlyTokenInSecretStore() async throws {
        let tokens = MemoryTokenStore()
        let config = MemoryConfigStore()
        let records = MemoryRecordStore()
        let probe = StubProbe()
        let coordinator = makeCoordinator(
            tokenStore: tokens,
            configStore: config,
            recordStore: records,
            probe: probe
        )

        let record = try await coordinator.enroll(
            controlURL: "https://control.example.test/",
            workerToken: validToken
        )

        XCTAssertEqual(tokens.token, validToken)
        XCTAssertNotNil(config.contents)
        XCTAssertFalse(config.contents?.contains(validToken) ?? true)
        XCTAssertEqual(records.record, record)
        XCTAssertEqual(record.verifiedAt, fixedDate)
        XCTAssertTrue(try coordinator.snapshot().isReady)
        XCTAssertEqual(try coordinator.launchCredentials().workerToken, validToken)
    }

    func testProbeFailurePersistsNothing() async {
        let tokens = MemoryTokenStore()
        let config = MemoryConfigStore()
        let records = MemoryRecordStore()
        let probe = StubProbe()
        probe.error = EnrollmentProbeError.invalidToken
        let coordinator = makeCoordinator(
            tokenStore: tokens,
            configStore: config,
            recordStore: records,
            probe: probe
        )

        do {
            _ = try await coordinator.enroll(
                controlURL: "https://control.example.test",
                workerToken: validToken
            )
            XCTFail("invalid token unexpectedly enrolled")
        } catch {
            XCTAssertEqual(error as? EnrollmentProbeError, .invalidToken)
        }
        XCTAssertNil(tokens.token)
        XCTAssertNil(config.contents)
        XCTAssertNil(records.record)
    }

    func testRecordFailureRollsBackConfigAndKeychain() async {
        let tokens = MemoryTokenStore()
        let config = MemoryConfigStore()
        let records = MemoryRecordStore()
        records.failSave = true
        let coordinator = makeCoordinator(
            tokenStore: tokens,
            configStore: config,
            recordStore: records,
            probe: StubProbe()
        )

        do {
            _ = try await coordinator.enroll(
                controlURL: "https://control.example.test",
                workerToken: validToken
            )
            XCTFail("failed record store unexpectedly enrolled")
        } catch {
            XCTAssertEqual(error as? EnrollmentCoordinatorError, .persistenceFailed)
        }
        XCTAssertNil(tokens.token)
        XCTAssertNil(config.contents)
        XCTAssertNil(records.record)
    }

    func testTokenSaveFailureRollsBackOnlyNewConfig() async {
        let tokens = MemoryTokenStore()
        tokens.failSave = true
        let config = MemoryConfigStore()
        let records = MemoryRecordStore()
        let coordinator = makeCoordinator(
            tokenStore: tokens,
            configStore: config,
            recordStore: records,
            probe: StubProbe()
        )

        do {
            _ = try await coordinator.enroll(
                controlURL: "https://control.example.test",
                workerToken: validToken
            )
            XCTFail("failed token store unexpectedly enrolled")
        } catch {
            XCTAssertEqual(error as? EnrollmentCoordinatorError, .persistenceFailed)
        }
        XCTAssertNil(tokens.token)
        XCTAssertNil(config.contents)
        XCTAssertNil(records.record)
    }

    func testRollbackFailureIsSurfaced() async {
        let tokens = MemoryTokenStore()
        tokens.failDelete = true
        let config = MemoryConfigStore()
        let records = MemoryRecordStore()
        records.failSave = true
        let coordinator = makeCoordinator(
            tokenStore: tokens,
            configStore: config,
            recordStore: records,
            probe: StubProbe()
        )

        do {
            _ = try await coordinator.enroll(
                controlURL: "https://control.example.test",
                workerToken: validToken
            )
            XCTFail("rollback failure unexpectedly passed")
        } catch {
            XCTAssertEqual(error as? EnrollmentCoordinatorError, .rollbackFailed)
        }
        XCTAssertEqual(tokens.token, validToken)
        XCTAssertNil(config.contents)
    }

    func testExistingConfigIsNeverOverwrittenOrProbed() async {
        let tokens = MemoryTokenStore()
        let config = MemoryConfigStore()
        config.contents = "manual config"
        let records = MemoryRecordStore()
        let probe = StubProbe()
        let coordinator = makeCoordinator(
            tokenStore: tokens,
            configStore: config,
            recordStore: records,
            probe: probe
        )

        do {
            _ = try await coordinator.enroll(
                controlURL: "https://control.example.test",
                workerToken: validToken
            )
            XCTFail("existing config unexpectedly overwritten")
        } catch {
            XCTAssertEqual(
                error as? EnrollmentCoordinatorError,
                .existingEnrollmentOrConfiguration
            )
        }
        XCTAssertEqual(config.contents, "manual config")
        XCTAssertEqual(probe.calls, 0)
    }

    func testReverifyUpdatesReceiptUsingStoredToken() async throws {
        let tokens = MemoryTokenStore()
        tokens.token = validToken
        let config = MemoryConfigStore()
        config.contents = AgentBootstrapConfig.managedMarker
        let old = EnrollmentRecord(
            controlURL: URL(string: "https://control.example.test")!,
            verifiedAt: Date(timeIntervalSince1970: 1),
            probeStatus: EnrollmentProbeStatus(configured: false, connected: false, payoutsEnabled: false)
        )
        let records = MemoryRecordStore()
        records.record = old
        let probe = StubProbe()
        probe.status = EnrollmentProbeStatus(configured: true, connected: true, payoutsEnabled: true)
        let coordinator = makeCoordinator(
            tokenStore: tokens,
            configStore: config,
            recordStore: records,
            probe: probe
        )

        let updated = try await coordinator.reverify()
        XCTAssertEqual(updated.verifiedAt, fixedDate)
        XCTAssertTrue(updated.probeStatus.payoutsEnabled)
        XCTAssertEqual(probe.calls, 1)
    }

    func testResetDeletesCredentialBeforeOtherState() throws {
        let tokens = MemoryTokenStore()
        tokens.token = validToken
        let config = MemoryConfigStore()
        config.contents = AgentBootstrapConfig.managedMarker
        let records = MemoryRecordStore()
        records.record = EnrollmentRecord(
            controlURL: URL(string: "https://control.example.test")!,
            verifiedAt: fixedDate,
            probeStatus: EnrollmentProbeStatus(configured: false, connected: false, payoutsEnabled: false)
        )
        let bundleExchange = StubBundleExchange(token: validToken)
        let coordinator = makeCoordinator(
            tokenStore: tokens,
            configStore: config,
            recordStore: records,
            probe: StubProbe(),
            bundleExchange: bundleExchange
        )

        try coordinator.reset()
        XCTAssertNil(tokens.token)
        XCTAssertNil(config.contents)
        XCTAssertNil(records.record)
        XCTAssertFalse(try coordinator.snapshot().isReady)
        XCTAssertEqual(bundleExchange.deleteKeyCalls, 1)
    }

    func testBundleExchangeProbesBeforePersistingReturnedBearer() async throws {
        let tokens = MemoryTokenStore()
        let config = MemoryConfigStore()
        let records = MemoryRecordStore()
        let probe = StubProbe()
        let exchange = StubBundleExchange(token: validToken)
        let pending = MemoryPendingRequestStore()
        let coordinator = makeCoordinator(
            tokenStore: tokens,
            configStore: config,
            recordStore: records,
            pendingRequestStore: pending,
            probe: probe,
            bundleExchange: exchange
        )

        _ = try coordinator.makeDeviceRequest(controlURL: "https://control.example.test")
        XCTAssertNotNil(pending.request)
        let record = try await coordinator.enroll(bundle: validBundle())
        XCTAssertEqual(exchange.exchangeCalls, 1)
        XCTAssertEqual(probe.calls, 1)
        XCTAssertEqual(tokens.token, validToken)
        XCTAssertEqual(records.record, record)
        XCTAssertFalse(config.contents?.contains(validToken) ?? true)
        XCTAssertNil(pending.request)
    }

    func testPersistedPendingRequestRejectsApprovalOriginSubstitutionBeforeExchange() async throws {
        let tokens = MemoryTokenStore()
        let config = MemoryConfigStore()
        let records = MemoryRecordStore()
        let pending = MemoryPendingRequestStore()
        let exchange = StubBundleExchange(token: validToken)
        let firstCoordinator = makeCoordinator(
            tokenStore: tokens,
            configStore: config,
            recordStore: records,
            pendingRequestStore: pending,
            probe: StubProbe(),
            bundleExchange: exchange
        )
        let request = try firstCoordinator.makeDeviceRequest(
            controlURL: "https://control.example.test"
        )

        // A fresh coordinator simulates an app restart: the security decision
        // comes from durable pending state, not EnrollmentStore's in-memory text.
        let restarted = makeCoordinator(
            tokenStore: tokens,
            configStore: config,
            recordStore: records,
            pendingRequestStore: pending,
            probe: StubProbe(),
            bundleExchange: exchange
        )
        XCTAssertEqual(try restarted.pendingDeviceRequest(), request)
        do {
            _ = try await restarted.enroll(
                bundle: validBundle(controlOrigin: "https://relay.example.test")
            )
            XCTFail("approval with a substituted HTTPS origin unexpectedly exchanged")
        } catch {
            XCTAssertEqual(error as? EnrollmentBundleError, .requestMismatch)
        }
        XCTAssertEqual(exchange.exchangeCalls, 0)
        XCTAssertNil(tokens.token)
        XCTAssertNotNil(pending.request)
    }

    func testBundleProbeFailurePersistsNothingAfterExchange() async throws {
        let tokens = MemoryTokenStore()
        let config = MemoryConfigStore()
        let records = MemoryRecordStore()
        let probe = StubProbe()
        probe.error = EnrollmentProbeError.invalidToken
        let exchange = StubBundleExchange(token: validToken)
        let coordinator = makeCoordinator(
            tokenStore: tokens,
            configStore: config,
            recordStore: records,
            probe: probe,
            bundleExchange: exchange
        )

        _ = try coordinator.makeDeviceRequest(controlURL: "https://control.example.test")
        do {
            _ = try await coordinator.enroll(bundle: validBundle())
            XCTFail("unprobeable exchanged bearer unexpectedly persisted")
        } catch {
            XCTAssertEqual(error as? EnrollmentProbeError, .invalidToken)
        }
        XCTAssertEqual(exchange.exchangeCalls, 1)
        XCTAssertNil(tokens.token)
        XCTAssertNil(config.contents)
        XCTAssertNil(records.record)
    }

    func testCancellationDuringPersistenceRollsBackTokenConfigAndRecord() async throws {
        let tokens = MemoryTokenStore()
        let config = MemoryConfigStore()
        let records = MemoryRecordStore()
        records.cancelSave = true
        let coordinator = makeCoordinator(
            tokenStore: tokens,
            configStore: config,
            recordStore: records,
            probe: StubProbe(),
            bundleExchange: StubBundleExchange(token: validToken)
        )

        _ = try coordinator.makeDeviceRequest(controlURL: "https://control.example.test")
        do {
            _ = try await coordinator.enroll(bundle: validBundle())
            XCTFail("cancelled persistence unexpectedly completed")
        } catch {
            XCTAssertTrue(error is CancellationError)
        }
        XCTAssertNil(tokens.token)
        XCTAssertNil(config.contents)
        XCTAssertNil(records.record)
    }

    func testExistingStateBlocksBeforeConsumingBundle() async throws {
        let tokens = MemoryTokenStore()
        let config = MemoryConfigStore()
        let records = MemoryRecordStore()
        let exchange = StubBundleExchange(token: validToken)
        let coordinator = makeCoordinator(
            tokenStore: tokens,
            configStore: config,
            recordStore: records,
            probe: StubProbe(),
            bundleExchange: exchange
        )
        _ = try coordinator.makeDeviceRequest(controlURL: "https://control.example.test")
        config.contents = "manual config"
        do {
            _ = try await coordinator.enroll(bundle: validBundle())
            XCTFail("existing state consumed a one-time bundle")
        } catch {
            XCTAssertEqual(error as? EnrollmentCoordinatorError, .existingEnrollmentOrConfiguration)
        }
        XCTAssertEqual(exchange.exchangeCalls, 0)
        XCTAssertEqual(config.contents, "manual config")
    }
}
