# Competitor and Frontier Research 2026

Audit date: 2026-06-30

This is not a full market report. It is a product-pressure brief: what current public surfaces
force Compute Exchange to build, avoid, or position against. Prices and feature pages change, so
official links are the source of truth.

## Source Links

- OpenAI Batch guide: https://platform.openai.com/docs/guides/batch
- OpenAI API pricing: https://openai.com/api/pricing/
- RunPod pricing: https://www.runpod.io/pricing
- Vast.ai pricing: https://vast.ai/pricing
- Salad pricing: https://salad.com/pricing
- vLLM docs: https://docs.vllm.ai/
- MLX GitHub: https://github.com/ml-explore/mlx
- Hugging Face Candle GitHub: https://github.com/huggingface/candle
- NVIDIA confidential computing docs: https://docs.nvidia.com/confidential-computing/
- Phala Cloud docs: https://docs.phala.network/phala-cloud/
- Gensyn docs: https://docs.gensyn.ai/
- Render Network: https://rendernetwork.com/

## Market Pressure Summary

The market does not force us to become a generic GPU marketplace. It forces us to make our
verification, quote, and settlement loop visible enough that buyers understand why they are not
just renting a cheaper box.

Primary pressure:

- OpenAI normalizes async batch as an API shape.
- RunPod, Vast, and Salad normalize shopping for GPU supply by price and availability.
- vLLM normalizes high-throughput CUDA serving.
- MLX and Apple-focused stacks normalize local Apple Silicon inference.
- Confidential computing providers normalize attestation language.
- Decentralized compute projects normalize "network of compute" narratives, even when their
  verification model differs from ours.

Our answer:

- Do not sell only "cheap compute."
- Sell "quoted, verified, settled compute."
- Make every engine lane subordinate to that clearing loop.

## OpenAI Batch

What they prove:

- Buyers understand asynchronous batch workflows.
- A discounted, delayed, file-oriented processing model is acceptable for many workloads.
- The migration path matters as much as raw speed.

What this forces us to build:

- OpenAI Batch-compatible examples and tests.
- Quote-before-submit for OpenAI-shaped JSONL.
- Output and error-file parity.
- A receipt endpoint that makes Compute Exchange more trustworthy than a generic batch run.

Repo connection:

- `control/openai.go` is not a side feature. It should become a buyer-acquisition lane.
- `control/quote.go` should price OpenAI-shaped batch input before job creation.

Do not copy:

- Do not become opaque just because the API shape is familiar. Our differentiator is proof and
  settlement visibility.

## RunPod, Vast, and Salad

What they prove:

- Buyers compare GPU supply by price, class, and availability.
- Marketplace-like supply discovery is already normal.
- Cheap public GPU supply is not enough to be unique.

What this forces us to build:

- Live supply and risk in the quote.
- Capacity ticker and SLA/coverage flags.
- Spot index with honest bound quotes.
- Private pools for buyers who do not want public marketplace uncertainty.

Repo connection:

- `control/quote.go` already calculates cost, risk, ETA, eligible workers, warm workers, and pool
  reputation.
- `control/scheduler.go` already has private-pool, reputation, hardware, memory, and offered-rate
  gates.
- The missing part is productization and binding through every launch path.

Do not copy:

- Do not make the worker listing the product. A buyer wants a completed verified result, not a
  shopping page full of machines.

## vLLM

What it proves:

- CUDA serving has a strong reference implementation.
- High-throughput batching, KV-cache discipline, and production serving APIs are table stakes on
  NVIDIA hardware.

What this forces us to build:

- Treat vLLM as the CUDA serving lane unless Candle beats it in a measured, verified benchmark.
- Create a vLLM engine class with deterministic soak, build-hash inputs, and class-specific
  honeypots.
- Keep Apple/Candle/Hawking comparisons separate from NVIDIA/vLLM comparisons.

Repo connection:

