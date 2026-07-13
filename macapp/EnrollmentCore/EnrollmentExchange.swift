import Foundation

public enum EnrollmentExchangeError: Error, Equatable, LocalizedError {
    case unavailable
    case rejected
    case endpointNotFound
    case rateLimited
    case redirectRejected
    case serverUnavailable(status: Int)
    case invalidResponse
    case cacheableSecretResponse
    case transportFailure

    public var errorDescription: String? {
        switch self {
        case .unavailable:
            return "One-time device enrollment is unavailable in this build."
        case .rejected:
            return "The one-time enrollment was rejected. It may be expired, used, revoked, or issued for another Mac."
        case .endpointNotFound:
            return "This control plane does not expose one-time device enrollment."
        case .rateLimited:
            return "The control plane is rate limiting enrollment. Wait a moment before requesting a new approval."
        case .redirectRejected:
            return "The control plane redirected the enrollment exchange. Use its final HTTPS origin directly."
        case .serverUnavailable:
            return "The control plane could not complete enrollment. Retry shortly."
        case .invalidResponse:
            return "The control plane returned an invalid enrollment credential."
        case .cacheableSecretResponse:
            return "The control plane did not protect the credential response from caching. Enrollment stopped."
        case .transportFailure:
            return "Could not reach the control plane. Check its URL, TLS certificate, and network."
        }
    }
}

public struct EnrollmentExchangeCredential: Equatable, Sendable {
    public let credentialID: UUID
    public let workerID: UUID
    public let supplierID: UUID
    public let workerToken: String
    public let deviceFingerprint: String
    public let credentialVersion: Int
    public let rotated: Bool

    public init(
        credentialID: UUID,
        workerID: UUID,
        supplierID: UUID,
        workerToken: String,
        deviceFingerprint: String,
        credentialVersion: Int,
        rotated: Bool
    ) {
        self.credentialID = credentialID
        self.workerID = workerID
        self.supplierID = supplierID
        self.workerToken = workerToken
        self.deviceFingerprint = deviceFingerprint
        self.credentialVersion = credentialVersion
        self.rotated = rotated
    }
}

public protocol EnrollmentBundleExchanging: AnyObject {
    func makeDeviceRequest(
        controlURL: String,
        allowInsecureLoopback: Bool
    ) throws -> String
    func exchange(_ bundle: EnrollmentBundle) async throws -> EnrollmentExchangeCredential
    func deleteDeviceKey() throws
}

public final class EnrollmentExchangeClient: EnrollmentBundleExchanging {
    private let keyStore: EnrollmentDeviceKeyStoring
    private let transport: EnrollmentHTTPTransport
    private let timeout: TimeInterval
    private let allowInsecureLoopback: Bool

    public init(
        keyStore: EnrollmentDeviceKeyStoring = KeychainEnrollmentDeviceKeyStore(),
        transport: EnrollmentHTTPTransport = NoRedirectURLSessionTransport(),
        timeout: TimeInterval = 10,
        allowInsecureLoopback: Bool = false
    ) {
        self.keyStore = keyStore
        self.transport = transport
        self.timeout = timeout
        self.allowInsecureLoopback = allowInsecureLoopback
    }

    public func makeDeviceRequest(
        controlURL rawControlURL: String,
        allowInsecureLoopback: Bool = false
    ) throws -> String {
        let controlURL = try EnrollmentValidator.normalizedControlURL(
            rawControlURL,
            allowInsecureLoopback: allowInsecureLoopback
        )
        let key = try keyStore.loadOrCreateKey()
        return try EnrollmentDeviceRequest(
            controlURL: controlURL,
            publicKeyX963: key.publicKeyX963()
        ).encoded()
    }

