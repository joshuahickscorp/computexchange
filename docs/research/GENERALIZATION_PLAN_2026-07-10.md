# Generalization Plan — Any Scene, Any Media, Any Silicon (2026-07-10)

Status: LIVE. Successor to `CONSOLIDATION_PLAN_2026-07-09.md`, which CLOSED with the strict-
delivery result (DECISION=GROW: global 0.9902 / worst-tile 0.9501 all frames, measured 2.45x
end-to-end on H100; quality/speed dial vs 5.56x@0.91 preview on A100). The engine is proven on
ONE scene, ONE renderer, ONE media type. This plan earns the word "generalized."

## Objectives (no fixed scope — branch, measure, prune)

1. **Any scene:** the strict-delivery recipe (anchor + aov_edge select + match-reference raw
   repair + per-frame budget) must transfer across diverse scenes WITHOUT per-scene tuning.
2. **Any silicon:** cross-architecture gates. The fleet is Apple Silicon (Metal); rented GPUs
   are CUDA. If a Mac drafts and a CUDA box verifies (or vice versa), quality claims must hold
   ACROSS architectures. Establish: (a) Metal-vs-Metal self-consistency (local, $0), then
   (b) Metal-vs-CUDA per-tile SSIM gates. Cycles is NOT expected byte-identical across device
   kernels — the question is whether cross-arch worst-tile clears the delivery gate.
3. **Any media:** wire a SECOND real modality through the owned SpecEngine — the transcode lane
   (VP9 draft->verify, 5.2x standalone in prior campaigns) as a canonical SpecReceipt adapter,
   measured locally with real ffmpeg. Render + transcode + token through ONE engine = the
   "generalized media" claim becomes concrete.
4. **Evident speed levers (owner: add absolutely):**
   - L1 TUNED REPAIR BUDGET: capstone over-repaired (32 tiles, many already >0.95). Tonight's
     receipts contain per-tile selector scores AND post-hoc SSIM — calibrate a score threshold
     OFFLINE ($0) that predicts sub-0.95 tiles; repair only those (~2-4/frame) ->
     projected ~3-3.5x AT delivery quality. Validate inside the scene sweep (no extra run).
   - L2 REPAIR REGION PACKING: frame-0 repair (5 tiles, 1 merged region) cost 72.8s vs 28s for
     2 tiles — merged-region area drives cost; smarter packing trims it.
   - L3 RESIDENT/WARM RENDERER: fixed_overhead 5.7-12.5s per subprocess launch; the fork's
     batch-manifest patches (0004/0006) amortize it. Modest at 4 frames, real at production scale.
   - L4 BLENDER VERSION PIN: finish the sm_90/sm_100 kernel research -> a newer Blender that
     ships Hopper kernels removes the H100 first-render JIT tax AND may unlock Blackwell
     (availability + speed). Research-only this wave.
5. **Cost transparency:** a RunPod quote/estimator built from OUR measured ledgers, so every
   proposed run carries a $ estimate up front — and the same basis feeds the CX product quote
   (`control/render_spec_job.go`).

## Measured cost basis (from this campaign's ledgers)

| workload | GPU | measured | est. cost |
|---|---|---|---|
| 1080p 4-frame pipeline (ref1536/draft192) | A100 SECURE $1.42/hr | ~17 min | ~$0.40 |
| 4K 4-frame strict-delivery w/ repair | H100 SECURE ~$3.02/hr | ~75-80 min | ~$3.8-4.0 |
| 4K single-frame probe (2 renders + ref) | A100/H100 | ~26-43 min | ~$0.8-1.6 |
| cross-arch CUDA half (1 frame 1080p) | A100 | ~10 min | ~$0.25-0.50 |

Balance at plan write: **$10.63**. The wave itself is LOCAL-ONLY ($0). The sequenced cloud
validations below total ~$6-9 at 1080p scale (fits) but the 4K cross-scene strict validation
(~$4/scene) needs a top-up — the estimator branch produces the exact quote table.

### Refreshed cost table (2026-07-10, Branch D — `scripts/spec-lab/runpod_cost_quote.py`)

Computed from the four campaign ledgers (`integrated_spec_render_token_ledger.jsonl`,
`cross_denoiser_probe_ledger.jsonl`, `multi_selector_probe_ledger.jsonl`,
`reference_consistency_ledger.jsonl`). Per-run $ figures are MEASURED RunPod **balance
deltas** (balance at the run's own preflight minus the first snapshot at/after its terminal
event — includes provisioning/setup/scoring/termination slack, so quotes err high, not low).
Cross-check: measured successful spend $18.48 + measured failed-attempt spend $7.09 = $25.57
vs the ledger window's total balance drop $40.12 -> $14.56 = $25.56 (consistent to a cent).

**Per-GPU $/hr basis (MEASURED-derived, extracted from balance deltas):**

| GPU | documented $/hr | extracted median $/hr (n) | quote basis |
|---|---|---|---|
| A100 SECURE | 1.42 | **1.42** (n=4: 1.38/1.42/1.42/1.49) | 1.42 — extraction exactly reproduces the documented constant |
| H100 SECURE | 3.02 | **2.92** (n=5: 2.13/2.71/2.92/2.96/2.96; the 2.13 is a short-session billing-granularity outlier) | 2.92 |

