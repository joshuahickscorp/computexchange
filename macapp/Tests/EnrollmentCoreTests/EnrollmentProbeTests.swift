import XCTest
@testable import EnrollmentCore

private final class StubTransport: EnrollmentHTTPTransport {
    var request: URLRequest?
    var result: Result<(Data, HTTPURLResponse), Error>

    init(status: Int, body: String = "{}") {
        let url = URL(string: "https://control.example.test/v1/worker/connect/status")!
        let response = HTTPURLResponse(
            url: url,
            statusCode: status,
            httpVersion: "HTTP/1.1",
            headerFields: ["Content-Type": "application/json"]
        )!
        result = .success((Data(body.utf8), response))
    }

    init(error: Error) {
        result = .failure(error)
    }

    func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        self.request = request
        return try result.get()
    }
}

final class EnrollmentProbeTests: XCTestCase {
    private let token = "cxw_" + String(repeating: "B", count: 43)
    private let origin = URL(string: "https://control.example.test")!

    func testAuthenticatedStatusRequestAndDecode() async throws {
        let transport = StubTransport(
            status: 200,
            body: #"{"configured":true,"connected":false,"payouts_enabled":false}"#
        )
        let status = try await EnrollmentProbeClient(transport: transport).verify(
            controlURL: origin,
            workerToken: token
        )

        XCTAssertEqual(status, EnrollmentProbeStatus(
            configured: true,
            connected: false,
            payoutsEnabled: false
        ))
        XCTAssertEqual(transport.request?.httpMethod, "GET")
        XCTAssertEqual(
            transport.request?.url?.path,
            "/v1/worker/connect/status"
        )
        XCTAssertEqual(
            transport.request?.value(forHTTPHeaderField: "X-Worker-Token"),
            token
        )
    }

    func testTypedHTTPFailures() async {
        let cases: [(Int, EnrollmentProbeError)] = [
            (302, .redirectRejected),
            (401, .invalidToken),
            (403, .invalidToken),
            (404, .endpointNotFound),
            (429, .rateLimited),
            (503, .serverUnavailable(status: 503)),
        ]
        for (status, expected) in cases {
            do {
                _ = try await EnrollmentProbeClient(
                    transport: StubTransport(status: status)
                ).verify(controlURL: origin, workerToken: token)
                XCTFail("status \(status) unexpectedly passed")
            } catch {
                XCTAssertEqual(error as? EnrollmentProbeError, expected)
            }
        }
    }

    func testMalformedSuccessIsRejected() async {
        do {
            _ = try await EnrollmentProbeClient(
                transport: StubTransport(status: 200, body: #"{"configured":true}"#)
            ).verify(controlURL: origin, workerToken: token)
            XCTFail("malformed response unexpectedly passed")
        } catch {
            XCTAssertEqual(error as? EnrollmentProbeError, .invalidResponse)
        }
    }

    func testTransportFailureDoesNotExposeToken() async {
        do {
            _ = try await EnrollmentProbeClient(
                transport: StubTransport(error: URLError(.timedOut))
            ).verify(controlURL: origin, workerToken: token)
            XCTFail("transport failure unexpectedly passed")
        } catch {
            XCTAssertEqual(error as? EnrollmentProbeError, .transportFailure)
            XCTAssertFalse(error.localizedDescription.contains(token))
        }
    }
}
