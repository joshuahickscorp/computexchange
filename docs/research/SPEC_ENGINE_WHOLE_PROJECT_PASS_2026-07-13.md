# Spec engine whole-project pass — current evidence and production boundary

Date: 2026-07-13

Status: authoritative current-state report for this pass; lab and wire-preflight
evidence only. No new production admission, execution, advertising, billing,
delivery, or settlement authority is granted by this document.

This report supersedes older render-frontier numbers and readiness statements
where they conflict with the evidence ledger below. Older reports remain useful
historical records, but they are not the current performance or authorization
source.

## Outcome

The boundary moved materially, but it did not become a bulletproof whole-project
production engine.

- Exact-request, byte-identical cache transport is **16.491 ms median / 6,815.52x**
  and **17.148 ms slowest / 6,554.46x** against the bound 112.396726 s frame-9
  reference. All 9/9 measured trials exceeded 1,000x.
- The current fresh, independently pair-gated Spatial75 preview is **541.767 ms
  median / 207.644x** against the bound 112.494903958 s frame-11 reference. It
  passes its predeclared quality-v3 proof and independent verifier replay.
- The closed one-render v4 upper bound is **331.158 ms median / 339.701x**. Its
  seven post-timing quality proofs and verifier replays pass, but the second
  independent render and pair gate were deliberately removed. It is measurement
  only and cannot authorize publication or production.
- Two temporal arms remain above 1,000x only as **composed**, local-unattested,
  non-integrated estimates: **1,357.392x** for direct prior and **1,211.411x** for
  linear extrapolation. Both are explicitly unauthorized cross-frame audits.
- Real-model Candle prompt-lookup speculation is length-sensitive: the measured
  local diagnostic was **0.963x at 64 tokens**, **1.883x at 256**, and **2.848x
  at 512** on the final five-repetition replay, with exact greedy parity. The
  lane remains opt-in and default-off.
- The checked common engine's recorded synthetic orchestration floor was
  **18.458 ms for 100,000 units, or 5.417 million units/s**. An immediate replay
  during report closure measured 18.469 ms / 5.414 million units/s. This is an
  almost-empty-callback bookkeeping floor, not render or model throughput.
- The exact synthetic token protocol has a **2.376372x (2.377x rounded)** local
  result. Its own receipt labels it non-model-backed; it is not a production LLM
  claim.

The honest product interpretation is therefore narrow: exact reuse can cross
1,000x; a new gated preview reached about 208x; removing a required gate buys only
another 1.636x over that gated preview; and no fresh final-semantics whole-project
render has crossed 1,000x.

## Current evidence matrix

| Lane | Current result | Quality/evidence class | Authorization and valid claim |
|---|---:|---|---|
| Basic Cycles references | 112.396726 s at frame 9; 112.494903958 s at frame 11 | One bound local-unattested reference per benchmark domain; no denominator variance estimate | Reference denominator only, not a customer SLA |
| Exact-request cache transport | 16.491292 ms median; 17.039759 ms type-7 p95; 17.148125 ms slowest; 6,815.520x median; 6,554.462x slowest | 9/9 copies are the same 8,310,590 bytes with the same SHA-256; lookup, full source hash, durable no-clobber artifact publication, and bound sidecar publication are charged | Transport mechanism is authorized only for the exact request key and inherits source eligibility. The measured source is preview-only, not artifact-verified, not production-ready, and not billable |
| Spatial75, two-render gated | 541.767208 ms median; 546.346463 ms p95; 207.644x | Two disjoint 4-SPP render ranges, reference-free pair gate, full reconstruction/publication, predeclared quality-v3 proof, current independent verifier replay; local-unattested | Fresh single-frame experimental preview evidence. Not final-render, production, billing, or generalized scene evidence |
| Spatial75, one-render v4 | 331.158292 ms median; 347.565883 ms p95; 339.701x | Closed report; 7/7 post-timing quality-v3 proofs and verifier replays pass | Measurement-only upper bound. No independent verify render, no pair gate, no publication authorization, and no production change authorization |
| Temporal direct prior | 82.803418 ms composed estimate; 1,357.392x | One-time input validation plus resident median; quality-v3 and verifier pass post-hoc | Unauthorized cross-frame approximation; not an integrated wall measurement or reference-free product decision |
| Temporal linear extrapolation | 92.781626 ms composed estimate; 1,211.411x | Same composed methodology; quality-v3 and verifier pass post-hoc | Unauthorized cross-frame approximation; not an integrated wall measurement or reference-free product decision |
| Candle real-model speculative decode | 0.963x at 64; 1.883x at 256; 2.848x at 512 | Local AB/BA diagnostic on cached Llama-3.2-1B Q4_K_M; speculative output equals independent greedy output | Opt-in through `CX_SPEC_DECODE`; default-off; single-prompt/device evidence, not a fleet or general workload claim |
| Common spec-engine floor | 18.458 ms / 100,000 units; 5.417M units/s | Release build, synthetic accept-all callbacks; outer checked batch wall includes identity preflight and driver bookkeeping | Engine scalability floor only. It says nothing about adapter deadlines, memory, GPU work, rendering, or model throughput |
| Synthetic token protocol | 2.376372x | Exact local protocol receipt on a synthetic code stream | Non-model-backed engineering evidence only |
| Customer PNG re-encode | Level 3: 1,416,180 bytes, 40.058 ms encode median, 153.353 ms modeled encode+wire at 100 Mbps; level 1: 1,956,482 bytes, 31.987 ms encode median, 47.639 ms modeled encode+wire at 1 Gbps | Closed v4 receipt; every measured output is deterministic and raw decoded-RGBA exact; statistics, line-rate arithmetic, source, candidates, code, and environment replay | Single-frame transport option only. It changes encoded bytes and metadata and excludes RTT, TLS, HTTP, congestion, durable/network publication, browser decode, and display |
| Full-project v2 contract | No speed claim | Strict, self-hashed, local-unattested wire preflight | `wire_only`; execution, advertising, production, billing, and delivery all remain false |

