<!-- CLAIM-SCOPE: internal-engineering-non-authoritative -->
# vLLM-Metal speculative decode on M3 Ultra — measured, quarantined

*2026-07-14. Local experiment only. These are physical Apple Metal/MLX endpoint
measurements, not CUDA results, production capacity, price claims, or a promoted
ComputeExchange runtime cell.*

## Pinned setup

| field | value |
|---|---|
| host | Apple M3 Ultra, 96 GB unified memory; macOS 27.0 build 26A5378j |
| vLLM core | `0.24.0+cpu`, source baseline `ee0da84ab9e04ac7610e28580af62c365e898389` |
| Metal plugin | `vllm-metal 0.3.0.dev20260713103604`; source baseline `4c18ee0e6e3ce2b594ab114d0a53ca24eafb1d58` |
| compute backend | MLX `0.32.0`, native Metal paged attention |
| model | `mlx-community/Llama-3.2-1B-Instruct-4bit` at revision `08231374eeacb049a0eade7922910865b8fce912` |
| weight digest | `model.safetensors` SHA-256 `35e396644bca888eec399f9c0f843ec7fa78b8f8c5e06841661be62b4edf96dd` |
| tokenizer digest | `tokenizer.json` SHA-256 `6b9e4e7fb171f92fd137b777cc2714bf87d11576700a1dcd7a399e7bbe39537b` |
| workload | Sonnet corpus, seed 0; approximately 500 input tokens and exactly 150 greedy output tokens per prompt; EOS ignored |
| candidate | n-gram prompt lookup, 3 speculative tokens, lookup range 2–4, synchronous scheduling |

The source build of the Metal plugin could not compile because this host has the
Command Line Tools but not the full Xcode Metal toolchain. The experiment therefore
used the project's prebuilt plugin wheel inside the isolated
`vllm-metal-cx/.venv-vllm-metal` environment. That distinction is part of the runtime
identity and is why this run cannot attest a source-built fork binary.

## Throughput and latency

Warm/repeated arms used identical prompts and output limits. The first cold-ish
baseline was excluded from the steady comparison.

| arm | concurrency | prompts | output tok/s | mean E2E | p95 E2E | output parity |
|---|---:|---:|---:|---:|---:|---|
| baseline repeat 1 | 1 | 16 | 190.20 | 788.40 ms | 838.22 ms | stable across restart |
| baseline repeat 2 | 1 | 16 | 184.25 | 813.89 ms | 867.70 ms | stable across restart |
| n-gram repeat 1 | 1 | 16 | 322.83 | 464.48 ms | 495.19 ms | stable across restart; differs from baseline on 1/16 |
| n-gram repeat 2 | 1 | 16 | 312.21 | 480.26 ms | 520.77 ms | stable across restart; differs from baseline on 1/16 |
| baseline | 16 | 32 | 698.32 | 3426.90 ms | 4307.42 ms | 32/32 match candidate |
| n-gram | 16 | 32 | 872.58 | 2698.32 ms | 3878.03 ms | 32/32 match baseline |

The single-stream throughput improvement spans **1.64–1.75×** across the two warm
measurements (about 1.70× at their midpoints). At concurrency 16 it is **1.250×**;
mean E2E improves 1.270× and p95 E2E improves 1.111×. The concurrent gain does not
clear the repository's 1.30× deeper-ownership threshold.

Output-array SHA-256 receipts:

- baseline, concurrency 1, both repeats:
  `4c415c07d9a248ae4d93cb7bb59eb35523e227027ed5b118225476aa90d163c9`;
- n-gram, concurrency 1, both repeats:
  `8664d3384c596688938bcf6bfca878a32deb5add94d8332a58e2eef95f71c6d5`;
- baseline and n-gram, concurrency 16:
  `20fb5c096a2339a16453fa432eb50e54f56aa46e761a5dfe80bef6cda28517cd`.

The raw benchmark captures are retained locally under `.artifacts/vllm-lab/` and
are intentionally gitignored. Use `scripts/spec-lab/run_local_vllm_lab.py` for new
versioned captures rather than treating those working files as durable proof.

## Correctness finding: do not promote this candidate

