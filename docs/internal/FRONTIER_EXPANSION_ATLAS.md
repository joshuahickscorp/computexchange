# Computexchange Frontier Expansion Atlas

Audit date: 2026-06-30

This is the whole-codebase expansion pass that sits above the Candle-specific docs. The Candle
work remains important, but the bigger moat is not a faster local model runner by itself. The
bigger moat is a verified clearing layer: quote it, route it, prove it, settle it, make it liquid,
then make the engine lanes faster without weakening the proof.

Anchor docs already in the repo:

- [CANDLE_EXPANSION_RESEARCH.md](CANDLE_EXPANSION_RESEARCH.md) - inference-engine levers,
  Candle fork thresholds, Qwen correctness, Hawking/vLLM gates.
- [CANDLE_FORK.md](CANDLE_FORK.md) - vendored Candle patch surface and fork policy.
- [VLLM_LANE.md](VLLM_LANE.md) - CUDA serving lane.
- [HAWKING_PORT_PLAN.md](HAWKING_PORT_PLAN.md) - Apple continuous-batch lane.
- [BROKER_PIPELINE_AUDIT.md](BROKER_PIPELINE_AUDIT.md) - intake, quote, distribution,
  settlement, exchange economy.
- [ROADMAP_REVIEW.md](ROADMAP_REVIEW.md) - current ordered roadmap.
- [BUILD_STATUS.md](BUILD_STATUS.md) - proof state for the perf wave.

Tests observed during this pass:

- `cd control && go test ./...` - green.
- `cd agent && cargo test --no-default-features` - green, 90 passed, 14 ignored.

## 0. Core Thesis

Competitors can rent GPUs. Competitors can wrap vLLM. Competitors can publish a cheaper hourly
rate. The hard thing to copy is a market that clears heterogeneous supply with buyer-visible
proof, bounded price risk, and automatic settlement.

The moat should therefore be measured by five contracts:

1. Buyer can know cost, ETA, risk, and verification overhead before spend.
2. Buyer can bind that quote so submit cannot drift into a surprise bill.
3. Worker selection cannot let one economic actor verify its own output.
4. Result proof is legible at completion and tied to settlement.
5. Faster engines are isolated by `(hw_class, engine, build_hash)` before they touch money.

If a change does not strengthen one of those contracts, it must clear a high bar.

## 1. What Is Already Real

The control plane is much further along than a demo queue. It has authenticated buyer routes,
supplier onboarding, job creation, quote binding, worker poll/claim/commit, webhooks, disputes,
OpenAI-compatible batch endpoints, admin surfaces, Stripe-adjacent billing, API keys, and a real
Postgres scheduler.

The verified-result spine is also real. The server splits JSONL, writes task inputs, injects
redundancy/honeypots, stores result objects, compares peers, records append-only verification
events, applies reputation movement, schedules held payouts, and surfaces a buyer-facing
verification receipt. This is enough substrate to sell "cleared compute," not merely "cheap
inference."

The agent is real across local inference, warm pools, hardware heartbeat, build hashes, typed
failures, sandboxed custom containers, and a Mac menu bar app. The current weak spots are mostly
not "missing everything"; they are seams where a strong primitive is not yet carried through the
next product layer.

## 2. Non-Candle Frontier Lanes

Each lane below includes the reason it matters, the repo surface, and the proof gate.

### F1. Pipeline-Safe Launch Contract

Current finding: direct `POST /v1/jobs` can bind `quote_id`, `max_usd`, `verification`,
`min_reputation`, and `private_pool`. Auto-launched intake and user-defined pipelines create
jobs through server-side chaining paths that do not carry that full contract.

Surfaces:

- `control/api.go` job creation has the strong path.
- `control/intake.go` auto-launch creates work from detected repository inputs.
- `control/pipeline.go` user-defined pipeline stages create follow-on jobs.
- `web/demo.html` live launch routes through pipelines rather than through a quote-first flow.

Why it matters: this is the biggest "against ourselves" bug class. A polished buyer path can
silently bypass the very proof, spend-cap, quote, and private routing machinery that makes the
product differentiated.

Build:

- Introduce a `LaunchContract` struct that carries `quote_id`, per-stage or aggregate `max_usd`,
  `verification`, `min_reputation`, `private_pool`, data residency, and requested tier.
- Make intake launch and pipeline stage submission require a contract object.
- Add pipeline-level quote planning: either one composite quote or one quote per generated stage.
- Refuse live launch when a stage cannot be priced or verified.

