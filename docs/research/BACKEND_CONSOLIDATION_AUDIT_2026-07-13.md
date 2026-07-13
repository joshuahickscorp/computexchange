<!-- CLAIM-SCOPE: internal-engineering-non-authoritative -->
# Backend consolidation audit — 2026-07-13

Date: 2026-07-13

Scope: backend execution, scheduling, verification, billing, settlement, deployment,
and customer performance. Legal and terms work is deliberately excluded.

## Executive verdict

The repository now has a coherent path from a bounded buyer request to an exact
runtime dispatch, checked result, receipt, and funded supplier payout. The strongest
current product shape is not a generic cheap-GPU marketplace. It is **a fixed-quote
verified-work exchange whose production projection is seven explicit Candle/Metal
cells**.

That is a meaningful backend release candidate, but it is not yet an unconditional
go-live attestation:

- production startup correctly refuses an incomplete Stripe/economic configuration;
- no live buyer charge, refund/dispute sequence, or supplier payout was exercised in
  this repository pass;
- worker hardware identity is still self-declared rather than remotely attested;
- CUDA, vLLM, Hawking, Apple clustering, arbitrary containers, and whole-project
  speculative rendering remain outside production admission; and
- the physical multi-machine and sustained-soak gates remain external.

Those are evidence and deployment gates, not reasons to weaken the fail-closed code.

## Consolidated backend path

```text
buyer request
  -> strict schema/resource/max-price validation
  -> immutable per-job economic plan and bounded quote
  -> buyer-cash collection/funding authority
  -> exact generated runtime-cell admission
  -> constraint-aware, bounded-fair worker claim
  -> sandbox-required production agent execution
  -> result cardinality/artifact/independent verification
  -> immutable execution + runtime receipt
  -> one-time ledger settlement
  -> funded, reconciled supplier payout
```

The important property is that mutable catalog or worker profile data can no longer
silently rewrite what a task claims to have executed. Admission is derived from the
generated runtime matrix, the selected axes are frozen on the task, and receipts read
the frozen values.

## What is wired in this pass

### Runtime and inference authority

- One canonical runtime source generates the Go, Rust, JSON, and documentation
  projections.
- Worker registration stores exact authorized tuples rather than treating job and
  model arrays as an accidental Cartesian product.
- Worker identity and benchmark telemetry are rejected unless text fields are
  bounded valid UTF-8, memory/payout/rate values are finite and operationally
  bounded, native throughput is positive, benchmark tuples are unique, and every
  advertised cell fits the worker's declared memory. The same projection is checked
  again at the store boundary rather than trusting only the HTTP handler.
- A claim freezes runtime cell, runtime ID, matrix digest, model wire kind, worker,
  supplier, hardware class, engine, and build identity for the attempt.
- The agent rejects a dispatch whose exact tuple or model wire kind differs from its
  generated manifest.
- Only seven Candle/Metal tuples are advertised. CUDA and experimental runtimes stay
  fail-closed.

### Scheduling and service tiers

- Buyer memory, hardware-class, duration, and data-residency constraints survive the
  full request-to-agent round trip.
- Priority is explicitly a queue preference, not a capacity, fan-out, or latency SLA.
- A durable three-priority-to-one-batch opportunity rule bounds starvation on each
  worker while allowing priority to continue if no eligible batch work exists.
- Pinned independent verification remains ahead of ordinary service-tier work and
  does not erase or consume the fairness debt.
- Per-job budget exposure is serialized at the cap boundary and uses the frozen
  economic plan, including already-running/verifying work and the one-time premium.
- A composed pipeline is priced in full before it creates any job. One buyer-visible
  aggregate `max_usd` is divided into disjoint positive per-stage caps whose sum is
  the aggregate cap; it is no longer copied in full to every concurrently launched
  stage.
- Every indirect stage persists and reapplies the complete launch contract—budget,
  verification policy, reputation floor, private-pool routing, and quote binding.
  A chained stage with a missing or non-positive persisted cap fails closed instead
  of launching uncapped, and a cross-replica advisory lock prevents duplicate stage
  creation.

### Verification, receipts, and settlement

- Expected output cardinality is frozen per task; unknown legacy cardinality does not
  become an invented success count.
- Verification resources, retries, independent-supplier selection, tiebreaks,
  crash recovery, immutable artifacts, and one-time apply/settlement paths are covered
  by dedicated unit and database integration tests.
