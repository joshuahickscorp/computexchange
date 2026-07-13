import CryptoKit
import Foundation
import Security
import XCTest
@testable import EnrollmentCore

private final class CryptoKitEnrollmentKey: EnrollmentDeviceKeySigning {
    let privateKey = P256.Signing.PrivateKey()

    func publicKeyX963() throws -> Data {
        privateKey.publicKey.x963Representation
    }

    func signEnrollmentTranscript(_ transcript: Data) throws -> Data {
        try privateKey.signature(for: transcript).derRepresentation
    }
}

private final class MemoryEnrollmentKeyStore: EnrollmentDeviceKeyStoring {
    let key: CryptoKitEnrollmentKey
    var deleteCalls = 0

    init(key: CryptoKitEnrollmentKey = CryptoKitEnrollmentKey()) {
        self.key = key
    }

    func loadOrCreateKey() throws -> EnrollmentDeviceKeySigning { key }
    func deleteKey() throws { deleteCalls += 1 }
}

private final class ExchangeStubTransport: EnrollmentHTTPTransport {
    var request: URLRequest?
    var calls = 0
    var result: Result<(Data, HTTPURLResponse), Error>

    init(
        status: Int,
        body: String = "{}",
        headers: [String: String] = ["Content-Type": "application/json", "Cache-Control": "no-store"]
    ) {
        let url = URL(string: "https://control.example.test/v1/worker/enrollment/exchange")!
        result = .success((
            Data(body.utf8),
            HTTPURLResponse(
                url: url,
                statusCode: status,
                httpVersion: "HTTP/1.1",
                headerFields: headers
            )!
        ))
    }

    func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        calls += 1
        self.request = request
        return try result.get()
    }
}

final class EnrollmentExchangeTests: XCTestCase {
    private let origin = URL(string: "https://control.example.test")!
    private let accountID = UUID(uuidString: "12345678-1234-4abc-8def-1234567890ab")!
    private let code = "cxe_" + EnrollmentProtocol.base64URLEncode(Data(repeating: 0x5a, count: 32))
    private let workerToken = "cxw_" + String(repeating: "T", count: 43)

    private func bundle(for key: CryptoKitEnrollmentKey) throws -> EnrollmentBundle {
        let publicKey = try key.publicKeyX963()
        return EnrollmentBundle(
            controlURL: origin,
            accountID: accountID,
            enrollmentCode: code,
            requestID: try EnrollmentProtocol.requestID(publicKey: publicKey),
            deviceFingerprint: try EnrollmentProtocol.deviceFingerprint(publicKey: publicKey)
        )
    }

    func testPublicRequestAndOnePasteBundleRoundTripStrictly() throws {
        let key = CryptoKitEnrollmentKey()
        let publicKey = try key.publicKeyX963()
        let request = try EnrollmentDeviceRequest(controlURL: origin, publicKeyX963: publicKey)
        let encodedRequest = try request.encoded()
        XCTAssertTrue(encodedRequest.hasPrefix("cxer2_"))
        XCTAssertEqual(try EnrollmentDeviceRequest.decode(encodedRequest + "\n"), request)
        XCTAssertEqual(request.requestID, try EnrollmentProtocol.requestID(publicKey: publicKey))

        let bundle = try bundle(for: key)
        let encodedBundle = try bundle.encoded()
        XCTAssertTrue(encodedBundle.hasPrefix("cxeb2_"))
        XCTAssertEqual(try EnrollmentBundle.decode("  \(encodedBundle)\n"), bundle)
        XCTAssertFalse(encodedRequest.contains(code))
        XCTAssertFalse(encodedBundle.contains(workerToken))
    }

    func testAccountSideRequestDecoderRejectsTamperedKeyBinding() throws {
        let key = CryptoKitEnrollmentKey()
        let request = try EnrollmentDeviceRequest(
            controlURL: origin,
            publicKeyX963: key.publicKeyX963()
        )
        let tampered = EnrollmentDeviceRequestPayload(
            version: request.version,
            controlOrigin: request.controlOrigin,
            audience: request.audience,
            requestID: String(repeating: "A", count: 22),
            deviceKeyAlgorithm: request.deviceKeyAlgorithm,
            devicePublicKey: request.devicePublicKey
        )
        let encoded = try EnrollmentProtocol.encode(tampered, prefix: EnrollmentProtocol.requestPrefix)
        XCTAssertThrowsError(try EnrollmentDeviceRequest.decode(encoded)) { error in
            XCTAssertEqual(error as? EnrollmentBundleError, .requestMismatch)
        }
    }

