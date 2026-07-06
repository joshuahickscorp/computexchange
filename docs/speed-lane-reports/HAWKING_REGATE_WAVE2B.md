# Wave 2B — Hawking cross-worker determinism re-gate (class-aware honeypot + golden baseline)

2026-07-06 · reference Apple M3 Pro (Metal) · the Week-6b control-side gap CREED
entries 84/86 named: "seed `(apple_silicon, hawking, build_hash)` honeypots + the
golden-hash baseline before the lane carries byte-exact money work."

## What now gates byte-exact money work on the hawking lane (exactly)

1. **A dispatchable, class-aware, byte-exact `batch_infer` honeypot EXISTS for the
   first time.** Before this wave, `control/seed.go` deliberately seeded NO
   byte-exact honeypot at all (a blank-class row can never fire; a fake answer
   paired with a class would wrongly quarantine). Now `control seed` inserts a
   probe whose `known_answer` is a REAL `BatchInferResult` document produced by
   the REAL production dispatch path (`HawkingRunner::run` → real Llama-3.2-1B
   Q4_K_M GGUF → real Metal) on this repo's reference box, with
   `answer_class = classKey("hawking", <this box's real registration build_hash>)`
   = `hawking|a0ce01606255c06e` at capture time (the seed constants are
   authoritative). It also uploads the honeypot's input OBJECT (the exact chunk
   the answer was recorded for), so a real worker's presigned GET serves the real
   probe.
2. **The gate fires only where it is valid evidence.** Proven against the real
   Postgres + MinIO stack (control/honeypot_hawking_regate_test.go, real
   Verifier, real receipts):
   - a worker of the EXACT producing class committing the EXACT answer →
     `honeypot_pass` receipt, no dock, no quarantine;
   - the same class committing a plausible-but-WRONG answer → `honeypot_fail`,
     reputation dock (0.90 → ≤0.75), credit clawback, supplier quarantined
     (`suspended`), task requeued;
   - a candle-class worker, and a hawking worker with an UNKNOWN (`""`) build
     hash, committing DISAGREEING bytes → the probe SKIPS: zero honeypot
     receipts of either kind, no dock, no quarantine, no requeue (never a
     fabricated pass, never a wrongful quarantine);
   - the store REFUSES a class-blind byte-exact seed (`errHoneypotBlankClass`)
     and writes nothing — pinned at unit level and against the live store.
3. **The membership-stability property is machine-enforced at seed time.** The
   new agent harness (below) is the only sanctioned recorder, and it FAILS —
   making the answer unseedable — unless the chunk is byte-identical at
   pool_size 1, 2, 4 and 8 through the production path.

## The correctness subtlety that made this wave non-trivial (measured, not theoretical)

`hawking_pool_size` is operator-configurable (`1..=8`), so the SAME honeypot chunk
decodes under different co-batch memberships on different workers of the SAME
verification class — and the lane's one documented byte-nondeterminism (a genuine
argmax near-tie tipping under the multi-seq kernel's reduction order; 11/24
free-form rows in the 2026-07-06 dispatch measurement) is membership-DEPENDENT. A
membership-unstable known answer would auto-quarantine an honest same-class worker
that merely runs a different pool size.

**The harness caught exactly this on its FIRST real run.** The candidate chunk was
the dispatch gate's six prompts (proven byte-identical hawking-vs-candle and
vs-solo-serial at pools 2 and 8). At pool_size **1**, prompt six — "The opposite
of hot is" — flipped: `is "cold".` (10 tok, pools ≥2) vs `is actually "cold".`
(11 tok, pool 1). Two-point stability is not stability. The prompt was REJECTED
per the harness's own rule and replaced ("The chemical symbol for gold is" →
"…Au.", 8 tok); the final chunk is byte-stable at all four pool sizes.

## Proof artifacts (all real, this box, 2026-07-06)

Agent side (`agent/src/runners.rs`, `#[ignore]`d + metal-gated, real GGUF):

- NEW `hawking_honeypot_seed_blob_membership_stable_across_pool_sizes` — drives
  the fixed chunk end-to-end through `HawkingRunner::run` at pool_size 1/2/4/8,
  asserts byte-identity across all four AND natural EOS strictly below
  max_tokens=24 for every row (max row 10 — the max_tokens-invariance
  precondition), then emits the seed blob `{engine, build_hash,
  recorded_max_tokens, max_row_tokens, input_jsonl, known_answer}` with the
  box's REAL registration-path class identity
  (`hardware::engine_build_hash("hawking", agent_version)` — never
  hand-computed). Capture run: 42 tokens; 2416 / 1169 / 982 / 852 ms at pools
  1 / 2 / 4 / 8; byte-stable.
- Zero-regression re-run of ALL six real-Metal hawking gates in ONE process
  (`cargo test --features metal --release hawking -- --ignored
  --test-threads=1`): `hawking_real_gguf_decode_matches_serial_and_is_coherent`,
  `hawking_churn_reuses_freed_slots_and_matches_solo_serial`,
  `hawking_churn_neartie_flip_is_membership_dependent_not_corruption`, the two
  dispatch gates (end-to-end byte-identity at pools 2+8; the throughput
  measurement, which remains honestly negative for the lane), and the new
  harness: **6 passed / 0 failed in 106.4 s**.