**Quote table (MEASURED-derived where labeled; MODELED rows are arithmetic on MEASURED
components, never observed as a whole):**

| workload | quote | measured range | wall (median) | GPUs | label |
|---|---|---|---|---|---|
| 1080p 4-frame pipeline / scene (ref 1536 / draft 192) | **$0.53** | $0.40-$0.66 | 18 min | A100/H100 | MEASURED (n=2; the range IS the per-GPU spread) |
| 4K 4-frame pipeline, no repair / scene (ref 4096 / draft 512) | **$2.58** | — | 104 min | A100 | MEASURED (n=1, RUN 3 the 5.561x headline) |
| 4K strict-delivery w/ repair / scene (ref 4096 / draft 512 + tile repair) | **$3.60** | $3.56-$4.24 | 77 min | H100 | MEASURED (n=3, + the DECISION=GROW RUN 7 itself MODELED $3.90 — no closing balance snapshot yet) |
| single-frame probe (draft(s) + 4K ref + scoring) | **$1.05** | $0.79-$1.60 | 35 min | A100/H100 | MEASURED (n=3: multi-selector $0.79 / ref-consistency $1.05 / cross-denoiser $1.60) |
| cross-arch CUDA half (1 frame 1080p: reference + anchor draft) | **A100 $0.34 / H100 $0.69** | — | ~14 min | A100/H100 | MODELED from MEASURED components (median session overhead 0.206 h + 88.6 s ref frame + 25.9 s anchor, medians across the A100+H100 1080p runs) |
| scene sweep @1080p, N scenes (one warm pod) | **N=3 $0.80 / N=4 $0.97 / N=6 $1.32** (cold-pod bound: N x $0.53 -> $1.59/$2.12/$3.19) | — | — | A100 basis | MODELED: `1.42 $/hr x (0.206 h + N x 0.1201 h)`; per-scene wall is classroom-MEASURED — unseen scenes vary |

**Failed-attempt overhead is real and MEASURED: $7.09 across 12 pruned/abandoned attempts =
38.4% of successful spend** (the 07-09 17:30-19:20 A100/H100/H200 capacity drought alone burned
~$4.5 in provisioning attempts that never reached a render). Sequenced cloud runs should carry
a ~40% contingency on top of the table above. This revises the original basis table upward in
spirit: the per-run numbers hold, but the campaign-level $ needs the contingency line.

Reproduce: `python3 scripts/spec-lab/runpod_cost_quote.py` (markdown) or `--json`
(full per-run attribution); tests: `python3 scripts/spec-lab/test_runpod_cost_quote.py` (17/17).
NOTE: the optional `control/render_spec_job.go` quote-basis refresh was SKIPPED per the
friction rule — that file's basis is a speedup-band prose constant welded to "delivery is
never quoted" semantics (now stale post-GROW), so updating it is a behavior change (quote
tier + tests), not a constant swap; it needs its own owned change.

## Wave branches (all LOCAL build+verify; disjoint write paths)

- **A — Scene-sweep harness + L1 calibration** (`scripts/spec-lab/run_scene_sweep.py`,
  `calibrate_repair_budget.py`, `docs/research/SCENE_SWEEP_DESIGN.md`): parameterize the full
  pipeline over a diverse scene matrix; calibrate the repair threshold from banked receipts.
  Gate: dry-runs + calibration table from real data. Kill: a scene the pipeline structurally
  cannot ingest gets documented, not forced.
- **B — Cross-architecture gates** (`scripts/spec-lab/run_cross_arch_gate.py` + helpers,
  `docs/research/CROSS_ARCH_GATE_DESIGN.md`): Metal-vs-Metal local self-consistency RUN FOR
  REAL on the M3 Pro (tiny config, $0) + the Metal-vs-CUDA gate scaffold (CUDA half fired
  later, ~$0.50). Gate thresholds defined honestly (same-arch baseline first).
- **C — Transcode adapter through the SpecEngine** (`scripts/spec-lab/cx_transcode_spec_adapter.py`
  + test, `docs/research/TRANSCODE_SPEC_ADAPTER.md`): VP9/x264 draft->verify->accept/repair as
  a canonical SpecReceipt emitter; REAL local ffmpeg measurement on real content ($0).
- **D — RunPod cost estimator/quote** (`scripts/spec-lab/runpod_cost_quote.py`): reads the
  campaign ledgers, emits the per-experiment quote table; refreshes this plan's cost basis and
  (if clean, tests green) the `render_spec_job.go` quote basis.
- **E — Blender kernel matrix research** (`docs/research/BLENDER_KERNEL_MATRIX.md`): which
  official Blender ships sm_90/sm_100 Cycles kernels; recommended pin + API-break audit vs 4.2.

## Sequenced cloud runs (AFTER the wave, one driver at a time, money-safe, cost-quoted)

1. Cross-arch CUDA half (~$0.50) -> first Metal↔CUDA gate numbers.
2. Scene sweep @1080p, 3-4 scenes (~$2-3) -> generalization + L1 threshold validation.
3. 4K strict-delivery on 1-2 unseen scenes (~$4-8, NEEDS TOP-UP) -> "generalized delivery" earned.

Rules unchanged: honest number model (no lane multiplication; measured/modeled/synthetic labels),
strict gate untouched, GPU policy ladder (A100->H100->H200, upgrade-never-downgrade), zero
unattended orphans, one pod driver at a time.
