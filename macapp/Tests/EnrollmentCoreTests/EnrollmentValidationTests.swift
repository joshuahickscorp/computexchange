import XCTest
@testable import EnrollmentCore

final class EnrollmentValidationTests: XCTestCase {
    private let validToken = "cxw_" + String(repeating: "A", count: 43)

    func testValidHTTPSOriginIsNormalized() throws {
        let input = try EnrollmentValidator.validate(
            controlURL: "  HTTPS://Control.Example.test/  ",
            workerToken: "\n\(validToken)\n"
        )
        XCTAssertEqual(input.controlURL.absoluteString, "https://control.example.test")
        XCTAssertEqual(input.workerToken, validToken)
    }

    func testReleaseValidationRequiresHTTPS() {
        XCTAssertThrowsError(try EnrollmentValidator.validate(
            controlURL: "http://control.example.test",
            workerToken: validToken
        )) { error in
            XCTAssertEqual(error as? EnrollmentValidationError, .httpsRequired)
        }
    }

    func testDebugPolicyCanAllowOnlyLoopbackHTTP() throws {
        let input = try EnrollmentValidator.validate(
            controlURL: "http://127.0.0.1:8080/",
            workerToken: validToken,
            allowInsecureLoopback: true
        )
        XCTAssertEqual(input.controlURL.absoluteString, "http://127.0.0.1:8080")

        XCTAssertThrowsError(try EnrollmentValidator.validate(
            controlURL: "http://192.168.1.20:8080",
            workerToken: validToken,
            allowInsecureLoopback: true
        ))
    }

    func testOriginRejectsCredentialsPathsQueriesAndFragments() {
        let invalid = [
            "https://user:pass@example.test",
            "https://example.test/control",
            "https://example.test?token=nope",
            "https://example.test#fragment",
        ]
        for value in invalid {
            XCTAssertThrowsError(try EnrollmentValidator.validate(
                controlURL: value,
                workerToken: validToken
            ), value)
        }
    }

    func testWorkerTokenMustMatchProductionShape() {
        let invalid = [
            "",
            "dev-worker-token-0001",
            "cxw_" + String(repeating: "A", count: 42),
            "cxw_" + String(repeating: "A", count: 44),
            "cxw_" + String(repeating: "A", count: 42) + "+",
        ]
        for token in invalid {
            XCTAssertFalse(EnrollmentValidator.isValidWorkerToken(token), token)
        }
        XCTAssertTrue(EnrollmentValidator.isValidWorkerToken(validToken))
    }
}