Results from different rows must not be multiplied. They use different requests,
frames, semantics, evidence classes, and measured intervals.

## What this means for a customer

### Basic Cycles versus the current spec paths

| Customer situation | Basic Cycles | Current spec behavior | Trade-off |
|---|---|---|---|
| First final render of a novel project | Executes the requested pinned recipe | Still routes to Basic Cycles today | The current fast render lanes prove preview research, not full final semantics |
| Exact rerender of an unchanged request | Normally renders again unless another cache intervenes | Can reuse the exact bytes when project, dependencies, frame, recipe, renderer, policy, and output contract all match | Millisecond-scale service is possible, but a single identity or eligibility mismatch is a miss |
| Interactive preview | Pays the full reference path or a conventional low-quality preview | Gated Spatial75 can produce the tested preview in about 0.542 s | Large time-to-first-preview win, with a narrower single-frame RGBA8 PNG quality contract |
| Removing the second render | Not applicable | Reduces the tested path to about 0.331 s | Only 1.636x faster than gated Spatial75 while deleting the independent agreement gate; this is not a production bargain |
| Animation reuse | Renders each evaluated frame | Temporal reconstruction can be extremely fast in the measured composed audit | Camera cuts, motion, topology, visibility, simulation, and stale pixels can fail; current decisions used post-hoc target references |
| Long copied inference output | Greedy decoding pays one target step at a time | Prompt lookup can amortize target calls and wins at 256/512 tokens in the tested prompt | Short requests lose; diversity and poor copy yield erase the advantage, so default-on would be unsafe |
| Speculation fails | Cycles cost is paid once | A speculative attempt may add work before fallback | Admission must reserve the full fallback and the platform must absorb failed speculative work |

The customer promise should be the requested output, quality tier, and deadline,
not a universal multiplier. Upload, queueing, cold start, egress, browser decode,
and display are not included in the render ratios above.

### Exact-hit economics

For a two-point model where a cache hit costs 0.016491292 s and a miss costs the
full 112.396726 s frame-9 reference, sustaining 1,000x expected speed requires an
exact-hit rate of at least **99.9146598739%**:

`(112.396726 - 112.396726 / 1000) / (112.396726 - 0.016491292)`

At 99.9% hits, the same model yields only **872.161x**. At 99.99%, it yields about
4,053.354x. These are workload models, not measured production hit rates or tail
latency.

### Why the fresh path stopped below 1,000x

The frame-11 1,000x budget is 0.112494904 s. The one-render v4 median is 0.331158292
s, or 2.944 times that budget. Its median target endpoint and manifest alone cost
0.255030958 s. Holding the other median components constant leaves roughly
0.037804028 s for that endpoint, requiring about another 6.746x endpoint reduction.