Proof gate:

- Integration test that a launched intake job has non-null `quote_id`, a persisted `max_usd`, and
  a non-empty verification policy.
- Integration test that pipeline stage 1 inherits the launch contract from stage 0.
- UI test that live launch cannot skip the quote step.

### F2. Supplier-Distinct Verification

Current finding: redundancy selection excludes the anchor worker, but the current tests and code
allow a second worker owned by the same supplier to act as the verification peer.

Surfaces:

- `control/scheduler.go` peer selection.
- `control/store.go` peer result lookup.
- `control/integration_test.go` has tests that currently encode "different worker" behavior.

Why it matters: fleet enrollment without supplier-distinct verification creates a self-confirming
fraud hole. The bigger the supplier becomes, the more dangerous the current rule gets.

Build:

- Add supplier exclusion to redundancy peer selection.
- Add supplier exclusion to peer result lookup and dispute reverification.
- Allow an explicit "same supplier lab mode" only in tests or admin experiments, never default
  paid verification.
- Surface "no independent peer available" honestly in the receipt.

Proof gate:

- Test with two workers under one supplier plus one under another supplier: peer must be the other
  supplier even if slower.
- Test with only one supplier: no auto-dock, no fake match, receipt says independent peer missing.

### F3. Generation Honeypot Activation

Current finding: class-aware honeypot machinery exists, but byte-exact generation honeypots are
safe-but-inert when `answer_class` is blank or when the known answer is a placeholder that cannot
match the actual result schema.

Surfaces:

- `control/verification.go` class-gated honeypot comparison.
- `control/store.go` `GetHoneypotAnswer`.
- `control/seed.go` placeholder honeypot rows.
- `agent/tests/golden` and `docs/BUILD_STATUS.md` for seeded build classes.

Why it matters: redundancy checks are expensive and coverage-limited. Honeypots are the cheap
fraud tripwire, but generation fraud is currently not strongly caught by them.

Build:

- Record schema-valid greedy outputs for each reference `(device, engine, build_hash)` class.
- Store `answer_class` as `engine|build_hash`.
- Add an admin/CLI seed path that refuses to write blank-class byte-exact honeypots.
- Include honeypot coverage in the buyer receipt and supplier trust panel.

Proof gate:

- Honeypot pass and fail integration tests for `batch_infer` with answer class present.
- Negative test: wrong class never auto-quarantines.

### F4. Intake Hardening and Detection Honesty

Current finding: repository raw file reads are not strongly bounded, `.pdf` can be detected as
document-set even though extraction handles only text/html-like content, and audio can be detected
before the upload path is fully wired.

Surfaces:

- `control/intake.go` detection, `RawFile`, extraction.
- `control/intake_test.go`.
- `web/demo.html` client detection.

Why it matters: the conversion funnel should be aggressive, but never confidently wrong. Every
"supported then zero records" launch trains buyers not to trust the product.

Build:

- Add per-file and aggregate read caps with `io.LimitReader`.
- Make server detection the only detection authority, and let the UI consume it.
- Remove `.pdf` from supported document-set until a real extractor exists, or add a bounded PDF
  text extraction path.
- Make audio detection return "supported=false, needs upload path" until the agent path is real.
- Add code-repo to embed/index detection for `.go`, `.rs`, `.ts`, `.tsx`, `.py`, `.md`, and config
  files with deterministic chunking.

Proof gate:

- Tests for oversize files, truncated trees, PDF-only repos, audio-only repos, mixed code repos,
  and stray CSVs.

### F5. Buyer Receipt as the Product

Current finding: job status and invoice already contain verification and quoted-vs-actual data,
but the pipeline/live product surface does not make it the hero object after completion.

Surfaces:

- `control/store.go` `JobVerification` and invoice projection.
- `control/api.go` job status and invoice routes.
- `web/demo.html`, `web/dashboard.html`, `web/skeleton.html`.

Why it matters: if buyers do not see the proof, the proof does not sell. A cheap result with no
receipt looks like every other batch API. A cleared trade with quote, peer coverage, class, and
settlement looks like an exchange.

Build:

- Add a first-class `ClearingReceipt` projection.
- Include quote, actual charge, supplier paid, platform take, verification label, checked counts,
  cross-class skips, dispute status, engine/build class, and webhook delivery state.
- Add per-task drilldown for enterprise/debug users.

