//! Control-plane HTTP client — REAL reqwest calls.
//!
//! Every method issues a real request, attaches the `X-Worker-Token` auth
//! header, and maps any non-2xx response to a typed `ProtocolError`. There are
//! no silent fallbacks: a missing token, a transport failure, or an unexpected
//! status all surface explicitly.

use std::time::Duration;

use reqwest::{Client, Response, StatusCode};
use uuid::Uuid;

use crate::types::{
    ConnectStatus, Earnings, FailReport, Heartbeat, SupplierVerification, TaskCommit, TaskDispatch,
    WorkerCapability,
};

/// Long-poll budget. Slightly above the server's ~30s poll window so we don't
/// time out a poll the server is legitimately holding open.
const POLL_TIMEOUT: Duration = Duration::from_secs(35);
/// Timeout for ordinary (non-poll) requests.
const REQUEST_TIMEOUT: Duration = Duration::from_secs(20);

// A result object is already durably uploaded before commit_task runs. Losing a
// single commit response must therefore not abandon that uploaded work until the
// stale-task reaper fires. Rebuild the same authenticated JSON request a bounded
// number of times on transport failures, 429, and 5xx. Four total attempts with
// 200/400/800ms delays stay well below the task-recovery horizon while avoiding
// an unbounded retry loop on an unhealthy control plane.
const COMMIT_MAX_ATTEMPTS: usize = 4;
const COMMIT_RETRY_BASE_DELAY: Duration = Duration::from_millis(200);

/// Poll path with the long-poll request (Plane D §7 D1). The control plane holds
/// the poll open for up to `wait_ms` (it caps the value server-side) before
/// answering 204, so an idle worker gets a freshly-submitted task in ms. Kept
/// strictly below `POLL_TIMEOUT` so the transport never severs a poll the server is
/// legitimately holding. A pre-long-poll control plane ignores the query param.
const POLL_PATH: &str = "/v1/worker/poll?wait_ms=25000";

#[derive(Debug, thiserror::Error)]
pub enum ProtocolError {
    /// Config provided no worker token; we refuse to send unauthenticated.
    #[error("worker token is empty; refusing to send unauthenticated request")]
    MissingToken,
    /// Transport / connection / TLS failure.
    #[error("transport error calling {endpoint}: {source}")]
    Transport {
        endpoint: String,
        #[source]
        source: reqwest::Error,
    },
    /// Server returned a status we did not expect for this endpoint.
    #[error("unexpected status {status} from {endpoint}: {body}")]
    Status {
        endpoint: String,
        status: StatusCode,
        body: String,
    },
    /// Body could not be decoded into the expected type.
    #[error("decoding response from {endpoint}: {source}")]
    Decode {
        endpoint: String,
        #[source]
        source: reqwest::Error,
    },
}

pub struct ControlPlaneClient {
    http: Client,
    base_url: String,
    token: String,
}

impl ControlPlaneClient {
    /// Build a client. Fails fast if the token is empty so we never silently
    /// issue unauthenticated requests later.
    pub fn new(
        base_url: impl Into<String>,
        token: impl Into<String>,
    ) -> Result<Self, ProtocolError> {
        let token = token.into();
        if token.is_empty() {
            return Err(ProtocolError::MissingToken);
        }
        let http = Client::builder()
            .timeout(REQUEST_TIMEOUT)
            .user_agent(concat!("cx-agent/", env!("CARGO_PKG_VERSION")))
            .build()
            .expect("reqwest client builds with rustls");
        Ok(Self {
            http,
            base_url: base_url.into().trim_end_matches('/').to_string(),
            token,
        })
    }

    fn url(&self, path: &str) -> String {
        format!("{}{}", self.base_url, path)
    }

    /// Map a reqwest send error into a typed transport error.
    fn transport(endpoint: &str, source: reqwest::Error) -> ProtocolError {
        ProtocolError::Transport {
            endpoint: endpoint.to_string(),
            source,
        }
    }

    /// Consume a response, erroring unless its status is in `ok`.
    async fn expect_status(
        endpoint: &str,
        resp: Response,
        ok: &[StatusCode],
    ) -> Result<Response, ProtocolError> {
        let status = resp.status();
        if ok.contains(&status) {
            return Ok(resp);
        }
        let body = resp.text().await.unwrap_or_default();
        Err(ProtocolError::Status {
            endpoint: endpoint.to_string(),
            status,
            body,
        })
    }

