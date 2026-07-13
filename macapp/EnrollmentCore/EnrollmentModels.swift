import Foundation

public struct EnrollmentInput: Equatable, Sendable {
    public let controlURL: URL
    public let workerToken: String

    public init(controlURL: URL, workerToken: String) {
        self.controlURL = controlURL
        self.workerToken = workerToken
    }
}

public enum EnrollmentValidationError: Error, Equatable, LocalizedError {
    case missingControlURL
    case invalidControlURL
    case httpsRequired
    case unsupportedControlURLComponents
    case invalidWorkerToken

    public var errorDescription: String? {
        switch self {
        case .missingControlURL:
            return "Enter the control-plane URL."
        case .invalidControlURL:
            return "The control-plane URL is not valid."
        case .httpsRequired:
            return "The control-plane URL must use HTTPS."
        case .unsupportedControlURLComponents:
            return "Use only the control-plane origin, without credentials, a path, query, or fragment."
        case .invalidWorkerToken:
            return "Paste the raw worker token issued for this Mac (it starts with cxw_)."
        }
    }
}

public enum EnrollmentValidator {
    private static let tokenPrefix = "cxw_"
    private static let encodedTokenLength = 43 // 32 random bytes as unpadded base64url.
    private static let tokenCharacters = CharacterSet(
        charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
    )

    public static func validate(
        controlURL rawControlURL: String,
        workerToken rawWorkerToken: String,
        allowInsecureLoopback: Bool = false
    ) throws -> EnrollmentInput {
        let controlURL = try normalizedControlURL(
            rawControlURL,
            allowInsecureLoopback: allowInsecureLoopback
        )
        let workerToken = rawWorkerToken.trimmingCharacters(in: .whitespacesAndNewlines)
        guard isValidWorkerToken(workerToken) else {
            throw EnrollmentValidationError.invalidWorkerToken
        }
        return EnrollmentInput(controlURL: controlURL, workerToken: workerToken)
    }

    public static func normalizedControlURL(
        _ rawValue: String,
        allowInsecureLoopback: Bool = false
    ) throws -> URL {
        let value = rawValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty else { throw EnrollmentValidationError.missingControlURL }
        guard value.rangeOfCharacter(from: .whitespacesAndNewlines) == nil,
              var components = URLComponents(string: value),
              let scheme = components.scheme?.lowercased(),
              let host = components.host?.lowercased(),
              !host.isEmpty else {
            throw EnrollmentValidationError.invalidControlURL
        }

        let loopbackHosts = Set(["localhost", "127.0.0.1", "::1"])
        let secure = scheme == "https"
        let allowedDevelopmentHTTP = allowInsecureLoopback
            && scheme == "http"
            && loopbackHosts.contains(host)
        guard secure || allowedDevelopmentHTTP else {
            throw EnrollmentValidationError.httpsRequired
        }

        let path = components.percentEncodedPath
        guard components.user == nil,
              components.password == nil,
              components.query == nil,
              components.fragment == nil,
              path.isEmpty || path == "/" else {
            throw EnrollmentValidationError.unsupportedControlURLComponents
        }

        components.scheme = scheme
        components.host = host
        components.path = ""
        guard let normalized = components.url else {
            throw EnrollmentValidationError.invalidControlURL
        }
        return normalized
    }

    public static func isValidWorkerToken(_ token: String) -> Bool {
        guard token.hasPrefix(tokenPrefix) else { return false }
        let suffix = String(token.dropFirst(tokenPrefix.count))
        guard suffix.count == encodedTokenLength else { return false }
        return suffix.unicodeScalars.allSatisfy { tokenCharacters.contains($0) }
    }
}

public struct EnrollmentProbeStatus: Codable, Equatable, Sendable {
    public let configured: Bool
    public let connected: Bool
    public let payoutsEnabled: Bool

    public init(configured: Bool, connected: Bool, payoutsEnabled: Bool) {
        self.configured = configured
        self.connected = connected
        self.payoutsEnabled = payoutsEnabled
    }

    enum CodingKeys: String, CodingKey {
        case configured
        case connected
        case payoutsEnabled = "payouts_enabled"
    }
}

public struct EnrollmentRecord: Codable, Equatable, Sendable {
    public let controlURLString: String
    public let verifiedAt: Date
    public let probeStatus: EnrollmentProbeStatus

    public init(controlURL: URL, verifiedAt: Date, probeStatus: EnrollmentProbeStatus) {
        self.controlURLString = controlURL.absoluteString
        self.verifiedAt = verifiedAt
        self.probeStatus = probeStatus
    }

    public var controlURL: URL? { URL(string: controlURLString) }
}

/// Durable, non-secret description of the exact public request awaiting account
/// approval. Persisting this tuple lets the app reject a cxeb2 bundle whose
/// origin was substituted after copy/paste, including after an app restart.
public struct EnrollmentPendingRequest: Codable, Equatable, Sendable {
    public let version: Int
    public let encodedRequest: String
    public let controlURLString: String
    public let requestID: String
    public let deviceFingerprint: String

    public init(
        encodedRequest: String,
        controlURL: URL,
        requestID: String,
        deviceFingerprint: String
    ) {
        version = EnrollmentProtocol.version
        self.encodedRequest = encodedRequest
        controlURLString = controlURL.absoluteString
        self.requestID = requestID
        self.deviceFingerprint = deviceFingerprint
    }

    public var controlURL: URL? { URL(string: controlURLString) }
}

public struct EnrollmentSnapshot: Equatable, Sendable {
    public let record: EnrollmentRecord?
    public let hasWorkerToken: Bool
    public let hasConfiguration: Bool

    public init(record: EnrollmentRecord?, hasWorkerToken: Bool, hasConfiguration: Bool) {
        self.record = record
        self.hasWorkerToken = hasWorkerToken
        self.hasConfiguration = hasConfiguration
    }

    public var isReady: Bool {
        record?.controlURL != nil && hasWorkerToken && hasConfiguration
    }
}

public struct EnrollmentCredentials: Equatable, Sendable {
    public let controlURL: URL
    public let workerToken: String

    public init(controlURL: URL, workerToken: String) {
        self.controlURL = controlURL
        self.workerToken = workerToken
    }
}

public protocol WorkerTokenStoring: AnyObject {
    func containsToken() throws -> Bool
    func loadToken() throws -> String?
    func saveToken(_ token: String) throws
    func deleteToken() throws
}

public protocol EnrollmentConfigStoring: AnyObject {
    func configurationExists() throws -> Bool
    func writeNewConfiguration(_ contents: String) throws
    func deleteManagedConfiguration() throws
}

public protocol EnrollmentRecordStoring: AnyObject {
    func loadRecord() throws -> EnrollmentRecord?
    func saveRecord(_ record: EnrollmentRecord) throws
    func deleteRecord() throws
}

public protocol EnrollmentPendingRequestStoring: AnyObject {
    func loadPendingRequest() throws -> EnrollmentPendingRequest?
    func savePendingRequest(_ request: EnrollmentPendingRequest) throws
    func deletePendingRequest() throws
}

public protocol EnrollmentProbing: AnyObject {
    func verify(controlURL: URL, workerToken: String) async throws -> EnrollmentProbeStatus
}