    func testExactTranscriptBindsCanonicalOriginRequestAndLowercaseAccountUUID() {
        let requestID = "ptTtmFOpwVsIlLyS0VBcnQ"
        let transcript = EnrollmentProtocol.transcript(
            code: code,
            audience: EnrollmentProtocol.audience,
            accountID: accountID,
            controlOrigin: origin.absoluteString,
            requestID: requestID
        )
        XCTAssertEqual(
            String(data: transcript, encoding: .utf8),
            "cx-worker-enrollment-exchange-v2\n" +
            "cx-macos-agent-v2\n12345678-1234-4abc-8def-1234567890ab\n" +
            "https://control.example.test\n\(requestID)\n\(code)"
        )
    }

    func testFingerprintAndRequestIDMatchStableServerWireAlgorithm() throws {
        let generatorPoint = try XCTUnwrap(EnrollmentProtocol.base64URLDecode(
            "BGsX0fLhLEJH-Lzm5WOkQPJ3A32BLeszoPShOUXYmMKWT-NC4v4af5uO5-tKfA-eFivOM1drMV7Oy7ZAaDe_UfU"
        ))
        XCTAssertEqual(
            try EnrollmentProtocol.deviceFingerprint(publicKey: generatorPoint),
            "p256:sha256:802d6f79ddfd7f53c5933f91d41ef41b466f68c0b03eee10aca5dd00a19791dd"
        )
        XCTAssertEqual(
            try EnrollmentProtocol.requestID(publicKey: generatorPoint),
            "ptTtmFOpwVsIlLyS0VBcnQ"
        )
    }

