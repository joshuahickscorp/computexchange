# Transcode Spec Adapter — the Second Real Media Modality Through the Owned SpecEngine (2026-07-10)

Status: BUILT + MEASURED LOCALLY. Branch C of `GENERALIZATION_PLAN_2026-07-10.md`.
Code: `scripts/spec-lab/cx_transcode_spec_adapter.py` (+ `test_cx_transcode_spec_adapter.py`).
Ledger: `docs/speed-lane-reports/spec-lab/transcode_spec_ledger.jsonl` (5 MEASURED/local rows).
Rust ingest proof: `spec-engine/tests/ingest_lanes.rs::lane4_transcode_adapter_ingests_natively`
(cargo test green: 16/16 across the crate).

## What this is

The speculative-transcode mechanism proven standalone in prior campaigns (segment-wise
fast-preset draft -> SSIM verify -> re-encode rejected segments; VP9 5.2x banked) is now a
canonical SpecEngine lane:

| SpecEngine stage | transcode instantiation | cost charged |
|---|---|---|
| SpecUnit | one keyframe-aligned video segment | — |
| DraftProducer | FAST-preset encode (same codec + same CRF as baseline, cheaper preset) | `draft_cost_s` (also carries segmentation + concat mux, broken out in details) |
| Verifier | per-segment SSIM vs the SOURCE segment (decoded-frame MD5 in lossless mode) | `verify_cost_s` — CHARGED (see below) |
| AcceptancePolicy | segment clears the gate (`ssim >= gate`, or byte-exact frames) | — |
| RepairPolicy | re-encode the rejected segment with the EXACT baseline recipe | `repair_cost_s` |
| SpecReceipt | the canonical `spec-engine/src/receipt.rs` wire shape, emitted NATIVELY | — |

**Baseline (the honest denominator):** ONE whole-video encode of the same source at the slow
preset — a real single-lane run of the same delivered unit. The ONLY headline is
`speedup_vs_baseline = baseline_total_time_s / total_product_time_s`; per-segment ratios are
never multiplied.

**Schema note (first alias-free lane):** unlike the render adapter (no-suffix `*_cost` keys,
read into `spec-engine` via serde aliases) and the token POC (legacy `*_s` keys), this lane
emits `draft_cost_s / verify_cost_s / repair_cost_s / total_product_time_s /
baseline_total_time_s / accepted_fraction / repaired_fraction / exact / quality_tier enum
(fail|preview|delivery) / evidence (lower-case) / baseline_source / speedup_vs_baseline /
details` directly — `serde_json::from_str::<SpecReceipt>` succeeds with ZERO aliases
exercised (proven in `ingest_lanes.rs`).

## Honest accounting rules (all enforced by `assert_canonical` + unit tests)

1. `total_product_time_s == draft_cost_s + verify_cost_s + repair_cost_s`, where draft
   carries the FULL cheap path (segmentation + drafts + concat). Nothing hides outside the
   charged ratio.
2. **Verify is charged here** (unlike the render lane): the source IS the pipeline input, so
   the SSIM/MD5 gate is a real product step, not a measurement-only reference comparison.
3. **SSIM-gated is NOT lossless.** `exact=false` by construction in ssim mode, even at
   `quality_tier=delivery`; the delivered file is byte-compared vs the baseline file
   (`details.bitexact_vs_baseline`, expected false) so the tier is never mistaken for
   losslessness. `exact=true` requires the lossless mode's decoded-frame MD5 chain
   (per-segment vs source, plus the final delivered concat vs the whole source).
4. **Bitrate is part of the price.** Same-CRF fast presets pay in file size:
   `details.delivered_bytes` vs `details.baseline_bytes` rides in every receipt (the 2.52x
   x264 win below ships a 2.8x larger file — the receipt says so).
5. **Reference self-consistency, applied on day one** (the render lane's hard-won 0.914→1.0
   lesson): `details.gate_achievable_by_baseline` records whether the baseline recipe itself
   clears the gate. When it does not, rejecting a draft buys a repair that can score WORSE
   than the draft, and the run is structurally <=1x — it self-prunes honestly instead of
   arguing with the gate.