- Clearing receipts include the exact runtime and execution provenance rather than a
  later view of the worker profile.
- Payout funding is bound to collected cash. A refund or Stripe dispute makes affected
  funding unavailable, blocks a new release, and marks already-crossed transfers as
  requiring operator-visible reversal; it does not pretend that supplier cash was
  automatically recovered.

### Stripe and deployment

- Live/production startup requires a Stripe secret, distinct buyer and Connect webhook
  secrets, the hardening secrets, and the complete versioned economic schedule.
- Webhook signature rotation accepts any valid `v1` signature while retaining timestamp
  tolerance and durable event idempotency.
- Buyer payment-method webhook database failures return a retryable server error rather
  than acknowledging lost state.
- Refund/dispute snapshots are reduced to exact minor-unit cash availability with
  durable Stripe object bindings and out-of-order protection.
- Production Compose passes required values explicitly; the setup and webhook scripts
  document the same contract.

### Customer webhooks and OAuth linking

- Every buyer/job webhook registration receives an independent random 256-bit signing
  secret. Only its `CX_TOKEN_KEY`-sealed ciphertext is stored; the authenticated
  registration response returns the plaintext under `Cache-Control: no-store` and an
  exact duplicate registration can recover the same secret after a lost response.
- Delivery always signs the exact body with the registration's secret. A legacy or
  unreadable row is dead-lettered before network I/O and can be re-armed only through
  authenticated exact re-registration; there is no unsigned or process-global-secret
  fallback.
- Webhook delivery accepts HTTPS only, resolves immediately before delivery, rejects
  private and special-use addresses, and dials only the validated IP set while
  preserving the registered host for HTTP and TLS verification. Environment proxies
  and redirects are refused, closing the validation-to-dial DNS-rebinding and redirect
  exfiltration paths.
- GitHub account linking uses independent random state and browser-initiation values.
  The database stores hashes only; state is provider/buyer/browser-bound, expires,
  and is atomically single-use. The browser binding is an HttpOnly, Secure,
  SameSite=Lax cookie. No reusable `CX_STATE_SECRET` or bare buyer identifier appears
  in the OAuth state parameter.

### Control-plane resource and HA hardening

- Request bodies are centrally capped with `http.MaxBytesReader`: 4 MiB for ordinary
  JSON, 32 MiB for inline job JSON, 72 MiB only for the supported 64 MiB file upload,
  and a dedicated smaller audio-multipart ceiling. Larger direct-job inputs use the
  existing streamed object-reference path.
- OpenAI-shaped file uploads use a closed multipart field set, exact per-file and
  scalar limits, and mode-0600 disk staging before a seekable object-store upload;
  neither multipart parsing nor object download retains a 64 MiB file in control-plane
  memory. Temporary files are removed on every success and rejection path.
- Stripe and GitHub responses are read through explicit sentinel-byte limits and
  bounded clients rather than unbounded `ReadAll` calls. The HTTP server has header,
  read, and idle deadlines plus a header-size limit; Postgres pool size is bounded;
  and the control process installs a soft memory ceiling when the operator has not
  supplied one.
- `control seed` refuses to install public development credentials whenever the
  environment is production or a live Stripe secret is present.
- Authentication and OAuth cookies force the `Secure` attribute whenever
  `CX_ENV=production`, even if proxy metadata is missing; a bad ingress configuration
  therefore fails closed instead of weakening cookie transport.
- Schema migration takes a PostgreSQL session advisory lock across the migration,
  runs the ordinary DDL in one transaction, and retains serialization across the
  per-table telemetry conversions. If unlock cannot be confirmed, the connection is
  discarded rather than returned to request traffic with a leaked lock.
- Every eligible control replica enters PostgreSQL session-lock leader election for
  side-effecting background sweeps. Followers continue serving API traffic and take
  over after leader-session loss. `/readyz` now requires database reachability,
  recent election progress, and live owned sweep tickers, so an inert worker owner
  cannot remain silently ready.

### Dependency vulnerability status

- The Go control and CLI modules, CI, and control builder use Go 1.26.5. The final
  `govulncheck` pass reports no reachable vulnerability after moving off the
  vulnerable Go 1.26.4 `crypto/tls` standard library and upgrading
  `golang.org/x/net` to 0.55.0.
