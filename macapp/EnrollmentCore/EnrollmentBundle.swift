import CryptoKit
import Foundation

public enum EnrollmentBundleError: Error, Equatable, LocalizedError {
    case invalidEncoding
    case invalidSchema
    case unsupportedVersion
    case invalidAudience
    case invalidAccountID
    case invalidEnrollmentCode
    case invalidDeviceKey
    case requestMismatch

    public var errorDescription: String? {
        switch self {
        case .invalidEncoding:
            return "The enrollment text is damaged or incomplete. Copy it again from the authenticated account."
        case .invalidSchema:
            return "The enrollment text has an unexpected format."
        case .unsupportedVersion:
            return "This enrollment text was created for an unsupported app version."
        case .invalidAudience:
            return "The enrollment text is not intended for the macOS supplier agent."
        case .invalidAccountID:
            return "The enrollment text contains an invalid account identifier."
        case .invalidEnrollmentCode:
            return "The enrollment code is invalid or incomplete."
        case .invalidDeviceKey:
            return "The enrollment request contains an invalid device public key."
        case .requestMismatch:
            return "This enrollment approval was issued for a different Mac or enrollment request."
        }
    }
}

enum EnrollmentProtocol {
    static let version = 2
    static let audience = "cx-macos-agent-v2"
    static let keyAlgorithm = "p256"
    static let requestPrefix = "cxer2_"
    static let bundlePrefix = "cxeb2_"
    static let codePrefix = "cxe_"
    static let base64URLCharacters = CharacterSet(
        charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
    )

    static func base64URLEncode(_ data: Data) -> String {
        data.base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    static func base64URLDecode(_ value: String) -> Data? {
        guard !value.isEmpty,
              value.unicodeScalars.allSatisfy({ base64URLCharacters.contains($0) }) else {
            return nil
        }
        var base64 = value
            .replacingOccurrences(of: "-", with: "+")
            .replacingOccurrences(of: "_", with: "/")
        let remainder = base64.count % 4
        if remainder != 0 {
            base64 += String(repeating: "=", count: 4 - remainder)
        }
        guard let decoded = Data(base64Encoded: base64),
              base64URLEncode(decoded) == value else {
            return nil
        }
        return decoded
    }

    static func validatePublicKey(_ data: Data) throws {
        guard data.count == 65, data.first == 0x04 else {
            throw EnrollmentBundleError.invalidDeviceKey
        }
        _ = try P256.Signing.PublicKey(x963Representation: data)
    }

    static func deviceFingerprint(publicKey: Data) throws -> String {
        try validatePublicKey(publicKey)
        var material = Data("p256\0".utf8)
        material.append(publicKey)
        return "p256:sha256:" + SHA256.hash(data: material)
            .map { String(format: "%02x", $0) }
            .joined()
    }

    static func requestID(publicKey: Data) throws -> String {
        try validatePublicKey(publicKey)
        var material = Data("cx-enrollment-request-v1\0".utf8)
        material.append(publicKey)
        return base64URLEncode(Data(SHA256.hash(data: material).prefix(16)))
    }

    static func transcript(
        code: String,
        audience: String,
        accountID: UUID,
        controlOrigin: String,
        requestID: String
    ) -> Data {
        Data((
            "cx-worker-enrollment-exchange-v2\n\(audience)\n" +
            "\(accountID.uuidString.lowercased())\n\(controlOrigin)\n\(requestID)\n\(code)"
        ).utf8)
    }

    static func isValidEnrollmentCode(_ code: String) -> Bool {
        guard code.hasPrefix(codePrefix) else { return false }
        let suffix = String(code.dropFirst(codePrefix.count))
        guard let decoded = base64URLDecode(suffix) else { return false }
        return decoded.count == 32
    }

    static func encode<T: Encodable>(_ value: T, prefix: String) throws -> String {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        return prefix + base64URLEncode(try encoder.encode(value))
    }

    static func strictPayload(_ encoded: String, prefix: String, keys: Set<String>) throws -> Data {
        // Copy/paste commonly adds one trailing newline. Accept only outer
        // whitespace; the base64url payload itself remains canonical and strict.
        let value = encoded.trimmingCharacters(in: .whitespacesAndNewlines)
        guard value.hasPrefix(prefix),
              let data = base64URLDecode(String(value.dropFirst(prefix.count))),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              Set(object.keys) == keys else {
            throw EnrollmentBundleError.invalidEncoding
        }
        return data
    }
}

/// Public, non-secret request copied from this Mac into the authenticated account
/// surface. It carries only the origin, P-256 public key, and a deterministic
/// request id bound to that key. The private key never leaves Keychain.
public struct EnrollmentDeviceRequest: Codable, Equatable, Sendable {
    public let version: Int
    public let controlOrigin: String
    public let audience: String
    public let requestID: String
    public let deviceKeyAlgorithm: String
    public let devicePublicKey: String

    public init(controlURL: URL, publicKeyX963: Data) throws {
        try EnrollmentProtocol.validatePublicKey(publicKeyX963)
        version = EnrollmentProtocol.version
        controlOrigin = controlURL.absoluteString
        audience = EnrollmentProtocol.audience
        requestID = try EnrollmentProtocol.requestID(publicKey: publicKeyX963)
        deviceKeyAlgorithm = EnrollmentProtocol.keyAlgorithm
        devicePublicKey = EnrollmentProtocol.base64URLEncode(publicKeyX963)
    }