The useful next speed work is therefore architectural: retained compiled scene
state with complete invalidation, native reconstruction/encoding, faster verified
render representations, and authorized reuse. Removing another verifier or moving
required work outside the timer would not improve customer value.

## Hardening landed in this pass

### Exact cache publication

- The destination descriptor is retained through staged publication and parent
  directory sync, then its inode, size, and full SHA-256 are rebound before return.
- Every consumption performs full source validation; output and sidecar publication
  are no-clobber and rollback-aware.
- The receipt explicitly retains the same-UID storage trust boundary. This is not
  protection from a malicious peer with the same filesystem authority.

The stronger descriptor path costs more than the superseded transport screen, but
still measures 6,815.52x median and 6,554.46x at its slowest trial.

### Snapshot and evidence integrity

- Project bundle v1 hashes bounded no-follow regular-file descriptors, rejects
  links/special entries and portable-path aliases, binds empty directories, and
  replays every early file identity after the full hash pass. A file rewritten
  while a later file is hashed now fails closed.
- Customer-model inputs use bounded descriptor snapshots, finite duplicate-free
  JSON, and a final byte-snapshot substitution check before a model is emitted.
- One-render finalization is restartable after an interrupted proof-publication
  attempt while remaining no-clobber and symlink rejecting.

These checks close the reproduced local races. A hostile atomic project snapshot
still requires sealed extraction or filesystem/VM snapshotting, not repeated
`stat` checks on a mutable operator tree.

### Strict whole-project v2 wire contract

The v2 preflight now binds:

- content-addressed project object key/version/SHA/bytes;
- bundle manifest, bundle, scene, file, directory, and total-byte identities;
- frame range, dimensions, feature cell, output semantics, policies, pinned runtime,
  verifier, fallback requirements, and bounded resources;
- project file/directory limits, total pixel-frames, output objects, disk, memory,
  process, duration, and output-byte ceilings; and
- request and contract self-hashes with strict duplicate-free finite JSON replay.

The field is deliberately named `request_feature_cell_schema_validated`: it means
the request matches a schema cell, not that Blender opened or rendered the project.
The contract remains structurally `wire_only`, unadvertised, non-executable,
non-production-ready, non-billable, and non-deliverable.

### Request identity and billing behavior

- Exact-cache and Spatial75 evidence must match the current request identity.
- A malformed customer request contract now returns
  `rejected_no_execution`; it cannot accidentally select free Basic Cycles work.
- A valid final request with missing or corrupt speculative evidence falls back to
  ordinary Basic Cycles while retaining the standard customer-render billing
  ceiling.
- A valid experimental preview with corrupt evidence also falls back to Basic
  Cycles but stays non-billable.
- One-render and temporal rows remain structurally audit-only and unselectable.

This is still a pure lab policy. It has not been installed as production routing
or settlement authority.

### PNG transport screen

The current code now:

- rejects animated PNG/APNG instead of silently collapsing it to one frame;
- checks decoded RGBA identity for the warmup and every measured payload;
- requires deterministic encoded bytes, size, and SHA across measured trials;
- uses bounded, no-follow, singly-linked file reads;
- emits a self-hashed receipt with code, Python, Pillow, zlib, clock, OS, and machine
  environment pins;
- strictly replays statistics, type-7 p95, compression rows, Pareto selection, and
  line-rate arithmetic; and
- can externally re-open the source and deterministically regenerate candidates.

A post-hardening v4 receipt was generated and externally replayed. Level 3 is the
lowest measured encode-plus-line-rate result from 25 through 500 Mbps: 0.493236 s
at 25 Mbps, 0.266647 s at 50 Mbps, 0.153353 s at 100 Mbps, and 0.062717 s at
500 Mbps. At 1 Gbps, level 1 wins at 0.047639 s. Level 9 is a clear practical loss
on this image: its 0.250884 s median encode cost overwhelms its small additional
byte reduction. These are modeled line-rate floors added to measured in-memory
encode medians, not end-to-end network measurements.

Decoded RGBA identity also does not by itself preserve ICC profiles, gamma chunks,
or arbitrary ancillary metadata. A color-managed customer contract must bind those
semantics separately or avoid re-encoding.

### Common engine and inference bridge

- The checked batch receipt now charges one outer wall interval including unit-id
  and modality preflight, duplicate detection, allocations, folding, and driver
  bookkeeping. This closes the earlier near-zero accounting omission.