    /// `POST /v1/worker/register` — advertise capability, expect an echo back.
    pub async fn register(
        &self,
        cap: &WorkerCapability,
    ) -> Result<WorkerCapability, ProtocolError> {
        let endpoint = "/v1/worker/register";
        let resp = self
            .http
            .post(self.url(endpoint))
            .header("X-Worker-Token", &self.token)
            .json(cap)
            .send()
            .await
            .map_err(|e| Self::transport(endpoint, e))?;
        let resp =
            Self::expect_status(endpoint, resp, &[StatusCode::OK, StatusCode::CREATED]).await?;
        resp.json::<WorkerCapability>()
            .await
            .map_err(|e| ProtocolError::Decode {
                endpoint: endpoint.to_string(),
                source: e,
            })
    }

    /// `POST /v1/worker/heartbeat` — expect 204.
    pub async fn heartbeat(&self, hb: &Heartbeat) -> Result<(), ProtocolError> {
        let endpoint = "/v1/worker/heartbeat";
        let resp = self
            .http
            .post(self.url(endpoint))
            .header("X-Worker-Token", &self.token)
            .json(hb)
            .send()
            .await
            .map_err(|e| Self::transport(endpoint, e))?;
        Self::expect_status(endpoint, resp, &[StatusCode::NO_CONTENT, StatusCode::OK]).await?;
        Ok(())
    }

    /// `GET /v1/worker/poll` — long-poll. Returns `Some(task)` on 200,
    /// `None` on 204 (no work). Uses an extended timeout for the held request.
    ///
    /// We ask the control plane to hold the poll open for up to `?wait_ms=25000`
    /// (Plane D §7 D1) so an idle worker is handed a just-submitted task in
    /// milliseconds instead of after a full poll-loop sleep. `POLL_TIMEOUT` (35s) is
    /// the transport ceiling above that server-side wait, so a held poll is never cut
    /// off early. Fully backwards compatible: a control plane that predates long-poll
    /// ignores the query param and answers immediately (200 with work, else 204).
    pub async fn poll_task(&self) -> Result<Option<TaskDispatch>, ProtocolError> {
        let endpoint = "/v1/worker/poll";
        let resp = self
            .http
            .get(self.url(POLL_PATH))
            .header("X-Worker-Token", &self.token)
            .timeout(POLL_TIMEOUT)
            .send()
            .await
            .map_err(|e| Self::transport(endpoint, e))?;
        let status = resp.status();
        if status == StatusCode::NO_CONTENT {
            return Ok(None);
        }
        let resp = Self::expect_status(endpoint, resp, &[StatusCode::OK]).await?;
        let task = resp
            .json::<TaskDispatch>()
            .await
            .map_err(|e| ProtocolError::Decode {
                endpoint: endpoint.to_string(),
                source: e,
            })?;
        Ok(Some(task))
    }

    /// `POST /v1/worker/task/{id}/start` — claim a task, expect 204.
    pub async fn start_task(&self, task_id: Uuid) -> Result<(), ProtocolError> {
        let endpoint = "/v1/worker/task/{id}/start";
        let path = format!("/v1/worker/task/{task_id}/start");
        let resp = self
            .http
            .post(self.url(&path))
            .header("X-Worker-Token", &self.token)
            .send()
            .await
            .map_err(|e| Self::transport(endpoint, e))?;
        Self::expect_status(endpoint, resp, &[StatusCode::NO_CONTENT, StatusCode::OK]).await?;
        Ok(())
    }