    public func encoded() throws -> String {
        try EnrollmentProtocol.encode(self, prefix: EnrollmentProtocol.requestPrefix)
    }

    public static func decode(
        _ encoded: String,
        allowInsecureLoopback: Bool = false
    ) throws -> EnrollmentDeviceRequest {
        let data = try EnrollmentProtocol.strictPayload(
            encoded,
            prefix: EnrollmentProtocol.requestPrefix,
            keys: ["v", "control_origin", "audience", "request_id", "device_key_algorithm", "device_public_key"]
        )
        let request: EnrollmentDeviceRequest
        do {
            request = try JSONDecoder().decode(EnrollmentDeviceRequest.self, from: data)
        } catch {
            throw EnrollmentBundleError.invalidSchema
        }
        guard request.version == EnrollmentProtocol.version else { throw EnrollmentBundleError.unsupportedVersion }
        guard request.audience == EnrollmentProtocol.audience else {
            throw EnrollmentBundleError.invalidAudience
        }
        guard request.deviceKeyAlgorithm == EnrollmentProtocol.keyAlgorithm,
              let publicKey = EnrollmentProtocol.base64URLDecode(request.devicePublicKey) else {
            throw EnrollmentBundleError.invalidDeviceKey
        }
        try EnrollmentProtocol.validatePublicKey(publicKey)
        guard request.requestID == (try EnrollmentProtocol.requestID(publicKey: publicKey)) else {
            throw EnrollmentBundleError.requestMismatch
        }
        let normalized = try EnrollmentValidator.normalizedControlURL(
            request.controlOrigin,
            allowInsecureLoopback: allowInsecureLoopback
        )
        guard normalized.absoluteString == request.controlOrigin else {
            throw EnrollmentBundleError.invalidSchema
        }
        return request
    }

    enum CodingKeys: String, CodingKey {
        case version = "v"
        case controlOrigin = "control_origin"
        case audience
        case requestID = "request_id"
        case deviceKeyAlgorithm = "device_key_algorithm"
        case devicePublicKey = "device_public_key"
    }
}

/// One paste copied back from the authenticated account after it approves the
/// public device request and issues a short-lived code bound to that exact key.
/// This contains no private key and no long-lived worker bearer token.
public struct EnrollmentBundle: Codable, Equatable, Sendable {
    public let version: Int
    public let controlOrigin: String
    public let accountID: UUID
    public let audience: String
    public let enrollmentCode: String
    public let requestID: String
    public let deviceFingerprint: String

    public init(
        controlURL: URL,
        accountID: UUID,
        enrollmentCode: String,
        requestID: String,
        deviceFingerprint: String,
        audience: String = "cx-macos-agent-v2"
    ) {
        version = EnrollmentProtocol.version
        controlOrigin = controlURL.absoluteString
        self.accountID = accountID
        self.audience = audience
        self.enrollmentCode = enrollmentCode
        self.requestID = requestID
        self.deviceFingerprint = deviceFingerprint
    }

    public var controlURL: URL? { URL(string: controlOrigin) }

    public func encoded() throws -> String {
        try EnrollmentProtocol.encode(self, prefix: EnrollmentProtocol.bundlePrefix)
    }

    public static func decode(
        _ encoded: String,
        allowInsecureLoopback: Bool = false
    ) throws -> EnrollmentBundle {
        let data = try EnrollmentProtocol.strictPayload(
            encoded,
            prefix: EnrollmentProtocol.bundlePrefix,
            keys: ["v", "control_origin", "account_id", "audience", "enrollment_code", "request_id", "device_fingerprint"]
        )
        let bundle: EnrollmentBundle
        do {
            bundle = try JSONDecoder().decode(EnrollmentBundle.self, from: data)
        } catch {
            throw EnrollmentBundleError.invalidSchema
        }
        guard bundle.version == EnrollmentProtocol.version else { throw EnrollmentBundleError.unsupportedVersion }
        guard bundle.audience == EnrollmentProtocol.audience else {
            throw EnrollmentBundleError.invalidAudience
        }
        guard EnrollmentProtocol.isValidEnrollmentCode(bundle.enrollmentCode) else {
            throw EnrollmentBundleError.invalidEnrollmentCode
        }
        guard bundle.accountID.uuidString != "00000000-0000-0000-0000-000000000000" else {
            throw EnrollmentBundleError.invalidAccountID
        }
        guard bundle.requestID.count == 22,
              EnrollmentProtocol.base64URLDecode(bundle.requestID) != nil else {
            throw EnrollmentBundleError.invalidSchema
        }
        guard bundle.deviceFingerprint.hasPrefix("p256:sha256:"),
              bundle.deviceFingerprint.count == "p256:sha256:".count + 64 else {
            throw EnrollmentBundleError.invalidSchema
        }
        let normalized = try EnrollmentValidator.normalizedControlURL(
            bundle.controlOrigin,
            allowInsecureLoopback: allowInsecureLoopback
        )
        guard normalized.absoluteString == bundle.controlOrigin,
              bundle.controlURL == normalized else {
            throw EnrollmentBundleError.invalidSchema
        }
        return bundle
    }

    enum CodingKeys: String, CodingKey {
        case version = "v"
        case controlOrigin = "control_origin"
        case accountID = "account_id"
        case audience
        case enrollmentCode = "enrollment_code"
        case requestID = "request_id"
        case deviceFingerprint = "device_fingerprint"
    }
}
