# CX Cycles-Killer Overnight Report - 2026-07-09

## Executive Result

This pass does not claim that CX has replaced all of Cycles. It does prove a sharper platform
story:

- Autoprobe detects Apple Silicon, local CUDA, runtime tars, and RunPod readiness.
- Apple Silicon developer lane passed on this host with Rust renderer tests and Metal wgpu smoke.
- Live CUDA no-build proof ran on RunPod L40S from the Ada `sm_89` runtime tar.
- OIDN was validated as an external denoise lane with denoise time included.
- Speculative render gating was rebuilt around `draft -> verify -> gate -> refine/escalate`.
- Post-run money safety was independently verified.

Best new live OIDN result:

- `scene_monkey.xml`, `1024 spp` reference, L40S CUDA:
  - raw `4 spp`: global SSIM `0.994304903`, worst tile `0.702008074`, tier `fail`;
  - OIDN `4 spp`: global SSIM `0.999513701`, worst tile `0.991403939`, tier `delivery`;
  - OIDN total time `0.725737s` vs reference `1.76s`;
  - measured speedup including denoise: `2.4251x`.

Best current raw renderer-only result:

- `scene_world_volume.xml`, raw `2 spp` vs `4096 spp`;
- global SSIM `1.0`;
- worst-tile SSIM `1.0`;
- p5 tile SSIM `1.0`;
- measured speedup `14.3372x`.

The previous `7.4286x` row is superseded as the friendly-scene ceiling. The hard-scene boundary
from the latest live run is still below general `10x`: `7.8721x` cube volume, `6.88x` monkey,
`4.5932x` sphere bump, and `3.8656x` CX glass.

## Receipts

- `docs/speed-lane-reports/spec-lab/CX_RENDER_AUTOPROBE_2026-07-09.md`
- `docs/speed-lane-reports/spec-lab/SPECULATIVE_RENDER_LADDER_2026-07-09.md`
- `docs/speed-lane-reports/spec-lab/RENDERER_5X_PUSH_AND_3AM_RESTART_2026-07-09.md`
- `docs/speed-lane-reports/spec-lab/RENDERER_FRONTIER_10X_AND_DIMINISHING_RETURNS_2026-07-09.md`
- `docs/speed-lane-reports/spec-lab/EMPIRICAL_RENDERER_10X_PATH_2026-07-09.md`
- `docs/speed-lane-reports/spec-lab/RENDER_POLICY_TRAINING_2026-07-09.md`
- `docs/speed-lane-reports/spec-lab/cx_render_autoprobe_ledger.jsonl`
- `docs/speed-lane-reports/spec-lab/speculative_render_ladder_ledger.jsonl`
- `docs/speed-lane-reports/spec-lab/cycles_quality_ladder_ledger.jsonl`

## Verification

Cheap script verification:

```bash
python3 -m py_compile \
  scripts/spec-lab/cx_render_autoprobe.py \
  scripts/spec-lab/run_speculative_render_ladder.py \
  scripts/spec-lab/test_cx_render_autoprobe.py \
  scripts/spec-lab/test_speculative_render_ladder.py
```

Result: pass.

```bash
python3 -m unittest \
  scripts/spec-lab/test_cx_render_autoprobe.py \
  scripts/spec-lab/test_speculative_render_ladder.py
```

Result: `5` tests passed.

Apple Silicon autoprobe:

- Host: `Darwin arm64`, `Apple M3 Pro`.
- `renderer_cargo_test_release`: pass.
- `renderer_decoupled_micro`: pass.
- `renderer_wgpu_smoke`: pass, Metal backend, adapter `Apple M3 Pro`, max abs err `0.000e0`.

## Platform Scaffold Checklist

| Component | Status | Receipt |
|---|---|---|
| Autoprobe | scaffolded and run | `CX_RENDER_AUTOPROBE_2026-07-09.md` |
| Runtime root resolver | scaffolded via tar inventory and tier selection | `cx_render_autoprobe_ledger.jsonl` |
| Scene catalog | scaffolded through quality/speculative rows and synthetic scene tags | `cycles_quality_ladder_ledger.jsonl` |
| Quality ladder | existing runner reused and live-run extended | `cycles_quality_ladder_ledger.jsonl` |
| Denoise lane | live OIDN CPU rows validated | `cycles_quality_ladder_ledger.jsonl` |
| Speculative gate | scaffolded and run | `SPECULATIVE_RENDER_LADDER_2026-07-09.md` |
| Cost/SLA model | upload/extract/render/denoise timings recorded | `cycles_quality_ladder_ledger.jsonl` |

## Boundaries

- Cycles remains an upstream reference/fork boundary.
- No upstream license or authorship claim was changed.
- Hawking remains a thin capability/receipt boundary only.
- Do not repeat one-GPU subprocess or manifest-worker fanout as a speed claim.
- Do not let global SSIM hide worst-tile failure.

## Next-Step Prompt

```text
/goal Read and execute `/Users/scammermike/Downloads/computexchange/docs/research/RENDERER_FRONTIER_ITERATIVE_GOAL_PROMPT_2026-07-09.md`. Start from the measured `14.3372x` friendly-scene ceiling and the harder-scene ceilings (`7.8721x` cube volume, `6.88x` monkey, `4.5932x` sphere bump, `3.8656x` CX glass). Keep cloud safety exact. Push renderer-only branches first until the next gain is no longer honest or budget-safe: actual tile/crop refinement, denoise/adaptive sampling, trained routing, representative scenes, and warm runtime logistics. Do not stop because one new best row appears. Then stack speculative render/decode orchestration on top of the strongest renderer base, separating measured end-to-end multipliers from staged/modelled ones.
```

<!-- cx-render-autoprobe-refresh -->

## Latest Autoprobe Refresh

- Autoprobe report: `docs/speed-lane-reports/spec-lab/CX_RENDER_AUTOPROBE_2026-07-09.md`
- Selected lane: `apple_silicon`
- Status: `passed_with_skips`
- Live pods: `[]`
- Tracked pods: `[]`
- Balance: `{'clientBalance': 43.2230308581, 'currentSpendPerHr': 0.01}`

## Speculative Render Ladder

- Report: `docs/speed-lane-reports/spec-lab/SPECULATIVE_RENDER_LADDER_2026-07-09.md`
- Ledger: `docs/speed-lane-reports/spec-lab/speculative_render_ladder_ledger.jsonl`
- Gate rows: `187`
- Delivery rows: `141`
- Best delivery speedup: `14.3372`

Boundary: these are imported measured Cycles quality-ladder receipts unless a live run is explicitly added.

## Next-Step Prompt

```text
/goal Continue `/Users/scammermike/Downloads/computexchange/docs/research/CYCLES_KILLER_SPECULATIVE_OVERNIGHT_GOAL_2026-07-09.md`: run a live CUDA quality/speculative ladder when RunPod credentials are present, validate OIDN with background remote logging, add a representative CX scene, preserve license attribution, keep cloud teardown receipts, and do not claim Cycles-killer status without measured global+worst-tile quality and wall-clock receipts.
```