6. Repaired segments use the EXACT baseline recipe, so delivered quality is gate-or-baseline
   everywhere (mirrors render's repaired-to-reference discipline -> `delivery` tier). A
   repair-disabled run that ships a below-gate draft is `preview`, never `delivery`.
7. Post-delivery audits (baseline/delivered/repaired SSIM, byte checks) are measurement-only,
   never charged.
8. Segments are encoded SEQUENTIALLY — the ratio contains no parallel-segment credit.
   Segment-parallel fan-out is a real, unexercised lever (noted in every receipt).

## MEASURED results (local, Apple Silicon CPU encode, ffmpeg 8.1.1, 2026-07-10)

Content is synthesized locally (no external asset): testsrc2 + mandelbrot halves, 1280x720@30,
12 s, optional temporal noise, stored as a lossless x264 qp0 mezzanine with keyframes forced
on 2 s segment boundaries (synthesis charged to NEITHER lane). 6 segments per run. Every row
below is MEASURED/local end-to-end wall-clock and ledgered.

| # | codec (slow vs fast) | mode | noise | gate | gate achievable by baseline? | accepted | repaired | speedup | exact | tier |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | x264 veryslow vs ultrafast, CRF 23 | ssim | 8 | 0.95 | **NO** (baseline 0.790) | 0/6 | 6/6 | **0.589x — honest negative, self-pruned** | false | delivery* |
| 2 | x264 veryslow vs ultrafast, CRF 23 | ssim | 0 | 0.95 | yes (0.9927) | 6/6 | 0/6 | **2.517x** | false | delivery |
| 3 | x264 veryslow vs ultrafast, CRF 23 | ssim | 0 | 0.99 | yes (0.9927) | 4/6 | 2/6 | **1.038x** | false | delivery |
| 4 | VP9 good/cpu-1 vs realtime/cpu-8, CRF 32 | ssim | 0 | 0.95 | yes (0.9945) | 6/6 | 0/6 | **3.448x** | false | delivery |
| 5 | x264 qp0 medium vs ultrafast (lossless) | frame-MD5 | 0 | byte-exact | n/a | 6/6 | 0/6 | **0.502x — honest negative** | **true (proven)** | delivery |

\* Row 1's `delivery` tier is technically earned (every segment repaired to the baseline
recipe = contract quality), but the run is economically dead and the receipt carries
`gate_achievable_by_baseline: false` — on noise-heavy content the 0.95 SSIM gate is
unreachable by the SLOW recipe itself (0.790), so speculation cannot help; only a different
codec contract can.

What the five rows establish:

- **The mechanism wins when the gate is real and content is compressible** (rows 2–4;
  VP9 3.448x is the local headline, consistent in kind with the 5.2x banked on GPU-class
  content in prior campaigns — different content/hardware, so the numbers are NOT equated).
- **The accept/repair machinery works measured, not just unit-tested** (row 3: 2 mandelbrot
  segments genuinely rejected at the 0.99 premium gate and re-encoded at the baseline
  recipe; the win compresses to 1.038x — repair is expensive, exactly as the render lane
  found).
- **Failure modes self-report** (row 1: gate unachievable -> structural <1x, labeled, not
  massaged; row 5: lossless verify overhead > encode savings at qp0 where the baseline
  encoder is already fast — byte-exactness is PROVEN but there is no wall-clock win at this
  scale).
- **A curiosity worth recording:** at the same CRF on noisy content, ultrafast drafts score
  HIGHER SSIM than the veryslow baseline (0.83 vs 0.78, row 1) by spending ~3x the bitrate.
  CRF is not preset-invariant; the gate must always be read together with the bitrate line.

## Limits / follow-ups (sequenced, not this wave)

- Numbers are local CPU (M3 Pro) on synthetic content; server-class numbers (and the honest
  comparison against the banked VP9 5.2x) need a sequenced cloud run — LOCAL-ONLY rule held,
  no pod was touched.
- Segment-parallel fan-out and smarter verify (downscaled SSIM prepass; the full-res SSIM
  costs nearly as much as the draft encode at 720p) are the two evident levers.
- The receipt's `per_segment` details are sized for short clips; long-form content should
  summarize (top-k offenders + histogram) before ledgering.
- Go-side ingest (`control/`) is unchanged this wave; the Rust ingest test is the
  composition proof. Wiring transcode receipts into the control plane's staged table is a
  follow-up.
