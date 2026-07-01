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

- **hw_class-aware honeypot seeding.** The `honeypots.answer_class` column exists and is
  honored, but the seed honeypots (`control/seed.go`) are still class-blind (`""`). A
  byte-exact honeypot therefore does NOT auto-quarantine today (the safe behavior) until
  answers are seeded WITH their producing class. This is the audit's named de-risking
  step #1 before the vLLM lane go-live.
- **Seed the golden baseline.** The `.hashes` file ships UNSEEDED; run the harness with
  `CX_GOLDEN_RECORD=1` on the reference box of each shipped `(device, engine,
  build_hash)` class and pin the recorded rows.
- **Per-model quant in build_hash.** `CATALOGUE_QUANT` is a fixed `q4_k_m` constant
  today (every shipped GGUF is Q4_K_M). When a runtime ever varies quant per model
  (e.g. a sub-Q4 codec lane), derive it from the loaded weights so a requant lands in a
  distinct class automatically.