    func testExchangeSignsExactBodyAndAcceptsOnlyNoStoreCredential() async throws {
        let keyStore = MemoryEnrollmentKeyStore()
        let bundle = try bundle(for: keyStore.key)
        let responseBody = """
        {"credential_id":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
         "worker_id":"bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
         "supplier_id":"cccccccc-cccc-4ccc-8ccc-cccccccccccc",
         "worker_token":"\(workerToken)",
         "device_fingerprint":"\(bundle.deviceFingerprint)",
         "credential_version":1,"rotated":false}
        """
        let transport = ExchangeStubTransport(status: 201, body: responseBody)
        let client = EnrollmentExchangeClient(keyStore: keyStore, transport: transport)

        let result = try await client.exchange(bundle)
        XCTAssertEqual(result.workerToken, workerToken)
        XCTAssertEqual(transport.request?.httpMethod, "POST")
        XCTAssertEqual(transport.request?.url?.path, "/v1/worker/enrollment/exchange")
        XCTAssertEqual(transport.request?.value(forHTTPHeaderField: "Cache-Control"), "no-store")

        let body = try XCTUnwrap(transport.request?.httpBody)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: body) as? [String: Any])
        XCTAssertEqual(object["enrollment_code"] as? String, code)
        XCTAssertEqual(object["v"] as? Int, EnrollmentProtocol.version)
        XCTAssertEqual(object["control_origin"] as? String, origin.absoluteString)
        XCTAssertEqual(object["request_id"] as? String, bundle.requestID)
        XCTAssertEqual(object["audience"] as? String, EnrollmentProtocol.audience)
        XCTAssertEqual(object["device_key_algorithm"] as? String, "p256")
        XCTAssertEqual(
            object["device_public_key"] as? String,
            EnrollmentProtocol.base64URLEncode(try keyStore.key.publicKeyX963())
        )
        let proofText = try XCTUnwrap(object["proof"] as? String)
        let proofDER = try XCTUnwrap(EnrollmentProtocol.base64URLDecode(proofText))
        let signature = try P256.Signing.ECDSASignature(derRepresentation: proofDER)
        XCTAssertTrue(keyStore.key.privateKey.publicKey.isValidSignature(
            signature,
            for: EnrollmentProtocol.transcript(
                code: code,
                audience: EnrollmentProtocol.audience,
                accountID: accountID,
                controlOrigin: origin.absoluteString,
                requestID: bundle.requestID
            )
        ))
    }

    func testExchangeRejectsWrongPendingKeyBeforeNetwork() async throws {
        let approvedKey = CryptoKitEnrollmentKey()
        let localStore = MemoryEnrollmentKeyStore()
        let transport = ExchangeStubTransport(status: 201)
        let client = EnrollmentExchangeClient(keyStore: localStore, transport: transport)
        do {
            _ = try await client.exchange(bundle(for: approvedKey))
            XCTFail("approval for another key unexpectedly exchanged")
        } catch {
            XCTAssertEqual(error as? EnrollmentBundleError, .requestMismatch)
        }
        XCTAssertEqual(transport.calls, 0)
    }

    func testExchangeRevalidatesOriginAndRejectsCacheableOrNilIDResponses() async throws {
        let keyStore = MemoryEnrollmentKeyStore()
        let validBundle = try bundle(for: keyStore.key)
        let insecure = EnrollmentBundle(
            controlURL: URL(string: "http://control.example.test")!,
            accountID: accountID,
            enrollmentCode: code,
            requestID: validBundle.requestID,
            deviceFingerprint: validBundle.deviceFingerprint
        )
        let unused = ExchangeStubTransport(status: 201)
        do {
            _ = try await EnrollmentExchangeClient(keyStore: keyStore, transport: unused).exchange(insecure)
            XCTFail("constructed HTTP bundle bypassed client validation")
        } catch {
            XCTAssertEqual(error as? EnrollmentValidationError, .httpsRequired)
        }
        XCTAssertEqual(unused.calls, 0)

        let cacheable = ExchangeStubTransport(status: 201, headers: ["Content-Type": "application/json"])
        do {
            _ = try await EnrollmentExchangeClient(keyStore: keyStore, transport: cacheable).exchange(validBundle)
            XCTFail("cacheable credential response unexpectedly accepted")
        } catch {
            XCTAssertEqual(error as? EnrollmentExchangeError, .cacheableSecretResponse)
        }

        let nilIDBody = """
        {"credential_id":"00000000-0000-0000-0000-000000000000",
         "worker_id":"bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
         "supplier_id":"cccccccc-cccc-4ccc-8ccc-cccccccccccc",
         "worker_token":"\(workerToken)",
         "device_fingerprint":"\(validBundle.deviceFingerprint)",
         "credential_version":1,"rotated":false}
        """
        do {
            _ = try await EnrollmentExchangeClient(
                keyStore: keyStore,
                transport: ExchangeStubTransport(status: 201, body: nilIDBody)
            ).exchange(validBundle)
            XCTFail("nil credential id unexpectedly accepted")
        } catch {
            XCTAssertEqual(error as? EnrollmentExchangeError, .invalidResponse)
        }
    }

    func testSecKeySignerExportsOnlyCanonicalPublicKeyAndProducesDERProof() throws {
        var error: Unmanaged<CFError>?
        let privateKey = try XCTUnwrap(SecKeyCreateRandomKey([
            kSecAttrKeyType: kSecAttrKeyTypeECSECPrimeRandom,
            kSecAttrKeySizeInBits: 256,
        ] as CFDictionary, &error))
        let signer = SecKeyEnrollmentDeviceKey(privateKey: privateKey)
        let publicData = try signer.publicKeyX963()
        XCTAssertEqual(publicData.count, 65)
        XCTAssertEqual(publicData.first, 0x04)
        let transcript = EnrollmentProtocol.transcript(
            code: code,
            audience: EnrollmentProtocol.audience,
            accountID: accountID,
            controlOrigin: origin.absoluteString,
            requestID: "ptTtmFOpwVsIlLyS0VBcnQ"
        )
        let signature = try signer.signEnrollmentTranscript(transcript)
        let publicKey = try XCTUnwrap(SecKeyCopyPublicKey(privateKey))
        var verifyError: Unmanaged<CFError>?
        XCTAssertTrue(SecKeyVerifySignature(
            publicKey,
            .ecdsaSignatureMessageX962SHA256,
            transcript as CFData,
            signature as CFData,
            &verifyError
        ))
    }

    func testDeviceKeyStorePersistsSameNonExportablePrivateKey() throws {
        let tag = "dev.computeexchange.tests.enrollment.\(UUID().uuidString)"
        let store = KeychainEnrollmentDeviceKeyStore(applicationTag: tag)
        defer { try? store.deleteKey() }

        let first: EnrollmentDeviceKeySigning
        do {
            first = try store.loadOrCreateKey()
        } catch let EnrollmentDeviceKeyError.keychain(status) where status == OSStatus(-34018) {
            throw XCTSkip("SwiftPM test host lacks the signed-app Keychain entitlement")
        }
        let second = try store.loadOrCreateKey()
        XCTAssertEqual(try first.publicKeyX963(), try second.publicKeyX963())

        let query: [CFString: Any] = [
            kSecClass: kSecClassKey,
            kSecAttrKeyType: kSecAttrKeyTypeECSECPrimeRandom,
            kSecAttrKeyClass: kSecAttrKeyClassPrivate,
            kSecAttrApplicationTag: Data(tag.utf8),
            kSecReturnRef: true,
            kSecMatchLimit: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        XCTAssertEqual(SecItemCopyMatching(query as CFDictionary, &item), errSecSuccess)
        let privateKey = item as! SecKey
        var exportError: Unmanaged<CFError>?
        XCTAssertNil(SecKeyCopyExternalRepresentation(privateKey, &exportError))

        let transcript = EnrollmentProtocol.transcript(
            code: code,
            audience: EnrollmentProtocol.audience,
            accountID: accountID,
            controlOrigin: origin.absoluteString,
            requestID: "ptTtmFOpwVsIlLyS0VBcnQ"
        )
        XCTAssertFalse(try second.signEnrollmentTranscript(transcript).isEmpty)
    }

    func testSharedWireFixtureBridgesSwiftRequestAndServerBundleByteExactly() throws {
        let fixture = try EnrollmentWireFixture.load()
        XCTAssertEqual(fixture.version, EnrollmentProtocol.version)

        let request = try EnrollmentDeviceRequest.decode(fixture.cxer2)
        XCTAssertEqual(request.controlOrigin, fixture.controlOrigin)
        XCTAssertEqual(request.audience, fixture.audience)
        XCTAssertEqual(request.deviceKeyAlgorithm, fixture.deviceKeyAlgorithm)
        XCTAssertEqual(request.devicePublicKey, fixture.devicePublicKey)
        XCTAssertEqual(request.requestID, fixture.requestID)
        // cxer2 is emitted by the Swift encoder, so re-encoding must remain
        // byte-exact while the Go decoder accepts the same fixture.
        XCTAssertEqual(try request.encoded(), fixture.cxer2)

        // cxeb2 is emitted by the Go server encoder. This assertion proves the
        // shipped Swift decoder consumes that exact byte sequence, including
        // Go's lowercase UUID and JSON slash representation.
        let approval = try EnrollmentBundle.decode(fixture.cxeb2)
        XCTAssertEqual(approval.controlOrigin, fixture.controlOrigin)
        XCTAssertEqual(approval.accountID.uuidString.lowercased(), fixture.accountID)
        XCTAssertEqual(approval.audience, fixture.audience)
        XCTAssertEqual(approval.enrollmentCode, fixture.enrollmentCode)
        XCTAssertEqual(approval.requestID, fixture.requestID)
        XCTAssertEqual(approval.deviceFingerprint, fixture.deviceFingerprint)
        XCTAssertEqual(
            String(data: EnrollmentProtocol.transcript(
                code: fixture.enrollmentCode,
                audience: fixture.audience,
                accountID: approval.accountID,
                controlOrigin: fixture.controlOrigin,
                requestID: fixture.requestID
            ), encoding: .utf8),
            fixture.exchangeTranscript
        )
    }
}

