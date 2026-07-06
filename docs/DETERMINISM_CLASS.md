# Determinism class: the verification-class boundary

> **Update (feat/perf-wave pass 1):** `engine_build_hash` now folds
> `hardware::infer_content_id()` — a SHA-256 of the vendored
> `agent/src/quantized_llama_batched.rs` source — as a fifth class input alongside engine,
> agent_version, device backend, and CATALOGUE_QUANT. This closes the gap where a
> kernel/forward patch could ship byte-changing code into the SAME class without an
> agent_version bump (the moat hole in docs/CANDLE_EXPANSION_RESEARCH.md L17). Any edit to that
> inference module now moves the class automatically and requires a golden-hash + honeypot reseed.

> Source-of-truth for HOW computexchange decides two workers' outputs are
> byte-comparable. Adopted from the founder's Hawking engine
> (`/Users/scammermike/Downloads/hawking`), whose own research proves token-level
> determinism is impossible across heterogeneous Apple-Silicon generations. This is
> the #1 Hawking adoption from `docs/PERF_AND_CAPABILITY_AUDIT.md` (Wave 1).

## The governing truth

The market's trust spine is redundancy + honeypots: the same chunk is run by more than
one worker (or against a known answer), and the results are compared. For some job
types that comparison is **byte-exact** (`bytes.Equal`). Byte-exact comparison is only
sound when the two workers would, given a correct implementation, emit the **same
bytes**.

That assumption is FALSE across heterogeneous hardware and engines. Hawking's research
(`docs/plans/research/i_trust_verifiability.md`) documents up to **15% accuracy
variance and a 70% best-to-worst spread** across runs at temperature=0, root-caused to
GPU floating-point non-determinism — "semantic replay, not bit-exact." Hawking's own
kernel-profile identity therefore keys on **device_name + shader_hash +
tensor_layout_hash** (`crates/hawking-core/src/profile.rs`), and its golden-hash
baselines (`crates/hawking-core/tests/golden/*.hashes`) are explicitly valid only
WITHIN one such class.

So computexchange must pin byte-exact redundancy peers and honeypots to a
**verification class**, never assume cross-Mac (or cross-engine, or cross-build)
byte-equality. Comparing across the class boundary on a byte-exact job type would
auto-quarantine an honest worker whose kernels legitimately produce different bytes.

## The class definition

A worker's verification class is the tuple:

```
(hw_class, engine, build_hash)
```

- **hw_class** — the hardware family (`apple_silicon_*`, `nvidia_*`, `cpu`). Already the
  coarse class; the redundancy matcher has always pinned peers to one hw_class
  (`control/scheduler.go` Match), because Metal and CUDA FP kernels differ.
- **engine** — the on-device inference engine (`candle` | `mlx` | `vllm` | `hawking`).
  Two engines' FP kernels differ even on identical hardware, so a future
  mlx/vllm/hawking worker must never be byte-compared against a Candle one. Advertised
  by the agent (`InferenceBackend::engine_tag`), normalized + validated at registration
  (`normalizeEngine` + `validEngines`).
- **build_hash** — the FINER axis below engine: a stable hash of the
  byte-output-determining BUILD inputs computed by the agent
  (`hardware::engine_build_hash`): `engine + agent_version + device_backend +
  catalogue_quant`. A kernel/codegen change ships in a NEW agent build, so the agent
  version stands in for Hawking's `shader_hash`. Two workers in the same hw_class +
  engine but on **different agent builds** can therefore emit different bytes — they are
  a different class.

`build_hash` is the direct transplant of Hawking's "a future kernel change that
silently shifts bytes is caught" discipline into the cross-worker market.

### Unknown build = its own class

An agent that does not advertise a `build_hash` (an older binary, or the register echo
from a server that does not round-trip the field) reports `""`. The verifier treats an
empty build hash as **unknown — never provably the same kernels**, so it is its own
non-matching class: it is never drawn as a byte-exact redundancy peer and never
auto-docked on a pure byte mismatch (`sameVerificationClass` returns false whenever
either side is `""`). This is safe-by-default: an unknown-class result falls back to
**provisional trust**, the same pattern the missing-third-worker tiebreak already uses
(`control/verification.go`).

A single-build fleet where every worker reports the SAME `(hw_class, engine,
build_hash)` collapses the class back to today's behavior — no change.

## The policy (what is enforced)

For a **byte-exact** job type (`batch_infer`, `audio_transcribe`, `custom`, … —
anything `resultsAgree` compares with `bytes.Equal`; see `byteExactJobType`):

1. **Peer selection** (`SelectRedundancyPeerExcluding`) pins a redundancy peer to the
   anchor's full `(hw_class, engine, build_hash)` (Match's `PinEngine`/`PinBuildHash`).
   So same-class is the common path by construction.
2. **Redundancy disagreement** — a 2-result byte mismatch is a real, dockable mismatch
   ONLY when the two results share a class. A cross-class byte difference is NOT a
   defect: it is surfaced as `pass_with_penalty` + a `redundancy_cross_class` receipt,
   no dock, no cross-class tiebreak re-dispatch.
