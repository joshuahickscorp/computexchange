# Scene-Sweep Harness + L1 Repair-Budget Calibration (Branch A, 2026-07-10)

Serves `GENERALIZATION_PLAN_2026-07-10.md` objectives 1 (any scene) and 4/L1 (tuned
repair budget). Built LOCAL-ONLY this wave: zero pods provisioned, zero RunPod API
calls; the fire command below is sequenced by the orchestrator afterward.

Deliverables (all verified — 25/25 unit tests, `py_compile` clean, dry-run clean):

- `scripts/spec-lab/run_scene_sweep.py` — money-safe one-pod sequential scene-matrix
  driver for the FULL proven strict-delivery pipeline.
- `scripts/spec-lab/calibrate_repair_budget.py` — offline ($0) L1 calibration from
  the banked GROW capstone receipt.
- `scripts/spec-lab/test_scene_sweep.py` — unit tests for both (config build, quote
  math, selection semantics vs the real runner, projection math, real-ledger check).

## 1. What the sweep runs

The exact recipe that earned DECISION=GROW on classroom (integrated ledger
2026-07-10T09:31, `CONSOLIDATION_PLAN_2026-07-09.md` RESULT): per-frame anchor
(kf=1, zero reprojection, `modeled=false`) with adaptive+OIDN+guides+light-tree,
`aov_edge` selector, match-reference RAW repair (`repair_denoiser=none`,
`repair_spp=ref_spp`, light-tree left at scene default via `CX_MATCH_REF`), and the
per-frame repair budget — dispatched through `pod/exp_render_stack.py` exactly like
`run_integrated_production_benchmark.py` (whose `parse_gpu_plan` Blackwell guard,
`remote_stack` detached-SSH path, pod image and disk constants it imports rather
than re-implements).

Sweep defaults (plan-specified): 1920x1080, 4 frames, ref 1536 / draft 192, kf=1,
repair ON (`top_k=32`, `max_per_frame=8`, `min_divergence=0.0`). One provision, N
scenes sequentially on the same pod, teardown — provisioning + Blender bootstrap
amortized. **No per-scene tuning**: unit test `test_no_per_scene_tuning` proves
every scene's config differs ONLY in the scene argument (objective 1 is a transfer
claim; a tuned-per-scene pass would not earn it).

Receipts: one `RenderSpecReceipt` per scene (same shape as the capstone ledger) to
`docs/speed-lane-reports/spec-lab/scene_sweep_ledger.jsonl` (events:
`scene_sweep_preflight`, `scene_sweep_receipt`, `scene_sweep_scene_pruned`,
`scene_sweep_summary`). A failing scene is a REAL NEGATIVE: ledgered as pruned, the
sweep continues (`--fail-fast` to abort instead). The capstone report file is never
touched.

## 2. Scene matrix (only scenes PROVEN to resolve)

`resolve_scene()` in `pod/exp_render_stack.py` accepts exactly: native keys
`classroom` / `bmw27` (same `SCENE_SOURCES` table as `exp_cycles_render_prod.py`,
shared cache), a direct `.blend`/`.zip` URL, or `junkshop` which it silently
REWRITES to classroom (so junkshop is banned from the matrix).

| key | scene_arg | family | evidence |
|---|---|---|---|
| classroom | native | interior many-light GI | MEASURED — the proven control (1080p kf=1 receipt 2026-07-09; 4K GROW capstone 2026-07-10) |
| bmw27 | native | studio glossy/specular | MEASURED — rendered end-to-end 2026-07-07 on a live L40S via the sibling runner (cross_scene_ledger: bmw27-contender 3.0995x @ worst-tile 0.9783) |
| pavilion | `https://download.blender.org/demo/test/pabellon_barcelona_v1.scene_.zip` | archviz sun/exterior | LOCAL 2026-07-10 — URL HTTP 200 (24,661,092 bytes); zip holds exactly one .blend (`3d/pavillon_barcelone_v1.2.blend`); local Blender 4.2.1 opened it (engine CYCLES, active camera present, 102 objects) and completed a real 64x64@1spp Cycles render (0.6s). Not yet rendered on a CUDA pod — that is what the sweep measures |

Excluded, with the measured reason (checked 2026-07-10):

- `junkshop` — no stable direct URL; the runner itself falls back to classroom, which
  would silently duplicate the control scene.
- `fishy_cat`, `koro`, `barbershop_interior` — HTTP 404 on
  `download.blender.org/demo/test/` (curl-verified).

## 3. Money-safety (unchanged posture, verified by inspection + dry-run)

`register_cleanup()` before provisioning; refuses to start when tracked/live pods
exist (one pod driver at a time); balance must clear BOTH the `--min-balance` floor
(default $6) AND 2.5x the printed quote; monotonic GPU ladder
A100→H100→H200 with the imported Blackwell rejection; `arm_remote_watchdog()` is the
FIRST post-provision action, TTL sized to the sweep
(`1800 + N*per_scene_timeout + 1800`; 10,800s for 3 scenes at the 60-min default);
per-scene render runs DETACHED on the pod (`runpod.ssh_detached`, 20s polls);
`finally:` `terminate_all_tracked()` + hard failure if `.tracked_pods.json` is not
empty. `--dry-run` prints quote+manifest and provably touches nothing
(`test_manifest_build_is_pure`; verified after a real dry-run: tracking file `[]`,
no ledger created).

## 4. Cost quote (printed up front, per scene)

MEASURED anchors (named ledger rows in `COST_BASIS`): A100 1080p ref 105.65 s/frame
@1536spp, anchor 27.14 s/frame @192spp, fixed overhead 4.51s (integrated ledger
2026-07-09T17:30); H100 4K scoring 8.69 s/frame + composite 26.54 s/frame (GROW
receipt), pixel-scaled. The combination arithmetic is MODELED and labeled. Real
dry-run output (default 3-scene sweep):

