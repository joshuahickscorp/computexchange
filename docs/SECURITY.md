# Security posture

> Internal threat model and self-administered attack checklist (Security Posture
> 7→8, docs/internal/CREED_AND_PATH_TO_TEN.md). Every claim below names the file
> that enforces it — this is not a marketing document, and a claim with no
> file:line receipt is a gap, not a guarantee.

## Attack surface

Four distinct entry points, each with its own auth scheme and threat model:

1. **Buyer API** (`POST /v1/jobs`, `/v1/quote`, `/v1/keys`, `/v1/billing/*`) —
   bearer API key (`control/api.go:263`, `authBuyer`) or a `cx_sess_` session
   cookie from self-serve signup. Untrusted input: job payloads, quote requests,
   `s3_key` references.
2. **Worker/agent protocol** (`GET /v1/worker/poll`, `POST /v1/worker/task/{id}/
   commit`, `/v1/worker/register`) — `X-Worker-Token` header
   (`control/api.go:319`, `authWorker`), hashed at rest (`worker_tokens.token_hash`).
   Untrusted input: benchmark/capability claims, committed result bytes.
3. **Admin console** (`/admin/*`) — bearer admin key OR a WebAuthn passkey
   session (`control/api.go:300`, `authAdmin`; `control/webauthn.go`).
4. **Stripe webhooks** (`/v1/stripe/webhook`, `/v1/stripe/connect-webhook`) —
   unauthenticated by design, gated entirely on HMAC signature verification
   (`control/billing.go`, `verifyStripeSig`).

## What is mitigated today (file:line receipts)

- **IDOR on cross-tenant object references.** `resolveInput`'s `{"s3_key":...}`
  input form is bound to the submitting buyer's own jobs
  (`control/api.go`, `jobsKeyPattern` + `Store.JobBuyerID` check) — found and
  fixed in this pass (2026-07-05), not a pre-existing guarantee. Same rejection
  message for "job doesn't exist" and "belongs to someone else" so an
  unauthorized caller can't enumerate other buyers' job IDs by the error text.
- **Replay of a captured Stripe webhook.** `verifyStripeSig`'s HMAC now also
  checks the signature's own claimed timestamp against a 5-minute tolerance
  (`control/billing.go`, `stripeSigTolerance`) — a correctly-signed payload used
  to be valid forever once computed.
- **Live-Stripe deploy without hardening secrets.** `control/main.go` refuses
  to start (fatal, not a warning) when `CX_TOKEN_KEY`/`CX_STATE_SECRET` are
  unset AND either `CX_ENV=production` OR `STRIPE_SECRET_KEY` is a real
  `sk_live_` key — the second check exists so a live payment key alone is
  sufficient, independent of whether an operator also remembered to set `CX_ENV`.
- **Honeypot/redundancy task fingerprinting.** Every task's storage key is
  `jobs/{job}/tasks/{taskID}/result.json` — primary, redundancy, and honeypot
  tasks are byte-for-byte indistinguishable in their addressing
  (`control/api.go`, job-splitting loop). `is_honeypot`/`is_redundancy` never
  leave the database row.
- **Predictable redundancy-peer selection.** Which primaries get a redundancy
  peer is a keyed hash of `(jobID, that primary's own task UUID)`
  (`control/api.go`, `redundancySelectionHash`), not "the first N primaries in
  submission order" — a supplier watching dispatch order alone cannot infer
  which chunks are more likely to be checked.
- **Untrusted byte streams are capped.** `control/api.go`'s `capBody` middleware
  (`http.MaxBytesReader`, 256 MiB) on every inbound request; the agent's
  `s3_get` bounds a task input download at 512 MiB, checked against both a
  reported `Content-Length` and the real received size
  (`agent/src/main.rs`, `MAX_INPUT_DOWNLOAD_BYTES`).
- **The `custom` general-compute lane sandboxes untrusted buyer code.**
  `agent/src/sandbox.rs`: no network, read-only rootfs, every Linux capability
  dropped, runs as `nobody`, memory/pid caps, hard wall-clock timeout via
  coreutils `timeout`. This is the ONE lane that runs buyer-supplied
  executable content; everything else runs the platform's own fixed
  inference code against buyer-supplied *data*, not buyer-supplied *code*.