Proof gate:

- End-to-end test that a completed verified pipeline returns a receipt with non-zero checked
  counts and quoted-vs-actual fields.

### F6. Supplier Trust Panel Must Be Fed by Real Control-Plane State

Current finding: the Mac app has a trust panel and optional status fields, but the agent status
writer mostly emits local heartbeat/earnings state. The richer payout and verification fields are
usually empty.

Surfaces:

- `macapp/ComputeExchangeAgent/TrustPanel.swift`.
- `macapp/ComputeExchangeAgent/StatusModel.swift`.
- `agent/src/status.rs`.
- `agent/src/protocol.rs`.
- `control/suppliers.go` and worker status routes.

Why it matters: supplier retention is a moat. The app should prove "you are paid, trusted, and
safe" with live facts instead of empty placeholders.

Build:

- Add agent polling for payout readiness, last/next payout, held/ready balance, and verification
  counts.
- Write those fields into `status.json`.
- Show "unknown" only when the control-plane call failed, not as the normal state.
- Add a supplier trust receipt: recent tasks, honeypot passes, disputes, clawbacks, and payout
  state.

Proof gate:

- Fixture test for `status.json` containing trust fields.
- Manual Mac app run showing non-empty trust panel from a local control plane.

### F7. Mac App Preferences Must Actually Control the Agent

Current finding: the Mac app writes `agent.prefs.toml` as a sidecar. The code comments are honest
that it does not patch the canonical agent config. If the launched agent reads only `agent.toml`,
the menu toggles can become decorative.

Surfaces:

- `macapp/ComputeExchangeAgent/AgentController.swift`.
- `agent/src/config.rs`.
- install/launch scripts.

Why it matters: an operator who toggles quiet hours or minimum payout must be able to trust the
toggle. Decorative controls are worse than absent controls.

Build:

- Add an explicit `--prefs` or `CX_AGENT_PREFS` config overlay path.
- Merge sidecar prefs into runtime config with clear precedence.
- Emit the applied prefs back into `status.json`.
- Disable unavailable controls when the overlay cannot be written or read.

Proof gate:

- Agent unit test that sidecar prefs override defaults.
- Mac app launch test proving a changed minimum payout reaches heartbeat/scheduler fields.

### F8. Custom Container Lane Policy

Current finding: the custom BYO-container sandbox is much stronger than a stub: Docker, no
network, read-only rootfs, dropped caps, no-new-privileges, unprivileged user, memory/pids/time
caps. But the output verification policy is not as mature as the sandbox.

Surfaces:

- `agent/src/sandbox.rs`.
- `agent/src/runners.rs` custom runner.
- `control/verification.go` byte-exact default for unknown/custom job types.
- `proto/manifest.schema.json`.

Why it matters: custom compute can become the enterprise wedge, but unverifiable custom output
must not be sold as cleared deterministic inference.

Build:

- Split custom into explicit modes: `trusted_metered`, `byte_exact`, and future `artifact_tolerant`.
- Require buyers to choose a comparator or accept "metered only, not verified."
- Keep auto-launch away from custom until a comparator exists.
- Add manifest fields for output type, comparator, determinism class, and max artifact size.

Proof gate:

- Custom jobs with no comparator return receipt label `metered_unverified`, not `verified`.
- Custom byte-exact jobs require independent supplier peers.

### F9. OpenAI Batch Compatibility as Buyer Acquisition

Current finding: the control plane exposes OpenAI-compatible batch routes. This should be treated
as a migration surface, not a side route.

Surfaces:

- `control/openai.go`.
- `control/api.go`.
- docs and examples.

Why it matters: buyers already understand async batch. The wedge is "same request shape, cheaper
verified local/edge supply for eligible jobs."

Build:

- Add cookbook examples for OpenAI Batch-shaped inputs.
- Add compatibility tests for create, list, retrieve, cancel, output file, error file.
- Add quote-before-batch endpoint for OpenAI-shaped JSONL.
- Return a clearing receipt mapped into metadata or a companion endpoint.

Proof gate:

- A drop-in sample script can submit a batch, poll, download output, and fetch receipt.

### F10. Private Pools as Enterprise Product

Current finding: scheduler support for private pools exists, but the product surface is not yet
the enterprise workflow it implies.

Surfaces:

- `control/scheduler.go` private pool claim gate.
- `db/schema.sql` private pool tables.
- admin/buyer routes.