    public func exchange(_ bundle: EnrollmentBundle) async throws -> EnrollmentExchangeCredential {
        let controlURL = try EnrollmentValidator.normalizedControlURL(
            bundle.controlOrigin,
            allowInsecureLoopback: allowInsecureLoopback
        )
        guard controlURL.absoluteString == bundle.controlOrigin,
              bundle.audience == EnrollmentProtocol.audience,
              EnrollmentProtocol.isValidEnrollmentCode(bundle.enrollmentCode),
              bundle.accountID.uuidString != "00000000-0000-0000-0000-000000000000" else {
            throw EnrollmentBundleError.invalidSchema
        }
        let key = try keyStore.loadOrCreateKey()
        let publicKey = try key.publicKeyX963()
        let requestID = try EnrollmentProtocol.requestID(publicKey: publicKey)
        let fingerprint = try EnrollmentProtocol.deviceFingerprint(publicKey: publicKey)
        guard requestID == bundle.requestID,
              fingerprint == bundle.deviceFingerprint else {
            throw EnrollmentBundleError.requestMismatch
        }

        let transcript = EnrollmentProtocol.transcript(
            code: bundle.enrollmentCode,
            audience: bundle.audience,
            accountID: bundle.accountID,
            controlOrigin: bundle.controlOrigin,
            requestID: bundle.requestID
        )
        let proof = try key.signEnrollmentTranscript(transcript)
        let requestBody = ExchangeRequest(
            version: EnrollmentProtocol.version,
            enrollmentCode: bundle.enrollmentCode,
            controlOrigin: bundle.controlOrigin,
            requestID: bundle.requestID,
            audience: bundle.audience,
            accountID: bundle.accountID,
            deviceKeyAlgorithm: EnrollmentProtocol.keyAlgorithm,
            devicePublicKey: EnrollmentProtocol.base64URLEncode(publicKey),
            proof: EnrollmentProtocol.base64URLEncode(proof)
        )
        let endpoint = controlURL
            .appendingPathComponent("v1")
            .appendingPathComponent("worker")
            .appendingPathComponent("enrollment")
            .appendingPathComponent("exchange")
        var request = URLRequest(
            url: endpoint,
            cachePolicy: .reloadIgnoringLocalCacheData,
            timeoutInterval: timeout
        )
        request.httpMethod = "POST"
        request.httpBody = try JSONEncoder().encode(requestBody)
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("no-store", forHTTPHeaderField: "Cache-Control")

        let data: Data
        let response: HTTPURLResponse
        do {
            (data, response) = try await transport.send(request)
        } catch is CancellationError {
            throw CancellationError()
        } catch {
            if Task.isCancelled { throw CancellationError() }
            throw EnrollmentExchangeError.transportFailure
        }

        switch response.statusCode {
        case 201:
            let cacheControl = response.value(forHTTPHeaderField: "Cache-Control")?.lowercased() ?? ""
            guard cacheControl.split(separator: ",").contains(where: {
                $0.trimmingCharacters(in: .whitespaces) == "no-store"
            }) else {
                throw EnrollmentExchangeError.cacheableSecretResponse
            }
            guard let decoded = try? JSONDecoder().decode(ExchangeResponse.self, from: data),
                  EnrollmentValidator.isValidWorkerToken(decoded.workerToken),
                  decoded.deviceFingerprint == fingerprint,
                  decoded.credentialVersion > 0,
                  decoded.credentialID.uuidString != "00000000-0000-0000-0000-000000000000",
                  decoded.workerID.uuidString != "00000000-0000-0000-0000-000000000000",
                  decoded.supplierID.uuidString != "00000000-0000-0000-0000-000000000000" else {
                throw EnrollmentExchangeError.invalidResponse
            }
            return EnrollmentExchangeCredential(
                credentialID: decoded.credentialID,
                workerID: decoded.workerID,
                supplierID: decoded.supplierID,
                workerToken: decoded.workerToken,
                deviceFingerprint: decoded.deviceFingerprint,
                credentialVersion: decoded.credentialVersion,
                rotated: decoded.rotated
            )
        case 300...399:
            throw EnrollmentExchangeError.redirectRejected
        case 401, 403:
            throw EnrollmentExchangeError.rejected
        case 404:
            throw EnrollmentExchangeError.endpointNotFound
        case 429:
            throw EnrollmentExchangeError.rateLimited
        default:
            throw EnrollmentExchangeError.serverUnavailable(status: response.statusCode)
        }
    }

    public func deleteDeviceKey() throws {
        try keyStore.deleteKey()
    }
}

private struct ExchangeRequest: Encodable {
    let version: Int
    let enrollmentCode: String
    let controlOrigin: String
    let requestID: String
    let audience: String
    let accountID: UUID
    let deviceKeyAlgorithm: String
    let devicePublicKey: String
    let proof: String

    enum CodingKeys: String, CodingKey {
        case version = "v"
        case enrollmentCode = "enrollment_code"
        case controlOrigin = "control_origin"
        case requestID = "request_id"
        case audience
        case accountID = "account_id"
        case deviceKeyAlgorithm = "device_key_algorithm"
        case devicePublicKey = "device_public_key"
        case proof
    }
}

private struct ExchangeResponse: Decodable {
    let credentialID: UUID
    let workerID: UUID
    let supplierID: UUID
    let workerToken: String
    let deviceFingerprint: String
    let credentialVersion: Int
    let rotated: Bool

    enum CodingKeys: String, CodingKey {
        case credentialID = "credential_id"
        case workerID = "worker_id"
        case supplierID = "supplier_id"
        case workerToken = "worker_token"
        case deviceFingerprint = "device_fingerprint"
        case credentialVersion = "credential_version"
        case rotated
    }
}