- **The Mac inference child process runs under a macOS seatbelt sandbox that
  contains a malicious buyer payload's filesystem AND network blast radius — on
  every launch path, not only the `.app` install path.** The shipped seatbelt
  profile (`macapp/ComputeExchangeAgent/cx-agent.sb`) is applied two ways: the
  menu-bar app launches `cx-agent` via `sandbox-exec -f cx-agent.sb` rather than
  directly (`macapp/ComputeExchangeAgent/AgentController.swift`,
  `sandboxWrappedLaunch`), AND the `cx-agent` binary now RE-EXECS ITSELF under
  `sandbox-exec` on startup when it detects it is not already sandboxed
  (`agent/src/main.rs`, `reexec_under_sandbox_if_needed`, macOS-only) — so a direct
  binary launch (`cargo run`, a hand-rolled LaunchAgent, `make agent-run`) is
  contained too, not only the supported install path.
  - **Filesystem containment.** The profile denies ALL filesystem writes and
    re-allows only the model cache, the agent data dir, and the system temp dirs —
    and denies reads of `~/.ssh`, `~/.aws`, `~/Library/Keychains`, and the user's
    `Documents`/`Desktop`/`Downloads`. So a crafted GGUF/tokenizer/audio input that
    trips a parser bug in `cx-agent` or its dependencies can no longer plant a
    LaunchAgent for persistence, overwrite the operator's documents, or exfiltrate
    their SSH keys / cloud credentials / keychain.
  - **Network containment (added in this pass — what seatbelt CAN enforce).** The
    child is a pure network CLIENT, so ALL inbound connections and ALL socket binds
    are denied (`network-inbound`, `network-bind`) — a payload cannot open a
    listening backdoor. Outbound is denied by default and re-allowed only on the
    ports inference legitimately uses (443 HTTPS to control plane / storage /
    HuggingFace, 80, 53 DNS, loopback, unix sockets), so a payload can no longer
    phone home on an arbitrary port (SSH exfil on 22, an IRC C2 on 6667, a bespoke
    exfil port). Seatbelt's network filter accepts ONLY `*` or `localhost` as the
    host, so this is PORT-and-DIRECTION containment, not a per-HOST allowlist — the
    residual "only the control/storage host" half is a named gap below.
  This is *proven*, not asserted: `macapp/ComputeExchangeAgent/sandbox-profile-test.sh`
  runs the shipped profile against standalone binaries and asserts all 17 containment
  rows (8 legitimate ALLOWs, 9 hostile DENYs — including the 4 new network rows:
  no-listen DENY, loopback-egress ALLOW, :443-egress ALLOW, arbitrary-port :6667
  DENY, the last distinguished from an ordinary timeout by an in-kernel EPERM in
  ~15ms vs. ~75s); it is a CI gate on the macOS `agent` job
  (`.github/workflows/ci.yml`). The self-re-exec is proven end-to-end on real Apple
  Silicon: a DIRECT launch of the real `cx-agent` binary (not via the `.app`) re-execs,
  registers against a mock control plane, and is then DENIED writing its `status.json`
  into `~/Documents` (`CX_STATUS_PATH` probe) while the same unsandboxed run writes it
  successfully — the exact "a filesystem write it should be denied is denied" proof.
  When the profile can't be resolved (a bare dev build with no assembled `.app` bundle
  and no `CX_SANDBOX_PROFILE` override), both the launcher and the self-re-exec fall
  through to an UNSANDBOXED run and say so — the launcher records `sandboxActive=false`,
  the binary logs a loud "running UNSANDBOXED" warning — never claiming a protection it
  isn't applying (Security Posture 8→9, docs/internal/CREED_AND_PATH_TO_TEN.md).
- **Worker tokens and API keys are hashed at rest**, never stored or logged in
  plaintext (`worker_tokens.token_hash`, `api_keys` masked-hint pattern,
  `control/suppliers.go`).
- **Dependency vulnerability scanning is a CI gate, not a suggestion.**
  Go: `govulncheck` (`.github/workflows/ci.yml`, `control` job). Rust:
  `cargo audit` against the RustSec advisory database (same file, `agent` job)
  — added in this pass; caught and fixed one real HIGH-severity finding
  (`quinn-proto` RUSTSEC-2026-0185, remote memory exhaustion) and an `anyhow`
  unsoundness advisory on first run, both resolved via `cargo update`.

## Known, named gaps (not silently assumed fixed)

- **The Mac inference sandbox's network containment is PORT-and-DIRECTION level,
  NOT per-HOST — outbound 443 is allowed to ANY host, not only the control /
  storage host.** The direction-and-port half IS now enforced and proven (see the
  "mitigated today" entry: no inbound/listen, outbound pinned to 443/80/53 +
  loopback, proven by `sandbox-profile-test.sh`). What is NOT achievable in the
  seatbelt profile is the rung's literal "network allowed only to the control URL
  and storage host": macOS seatbelt's network filter accepts only `*` or
  `localhost` as the host — a numeric IP or a DNS name makes `sandbox-exec` reject
  the whole profile ("host must be * or localhost in network address", verified on
  macOS 26.6). So a compromised child could still open a TLS connection on 443 to an
  *attacker-controlled* host and exfiltrate over it; the profile pins the PORT, not
  the destination. Closing this genuinely requires a different mechanism than a text
  seatbelt profile — the precise remaining wiring is a per-agent egress proxy (or a
  PF anchor) that resolves the configured `control_url` and storage endpoint and
  allows outbound only to those resolved addresses, with the child's traffic forced
  through it. This is a bounded follow-up. The larger, more directly exploitable
  surface — a buyer-payload parser bug reaching the operator's private files, or a
  payload opening a listening backdoor / phoning home on an arbitrary port — is the
  part now closed (Security Posture 8→9, docs/internal/CREED_AND_PATH_TO_TEN.md).