The concurrency-1 mismatch is reproducible, not measurement noise: baseline repeats
match each other byte-for-byte, n-gram repeats match each other byte-for-byte, and
15 of 16 baseline/candidate completions match. Prompt index 12 first diverges at
output token 92. Baseline selects token 1875 (`"\n\n"`); the speculative path selects
token 702 (`"\n"`). Immediately before that token, the baseline log probabilities are
nearly tied: -1.243320 versus -1.246914, a margin of only 0.003594.

The divergence is the speculative **bonus row** after a fully accepted K=3 span: the
proposer drafts ` too`, ` cruel`, and `.\n`; all three are accepted, then the fourth packed
row chooses the different newline token. An isolated request for prompt 12 on a fresh
n-gram server matches baseline. Replaying prompts 0–11 first and then prompt 12 reproduces
the artifact exactly, so prior shared-prefix/cache history is part of the trigger.

An in-process instrumented replay captured the actual bonus-row logits: token 702 scores
21.44376755 and token 1875 scores 21.43920135, a 0.00456619 margin for the n-gram result.
The plain baseline favors token 1875 by a similarly tiny 0.00359345 log-probability margin.
The verifier correctly emits the target argmax it receives; this is not an acceptance or
accounting defect. It is a near-tie numerical flip in the 4-bit MLX target model's packed
speculative forward, conditioned by reusable prefix-cache K/V. The fact that concurrency
16 returns the baseline token is not evidence that this history- and shape-dependent defect
is safe. A correct parity repair needs transactional single-row re-verification for
low-margin bonus rows or shape-invariant target kernels; a unit-only tie rule cannot know
which token an ordinary decode would have selected.

`scripts/spec-lab/audit_vllm_metal_spec_parity.py` independently replays the retained
opaque captures and returns `parity_failed` (exit 2): matched identities, stable repeats,
15/16 exact outputs, and `baseline_candidate_output_mismatch`. Its receipt has both
`speed_claim_valid:false` and `production_enablement_valid:false`; it reports hashes and
token IDs without retaining completion text.

Consequences:

1. n-gram speculation remains an experiment-only Metal candidate;
2. its speedup cannot be represented as lossless ComputeExchange acceleration;
3. production comparison must include batch-shape and concurrency parity, not merely
   repeatability within one arm; and
4. a tolerance policy, deterministic tie-break/fallback, or numerically invariant
   verifier must be designed explicitly before this lane can influence user results.

## What this changes in the platform plan

This run validates the strategy of mining vLLM rather than treating it as a black-box
dependency: the scheduler, paged KV path, prompt-lookup proposer, acceptance telemetry,
and continuous batching are useful primitives. It also shows why ComputeExchange needs
its own policy and receipt layer around them.

The next controlled ladder is:

1. reproduce and fix the scalar packed-verifier parity defect in the Metal fork;
2. rerun baseline/ngram across concurrency 1, 2, 4, 8, 16, and 32 with output hashes;
3. use the new full-context custom-proposer seam to test CX adaptive speculation policy;
4. build and digest a downstream image from the fork, then bind it in a new completed
   runtime lock rather than inheriting the upstream image identity; and
5. repeat on the pinned CUDA lane before making cost, fleet-capacity, or customer-facing
   turnaround claims.

## Post-capture launch recovery (2026-07-15)

The preserved launch transcript for the raw C1 captures was recovered after this
report was written. It pins the server shape to `max_num_seqs=32`,
`max_num_batched_tokens=2048`, Metal memory fraction `0.5`, synchronous
single-process execution, and `--no-enable-prefix-caching`. The C1 label is
therefore client active-request concurrency, not the server capacity.

This corrects an over-specific causal statement above: reusable prefix-cache
K/V is **not** an established predicate for the divergence, because the known
failing launch had prefix caching disabled. The durable finding remains a
history- and single-active-target-forward-dependent packed speculative argmax
flip. Until a shape-invariant target forward exists, n-gram must take the
ordinary target-only path for every C1 step, regardless of cache configuration;
multi-request speculation remains separately parity-gated. The raw speed
numbers remain quarantined and this correction adds no performance claim.