3. **N-way tiebreak vote** (`resolveTiebreak`) — a losing result in a DIFFERENT class
   than the winner is NOT docked; it is credited a (provisional) match and recorded as
   `tiebreak_cross_class`.
4. **Honeypot** — a byte-exact honeypot only auto-quarantines when the known answer's
   recorded class (`honeypots.answer_class`) matches the committing worker's class. A
   class-blind (`""`) or cross-class byte honeypot SKIPS the probe and falls through to
   the normal path (reduced coverage, never a fabricated pass, never a wrongful
   quarantine). This directly discharges the audit's hazard: a Candle-seeded honeypot
   would byte-fail a correct vLLM result and auto-quarantine an honest worker.

For a **tolerant** job type (`embed` cosine, `batch_classification` top-1 label,
`json_extraction` canonical JSON, `rerank` exact order array) the comparison is over
SEMANTIC content that is robust to cross-kernel FP jitter, so it is safe across classes
and the class gate does NOT apply — these are unchanged.

The cross-class skip events (`redundancy_cross_class`, `tiebreak_cross_class`) are
recorded for operator forensics but are NOT counted into the buyer-facing "checked"
total (`JobVerification`), because an uncheckable comparison is a coverage gap, not
verified coverage (BLACKHOLE: surface the gap, never inflate coverage).

## The golden-hash gate (the within-class regression guard)

`agent/tests/golden/llama32_1b_q4k_greedy.hashes` pins the greedy token-baseline output
hash of the default `batch_infer` model. The harness
(`runners.rs::tests::golden_token_baseline_gate`, `#[ignore]` because it downloads the
GGUF and is device-class specific) RECORDS the baseline on a known-good build of the
target class and GATES against it thereafter: a future kernel/codegen change that
SILENTLY shifts bytes within a class is caught HERE — on the build that changed —
instead of as a false cross-worker auto-dock. This is the computexchange transplant of
Hawking's `tests/golden/*.hashes` concept.

Because token-level determinism is impossible across Mac generations, these hashes are
valid only within the `(device, engine, build_hash)` class that recorded them. A
mismatch on the SAME class is a real regression; a mismatch on a DIFFERENT class is
expected and must not be read as a regression.

## What is enforced now vs. documented-for-follow-up

ENFORCED in this change:

- `build_hash` advertised by the agent + persisted (`workers.build_hash`) and carried
  through `MatchWorker` / `WorkerProfile` / `CandidateWorkers` / `ChunkResult` /
  `CommitTaskInfo`.
- Redundancy peers pinned to the full `(hw_class, engine, build_hash)` class.
- Byte-exact redundancy disagreement, N-way tiebreak, and honeypot all gated on the
  class boundary — a cross-class byte mismatch is provisional trust, never an auto-dock
  or auto-quarantine.
- The golden-hash regression harness is wired (`#[ignore]`, seed-then-gate).

DOCUMENTED FOR FOLLOW-UP (Wave 2 prerequisites, per the audit):

- **hw_class-aware honeypot seeding.** PARTIALLY DONE (Week 6b, 2026-07-06): the
  `hawking` class's `batch_infer` honeypot is now seeded WITH its producing class
  (`control/seed.go`, the section below) — the first byte-exact honeypot that can
  actually fire. Still open: the `candle` class's byte-exact honeypot remains unseeded
  (its own stability question — `generate_batch`'s exact-length bucketing changes
  co-batch shape with chunk composition — needs its own harness proof before an answer
  is seedable), and the vLLM lane's seeding remains the audit's named de-risking step
  #1 before that lane's go-live. The embed honeypot stays tolerant/class-blind (safe).
- **Seed the golden baseline.** DONE for the `candle` reference class (rows recorded +
  gating, see the golden-hash section). For the `hawking` class the seed blob produced
  by the honeypot harness (below) IS the golden record — the full byte-exact result
  document, strictly stronger than a hash row — and the class-aware honeypot is the
  operative cross-worker gate. Extending `agent/tests/golden/*.hashes` to per-class
  rows (and driving the `.hashes` gate through `hawking_generate` for within-class
  drift-gating on non-honeypot prompts) is named follow-up; note that within-class
  SILENT drift is already structurally impossible for kernel edits, because any edit
  to the vendored inference module moves `build_hash` via `infer_content_id`.
- **Per-model quant in build_hash.** `CATALOGUE_QUANT` is a fixed `q4_k_m` constant
  today (every shipped GGUF is Q4_K_M). When a runtime ever varies quant per model
  (e.g. a sub-Q4 codec lane), derive it from the loaded weights so a requant lands in a
  distinct class automatically.

## Seeding a hawking-class byte-exact honeypot (operational, Week 6b)