Why it matters: a business may want "only my office Macs" or "only my approved supplier fleet."
That is a more defensible product than public cheap compute alone.

Build:

- Buyer UI for private pool membership.
- Supplier invitation and revocation.
- Quote response that separates public eligible workers from private eligible workers.
- Receipt field showing private pool routing.

Proof gate:

- Private-pool job cannot be claimed by a public worker even if otherwise cheaper/faster.

### F11. Spot Index and Credit Flywheel

Current finding: fixed catalogue pricing is simple, but an exchange must eventually expose price
discovery and liquidity loops. The ledger has enough substrate, but not the economic layer.

Surfaces:

- `control/payment.go`, `control/store.go`, billing and ledger tables.
- `control/quote.go`.
- scheduler offered-rate gates.

Why it matters: price discovery and compute credits can turn suppliers into buyers and help solve
two-sided cold start. This is where "exchange" becomes literal.

Build:

- Per job_type spot index from recent demand, eligible supply, warm supply, p90 latency, and
  verification coverage.
- Buyer price can discount/surge inside bounds, while supplier offered rate must not drop below
  claim-filter floor.
- Credit mint on verified pass, not payout release.
- Treasury cap, expiry, clawback parity, and Sybil limits.

Proof gate:

- Simulation over historical/synthetic queue data proving index does not starve supply.
- Ledger tests for mint, spend, clawback, expiry, and liability cap.

### F12. Scheduler Learning Without Breaking Explainability

Current finding: scheduler has strong deterministic gates and sensible tiebreaks. The next level
is learning from actual task durations and failure rates without making routing opaque.

Surfaces:

- `control/scheduler.go`.
- `control/store.go` task durations and drift rollups.
- `control/benchmark.go`.

Why it matters: marketplace quality comes from routing the right job to the right worker. But the
receipt must still explain why a worker was eligible.

Build:

- Per worker/model/job_type latency distribution.
- Failure and retry penalty in ranking.
- Thermal decay and warm-cache bonus.
- Quote uses the same learned features.
- Admin route explaining rejected and selected candidates.

Proof gate:

- Deterministic scheduling tests remain stable with fixed fixture data.
- Drift report shows quote p90 gets closer after learning.

### F13. API Key Scopes and Webhook Discipline

Current finding: buyer auth exists, webhooks have SSRF/HMAC discipline, and API keys exist. The
next enterprise step is scoped credentials and event-specific controls.

Surfaces:

- `control/accounts.go`.
- `control/api.go`.
- `control/workers.go` webhook delivery.

Why it matters: serious users will integrate this into production. They need limited keys,
rotatable webhooks, and auditability.

Build:

- Scoped API keys: quote-only, submit, read-results, admin, webhook-manage.
- Webhook event filters and secret rotation.
- Replay endpoint for failed webhooks.
- Audit log for key creation/revocation and launch actions.

Proof gate:

- Tests that quote-only keys cannot submit jobs.
- Webhook replay cannot deliver to a blocked/private address.

### F14. Background Worker Leadership

Current finding: background workers handle payout release, stale requeue, disputes, webhooks,
hedging, and reconcile. Comments note some sweeps are not safe to run concurrently from multiple
control instances.

Surfaces:

- `control/workers.go`.
- `control/main.go`.
- `control/reconcile.go`.

Why it matters: shippability and HA collide here. Two control-plane instances are good only if
side-effecting loops have leadership or idempotence.

Build:

- Postgres advisory-lock leadership per sweep.
- Metrics for lock owner and last success.
- Idempotency keys for every external side effect.
- Runbook for failover and stuck-lock recovery.

Proof gate:

- Two control processes in integration test, only one delivers webhook/payout per event.

### F15. Object and Artifact Hardening

Current finding: the system uses object storage for inputs/results. New lanes like PDF, render,
audio, and custom artifacts raise decompression and size risks.

Surfaces:

- `control/storage.go`.
- `control/extract.go`.
- `control/api.go` result merge.
- runner output paths.

Why it matters: every broader artifact lane introduces a new way to exhaust memory, CPU, or
storage. The market cannot clear bad artifacts safely without hard bounds.

Build:

- Per job_type input/output size ceilings.
- Streaming result merge for large outputs.
- MIME sniffing and magic-number checks.
- Decompression bomb limits for PDFs/images/archives.
- Result retention policies by tier.

Proof gate:

- Tests for oversize result rejection and safe failure receipts.