Control side (real Postgres :55494 + real MinIO :19400, schema.sql applied,
shared integration TestMain):

- NEW `control/honeypot_hawking_regate_test.go` (integration): the
  pass / fraud / cross-class-skip / unknown-build-skip / class-blind-refusal /
  seed-fidelity+idempotency matrix described above (5 top-level tests, 2
  subtests).
- EXTENDED `control/honeypot_class_test.go` (unit, no DB):
  `TestHawkingHoneypotSeedDataConsistency` pins the seeded constants — classKey
  format, seed-guard acceptance, the comparability matrix for the seeded class,
  document shape, one-completion-per-row, input order, and every row
  `1 <= tokens < 24`.

## Suite status (before → after)

- Agent `cargo test --features metal`: 176 passed / 43 ignored → 176 passed /
  44 ignored (the +1 ignored is the new metal-gated harness). `--no-default-features`:
  170 passed / 37 ignored → unchanged. Clippy: exactly the pre-existing
  4-warning hardware.rs doc baseline on both configs, zero new warnings.
- Control unit (`go test ./...`): 130 → 131 PASS, 0 FAIL. Build/vet/gofmt clean
  for this wave's files (`seed.go`, both test files); `gofmt -l` flags only
  files owned by other in-flight bundles (`webauthn.go` pre-existing;
  `api.go` appeared mid-session from the parallel bundle).
- Control integration (`go test -tags integration ./...`, real PG :55494 + real
  MinIO :19400, fresh schema each run): **241 PASS / 0 FAIL / 1 pre-existing
  gated SKIP before → 257 PASS / 0 FAIL / 1 SKIP after.** Of the +16: +6 are
  this wave's (5 new integration tests + the new unit pin, which the tagged run
  also counts) and +10 are the PARALLEL bundle's SLA/quote tests that landed in
  the shared tree between the two runs (verified by name-set diff: 0 tests
  removed, 0 failing). Every pre-existing test is green WITH the batch_infer
  honeypot now genuinely dispatchable — i.e. activation did not regress any
  existing flow that submits batch_infer jobs. (The parallel bundle's in-flight
  edits transiently broke the shared tree's compile twice mid-session
  (collect.go, quote_test.go); waited out — no files outside this wave's set
  were touched.)

## What remains (named honestly)

1. **Injection-time param/model guard (other bundle's files).** The honeypot
   injection path (`AvailableSeedHoneypots` / `createJob`) keys on `job_type`
   only. A `batch_infer` job for a DIFFERENT model, or with `max_tokens < 24`,
   that draws this probe would byte-fail an HONEST same-class worker (the
   completion legitimately differs). Documented in DETERMINISM_CLASS.md as a
   validity bound; safe for the dev seed (the seeded class exists only on the
   reference box, and the lane is opt-in), but a production fleet must land the
   guard (api.go / pricing_extra.go) before seeding byte-exact honeypots at
   scale.
2. **Per-operator re-generation.** The seeded answer is valid ONLY for
   `hawking|a0ce01606255c06e`. Any other reference box, any agent version bump,
   any edit to the vendored inference module (it moves `infer_content_id`, hence
   `build_hash`) requires re-running the harness and re-seeding. This is by
   design (the class boundary IS the safety), and the flow is documented in
   DETERMINISM_CLASS.md.
3. **Candle-class byte-exact honeypot — still unseeded.** Its stability question
   is different (per-task `generate_batch` exact-length bucketing changes
   co-batch shape with chunk composition) and needs its own harness proof before
   an answer is seedable. Until then candle workers keep zero byte-exact
   honeypot coverage (the safe direction: skip, never wrongful).
4. **Cross-Mac class-boundary test — owner-gated.** Proving a DIFFERENT
   Apple-Silicon generation lands in a different class and is correctly NOT
   byte-compared requires a second physical Mac.
5. **Golden `.hashes` per-class extension.** The `.hashes` golden file remains
   single-class (candle reference rows). For the hawking class the seed blob IS
   the golden record (a full byte-exact document, strictly stronger than a hash
   row) and the class-aware honeypot is the operative cross-worker gate;
   extending the `.hashes` format/harness to per-class rows driven through
   `hawking_generate` is follow-up. Within-class silent kernel drift already
   moves the class automatically via `infer_content_id`.
6. **Stale doc notes in other bundles' files.** `docs/HAWKING_PORT_PLAN.md`'s
   Week-6b line and `HawkingRunner`'s doc-comment paragraph still say the
   control-side seeding is "not yet landed"; both under-claim (never
   over-claim) and belong to file sets this wave does not own. One-line updates
   are follow-up.
7. **Prefix reuse, bulk prefill, B=8 soak** — the other Week-6b items, untouched
   here, still open. No throughput claim is made or changed by this wave.