- Unit/detail bounds, duplicate-ID preflight, acceptance arithmetic, and panic-to-
  error conversion remain covered by adversarial tests.
- The Candle bridge folds completed immutable counters only after successful decode,
  charges the supplied end-to-end time once, forces `artifact_verified=false`, has
  no baseline speedup, and stays ineligible for delivery.
- The bridge is feature-gated, default-off, and not attached to live routing,
  billing, settlement, or customer results.

`catch_unwind` is not process isolation: the global panic hook can still print,
`panic=abort`, native crashes, OOM, and hung callbacks remain outside that boundary.

## Why this is not yet a whole-production spec engine

“Every file type” currently means any bounded portable regular file may be hashed
as opaque bundle content. It does not mean every file is safe to parse, every
Blender feature is semantically supported, or arbitrary code/add-ons may execute.

The v2 object and the verified local project root are still checked independently.
`object_extraction_verified=false` is intentional: nothing yet proves that the
uploaded object safely extracts to exactly the manifest tree that Blender will
open. The current feature cells are also narrow request-schema cells, not executed
runtime cells.

The generic engine coordinates draft, verify, accept/repair, and receipts. It does
not yet provide deadlines, cancellation, output-byte limits, memory/GPU quotas,
process isolation, sealed staging, or transactional rollback for arbitrary
adapters. Earlier adapter side effects can survive a later unit failure.

Finally, a worker-generated receipt is telemetry, not artifact authority. Production
needs a trusted control-plane identity joining the customer request, immutable input
object, executed policy/build, output object version and digest, independent verifier,
attempt nonce, and settlement row. A receipt that merely says `measured` and
`artifact_verified` cannot be accepted as its own authority.

## Non-negotiable production gates

These gates cannot be traded for another benchmark multiplier.

### 1. Isolation and resource control

Run Blender and model adapters in disposable OS processes, containers, or VMs with
no network, no operator home/add-ons, read-only inputs, sealed scratch/output mounts,
hard CPU/GPU-memory/RAM/disk/PID/time/output limits, deadlines, cancellation, and
process-tree kill. Render and inference reservations must prevent one resident lane
from starving or corrupting the other.

### 2. Artifact and receipt authority

Split untrusted worker telemetry from trusted server admission types. Bind every
attempt to job ID, request/input digest, policy and build digests, unit ledger,
output object version/SHA/bytes, independent verifier, nonce, and server attestor.
Only a sealed control-plane authority row may enable delivery or settlement.

### 3. Object extraction and dependency closure

Stream the content-addressed upload into a private read-only extraction root; reject
archive traversal, links, aliases, sparse/decompression/resource bombs, missing or
extra bytes, and manifest mismatches. Then run a pinned Blender open and dependency
census inside the sandbox. Libraries, UDIMs/sequences, volumes, fonts/audio, IES,
simulation and Geometry Nodes caches, and modifier/node-specific paths need fixtures
and resolve-beneath enforcement.

### 4. Final-semantics execution

Create executed runtime cells that preserve the requested scene, view layer, camera,
color management, channels, bit depth, output format, passes/AOV/Cryptomatte,
multiview, motion blur, compositor, sequencer, Freestyle, transparency, and animation
semantics. Unsupported cells must reject before work or execute the pinned full
Cycles fallback without silently changing the contract.

### 5. Transactional fallback and final verification

Use idempotent attempt staging and a single commit boundary:

`preflight -> speculate -> verify -> accept | repair -> verify -> accept | full Cycles fallback -> verify -> seal`

Server-selected verification must check object bytes and format/cardinality semantics,
plus unpredictable reference frames/tiles and temporal/AOV/depth/normal/alpha checks
where contracted. A failed speculative branch can never publish, charge, or suppress
the fallback.

### 6. Soak and adversarial release gates

Run unseen projects and sequences across every advertised hardware/feature cell,
including corrupt bundles, parser/shader crashes, camera cuts, topology and visibility
changes, OOM/disk/PID/time limits, worker and control restarts, lease expiry, object-
store failure, verifier failure, cancellation, retry races, and duplicate commit.

Promotion requires zero silent corruption, zero unsupported combinations admitted,
zero duplicate artifact or settlement outcomes, complete fallback convergence under
injected failures, and independently replayable per-cell quality/latency distributions.