    /// `POST /v1/worker/task/{id}/commit` — submit the result. Any 2xx is
    /// success. Transport failures, 429, and 5xx are retried with bounded
    /// exponential backoff; every other 4xx is definitive and returned at once.
    /// Each attempt rebuilds the same authenticated JSON request, so neither the
    /// worker token nor the commit body can disappear on retry.
    pub async fn commit_task(
        &self,
        task_id: Uuid,
        commit: &TaskCommit,
    ) -> Result<(), ProtocolError> {
        let endpoint = "/v1/worker/task/{id}/commit";
        let path = format!("/v1/worker/task/{task_id}/commit");
        let mut delay = COMMIT_RETRY_BASE_DELAY;

        for attempt in 0..COMMIT_MAX_ATTEMPTS {
            let sent = self
                .http
                .post(self.url(&path))
                .header("X-Worker-Token", &self.token)
                .json(commit)
                .send()
                .await;

            match sent {
                Ok(resp) if resp.status().is_success() => return Ok(()),
                Ok(resp) => {
                    let status = resp.status();
                    let retryable =
                        status == StatusCode::TOO_MANY_REQUESTS || status.is_server_error();
                    if retryable && attempt + 1 < COMMIT_MAX_ATTEMPTS {
                        tracing::warn!(
                            attempt = attempt + 1,
                            max_attempts = COMMIT_MAX_ATTEMPTS,
                            %status,
                            delay_ms = delay.as_millis(),
                            "commit_task: transient status, retrying identical commit"
                        );
                        drop(resp);
                        tokio::time::sleep(delay).await;
                        delay *= 2;
                        continue;
                    }

                    let body = resp.text().await.unwrap_or_default();
                    return Err(ProtocolError::Status {
                        endpoint: endpoint.to_string(),
                        status,
                        body,
                    });
                }
                Err(err) => {
                    if attempt + 1 == COMMIT_MAX_ATTEMPTS {
                        return Err(Self::transport(endpoint, err));
                    }
                    tracing::warn!(
                        attempt = attempt + 1,
                        max_attempts = COMMIT_MAX_ATTEMPTS,
                        error = %err,
                        delay_ms = delay.as_millis(),
                        "commit_task: transport failure, retrying identical commit"
                    );
                    tokio::time::sleep(delay).await;
                    delay *= 2;
                }
            }
        }

        unreachable!("bounded commit retry loop always returns")
    }

    /// `POST /v1/worker/task/{id}/fail` — report a typed failure so the control
    /// plane requeues (retryable) or fails+refunds (terminal) in SECONDS instead of
    /// stranding the task for the stale reaper (Plane C/D D0). Expect 200/204.
    pub async fn fail_task(&self, task_id: Uuid, report: &FailReport) -> Result<(), ProtocolError> {
        let endpoint = "/v1/worker/task/{id}/fail";
        let path = format!("/v1/worker/task/{task_id}/fail");
        let resp = self
            .http
            .post(self.url(&path))
            .header("X-Worker-Token", &self.token)
            .json(report)
            .send()
            .await
            .map_err(|e| Self::transport(endpoint, e))?;
        Self::expect_status(endpoint, resp, &[StatusCode::NO_CONTENT, StatusCode::OK]).await?;
        Ok(())
    }

    /// `GET /v1/worker/earnings` — balance + lifetime totals. Polled each
    /// heartbeat to populate the menu-bar status file (see status.rs).
    pub async fn earnings(&self) -> Result<Earnings, ProtocolError> {
        let endpoint = "/v1/worker/earnings";
        let resp = self
            .http
            .get(self.url(endpoint))
            .header("X-Worker-Token", &self.token)
            .send()
            .await
            .map_err(|e| Self::transport(endpoint, e))?;
        let resp = Self::expect_status(endpoint, resp, &[StatusCode::OK]).await?;
        resp.json::<Earnings>()
            .await
            .map_err(|e| ProtocolError::Decode {
                endpoint: endpoint.to_string(),
                source: e,
            })
    }

    /// `GET /v1/worker/connect/status` — real payout readiness (Stripe key
    /// configured / account connected / payouts enabled). Polled each heartbeat
    /// (Supplier onboarding & safety 7->8) to populate the menu-bar trust panel's
    /// `payouts_configured/connected/enabled` fields instead of leaving them
    /// permanently absent.
    pub async fn connect_status(&self) -> Result<ConnectStatus, ProtocolError> {
        let endpoint = "/v1/worker/connect/status";
        let resp = self
            .http
            .get(self.url(endpoint))
            .header("X-Worker-Token", &self.token)
            .send()
            .await
            .map_err(|e| Self::transport(endpoint, e))?;
        let resp = Self::expect_status(endpoint, resp, &[StatusCode::OK]).await?;
        resp.json::<ConnectStatus>()
            .await
            .map_err(|e| ProtocolError::Decode {
                endpoint: endpoint.to_string(),
                source: e,
            })
    }