- **Dev-environment caveat: the profile's `~/Downloads` / `~/Documents` /
  `~/Desktop` read-denies will crash the agent if you run it FROM one of those
  directories.** On a real supplier Mac the binary lives in
  `/Applications/ComputeExchangeAgent.app`, the model cache in `~/.cache/huggingface`,
  and the data dir in `~/.compute-exchange` — none under the denied folders, so the
  profile is correct and the real Metal agent runs cleanly under it (proven on real
  Apple Silicon). But if a developer checks the repo out under `~/Downloads` (or
  `~/Documents`/`~/Desktop`) and launches the binary from there, the sandbox denies
  the process reading its own executable/resources and it SIGSEGVs at startup. This is
  a dev-layout artifact, not a profile bug — move the checkout out of those folders
  (or unset `CX_SANDBOX_PROFILE` for a local unsandboxed dev run). Documented here so
  it is a known caveat, not a mystery crash.
- **The systematic adversarial checklist below is written but only partially
  run.** See "Self-administered attack checklist."
- **The CUDA lane's own dependency surface has not been through `cargo audit`**
  — that build requires a real CUDA toolchain to even compile
  (`cargo check --features cuda` fails without `nvcc` present), which this
  session's environment does not have; the CI `agent-cuda` job builds on a
  CUDA container but does not currently run `cargo audit` against that
  feature combination.

## Self-administered attack checklist

Each item's status is honest, not aspirational — "not yet run" means exactly
that, not "presumed safe."

| Attack | Status | Notes |
|---|---|---|
| IDOR sweep (buyer A reads buyer B's job/input/output via a guessed or leaked ID) | **Run, found + fixed one real instance** | `resolveInput`'s `s3_key` — see above. A broader sweep across every buyer-facing endpoint that accepts an ID has not been systematically run. |
| Replay of a captured, validly-signed request | **Partially run** | Stripe webhook replay was found and fixed (see above). Worker-token and buyer-API-key requests have no replay window at all today (no nonce/timestamp) — they rely entirely on the bearer secret itself, which is the standard bearer-token threat model, not a gap unique to this system. |
| XFF / origin spoofing against IP-based rate limits or internal-IP checks | **Run, found one real (currently non-exploitable) gap** | `clientIP`/`isRemote` correctly take the LAST `X-Forwarded-For` hop (the one Caddy itself appends), so an attacker prepending a fake IP cannot spoof `clientIP`'s resolved address (`TestClientIPResistsXFFSpoofing`, `control/ratelimit_test.go`) — the rate-limit/IP-recording side is sound. BUT `isRemote` trusts that resolved value UNCONDITIONALLY, including a caller-supplied `X-Forwarded-For: 127.0.0.1` claim (`TestIsRemoteTrustsXFFLoopbackClaimUnconditionally`) — there is no application-level check that the request actually arrived via Caddy at all. Not exploitable TODAY only because the control plane's port is Docker `expose`d, never `ports`-published (`docker-compose.prod.yml`) — a network-topology guarantee, not a code-level one. |
| Hostile OCI image on the `custom` general-compute lane | **Not yet run against a real GPU host** | `sandbox_argv`'s hardening flags are unit-tested (`agent/src/sandbox.rs`), but no adversarial container (fork bomb, network-exfiltration attempt, privilege-escalation attempt) has been run against a real Docker + NVIDIA Container Toolkit host to confirm the flags hold under a genuine attempt, not just a well-behaved test container. |
| Admin-panel session/passkey hijack attempt | **Not yet run** | `control/webauthn.go`'s ceremonies are unit- and integration-tested for the happy path and basic rejection; no dedicated session-fixation or replay attempt has been run. |
| Honeypot/redundancy gaming (a worker tries to detect and pass only probe tasks) | **Structurally addressed, not adversarially proven** | Task addressing is uniform (see above) and this is the specific hole a prior audit found and closed. No live adversarial worker has been run against a real instance to confirm the fix holds under an actual gaming attempt (docs/internal/CREED_AND_PATH_TO_TEN.md's Verification & Result Trust 7→8 names this same gap). |

Landing the "not yet run" rows above against a real, disposable instance is the
next real step on this facet — each one is a bounded, well-specified task, not
an open-ended audit.
