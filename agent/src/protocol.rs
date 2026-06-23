//! Control-plane HTTP client — REAL reqwest calls.
//!
//! Every method issues a real request, attaches the `X-Worker-Token` auth
//! header, and maps any non-2xx response to a typed `ProtocolError`. There are
//! no silent fallbacks: a missing token, a transport failure, or an unexpected
//! status all surface explicitly.

use std::time::Duration;

use reqwest::{Client, Response, StatusCode};
use uuid::Uuid;

use crate::types::{Earnings, FailReport, Heartbeat, TaskCommit, TaskDispatch, WorkerCapability};

/// Long-poll budget. Slightly above the server's ~30s poll window so we don't
/// time out a poll the server is legitimately holding open.
const POLL_TIMEOUT: Duration = Duration::from_secs(35);
/// Timeout for ordinary (non-poll) requests.
const REQUEST_TIMEOUT: Duration = Duration::from_secs(20);

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

    /// `POST /v1/worker/task/{id}/commit` — submit the result, expect 204.
    pub async fn commit_task(
        &self,
        task_id: Uuid,
        commit: &TaskCommit,
    ) -> Result<(), ProtocolError> {
        let endpoint = "/v1/worker/task/{id}/commit";
        let path = format!("/v1/worker/task/{task_id}/commit");
        let resp = self
            .http
            .post(self.url(&path))
            .header("X-Worker-Token", &self.token)
            .json(commit)
            .send()
            .await
            .map_err(|e| Self::transport(endpoint, e))?;
        Self::expect_status(endpoint, resp, &[StatusCode::NO_CONTENT, StatusCode::OK]).await?;
        Ok(())
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
}

#[cfg(test)]
mod tests {
    use super::*;

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
}