### 7. Economics and customer contract

Admission must reserve full fallback compute, storage, and egress. Settlement occurs
only after final artifact verification. The platform absorbs an internally rejected
speculative attempt. Quotes expose exact-hit, expected speculative, and worst-case
fallback scenarios rather than promising one scene-agnostic multiplier.

## Production stopping rule

Keep all current whole-project and speculative lanes non-production until every gate
above is demonstrated for one exact advertised cell. In particular, do not change
the v2 flags `execution_enabled`, `production_ready`, `billing_eligible`, or
`delivery_eligible`; do not attach the Candle bridge to routing; and do not let a
preview or worker receipt release money.

Stop optimizing a branch when its apparent improvement requires any of the following:

- removing independent verification or fallback;
- excluding required reconstruction, publication, queue, or durability work;
- changing final output semantics without customer agreement;
- selecting thresholds or evidence after seeing the target;
- combining unrelated receipt ratios;
- relying on a local mean while the tail or negative-control workload regresses; or
- spending more expected compute/cost than the customer latency or margin saved.

Promote incrementally: one immutable runtime, hardware class, feature/output cell,
quality tier, and failure envelope at a time. A 1,000x exact-cache result does not
authorize a 1,000x novel render, animation, inference workload, or whole project.

## Current evidence ledger

### Closure receipts

| Evidence | Path | SHA-256 |
|---|---|---|
| Descriptor-retained exact transport | `/Users/scammermike/.cache/cx-spec-lab/cache-arm/koro-static-cache-copy-descriptor-retained-whole-engine-f9-v-f10-f11-20260713/receipt.json` | `57e43fcdcdde90c3cb17a02ce42214f930e395404f325b203615ad5cd8f4549b` |
| Fresh two-render Spatial75 | `/Users/scammermike/.cache/cx-spec-lab/frontier/koro-spatial75-whole-engine-hardened-f11-20260713/receipt.json` | `13649a6bc6687c4f5ee487a5185a8cf806b0159e19c4ede60c5ca0fffeb16bc6` |
| Closed one-render v4 | `/Users/scammermike/.cache/cx-spec-lab/frontier/koro-spatial75-one-render-whole-engine-hardened-v4-f11-20260713/spatial75-one-render-upper-bound.json` | `e69ce2cba813e8bc9f9e425b02de6f03be74e4ce5782b0bbfe12033c1e85e7ae` |
| Temporal prediction | `/Users/scammermike/.cache/cx-spec-lab/frontier/koro-temporal-prediction-f11-20260713-closed-v3/temporal-prediction-frontier.json` | `12f8be655933b5735ff2325125acb1507b1059b6c679735fe11d362d512e04f4` |
| Closed customer PNG transport v4 | `/Users/scammermike/.cache/cx-spec-lab/customer-path/koro-png-transport-whole-engine-v4-20260713/receipt.json` | `2f4d9e669d76bf9e738659051bb2c6d3fa7253a1ac4767767802e9df4a9d829c` (receipt self-hash `f7b9c130ca16a36df41d36a4de2331d48e0b0eeb78b4bcd0684c1ccf59ccef0b`) |
| Immutable temporal timing | `/Users/scammermike/.cache/cx-spec-lab/frontier/koro-temporal-prediction-f11-20260713-closed-v3/temporal-timing-evidence.json` | `b198b6d290ffe0adb210db950565f5f2c610769c865dfbf1499400f85dd8679f` |
| Frame-9 performance denominator | `/Users/scammermike/.cache/cx-spec-lab/cache-arm/koro-static-cache-copy-descriptor-retained-whole-engine-f9-v-f10-f11-20260713/evidence/frame-9-performance-receipt.json` | `b1ca40bc63288ad8736b76592c24a9baffe168f752c8309ac6925cc2c22f19c8` |

All five current closure validators replayed successfully against current code and
their bound files during this reporting pass.

### Code and diagnostic evidence

- Whole-project manifest and replay:
  `scripts/spec-lab/cx_render_project_bundle_v1.py` and
  `scripts/spec-lab/test_cx_render_project_bundle_v1.py`.
- Wire-only v2 contract:
  `scripts/spec-lab/cx_render_project_contract_v2.py` and
  `scripts/spec-lab/test_cx_render_project_contract_v2.py`.
