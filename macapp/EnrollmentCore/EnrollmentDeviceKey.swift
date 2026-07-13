import Foundation
import Security

public enum EnrollmentDeviceKeyError: Error, Equatable, LocalizedError {
    case keychain(OSStatus)
    case generationFailed
    case invalidPublicKey
    case signingFailed

    public var errorDescription: String? {
        switch self {
        case .keychain(let status):
            return "macOS Keychain could not access this Mac's enrollment key (status \(status))."
        case .generationFailed:
            return "This Mac could not create a protected enrollment key."
        case .invalidPublicKey:
            return "This Mac's enrollment public key is unreadable."
        case .signingFailed:
            return "This Mac could not prove possession of its enrollment key."
        }
    }
}

public protocol EnrollmentDeviceKeySigning: AnyObject {
    func publicKeyX963() throws -> Data
    func signEnrollmentTranscript(_ transcript: Data) throws -> Data
}

public protocol EnrollmentDeviceKeyStoring: AnyObject {
    func loadOrCreateKey() throws -> EnrollmentDeviceKeySigning
    func deleteKey() throws
}

/// Thin SecKey signer. Only the public key is exportable; signing stays inside
/// Security.framework and the private SecKey is never serialized by app code.
public final class SecKeyEnrollmentDeviceKey: EnrollmentDeviceKeySigning {
    private let privateKey: SecKey

    public init(privateKey: SecKey) {
        self.privateKey = privateKey
    }

    public func publicKeyX963() throws -> Data {
        guard let publicKey = SecKeyCopyPublicKey(privateKey) else {
            throw EnrollmentDeviceKeyError.invalidPublicKey
        }
        var error: Unmanaged<CFError>?
        guard let representation = SecKeyCopyExternalRepresentation(publicKey, &error) as Data? else {
            throw EnrollmentDeviceKeyError.invalidPublicKey
        }
        do {
            try EnrollmentProtocol.validatePublicKey(representation)
        } catch {
            throw EnrollmentDeviceKeyError.invalidPublicKey
        }
        return representation
    }

    public func signEnrollmentTranscript(_ transcript: Data) throws -> Data {
        var error: Unmanaged<CFError>?
        guard let signature = SecKeyCreateSignature(
            privateKey,
            .ecdsaSignatureMessageX962SHA256,
            transcript as CFData,
            &error
        ) as Data? else {
            throw EnrollmentDeviceKeyError.signingFailed
        }
        return signature
    }
}

/// Persistent P-256 key storage. Secure Enclave is attempted first. Macs without
/// an available enclave (and test/unsupported hosts) receive a non-extractable
/// software SecKey stored WhenUnlockedThisDeviceOnly. Neither path synchronizes.
public final class KeychainEnrollmentDeviceKeyStore: EnrollmentDeviceKeyStoring {
    private let applicationTag: Data

    public init(
        applicationTag: String = "dev.computeexchange.agent.menubar.enrollment-device-key-v1"
    ) {
        self.applicationTag = Data(applicationTag.utf8)
    }

    public func loadOrCreateKey() throws -> EnrollmentDeviceKeySigning {
        if let existing = try loadKey() {
            return SecKeyEnrollmentDeviceKey(privateKey: existing)
        }
        do {
            return SecKeyEnrollmentDeviceKey(privateKey: try createSecureEnclaveKey())
        } catch let EnrollmentDeviceKeyError.keychain(status) where Self.enclaveUnavailable(status) {
            do {
                return SecKeyEnrollmentDeviceKey(privateKey: try createSoftwareKey())
            } catch let EnrollmentDeviceKeyError.keychain(status) where status == errSecDuplicateItem {
                guard let existing = try loadKey() else {
                    throw EnrollmentDeviceKeyError.generationFailed
                }
                return SecKeyEnrollmentDeviceKey(privateKey: existing)
            }
        } catch let EnrollmentDeviceKeyError.keychain(status) where status == errSecDuplicateItem {
            guard let existing = try loadKey() else {
                throw EnrollmentDeviceKeyError.generationFailed
            }
            return SecKeyEnrollmentDeviceKey(privateKey: existing)
        }
    }

    public func deleteKey() throws {
        let status = SecItemDelete(keyQuery as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw EnrollmentDeviceKeyError.keychain(status)
        }
    }

    private var keyQuery: [CFString: Any] {
        [
            kSecClass: kSecClassKey,
            kSecAttrKeyType: kSecAttrKeyTypeECSECPrimeRandom,
            kSecAttrKeyClass: kSecAttrKeyClassPrivate,
            kSecAttrApplicationTag: applicationTag,
        ]
    }

    private func loadKey() throws -> SecKey? {
        var query = keyQuery
        query[kSecReturnRef] = true
        query[kSecMatchLimit] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let key = result else {
            throw EnrollmentDeviceKeyError.keychain(status)
        }
        return (key as! SecKey)
    }

    private func createSecureEnclaveKey() throws -> SecKey {
        var accessError: Unmanaged<CFError>?
        guard let access = SecAccessControlCreateWithFlags(
            nil,
            kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
            .privateKeyUsage,
            &accessError
        ) else {
            throw EnrollmentDeviceKeyError.generationFailed
        }
        let attributes: [CFString: Any] = [
            kSecAttrKeyType: kSecAttrKeyTypeECSECPrimeRandom,
            kSecAttrKeySizeInBits: 256,
            kSecAttrTokenID: kSecAttrTokenIDSecureEnclave,
            kSecPrivateKeyAttrs: [
                kSecAttrIsPermanent: true,
                kSecAttrApplicationTag: applicationTag,
                kSecAttrAccessControl: access,
            ],
        ]
        return try createKey(attributes)
    }

    private func createSoftwareKey() throws -> SecKey {
        let attributes: [CFString: Any] = [
            kSecAttrKeyType: kSecAttrKeyTypeECSECPrimeRandom,
            kSecAttrKeySizeInBits: 256,
            kSecPrivateKeyAttrs: [
                kSecAttrIsPermanent: true,
                kSecAttrApplicationTag: applicationTag,
                kSecAttrAccessible: kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
                kSecAttrIsExtractable: false,
                kSecAttrSynchronizable: false,
            ],
        ]
        return try createKey(attributes)
    }

    private func createKey(_ attributes: [CFString: Any]) throws -> SecKey {
        var error: Unmanaged<CFError>?
        guard let key = SecKeyCreateRandomKey(attributes as CFDictionary, &error) else {
            if let error = error?.takeRetainedValue() {
                throw EnrollmentDeviceKeyError.keychain(OSStatus(CFErrorGetCode(error)))
            }
            throw EnrollmentDeviceKeyError.generationFailed
        }
        return key
    }

    private static func enclaveUnavailable(_ status: OSStatus) -> Bool {
        status == errSecUnimplemented || status == errSecNotAvailable || status == errSecParam
    }
}
