import Foundation

public struct AgentBootstrapConfig: Equatable, Sendable {
    public static let managedMarker = "# Managed by ComputeExchangeAgent.app enrollment v1."
    public static let keychainPlaceholder = "KEYCHAIN_REQUIRED"
    public static let zeroSupplierID = "00000000-0000-0000-0000-000000000000"

    public let controlURL: URL
    public let dataDirectory: URL

    public init(controlURL: URL, dataDirectory: URL) {
        self.controlURL = controlURL
        self.dataDirectory = dataDirectory
    }

    /// Render the complete base config required by the current Rust AgentConfig.
    /// The real credential is intentionally not an input to this type, so it cannot
    /// accidentally reach disk; AgentController supplies it through the child env.
    public func renderTOML() -> String {
        [
            Self.managedMarker,
            "# The raw worker token is stored in macOS Keychain and injected at launch.",
            "control_url = \"\(Self.escape(controlURL.absoluteString))\"",
            "worker_token = \"\(Self.keychainPlaceholder)\"",
            "supplier_id = \"\(Self.zeroSupplierID)\"",
            "max_cpu_pct = 90.0",
            "power_only = true",
            "min_payout_usd_per_hr = 0.05",
            "data_dir = \"\(Self.escape(dataDirectory.path))\"",
            "memory_headroom_gb = 8.0",
            "max_memory_pct = 85.0",
            "checkpoint_secs = 30",
            "",
        ].joined(separator: "\n")
    }

    private static func escape(_ value: String) -> String {
        value
            .replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
            .replacingOccurrences(of: "\n", with: "\\n")
            .replacingOccurrences(of: "\r", with: "\\r")
            .replacingOccurrences(of: "\t", with: "\\t")
    }
}