- Customer policy/model:
  `scripts/spec-lab/customer_render_policy_v1.py` and
  `scripts/spec-lab/run_customer_render_path_benchmark.py` with their focused tests.
- PNG hardening and closed v4 receipt replay:
  `scripts/spec-lab/screen_customer_png_transport.py` and
  `scripts/spec-lab/test_screen_customer_png_transport.py`.
- Common engine accounting:
  `spec-engine/src/engine.rs`, `spec-engine/tests/hardening.rs`, and
  `spec-engine/examples/bench_engine_overhead.rs`.
- Default-off inference bridge and real-model gate:
  `agent/src/spec_receipt_bridge.rs` and
  `agent/src/runners.rs::candle_ngram_speculative_loop_matches_greedy_real_model`.
  The 64/256/512 timing rows are local diagnostic output, not a durable signed
  receipt.
- Synthetic token 2.376372x row:
  `docs/speed-lane-reports/spec-lab/spec_render_token_branch_ledger.jsonl`, row 84,
  branch `cx_native_token_gate_code_k4_c0p75`. The row itself says
  `not model-backed LLM throughput`.

## Verification performed

The following commands passed on the current workspace:

```text
python3 -m unittest \
  scripts/spec-lab/test_customer_render_policy_v1.py \
  scripts/spec-lab/test_run_customer_render_path_benchmark.py \
  scripts/spec-lab/test_cx_render_project_bundle_v1.py \
  scripts/spec-lab/test_cx_render_project_contract_v2.py \
  scripts/spec-lab/test_screen_customer_png_transport.py \
  scripts/spec-lab/test_screen_static_frame_cache_reuse.py \
  scripts/spec-lab/test_screen_spatial75_one_render_upper_bound.py
# 127 passed, 0 failed

(cd spec-engine && cargo test --all-targets)
# 29 passed, 0 failed

(cd spec-engine && cargo run --release --example bench_engine_overhead -- 100000 9)
# replay median 18.469083 ms; 5,414,453.982 units/s

(cd agent && cargo test --features spec-receipt-bridge spec_receipt_bridge)
# 5 passed, 0 failed

(cd agent && cargo test --features spec-receipt-bridge \
  completed_candle_stats_map_to_parked_common_receipts)
# 1 passed, 0 failed

(cd token-spec-poc && cargo test)
# 26 passed, 0 failed
```

The cache, Spatial75, one-render v4, and temporal validators were also called
directly with external-file replay enabled where supported; all four passed. The
closed PNG v4 validator then replayed its source, candidate encoding, code and
environment pins, self-hash, statistics, and wire arithmetic successfully.

Broader current-workspace regression also passed:

```text
python3 -m unittest discover -s scripts/spec-lab -p 'test_*.py'
# 795 passed, 2 skipped opt-in integration smokes

python3 -m unittest discover -s scripts -p 'test_*.py'
# 64 passed

(cd agent && cargo test)
# 275 passed, 52 ignored real-weight/live-hardware tests

(cd agent && cargo test --no-default-features --features spec-receipt-bridge)
# 260 passed, 44 ignored

CX_SPEC_TEST_MAX_TOKENS=256 CX_SPEC_TEST_REPS=5 CX_SPEC_TEST_WINDOW=32 \
  cargo test --release candle_ngram_speculative_loop_matches_greedy_real_model \
  -- --ignored --nocapture --test-threads=1
# exact greedy parity; 589.413 ms speculative median, 1108.756 ms greedy median,
# 1.883x paired-ratio median; 201/201 proposed tokens accepted

# The same command at 64 and 512 tokens also preserved exact greedy output:
# 64: 290.945 ms vs 280.137 ms, 0.963x paired median (a regression)
# 512: 822.957 ms vs 2347.964 ms, 2.848x paired median

(cd control && go test ./...)
# passed

make spec-test
# passed, including strict cross-language receipt validation
```

## Final decision

The pass establishes a strong experimental substrate and a strict whole-project
wire boundary. It also establishes where the moat is real today: exact identity,
measured speed, explicit evidence classes, fail-closed routing, and refusal to turn
an ungated result into delivery authority.

It does **not** establish a bulletproof renderer, a whole-production engine, support
for every Blender/file semantic, or simultaneous render/inference production safety.
The next milestone is not a larger isolated multiplier. It is one fully extracted,
sandboxed, final-semantics, independently verified, durably sealed, economically
reserved runtime cell that survives the soak and failure campaign above.