- `agent/src/runners.rs` already has a vLLM seam.
- `agent/src/hardware.rs` and `control/scheduler.go` already understand engine/build class.

Do not copy:

- Do not let vLLM become an unverifiable black box. It must report class identity and produce
  receipt-visible proof.

## MLX and Apple Silicon

What it proves:

- Apple Silicon is a legitimate ML compute target, not merely a local demo environment.
- The developer ecosystem expects Apple-native acceleration to keep improving.

What this forces us to build:

- Keep Apple supply as a differentiated lane.
- Prioritize per-model serve loop and continuous batching over generic MLX FFI work unless MLX
  clearly wins the measured path.
- Make the Mac supplier app excellent.

Repo connection:

- `agent/src/runners.rs` has Apple/Candle paths and an MLX seam.
- `docs/HAWKING_PORT_PLAN.md` is the higher-upside Apple batching plan.
- `macapp/*` is strategically important, not cosmetic.

Do not copy:

- Do not build a separate MLX stack just because it is fashionable. The proof gate is measured
  throughput under our verified job shapes.

## Candle

What it proves:

- Rust-native portable inference is practical.
- A small owned patch surface can beat a heavyweight fork when the product needs tight
  determinism discipline.

What this forces us to build:

- Keep vendored-module ownership where it is paying off.
- Fold output-changing changes into build class identity.
- Treat a full fork as a threshold decision, not a default personality trait.

Repo connection:

- `agent/src/quantized_llama_batched.rs` is already a CX-owned patch surface.
- `docs/CANDLE_EXPANSION_RESEARCH.md` defines fork thresholds.

Do not copy:

- Do not wait on upstream for product-specific verification requirements.

## Confidential Computing and Attestation

What it proves:

- Enterprise buyers may ask for runtime attestation.
- Trust is not only "did another worker agree?" It can also be "what exact environment ran this?"

What this forces us to build:

- Add optional attestation fields to worker heartbeat and receipt schema.
- Decide how attestation interacts with quote confidence and routing.
- Keep verification and attestation separate: attestation proves environment claims, not output
  correctness by itself.

Repo connection:

- Future fields in worker heartbeat, supplier capability, and receipt projection.

Do not copy:

- Do not overclaim confidential compute without a real attested worker and a receipt that can show
  the measurement.

## Decentralized Compute Networks

What they prove:

- The "network of compute" story is crowded.
- Buyers and suppliers may understand tokens, proofs, attestations, or marketplace narratives.

What this forces us to build:

- Clear positioning: Compute Exchange is a verified clearing layer for useful batch work, not only
  a decentralized-compute ideology.
- Public proof artifacts that make the system auditable without requiring buyers to understand
  the entire backend.
- Supplier economics that are simple enough to trust.

Repo connection:

- Verification events, receipts, ledger entries, and quote binding are our proof artifacts.

Do not copy:

- Do not introduce token mechanics before compute credits and cash settlement are safe.

## Render and GPU Media Networks

What they prove:

- Distributed render workloads are intuitive and sellable.
- Artifact comparison is its own discipline.

What this forces us to build:

- Perceptual comparator before render.
- Artifact codec and streaming merge.
- Anti-undersampling defenses.

Repo connection:

- `control/verification.go` needs a comparator branch before render/video can be verified.
- Current `splitJSONL` and merge paths are not enough for video artifacts.

Do not copy:

- Do not ship render as "verified" with only byte equality or no comparator.

## Strategic Positioning

The market map:

- OpenAI: polished async batch API.
- GPU clouds and marketplaces: supply, price, availability.
- vLLM: high-throughput NVIDIA serving.
- Apple stacks: local efficient inference.
- Confidential compute: attested runtime.
- Decentralized compute: network narrative.
- Render networks: artifact workloads.

Compute Exchange should combine only the parts that strengthen our core claim:

> A buyer can buy useful compute with a bound quote, independent verification, and a settlement
> receipt across heterogeneous supply.

That sentence is the north star. Everything else is a lane, not the company.