- Both Rust lockfiles use the patched `crossbeam-epoch` 0.9.20, and final
  Rust builds, tests, Clippy gates, and `cargo audit` scans report no security
  vulnerabilities. The transitive `paste` 1.0.15 unmaintained warning remains
  through the Candle/gemm/tokenizers dependency graph; that is tracked as
  maintenance debt, not reported as a fixed vulnerability.

### Production agent safety

- The production macOS launcher requires the sandbox profile and `sandbox-exec` path;
  a missing or failed sandbox exits before worker registration.
- Development runs retain a loud warning instead of silently claiming equivalent
  isolation.
- The resident/spec receipt seams compile and test behind explicit default-off features.
  They are not attached to customer routing, billing, delivery, or settlement.

### Named hardening boundaries

- Pipeline creation and output-to-input chaining synchronously inspect their input to
  quote and construct the next stage, so that workflow path has an explicit 32 MiB
  input ceiling even when the source is an object reference. Direct asynchronous job
  submission retains its streamed object path; a pipeline above this limit must be
  redesigned around bounded stages rather than assuming the synchronous coordinator
  will materialize an arbitrary object.
- `CX_TOKEN_KEY` is the recovery key for sealed OAuth tokens and webhook signing
  secrets. If it is lost or replaced without a migration, an existing webhook secret
  is intentionally unreadable and delivery fails closed. The owning buyer must make
  an authenticated exact re-registration for the same buyer/job/URL and install the
  newly returned secret at the receiver.
- Background-worker failover is control-process HA over one reachable PostgreSQL
  authority. It does not prove database, object-store, host, zone, or regional HA;
  those remain deployment and failure-injection gates.

## Customer-side performance and trade-offs

| Customer path | Current behavior | Practical value | Cost or risk |
|---|---|---|---|
| Ordinary first execution | Runs the admitted pinned engine and verifies the requested output | Predictable semantics and a bounded fallback | Pays normal compute, queue, verification, and transfer cost |
| Exact unchanged rerun | May reuse byte-identical eligible output | The measured render transport path was 6,815.52x median and all 9/9 trials exceeded 1,000x | Any project, dependency, recipe, policy, or output-identity change is a miss; the measured source is preview-only |
| Fresh speculative preview | Uses the lab-only gated Spatial75 path | About 0.542 s / 207.644x in the bound single-frame experiment | Narrow preview contract; not final-render or general-project semantics |
| One-render preview upper bound | Removes the independent agreement render | About 0.331 s / 339.701x | Only 1.636x faster than the gated preview while deleting a major quality gate; not production-worthy |
| Real-model prompt speculation | Opt-in prompt-lookup decode with exact greedy parity | Measured 1.883x at 256 and 2.848x at 512 output tokens | Measured 0.963x at 64 tokens; prompt diversity can erase the win, so default-on would hurt some customers |
| Basic/fallback path | Executes the ordinary engine once | Preserves requested semantics when speculation is ineligible or fails | The platform must reserve and absorb failed speculative work without exceeding the buyer's quote |

The customer promise should therefore be a price, output contract, verification
class, and deadline—not a universal speed multiplier. Upload, queue, model load,
fallback, egress, and verification must stay inside the customer benchmark.

## Cost reduction without quality reduction

Recommended order of operations:

1. **Avoid invalid work.** Exact runtime admission, resource caps, immutable economic
   plans, and artifact/cardinality checks prevent expensive jobs that cannot settle.
2. **Reuse exact eligible work.** Content-addressed inputs and full request identity can
   turn a true rerun into transport while inheriting the source's quality eligibility.
3. **Amortize model and scheduler overhead.** Resident models, continuous batching,
   paged KV, preallocated buffers, and bounded batch assembly improve real throughput
   without changing outputs. Measure cold and warm cohorts separately.
4. **Use speculation selectively.** Admit only where a cheap predictor, confidence gate,
   and full fallback reserve produce a positive expected-cost result. A failed draft is
   platform overhead, not a surprise customer charge.
5. **Amortize payment fixed costs.** Wallet/top-up or invoice batching can spread the
   configured processor fixed fee across many tasks. The current per-task reserve must
   not be reduced until a funded collection design proves the replacement.
6. **Fill idle capacity with batch work.** Fixed buyer quotes and bounded fairness let
   low-urgency work consume otherwise idle devices without creating a supplier auction.