private struct EnrollmentDeviceRequestPayload: Encodable {
    let version: Int
    let controlOrigin: String
    let audience: String
    let requestID: String
    let deviceKeyAlgorithm: String
    let devicePublicKey: String

    enum CodingKeys: String, CodingKey {
        case version = "v"
        case controlOrigin = "control_origin"
        case audience
        case requestID = "request_id"
        case deviceKeyAlgorithm = "device_key_algorithm"
        case devicePublicKey = "device_public_key"
    }
}

private struct EnrollmentWireFixture: Decodable {
    let version: Int
    let cxer2: String
    let cxeb2: String
    let controlOrigin: String
    let accountID: String
    let audience: String
    let deviceKeyAlgorithm: String
    let devicePublicKey: String
    let requestID: String
    let deviceFingerprint: String
    let enrollmentCode: String
    let exchangeTranscript: String

    static func load() throws -> EnrollmentWireFixture {
        let source = URL(fileURLWithPath: #filePath)
        let repository = source
            .deletingLastPathComponent() // EnrollmentCoreTests
            .deletingLastPathComponent() // Tests
            .deletingLastPathComponent() // macapp
            .deletingLastPathComponent() // repository root
        let fixtureURL = repository
            .appendingPathComponent("proto")
            .appendingPathComponent("enrollment-wire-fixtures.json")
        return try JSONDecoder().decode(
            EnrollmentWireFixture.self,
            from: Data(contentsOf: fixtureURL)
        )
    }

    enum CodingKeys: String, CodingKey {
        case version
        case cxer2
        case cxeb2
        case controlOrigin = "control_origin"
        case accountID = "account_id"
        case audience
        case deviceKeyAlgorithm = "device_key_algorithm"
        case devicePublicKey = "device_public_key"
        case requestID = "request_id"
        case deviceFingerprint = "device_fingerprint"
        case enrollmentCode = "enrollment_code"
        case exchangeTranscript = "exchange_transcript"
    }
}