| scene | quote | minutes |
|---|---|---|
| classroom (incl. one-time pod setup) | $0.54 | 22.8 |
| bmw27 | $0.37 | 15.8 |
| pavilion | $0.37 | 15.8 |
| **total (A100 SECURE $1.42/hr)** | **$1.28** | **54.5** |

Fits the plan's ~$2–3 envelope for cloud run #2 with margin; the driver refuses to
fire unless balance ≥ 2.5×quote ($3.20) and > $6.00.

## 5. L1 calibration — REAL results from the banked GROW receipt ($0)

`calibrate_repair_budget.py` reads the capstone ledger (and later
`scene_sweep_ledger.jsonl` — same receipt shape). Found 3 aov_edge repair receipts;
only the GROW capstone has COMPLETE label coverage (post-repair worst tile ≥0.95 on
every frame ⇒ every unselected tile was provably ≥0.95, so the 32 selected tiles
contain ALL sub-gate tiles — no labeling blind spot). The two top-12 runs are used
as stability checks only: across three independent H100 runs the aov_edge scores of
the 12 shared tiles are IDENTICAL to 6 decimals (max |Δ| = 0.0) — the selector is
deterministic for fixed scene/seed/config.

MEASURED inputs (GROW, classroom 4K): T_ref 2771.0s, T_stack 1130.4s (2.451x),
32 tiles repaired, only **8** below the 0.95 gate; per-tile repair render 14.84s,
per-tile composite 3.32s, selection 34.8s. Selection semantics are cross-checked
against the real `select_repair_tiles` in the pod runner
(`test_select_matches_runner_semantics`).

Score-floor table (`repair_min_divergence`; strict `score > floor`, matching the
runner; projections MODELED from the measured per-tile costs — "linear" scales
render+composite per tile, "conservative" charges the composite pass in full per
touched frame):

| floor | tiles | recall | over-repair | linear x | conservative x |
|---|---|---|---|---|---|
| 0.0 (= the GROW run) | 32 | 1.00 | 24 | 2.451 | 2.451 |
| 0.088913 | 20 | 1.00 | 12 | 3.037 | 2.910 |
| **0.092884 (max recall-1.0)** | **18** | **1.00** | **10** | **3.163** | **3.003** |
| 0.093437 | 17 | 0.88 ✗ | 10 | 3.230 | 3.053 |
| oracle (exactly the 8 sub-gate tiles; needs the reference — ceiling only) | 8 | 1.00 | 0 | 3.989 | 3.579 |

Per-frame rank-cap table (`repair_max_per_frame`): K=3 (12 tiles) misses 2 needed
tiles (recall 0.75); **K=4 (16 tiles) is the smallest recall-1.0 cap** — needed
tiles sit at per-frame aov_edge ranks 1 and 3–4 in every frame, i.e. the plan's
"~2–4/frame" holds measured.

**Recommendation (combined policy, what the ledgered JSON emits):**

```
repair_min_divergence = 0.092884   repair_max_per_frame = 4   repair_top_k = 16
-> 16 tiles, recall 1.00, over-repair 8
-> projected 3.10x (conservative) – 3.30x (linear) AT delivery quality  [MODELED]
   vs 2.451x MEASURED un-tuned; oracle ceiling 3.58–3.99x
```

Honest margins, stated plainly: the recall-1.0 floor sits in a razor-thin score gap
(±0.0003 to the nearest needed/unneeded tile) and is derived from ONE scene at ONE
resolution; score-scale transfer is UNVALIDATED. Therefore the sweep's DEFAULT stays
`repair_min_divergence=0.0` (the proven rank-only recipe): every scene then banks the
full 32-tile (score, ssim_pre, ssim_after) table, and re-running the calibrator on
`scene_sweep_ledger.jsonl` validates (or kills) the threshold cross-scene with zero
extra runs — exactly the plan's "validate inside the scene sweep". Apply the floor
via `--repair-min-divergence 0.092884` only after that validation.

## 6. Verification record (this wave, all local)

- `python3 -m py_compile` on all three files: clean.
- `python3 scripts/spec-lab/test_scene_sweep.py`: **25/25 OK** (includes the
  real-ledger integration tests and the runner-semantics cross-check).
- Neighboring suites still green: `test_runpod_safety.py` 9/9,
  `test_cx_integrated_speculation.py` 4/4.
- `run_scene_sweep.py --dry-run`: quote table above; `.tracked_pods.json` stayed
  `[]`; no sweep ledger created.
- `calibrate_repair_budget.py` on the real ledger: tables above (real output).

## 7. Fire command (for the orchestrator, AFTER the wave — one driver at a time)

```bash
# quote + manifest only ($0):
python3 scripts/spec-lab/run_scene_sweep.py --dry-run

# the real sweep (~$1.3–2 quoted, 3 scenes, one pod, ~55–80 min):
python3 scripts/spec-lab/run_scene_sweep.py

# afterwards, validate the L1 threshold cross-scene ($0):
python3 scripts/spec-lab/calibrate_repair_budget.py \
    --ledger docs/speed-lane-reports/spec-lab/scene_sweep_ledger.jsonl \
    --ledger docs/speed-lane-reports/spec-lab/integrated_spec_render_token_ledger.jsonl
```

Kill rules: a scene that structurally cannot ingest (no camera, unzip failure,
kernel timeout) is ledgered `scene_sweep_scene_pruned` and documented — never
forced. The strict delivery gate (global ≥0.98, worst-tile ≥0.95) is decided by the
same `RenderVerifier.decide` as the capstone and is never loosened; the calibrated
budget only removes repairs the receipt PROVES were above the gate.