7. **Route by measured completed-work cost.** Apple, CUDA, or another substrate should
   be admitted per exact cell only after quality, failure, total-cost, and physical-soak
   evidence—not from theoretical hardware throughput.

The release comparison metric is cost per verified workload delivered within its
quality and deadline, including retries, fallback, verification, idle retention,
transfer, and failure. Raw GPU-hour or isolated tokens/second can diagnose a lane but
cannot select the product winner alone.

## Inference: where the next real gains are

Inference remains the largest competitive gap because managed providers combine large
warm fleets, aggressive continuous batching, highly optimized kernels, broad model
catalogs, and capacity operations. The highest-value next experiments are:

1. publish p50/p95 end-to-end cold and warm baselines for each of the seven admitted
   cells, including queue and verification;
2. gate resident model pools by memory pressure and prove cancellation/OOM recovery;
3. compare continuous batching and paged KV at fixed output parity across realistic
   concurrency, prompt length, and output-length buckets;
4. admit prompt speculation only from an online policy whose lower confidence bound
   beats baseline after draft and fallback cost;
5. prove the existing CUDA lane on released hardware, then run a sustained soak before
   changing its lifecycle; and
6. benchmark a named buyer workload against the strongest compatible managed API and
   raw GPU substitute using the performance-proof manifest.

Chasing 1,000x on fresh autoregressive inference is not a useful release target. A
1,000x result is realistic only when most work is avoided—exact reuse, a precomputed
answer, or a changed semantic contract. For new tokens with the same model and exact
output contract, the meaningful target is lower verified end-to-end cost and tail
latency.

## Priority and multi-machine product

The current `priority` tier buys bounded queue preference only. A future higher-priced
capacity product should be a separately admitted reservation contract with:

- an explicit device count or capacity envelope and start/deadline window;
- atomic reservation or gang admission where work is synchronous;
- a fixed maximum customer price and stated refund/fallback behavior;
- non-overcommitted inventory, worker-loss replacement, expiry, and teardown;
- per-worker output, verification, receipt, and settlement identity; and
- evidence that added devices improve the named workload after coordination and
  transfer overhead.

More machines are not automatically faster. Independent chunks scale well; a single
model or render dependency graph may be limited by synchronization, interconnect,
memory placement, or an unsplittable critical path.

## Supply and price manipulation controls

The safest market design is deliberately not a free auction:

- buyers receive a fixed bounded quote rather than a price that rises because devices
  disappear;
- suppliers declare payout floors, but those floors may make them ineligible rather
  than automatically raising the buyer's accepted price;
- admission, identity, reputation, concurrency, verification diversity, and payout
  funding are controlled centrally at the micro level;
- one supplier cannot satisfy an independent-supplier verification requirement with a
  second mutable worker identity; and
- service-tier fairness and budget locks prevent a priority flood from silently
  starving all ordinary work or overspending capped jobs.

Future capacity management should add per-cell reserve targets, concentration limits,
admission throttles, anomaly alerts, and quote-expiry/requote rules. It should not let a
supplier bid shortage directly set an already-accepted buyer price.

## Release boundary and next gates

Repository-complete before a release candidate can be cut:

- all default and feature-gated Go/Rust/Python/contract tests pass from one final source
  snapshot;
- migration, deployment YAML, shell syntax, secret scan, generated files, and API
  projections are clean;
- the non-legal change set is committed from a clean index; and
- CI passes on the pushed commit.

External before a public production claim:

- supply the complete live Stripe and economic configuration, register all required
  webhook event types, and perform a bounded real charge/refund/dispute/payout drill;
- notarize and verify the macOS supplier release;
- produce two physical Apple worker receipts plus the required sustained soak;
- promote CUDA only after its own packaged physical proof and soak;
- run the dated, named-workload competitive benchmark; and
- keep whole-project render/spec execution wire-only until sandboxed extraction,
  dependency closure, artifact authority, resource control, and generalization gates
  close.

## Related evidence

- [Runtime capability matrix](../RUNTIME_MATRIX.md)
- [Performance proof contract](../PERFORMANCE_PROOF.md)
- [5x5 execution contract](../FIVE_BY_FIVE.md)
- [Whole-project spec-engine pass](SPEC_ENGINE_WHOLE_PROJECT_PASS_2026-07-13.md)
- [Backend competitive and cost-position audit](BACKEND_COMPETITIVE_AUDIT_2026-07-13.md)