    /// `GET /v1/worker/verification` — this supplier's real lifetime honeypot
    /// pass/fail counts + derived label. Polled each heartbeat (Supplier
    /// onboarding & safety 7->8) to populate the menu-bar trust panel's
    /// `honeypots_passed/failed/verification_label` fields.
    pub async fn verification(&self) -> Result<SupplierVerification, ProtocolError> {
        let endpoint = "/v1/worker/verification";
        let resp = self
            .http
            .get(self.url(endpoint))
            .header("X-Worker-Token", &self.token)
            .send()
            .await
            .map_err(|e| Self::transport(endpoint, e))?;
        let resp = Self::expect_status(endpoint, resp, &[StatusCode::OK]).await?;
        resp.json::<SupplierVerification>()
            .await
            .map_err(|e| ProtocolError::Decode {
                endpoint: endpoint.to_string(),
                source: e,
            })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[derive(Clone, Copy)]
    enum MockCommitResponse {
        DropConnection,
        Status(u16, &'static str),
    }

    // Minimal real HTTP server for commit retry tests. It records each complete
    // request (headers + body), then either drops the connection without a
    // response (transport failure) or returns the requested status.
    async fn spawn_commit_server(
        responses: Vec<MockCommitResponse>,
    ) -> (String, std::sync::Arc<tokio::sync::Mutex<Vec<Vec<u8>>>>) {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};

        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let captured = std::sync::Arc::new(tokio::sync::Mutex::new(Vec::new()));
        let captured_by_server = captured.clone();
        tokio::spawn(async move {
            for response in responses {
                let (mut socket, _) = listener.accept().await.unwrap();
                let mut request = vec![0u8; 64 * 1024];
                let mut total = 0usize;
                let header_end = loop {
                    let n = socket.read(&mut request[total..]).await.unwrap();
                    assert!(n > 0, "commit client closed before sending headers");
                    total += n;
                    if let Some(pos) = request[..total]
                        .windows(4)
                        .position(|window| window == b"\r\n\r\n")
                    {
                        break pos + 4;
                    }
                };
                let headers = String::from_utf8_lossy(&request[..header_end]).to_lowercase();
                let content_length = headers
                    .lines()
                    .find_map(|line| {
                        line.strip_prefix("content-length:")
                            .map(|value| value.trim().parse::<usize>().unwrap())
                    })
                    .unwrap_or(0);
                while total < header_end + content_length {
                    let n = socket.read(&mut request[total..]).await.unwrap();
                    assert!(n > 0, "commit client closed before sending its JSON body");
                    total += n;
                }
                request.truncate(header_end + content_length);
                captured_by_server.lock().await.push(request);

                match response {
                    MockCommitResponse::DropConnection => {
                        socket.shutdown().await.ok();
                    }
                    MockCommitResponse::Status(status, body) => {
                        let reason = match status {
                            200 => "OK",
                            202 => "Accepted",
                            204 => "No Content",
                            400 => "Bad Request",
                            429 => "Too Many Requests",
                            503 => "Service Unavailable",
                            _ => "Status",
                        };
                        let wire = format!(
                            "HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                            body.len()
                        );
                        socket.write_all(wire.as_bytes()).await.unwrap();
                        socket.shutdown().await.ok();
                    }
                }
            }
        });
        (format!("http://{addr}"), captured)
    }

    fn test_commit(task_id: Uuid) -> TaskCommit {
        TaskCommit {
            task_id,
            result_key: format!("jobs/test/tasks/{task_id}/result.json"),
            duration_ms: 123,
            tokens_used: 7,
            result_sha256: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
                .to_string(),
            hardware_temp_c: Some(51.5),
        }
    }

    fn assert_same_commit_requests(requests: &[Vec<u8>], task_id: Uuid, commit: &TaskCommit) {
        let expected_body = serde_json::to_value(commit).unwrap();
        for request in requests {
            let header_end = request
                .windows(4)
                .position(|window| window == b"\r\n\r\n")
                .unwrap()
                + 4;
            let headers = String::from_utf8_lossy(&request[..header_end]).to_lowercase();
            assert!(
                headers.starts_with(&format!(
                    "post /v1/worker/task/{task_id}/commit http/1.1\r\n"
                )),
                "wrong commit request line: {headers:?}"
            );
            assert!(
                headers.contains("x-worker-token: retry-secret\r\n"),
                "retry dropped or changed worker auth: {headers:?}"
            );
            let body: serde_json::Value = serde_json::from_slice(&request[header_end..]).unwrap();
            assert_eq!(body, expected_body, "retry changed commit JSON");
        }
    }

    // The poll path must carry the long-poll request (Plane D §7 D1): an idle worker
    // asks the control plane to hold the poll open for wait_ms rather than spin a
    // local sleep loop. Guards against the query param silently regressing to the
    // old single-shot path.
    #[test]
    fn poll_path_requests_long_poll() {
        assert!(
            POLL_PATH.starts_with("/v1/worker/poll"),
            "poll still hits the worker poll endpoint"
        );
        assert!(
            POLL_PATH.contains("wait_ms=25000"),
            "poll requests a 25s server-side wait, got {POLL_PATH:?}"
        );
    }

    // The requested wait must stay strictly under the transport ceiling, or reqwest
    // would sever a poll the server is legitimately holding open and we would lose
    // the just-claimed task. 25s wait < 35s POLL_TIMEOUT, with real headroom.
    #[test]
    fn long_poll_wait_fits_under_transport_ceiling() {
        let wait = Duration::from_millis(25_000);
        assert!(
            wait < POLL_TIMEOUT,
            "wait_ms ({wait:?}) must be below POLL_TIMEOUT ({POLL_TIMEOUT:?})"
        );
        // Generous margin so the final claim + response can land before the ceiling.
        assert!(
            POLL_TIMEOUT - wait >= Duration::from_secs(5),
            "need >=5s headroom between wait and transport ceiling"
        );
    }

    // url() composes the trimmed base URL with the (already query-bearing) path so
    // the long-poll param reaches the wire unmangled.
    #[test]
    fn url_joins_base_and_path() {
        let c = ControlPlaneClient::new("http://localhost:8080/", "tok").unwrap();
        assert_eq!(
            c.url(POLL_PATH),
            "http://localhost:8080/v1/worker/poll?wait_ms=25000"
        );
    }

    // An empty token is refused up front so we never issue an unauthenticated poll.
    #[test]
    fn empty_token_is_rejected() {
        assert!(matches!(
            ControlPlaneClient::new("http://localhost:8080", ""),
            Err(ProtocolError::MissingToken)
        ));
    }

    #[tokio::test]
    async fn commit_retries_5xx_and_429_with_identical_auth_and_body() {
        let (base, captured) = spawn_commit_server(vec![
            MockCommitResponse::Status(503, "restart in progress"),
            MockCommitResponse::Status(429, "slow down"),
            MockCommitResponse::Status(202, "queued"),
        ])
        .await;
        let task_id = Uuid::new_v4();
        let commit = test_commit(task_id);
        let client = ControlPlaneClient::new(base, "retry-secret").unwrap();

        client
            .commit_task(task_id, &commit)
            .await
            .expect("third transient-status attempt should accept the commit");

        let requests = captured.lock().await;
        assert_eq!(
            requests.len(),
            3,
            "503 and 429 should each trigger one retry"
        );
        assert_same_commit_requests(&requests, task_id, &commit);
    }

    #[tokio::test]
    async fn commit_retries_transport_drop_then_succeeds() {
        let (base, captured) = spawn_commit_server(vec![
            MockCommitResponse::DropConnection,
            MockCommitResponse::Status(204, ""),
        ])
        .await;
        let task_id = Uuid::new_v4();
        let commit = test_commit(task_id);
        let client = ControlPlaneClient::new(base, "retry-secret").unwrap();

        client
            .commit_task(task_id, &commit)
            .await
            .expect("a dropped response should retry the identical commit");

        let requests = captured.lock().await;
        assert_eq!(requests.len(), 2, "transport loss should trigger one retry");
        assert_same_commit_requests(&requests, task_id, &commit);
    }

    #[tokio::test]
    async fn commit_treats_non_429_4xx_as_definitive() {
        let (base, captured) =
            spawn_commit_server(vec![MockCommitResponse::Status(400, "bad commit")]).await;
        let task_id = Uuid::new_v4();
        let commit = test_commit(task_id);
        let client = ControlPlaneClient::new(base, "retry-secret").unwrap();

        let err = client
            .commit_task(task_id, &commit)
            .await
            .expect_err("400 must be returned without retry");
        match err {
            ProtocolError::Status { status, body, .. } => {
                assert_eq!(status, StatusCode::BAD_REQUEST);
                assert_eq!(body, "bad commit");
            }
            other => panic!("expected definitive status error, got {other:?}"),
        }

        let requests = captured.lock().await;
        assert_eq!(requests.len(), 1, "definitive 4xx must not be retried");
        assert_same_commit_requests(&requests, task_id, &commit);
    }
}
