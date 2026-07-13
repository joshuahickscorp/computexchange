import XCTest
@testable import EnrollmentCore

final class AgentBootstrapConfigTests: XCTestCase {
    func testRenderedConfigContainsNoCredential() throws {
        let controlURL = try XCTUnwrap(URL(string: "https://control.example.test"))
        let dataDirectory = URL(fileURLWithPath: "/Users/alice/Library/Application Support/Computexchange")
        let rendered = AgentBootstrapConfig(
            controlURL: controlURL,
            dataDirectory: dataDirectory
        ).renderTOML()

        XCTAssertTrue(rendered.hasPrefix(AgentBootstrapConfig.managedMarker))
        XCTAssertTrue(rendered.contains("control_url = \"https://control.example.test\""))
        XCTAssertTrue(rendered.contains("worker_token = \"KEYCHAIN_REQUIRED\""))
        XCTAssertTrue(rendered.contains("supplier_id = \"00000000-0000-0000-0000-000000000000\""))
        XCTAssertTrue(rendered.contains("power_only = true"))
        XCTAssertTrue(rendered.contains("data_dir = \"/Users/alice/Library/Application Support/Computexchange\""))
        XCTAssertFalse(rendered.contains("cxw_"))
        XCTAssertTrue(rendered.hasSuffix("\n"))
    }

    func testTOMLStringsAreEscaped() throws {
        let controlURL = try XCTUnwrap(URL(string: "https://control.example.test"))
        let config = AgentBootstrapConfig(
            controlURL: controlURL,
            dataDirectory: URL(fileURLWithPath: "/tmp/a\"b\\c")
        ).renderTOML()
        XCTAssertTrue(config.contains("data_dir = \"/tmp/a\\\"b\\\\c\""))
    }
}