### F16. Security Headers, Session, and CSP Sweep

Current finding: enough web/product surface exists that browser hardening matters.

Surfaces:

- `web/*.html`.
- `control/api.go` static serving/session cookies.

Why it matters: a shippable app needs boring web hardening before growth.

Build:

- CSP, frame-ancestors, referrer policy, nosniff, secure cookies.
- CSRF story for cookie-auth routes.
- Admin route isolation.
- Static asset hash or versioning.

Proof gate:

- Browser smoke test with headers present.
- Auth tests for CSRF-sensitive POSTs.

### F17. Observability and "No Silent Moat Decay"

Current finding: metrics exist, ticker liveness exists, and tests are broad. The next step is
alerts on the exact things that would quietly destroy trust.

Surfaces:

- `control/metrics.go`.
- background worker liveness.
- verification event counters.

Why it matters: if verification coverage drops to zero, the product can still appear to work.
That is unacceptable.

Build:

- Alerts for no verification events in N hours with paid volume.
- Alerts for quote-to-actual drift, no independent peers, webhook backlog, payout backlog,
  stale worker loops, and high cross-class skips.
- Dashboard panels by job_type and engine class.

Proof gate:

- Synthetic metric fixtures trip each alert.

### F18. TEE and Attestation Research Lane

Current finding: this repo's main moat is cross-worker verification, not trusted hardware. But
competitors can sell attestation, especially on NVIDIA confidential computing or TEE clouds.

Surfaces:

- Future worker heartbeat fields.
- Supplier enrollment.
- Receipt schema.

Why it matters: attestation can become a buyer checkbox. We do not need it for every job, but we
should be able to say where it fits.

Build:

- Research receipt fields: attestation provider, quote, measurement, GPU/driver identity,
  enclave/vm identity, and verification class.
- Add optional attested-worker capability in heartbeat.
- Decide whether attestation upgrades routing tier, quote confidence, or receipt only.

Proof gate:

- No production promise until a real attested worker submits a verifiable quote and a buyer
  receipt can display the measurement.

### F19. Engine-Lane Portfolio Discipline

Current finding: Candle, vLLM, MLX/Hawking, and custom can all absorb time. The danger is building
too many partial engines instead of one verified exchange.

Surfaces:

- `agent/src/runners.rs`.
- `agent/src/config.rs`.
- `agent/src/hardware.rs`.
- Candle/vLLM/Hawking docs.

Why it matters: speed is valuable only after it is class-isolated, measured, and sold through the
clearing product.

Build:

- Maintain one engine registry with capabilities, determinism type, batch behavior, comparator,
  and build-hash inputs.
- Every engine lane must produce a benchmark row and a verification-class receipt.
- Any engine that cannot be verified is metered-only.

Proof gate:

- Adding a new engine requires tests for heartbeat, scheduler eligibility, result comparison,
  quote, and receipt.

## 3. Forced Priorities

P0 lanes before any new TAM:

1. Pipeline-safe launch contract.
2. Supplier-distinct verification.
3. Generation honeypot activation.
4. Buyer receipt as product.
5. Intake hardening.
6. Mac app prefs/status truth.

P1 lanes that turn this into an exchange:

1. Private pools.
2. Spot index.
3. Compute credits.
4. Scheduler learning.
5. OpenAI Batch migration.

P2 lanes that expand TAM:

1. Artifact codecs and streaming merge.
2. Perceptual comparator.
3. Render/video.
4. TEE/attestation.
5. Larger model bands and code-repo intelligence.

P3 lanes that should not distract P0:

1. Network Candle fork.
2. Full custom engine rewrite.
3. Render before comparator.
4. Credits without treasury cap.
5. Fleet enrollment without supplier-distinct verification.

## 4. "Nothing Left To Improve" Bar

This codebase is squeezed only when:

- Every launch path carries quote, budget, verification, routing, and receipt contracts.
- Every verification peer is economically independent unless explicitly labeled otherwise.
- Generation honeypots are active per class.
- The Mac app controls the real agent and shows real payout/trust state.
- Every engine lane is either verified, metered-only, or killed.
- Every buyer-facing claim has a receipt field.
- Every background side effect is HA-safe or single-leader.
- Every artifact lane has bounded input, bounded output, and a comparator.
- Every price can be explained from quote inputs and settled against actuals.
- Every remaining open item is blocked by named external hardware, partner access, or an explicit
  business decision, not by "we did not look."

