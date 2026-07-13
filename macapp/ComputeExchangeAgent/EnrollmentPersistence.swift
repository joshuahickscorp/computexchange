import Foundation
import Security
import EnrollmentCore

enum EnrollmentPersistenceError: Error, LocalizedError {
    case keychain(OSStatus)
    case invalidKeychainData
    case existingConfiguration
    case unmanagedConfiguration

    var errorDescription: String? {
        switch self {
        case .keychain(let status):
            return "macOS Keychain could not complete the enrollment operation (status \(status))."
        case .invalidKeychainData:
            return "The stored worker credential is unreadable. Reset enrollment and request a new token."
        case .existingConfiguration:
            return "An agent config already exists. It was not overwritten."
        case .unmanagedConfiguration:
            return "The existing agent config was not created by enrollment and was not deleted."
        }
    }
}

/// The raw worker bearer token lives only in the user's macOS Keychain. No token
/// hint is copied to UserDefaults, TOML, status, or UI state after enrollment.
final class KeychainWorkerTokenStore: WorkerTokenStoring {
    private let service: String
    private let account: String

    init(
        service: String = "dev.computeexchange.agent.menubar.enrollment",
        account: String = "worker-token-v1"
    ) {
        self.service = service
        self.account = account
    }

    func containsToken() throws -> Bool {
        var query = baseQuery
        query[kSecMatchLimit] = kSecMatchLimitOne
        let status = SecItemCopyMatching(query as CFDictionary, nil)
        switch status {
        case errSecSuccess: return true
        case errSecItemNotFound: return false
        default: throw EnrollmentPersistenceError.keychain(status)
        }
    }

    func loadToken() throws -> String? {
        var query = baseQuery
        query[kSecMatchLimit] = kSecMatchLimitOne
        query[kSecReturnData] = true
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess else {
            throw EnrollmentPersistenceError.keychain(status)
        }
        guard let data = result as? Data,
              let token = String(data: data, encoding: .utf8),
              !token.isEmpty else {
            throw EnrollmentPersistenceError.invalidKeychainData
        }
        return token
    }

    func saveToken(_ token: String) throws {
        var item = baseQuery
        item[kSecValueData] = Data(token.utf8)
        item[kSecAttrAccessible] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
        let status = SecItemAdd(item as CFDictionary, nil)
        guard status == errSecSuccess else {
            throw EnrollmentPersistenceError.keychain(status)
        }
    }

    func deleteToken() throws {
        let status = SecItemDelete(baseQuery as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw EnrollmentPersistenceError.keychain(status)
        }
    }

    private var baseQuery: [CFString: Any] {
        [
            kSecClass: kSecClassGenericPassword,
            kSecAttrService: service,
            kSecAttrAccount: account,
            kSecAttrSynchronizable: false,
        ]
    }
}

/// Owns only the app-generated base agent config. It never overwrites or deletes
/// a manually-created config; the managed marker is the deletion authorization.
final class FileEnrollmentConfigStore: EnrollmentConfigStoring {
    private let url: URL
    private let fileManager: FileManager

    init(url: URL, fileManager: FileManager = .default) {
        self.url = url
        self.fileManager = fileManager
    }

    func configurationExists() throws -> Bool {
        fileManager.fileExists(atPath: url.path)
    }

    func writeNewConfiguration(_ contents: String) throws {
        guard !fileManager.fileExists(atPath: url.path) else {
            throw EnrollmentPersistenceError.existingConfiguration
        }
        try fileManager.createDirectory(
            at: url.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        do {
            try contents.write(to: url, atomically: true, encoding: .utf8)
            try fileManager.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
        } catch {
            // The file did not exist before this call, so removing a partially
            // created app-managed config cannot destroy a user's manual config.
            try? fileManager.removeItem(at: url)
            throw error
        }
    }

    func deleteManagedConfiguration() throws {
        guard fileManager.fileExists(atPath: url.path) else { return }
        let contents = try String(contentsOf: url, encoding: .utf8)
        guard contents.hasPrefix(AgentBootstrapConfig.managedMarker) else {
            throw EnrollmentPersistenceError.unmanagedConfiguration
        }
        try fileManager.removeItem(at: url)
    }
}

/// Non-secret enrollment receipt: origin, verification time, and the public
/// payout-readiness booleans returned by the authenticated probe.
final class UserDefaultsEnrollmentRecordStore: EnrollmentRecordStoring {
    private let defaults: UserDefaults
    private let key: String

    init(
        defaults: UserDefaults = .standard,
        key: String = "cx.enrollment.record.v1"
    ) {
        self.defaults = defaults
        self.key = key
    }

    func loadRecord() throws -> EnrollmentRecord? {
        guard let data = defaults.data(forKey: key) else { return nil }
        return try JSONDecoder().decode(EnrollmentRecord.self, from: data)
    }

    func saveRecord(_ record: EnrollmentRecord) throws {
        defaults.set(try JSONEncoder().encode(record), forKey: key)
    }

    func deleteRecord() throws {
        defaults.removeObject(forKey: key)
    }
}

/// The pending request is public protocol state, not a credential. Persisting
/// its canonical origin/request id lets a restarted app reject a substituted
/// approval origin before it releases a device signature or one-time code.
final class UserDefaultsEnrollmentPendingRequestStore: EnrollmentPendingRequestStoring {
    private let defaults: UserDefaults
    private let key: String

    init(
        defaults: UserDefaults = .standard,
        key: String = "cx.enrollment.pending-request.v2"
    ) {
        self.defaults = defaults
        self.key = key
    }

    func loadPendingRequest() throws -> EnrollmentPendingRequest? {
        guard let data = defaults.data(forKey: key) else { return nil }
        return try JSONDecoder().decode(EnrollmentPendingRequest.self, from: data)
    }

    func savePendingRequest(_ request: EnrollmentPendingRequest) throws {
        defaults.set(try JSONEncoder().encode(request), forKey: key)
    }

    func deletePendingRequest() throws {
        defaults.removeObject(forKey: key)
    }
}
