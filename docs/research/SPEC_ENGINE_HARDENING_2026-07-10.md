# SpecEngine hardening and speed frontier — 2026-07-10

Status: implemented and locally verified. This report covers the shared speculative-execution
substrate, render, transcode/video, token decoding, and the Go receipt projection. It does not
claim that the product hot path is wired: production execution and authoritative receipt
attestation remain explicit blockers below.

## Outcome

The speculative lanes now fail closed on malformed numbers, inconsistent accounting, broken
verifiers/repairs, hostile resource shapes, media timeline divergence, legacy delivery claims,
and unbounded work. One canonical v1 receipt travels through Python, Rust, and Go with explicit
orchestration cost and an `artifact_verified` proof bit. A receipt is locally delivery-eligible
only when it validates, carries measured evidence, carries a non-fail tier, and has current-schema
artifact proof. Rows predating `schema_version` remain inspectable but are forcibly parked.

`artifact_verified` means that the lane completed its local product proof. It is deliberately not
described as server attestation: an untrusted worker can still forge an unsigned JSON receipt.

No lane multiplier is multiplied by another lane multiplier. Every speedup remains one measured
or explicitly modeled `baseline / product` ratio for the same workload.

## Hardened boundaries

| boundary | previous risk | implemented invariant |
|---|---|---|
| Generic Python engine | unbounded iterables; callback/type lies; counterfactual baseline could steer delivery; failed repair could escape | hard unit cap; modality/unit binding; callback/type checks; charged verifier truth owns product acceptance; authoritative lazy baseline fallback on any failure; bounded failure ledger |
| Render gate | NaN/Inf comparisons could fail open; repair time implied repair success; synthetic receipt could look deliverable | finite/range validation; explicit repair proof; failed/unverified repair fails tier; only measured passing gates set artifact proof |
| Transcode/video | SSIM/MD5 could bless a shorter or retimed clip; final concat was not a charged gate; mixed x264 presets produced duplicate/non-monotonic boundary PTS and switched profiles; failed repairs could count | exact frame count + normalized cadence digest + 2 ms absolute/metadata PTS coverage; independently reverified repairs and final artifact; aligned High/high444 profile-critical settings, repeated in-band headers, and compatibility preflight for stream-copy assembly; bounded process/thread/disk/receipt resources |
| Rust substrate | unchecked acceptance/trace arithmetic; permissive receipt ingress | checked unit/batch/aggregate APIs; repair/outcome coherence; 1 MiB/depth/unit bounds; duplicate/alias-conflict and arithmetic checks; finite values; explicit schema/overhead/proof |
| Token loop | target/KV lifecycle was implicit; verifier length was debug-only; hostile draft/order/span could amplify work | target and draft reset/commit/rollback lifecycle; exact verifier length; bounded inputs and rolling n-gram keys; draft truncation; EOS-safe commit/accounting; authoritative greedy fallback |
| Hawking Metal custom op | malformed shapes/regions/positions could reach CPU indexing or Metal dispatch; compile cache could grow without bound | shared CPU/Metal tensor/storage/stride/head/KV/region/position/scale/overflow checks; device/threadgroup limits; fallible CPU allocation; device-keyed single-flight cache bounded to 8 libraries/16 pipelines |
| Go control mirror | duplicate/alias ambiguity; invalid UTF-8; NaN/Inf and phase lies; evidence+tier alone implied delivery | strict UTF-8 and duplicate/alias rejection; depth/size/range/arithmetic validation; explicit artifact proof; deep-copied bounded details; legacy and naked rows park |
| Integrated promotion | imported or modeled timing could be labeled measured and grow a branch | provenance allowlist; model-backed token flag; measured baseline + measured evidence + explicit artifact proof required to grow; unknown/imported rows park |

## Accounting contract

Canonical v1 product time is:

`total_product_time_s = draft_cost_s + verify_cost_s + repair_cost_s + overhead_cost_s`

`overhead_cost_s` includes policy, scheduling, assembly, concat, final verification, and other
product work not owned by the three adapter phases. Non-imported receipts whose total contradicts
the charged phase sum are rejected. A speedup is null when `baseline_source=absent`.

The generic and transcode runners now have two honest modes:

- benchmark mode measures the counterfactual baseline and may emit a measured ratio;
- production mode skips it (`measure_baseline=False` in the generic engine,
  `--skip-baseline` for transcode), still lazily executes and charges the authoritative baseline
  on fallback, and emits `baseline_source=absent` plus a null speedup.

A local transcode A/B produced identical decoded output in both modes. Its production receipt
remained measured and artifact-verified while claiming no baseline or speedup.

## Measured speed work

These are local, workload-scoped results—not product-wide multipliers:

