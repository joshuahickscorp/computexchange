# Customization and Ownership Map

Audit date: 2026-06-30

This is the "what should we own?" map. The aggressive posture is correct, but aggression should
mean owning the parts that create durable advantage, not forking everything that looks important.

## Principle

Own the market-specific contracts. Borrow the commodity substrate until it blocks those contracts.

For Compute Exchange, the market-specific contracts are:

- Verification class and result comparison.
- Quote, budget, and settlement.
- Worker eligibility and independent peer selection.
- Supplier trust and payout UX.
- Engine-lane capability registry.
- Artifact codecs and comparator policy.
- Marketplace liquidity: credits, spot index, private pools.

The commodity substrate is:

- HTTP, Postgres, object storage, Stripe, Docker, TLS, tokenization, low-level tensor primitives,
  and standard UI framework mechanics.

## Own Fully

### 1. Clearing Receipt

Why own it: this is the buyer-visible proof object. Nobody else will design it around our
verification events, quote binding, payout holds, disputes, and class isolation.

Surfaces:

- `control/store.go` verification/invoice projections.
- `control/api.go` status/invoice routes.
- `web/*` receipt surfaces.

Ownership target:

- A stable `ClearingReceipt` schema that can outlive UI redesigns.

### 2. Launch Contract

Why own it: every path into paid work must preserve the same trust semantics.

Surfaces:

- `control/api.go`.
- `control/intake.go`.
- `control/pipeline.go`.
- `control/openai.go`.

Ownership target:

- One internal contract object used by direct jobs, intake launch, pipelines, and OpenAI Batch.

### 3. Verification Comparator Registry

Why own it: this is the moat. Candle/vLLM/MLX are interchangeable only because our control plane
decides what counts as same, comparable, skipped, or fraudulent.

Surfaces:

- `control/verification.go`.
- `control/types.go`.
- future artifact lanes.

Ownership target:

- Per job_type comparator with determinism type, class requirements, tolerance, and receipt label.

### 4. Engine Capability Registry

Why own it: runners are not just code paths. They are promises about determinism, batching, model
support, warm state, and comparable output.

Surfaces:

- `agent/src/runners.rs`.
- `agent/src/hardware.rs`.
- `agent/src/config.rs`.
- `control/scheduler.go`.

Ownership target:

- Heartbeat includes engine, build hash, capabilities, comparator class, batch behavior, and
  benchmark rows.

### 5. Scheduler and Best Execution

Why own it: the exchange wins by choosing the right worker, not merely any worker.

Surfaces:

- `control/scheduler.go`.
- `control/quote.go`.
- `control/benchmark.go`.

Ownership target:

- Transparent scoring with learned latency/failure features, independent verification coverage,
  and quote consistency.

### 6. Supplier App Contract

Why own it: the supplier relationship is product, trust, and supply quality in one surface.

Surfaces:

- `macapp/*`.
- `agent/src/status.rs`.
- `agent/src/config.rs`.

Ownership target:

- App prefs are applied by the agent. App trust fields come from control-plane facts.

### 7. Credit and Spot Economy

Why own it: this is the exchange layer. A generic billing provider will not solve compute-credit
float, verified minting, or spot-price trust.

Surfaces:

- ledger tables.
- `control/payment.go`.
- `control/quote.go`.
- scheduler offered-rate logic.

Ownership target:

- Verified mint, spend, clawback, expiry, and price-index contracts.

## Own Selectively

### Candle

Current policy: keep the vendored-module strategy for model-specific changes. Do not network-fork
Candle merely to feel aggressive.

Own:

- The vendored quantized model module when it touches CX-specific determinism, build hash, Qwen
  correctness, KV cache behavior, and batch proof.
- `cx-infer` layer above Candle: serving loop, prefix sharing, batch composition, engine registry,
  and proof gates.

Do not own yet:

- A full candle-core fork.
- A network fork.
- A from-scratch tensor library.

Fork threshold:

- Fork only when a measured, named job_type needs a candle-core/candle-nn/kernel change that cannot
  live in the vendored module or in vLLM/Hawking, clears at least 1.3x or fixes correctness, and
  remains green on Metal and no-default-features builds.

### vLLM

Use it as the CUDA reference serving lane.

Own:

- Process isolation.
- Determinism soak.
- Engine/build hash construction.
- Scheduler integration.
- Receipt class fields.

Do not own:

- vLLM internals before a benchmark proves they are the bottleneck.

### Hawking / MLX / Apple Continuous Batch

Own the Apple scheduling differentiator.

Own:

- Per-model serve loop.
- Batch-composition determinism rule.
- Hawking runner interface.
- Class isolation and comparator policy.

Borrow:

- Apple/Metal primitives wherever possible.

Do not ship:

- A same-class byte-exact generation lane whose output depends on incidental batch composition.

### Custom Containers

Own policy and sandbox contract.

Borrow:

- Docker, NVIDIA container runtime, OS sandbox primitives.

Own:

- Manifest schema.
- Output comparator declaration.
- Metered-only vs verified labeling.
- Artifact size and network policy.

## Borrow by Default

Keep these boring and replaceable:

- Postgres and SQL driver.
- S3-compatible object storage.
- Stripe and Connect.
- Caddy/TLS/deployment scripts.
- SwiftUI and Sparkle for the Mac app.
- Tokenizers and Hugging Face download plumbing.
- Docker invocation mechanics.
- Prometheus exposition format.

Aggressive ownership here is usually a trap unless a specific contract is blocked.

## Build New

These should be first-party because they are business logic, not plumbing:

- `LaunchContract`.
- `ClearingReceipt`.
- `ComparatorRegistry`.
- `EngineClassRegistry`.
- `PipelineQuote`.
- `SupplierTrustSnapshot`.
- `SpotIndex`.
- `ComputeCreditLedger`.
- `ArtifactCodecRegistry`.
- `AttestationReceipt` fields.

## Kill or Delay

Do not spend serious time on:

- Network Candle fork.
- Custom tensor library.
- Render runner before comparator.
- Credits without treasury cap.
- Fleet enrollment before supplier-distinct verification.
- Attestation product promise before one real attested worker.
- MLX FFI lane if Hawking gives the Apple batching win sooner.

## Repo Refactor Targets

These are ownership-driven refactors, not aesthetic cleanups.

1. Move launch submission internals toward one shared builder used by jobs, intake, pipelines,
   and OpenAI Batch.
2. Move comparator decisions out of scattered conditionals and into a registry table/function.
3. Move engine capability declarations into a single agent-side registry that heartbeat reports.
4. Add a control-plane receipt projection rather than asking UI to assemble proof from many
   endpoints.
5. Add a supplier trust snapshot endpoint for agent/Mac app consumption.

## Ownership Scorecard

A lane deserves first-party ownership when:

- It changes who gets paid.
- It changes whether a result is trusted.
- It changes whether a buyer can predict or cap spend.
- It changes worker eligibility or independent verification.
- It creates marketplace liquidity.
- It appears on a buyer or supplier receipt.

A lane should be borrowed when:

- It is generic infrastructure.
- A mature project already does it and output proof can wrap it.
- Forking it creates sync burden without a measurable buyer/supplier advantage.