As of 2026-07-06 the FIRST byte-exact honeypot is seeded (`control/seed.go`): a
`batch_infer` probe whose known answer was produced by the REAL hawking engine —
`HawkingRunner::run`, the exact production dispatch path, real Llama-3.2-1B-Instruct
Q4_K_M GGUF on real Metal — on the repo's reference M3 Pro, recorded under that box's
real registration class (`hawking|a0ce01606255c06e` at capture time; the seed
constants in `control/seed.go` are authoritative, not this line). This section is the
recipe — and the hard requirements — for re-generating the seed on any other
reference box or build. NEVER hand-write a byte-exact known answer, never copy one
across classes, and never edit the chunk without re-recording.

### Requirement 1 — the answer must be CO-BATCH-MEMBERSHIP-STABLE

This is the hawking-specific subtlety, and it is measured, not theoretical.
`hawking_pool_size` is operator-configurable (`1..=8`, agent config), so two honest
workers of the SAME `(hw_class, engine, build_hash)` class can decode the same
honeypot chunk under DIFFERENT slot memberships. The lane's one documented
byte-nondeterminism — a genuine argmax near-tie tipping under the multi-seq kernel's
reduction order — is membership-DEPENDENT (characterized by
`hawking_churn_neartie_flip_is_membership_dependent_not_corruption`; at free-form
scale 11/24 rows diverged between memberships, 2026-07-06 dispatch report). A
membership-unstable known answer would auto-quarantine an HONEST same-class worker
that merely runs a different pool size.

Therefore a honeypot answer is seedable ONLY if its chunk is proven byte-identical at
pool_size 1, 2, 4 AND 8 on real Metal. The harness
`runners::tests::hawking_honeypot_seed_blob_membership_stable_across_pool_sizes`
(`#[ignore]`d, metal-gated) is both that proof and the recorder. Any prompt that
flips must be REJECTED and replaced — and this is not hypothetical: the harness's
FIRST real run (2026-07-06, M3 Pro) rejected "The opposite of hot is", which is
byte-stable at pools 2 and 8 (the dispatch gate's coverage) yet flips at pool 1
(`is "cold".` 10 tok vs `is actually "cold".` 11 tok). Two-point stability is NOT
stability.

### Requirement 2 — every row must hit natural EOS strictly below max_tokens

Honeypots ride on real buyer jobs and inherit their params (`api.go` injects the
probe into the submitted job; the dispatch manifest carries the BUYER's
`max_tokens`). A row that is truncated at the recorded `max_tokens` would produce
different bytes under a job with a larger budget. The harness asserts every row's
`tokens < max_tokens` (recorded chunk: max row 10 of 24), which makes the answer
invariant for any job `max_tokens >= 24`.

### Requirement 3 — class fidelity

The blob's `build_hash` is computed by the harness through
`hardware::engine_build_hash("hawking", agent_version)` — the exact function the
registration path advertises — on the box that ran the model. Never hand-compute it,
never reuse one across boxes/builds. The seed writes `answer_class` in the
verifier's `classKey` format (`engine|build_hash`) via `classKey()` itself, and
`validateHoneypotSeed` refuses a class-blind byte-exact row outright.

### The flow

1. On the reference box (agent tree, real Metal):
   `CX_HAWKING_SEED_OUT=/tmp/hawking_seed_blob.json cargo test -p cx-agent
   --features metal --release
   hawking_honeypot_seed_blob_membership_stable_across_pool_sizes -- --ignored
   --nocapture --test-threads=1`
   The test FAILS (and nothing is seedable) if any pool size flips a byte or any row
   fails to EOS; on success it writes the seed blob
   `{engine, build_hash, recorded_max_tokens, max_row_tokens, input_jsonl, known_answer}`.
2. Dev seed: wire the blob's values into `control/seed.go`'s
   `demoHoneypotHawk*` constants (`control seed` then inserts the row idempotently
   AND uploads the input object — both are required; a DB row whose object is
   missing 404s a real worker's presigned GET forever).
   Operational seed: `Store.InsertHoneypot(ctx, "batch_infer", "honeypots/...",
   knownAnswer, classKey("hawking", buildHash))` + `Storage.PutObject` of the
   blob's exact `input_jsonl` bytes at the same ref.
3. Verify activation on a live stack: `GetHoneypotAnswer` returns the class;
   a same-class commit of the exact bytes records `honeypot_pass`; any other class
   skips (control/honeypot_hawking_regate_test.go is the pinned proof of all paths).

### Known validity bounds (documented, enforcement is named follow-up)

The honeypot injection path (`AvailableSeedHoneypots`) keys on `job_type` only, so
this probe can ride on a `batch_infer` job for a DIFFERENT model or with
`max_tokens < 24`; an honest same-class worker would then legitimately produce
different bytes and be wrongfully docked. Until the injection-time guard lands
(api.go/pricing_extra.go — outside the Week-6b bundle's file set), byte-exact
honeypot coverage is safe ONLY on fleets whose `batch_infer` traffic matches the
recorded model + `max_tokens >= 24`, which holds for the dev seed (the class exists
only on the reference box). The lane's opt-in status is unchanged.
