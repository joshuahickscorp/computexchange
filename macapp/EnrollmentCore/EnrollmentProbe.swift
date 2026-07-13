import Foundation

public enum EnrollmentProbeError: Error, Equatable, LocalizedError {
    case invalidToken
    case endpointNotFound
    case rateLimited
    case redirectRejected
    case serverUnavailable(status: Int)
    case invalidResponse
    case transportFailure

    public var errorDescription: String? {
        switch self {
        case .invalidToken:
            return "The control plane rejected this worker token. Request a new token and try again."
        case .endpointNotFound:
            return "This server does not expose the worker enrollment status endpoint. Check the URL."
        case .rateLimited:
            return "The control plane is rate limiting enrollment checks. Wait a moment and retry."
        case .redirectRejected:
            return "The control plane redirected the authenticated check. Enter its final HTTPS origin directly."
        case .serverUnavailable:
            return "The control plane could not complete the enrollment check. Retry shortly."
        case .invalidResponse:
            return "The control plane returned an unexpected enrollment response."
        case .transportFailure:
            return "Could not reach the control plane. Check the URL, TLS certificate, and network, then retry."
        }
    }
}

public protocol EnrollmentHTTPTransport: AnyObject {
    func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse)
}

/// Ephemeral URLSession transport that refuses every redirect. The worker token is
/// an authentication credential; following even a seemingly harmless redirect
/// risks forwarding it to a different origin. The user must enter the final origin.
public final class NoRedirectURLSessionTransport: NSObject, EnrollmentHTTPTransport,
    URLSessionTaskDelegate, @unchecked Sendable {
    private lazy var session: URLSession = {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.urlCache = nil
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.timeoutIntervalForRequest = 10
        configuration.timeoutIntervalForResource = 15
        return URLSession(configuration: configuration, delegate: self, delegateQueue: nil)
    }()

    public override init() {
        super.init()
    }

    deinit {
        session.invalidateAndCancel()
    }

    public func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw EnrollmentProbeError.invalidResponse
        }
        return (data, http)
    }

    public func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) {
        completionHandler(nil)
    }
}

public final class EnrollmentProbeClient: EnrollmentProbing {
    private let transport: EnrollmentHTTPTransport
    private let timeout: TimeInterval

    public init(
        transport: EnrollmentHTTPTransport = NoRedirectURLSessionTransport(),
        timeout: TimeInterval = 10
    ) {
        self.transport = transport
        self.timeout = timeout
    }

    public func verify(controlURL: URL, workerToken: String) async throws -> EnrollmentProbeStatus {
        let endpoint = controlURL
            .appendingPathComponent("v1")
            .appendingPathComponent("worker")
            .appendingPathComponent("connect")
            .appendingPathComponent("status")
        var request = URLRequest(
            url: endpoint,
            cachePolicy: .reloadIgnoringLocalCacheData,
            timeoutInterval: timeout
        )
        request.httpMethod = "GET"
        request.setValue(workerToken, forHTTPHeaderField: "X-Worker-Token")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("no-store", forHTTPHeaderField: "Cache-Control")

        let data: Data
        let response: HTTPURLResponse
        do {
            (data, response) = try await transport.send(request)
        } catch is CancellationError {
            throw CancellationError()
        } catch let error as EnrollmentProbeError {
            throw error
        } catch {
            if Task.isCancelled { throw CancellationError() }
            throw EnrollmentProbeError.transportFailure
        }

        switch response.statusCode {
        case 200:
            guard let status = try? JSONDecoder().decode(EnrollmentProbeStatus.self, from: data) else {
                throw EnrollmentProbeError.invalidResponse
            }
            return status
        case 300...399:
            throw EnrollmentProbeError.redirectRejected
        case 401, 403:
            throw EnrollmentProbeError.invalidToken
        case 404:
            throw EnrollmentProbeError.endpointNotFound
        case 429:
            throw EnrollmentProbeError.rateLimited
        default:
            throw EnrollmentProbeError.serverUnavailable(status: response.statusCode)
        }
    }
}