| optimization | controlled result | correctness gate |
|---|---:|---|
| Generic default-fallback baseline reuse, 50 units, 7 alternating trials | 0.126240s -> 0.063336s median, **1.9932x**; baseline calls 100 -> 50 | identical outputs; fallback charged the reused measured baseline time |
| Generic production counterfactual skip, 20 units, 7 alternating trials | 0.063675s -> 0.000159s median; baseline calls 20 -> 0 | identical verified outputs; the controlled **400.05x** wall ratio is removed benchmark overhead, not a product multiplier |
| Incremental rolling n-gram index, 32,768 tokens, 7 alternating trials | 2.302583ms -> 0.861459ms median, **2.6729x** | `exact=true`, accepted fraction 1.0 |
| Resource-normalized transcode fanout, x264 SSIM production, 4s 320x180@12, gate 0.94 | workers 1 -> 4: 1.752031s -> 0.834944s median, **2.0984x** | 3 rotated trials per point; every run verified; baseline and all concurrent speculative encoders each had the same 12-thread aggregate envelope |
| Canonical transcode net check on that fixture, four workers | median measured `baseline/product` = **0.435438x** across 3 runs | honest negative result: verification dominates this small CPU workload, so the branch parks despite faster fanout |
| Lossless final verification | removed a redundant SSIM decode because decoded MD5 + full timeline is stronger; 0.138247s avoided from a 0.570150s old-path estimate, **24.25%** | 3 trials; `exact=true`, artifact verified, retained SSIM was 1.0 |
| Hawking Metal pipeline cache | repeated per-step compilation reduced to one MSL library plus two pipelines per Metal device | device-keyed single-flight cache; failures remain retryable; retention bounded; real Metal parity tests pass |

The transcode fanout ratio uses the enclosing product-pipeline wall clock, not summed overlapping
subprocess durations. It is an orchestration improvement, not evidence that the current CPU
transcode lane beats its baseline: the canonical same-workload check above is below 1x. The token
n-gram result measures local protocol/drafter overhead, not model-backed LLM throughput. The
generic production result demonstrates eliminated work only; without a same-workload baseline,
its canonical receipt correctly publishes no speedup.

## Verification completed

- Python spec-lab discovery: **418 tests passed**, including real FFmpeg timeline/concat attacks.
- Rust `spec-engine`: **24 tests passed**, including adversarial ingress and four-lane ingest.
- Rust `token-spec-poc`: **19 tests passed** in both default and Metal configurations, including
  hostile lengths, fallback, state reset, EOS, and resource-amplification bounds.
- Rustfmt and strict clippy for both owned crates: passed.
- Go control: full unit suite and `go vet ./...` passed.
- Rust agent: **191 tests passed**, 45 network/model-download tests remained explicitly ignored;
  all **20** targeted Hawking hostile-input/cache tests passed, plus the explicitly run real-Metal
  scheduler integration. File-specific formatting is clean. Repository-wide agent formatting and
  strict clippy remain blocked only by pre-existing unrelated files (9 clippy findings in
  `hardware.rs`/`runners.rs`).
- `make spec-test` passed; current Python/token emitters passed strict Rust v1 ingestion;
  workflow YAML and `git diff --check` passed.
- Real local FFmpeg benchmark, production delivery, hard-rejection/no-artifact, lossless, mixed
  fast/repair concat, shifted/truncated/variable-cadence regressions all passed.
- CI now owns the two Rust crates, Python adapters/adversarial tests, strict receipt ingress, and
  bounded real FFmpeg benchmark + production/failure/lossless smokes.

## Production blockers (fail closed)

1. **No authoritative receipt attestation yet.** `artifact_verified` closes accidental and legacy
   fail-open behavior, but an untrusted worker can forge every JSON field. Before billing, bind a
   receipt to job ID, input digest, delivered artifact digest, verifier-policy/build digest, and
   either a trusted signature or server-side artifact re-verification.
2. **No product vertical yet.** The Go quote/receipt code is additive scaffolding; there is no
   registered speculative job type, persistence column, collector endpoint, agent job variant,
   production runner, or clearing-receipt attachment.
3. **Token wall-clock claims are not model-backed.** The POC lifecycle is correct, but the stock
   Candle path still lacks the target-model fork that verifies a draft span in one forward pass
   with transactional KV truncation. Its wall-clock numbers remain modeled/lab-only.
4. **Render command latency is not yet an honest production ratio.** The experiment harness still
   renders some measurement inputs outside `T_stack`. Production needs an AOV-only non-key path,
   real border repair, and a resident/warm Blender session before quoting command latency.
5. **Cross-silicon delivery is pending.** Local Metal self-consistency exists; the matching CUDA
   half is still required before Metal<->CUDA drafting/verification is a delivery claim.

No external API call or new credential was required for this local hardening wave. Existing secret
loading was left untouched and no key material was read, printed, or committed.
