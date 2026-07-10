# Renderer Frontier Iteration - 2026-07-09

## Starting Frontier

- Friendly floor to beat: `14.3372x`, `scene_world_volume.xml`, raw `2 spp`, delivery.
- Hard-scene current bests:
  - `scene_cube_volume.xml`: `7.8721x`, raw `32 spp`, delivery.
  - `scene_monkey.xml`: `6.88x`, raw `32 spp`, delivery.
  - `scene_sphere_bump.xml`: `4.5932x`, raw `16 spp`, delivery.
  - `cx_many_glass.xml`: `3.8656x`, raw `8 spp`, delivery.

## Implemented

- Added `patches/cycles/0005-standalone-cx-crop-cli.patch`.
  - New CLI: `--cx-crop X,Y,W,H,FULL_W,FULL_H`.
  - Preserves full-frame camera dimensions while rendering a smaller buffer region.
- Added `patches/cycles/0006-standalone-cx-batch-crop-manifest.patch`.
  - Extends `--cx-batch-manifest` rows with optional crop fields.
  - Keeps one warm standalone Cycles process alive for multiple crop jobs.
- Updated `scripts/spec-lab/cycles_fork.py` smoke gates.
  - Proves legacy batch rows still render.
  - Proves batched crop output is `64 x 64`.
- Updated `scripts/spec-lab/run_tile_refinement_ladder.py`.
  - Uses batch crop manifests for selected tiles.
  - Scores actual merged output as a real receipt.
- Updated `scripts/spec-lab/run_speculative_render_ladder.py`.
  - Imports actual tile-refinement receipts as `tile_refine` gates.
- Updated `scripts/spec-lab/train_render_policy.py`.
  - Includes measured `tile_refine` rows in the routing policy dataset.

## Runtime Receipt

- Runtime tar: `.artifacts/cycles/runtime/cx-cycles-ada-sm89-batch-crop-runtime-20260709.tar.gz`
- SHA256: `79b5bfb41790a3f24618d48b8c8e10b81fc0b1340032421fef977dcef2c24728`
- Build pod: secure `NVIDIA L40S`.
- Build time: `342.1s`.
- Patch stack: `0001` through `0006`, all applied cleanly.
- Smoke:
  - `CX_CYCLES_BINARY_SMOKE_OK=1`
  - `CX_CYCLES_BATCH_CROP_SMOKE_OK=1`
  - `CX_CYCLES_PATCH_CLI_SMOKE_OK=1`
- Fanout sanity: `scene_monkey.xml`, `64 spp`, exact diff pass, SSIM `0.999999999`.

## Tile Refinement Result

Best valid row:

- Scene: `scene_cube_volume.xml`
- Draft: raw `16 spp`
- Refine: `32 spp` crops
- Selected tiles: `4 / 64`
- Draft worst tile: `0.931548839`
- Final worst tile: `0.946571643`
- Final global SSIM: `0.99388874`
- Tier: `preview`
- Product speedup: `3.4897x`

This beat the per-crop prototype (`1.617x`) but did not beat the hard-scene raw delivery policy
(`7.8721x`). Tile refinement is now a correct scaffold, not the current frontier winner.

## Speculative Layer

The speculative render ladder now imports both:

- quality rows from `cycles_quality_ladder_ledger.jsonl`;
- actual tile-refinement rows from `tile_refinement_ledger.jsonl`.

Latest speculative ladder:

- Gate rows: `187`
- Actual tile-refinement rows: `4`
- Delivery rows: `141`
- Best delivery remains `14.3372x`, raw `scene_world_volume.xml`.

Latest policy training:

- Rows trained on: `187`
- Global best delivery: `14.3372x`
- Hard-scene best delivery: `7.8721x`
- `tile_refine` rows are included but do not displace raw hard-scene policies.

## Cloud Safety

- Final tracked pods: `[]`
- Final listed pods: `[]`
- Final balance observed: `$41.335122758`
- API `currentSpendPerHr` still showed `$0.843` after teardown despite zero pods; treat as provider lag and recheck before any future paid run.

One failed 6-tile follow-up was terminated after upload stalled:

- Preflight: `1 MiB`, `0.2244 MiB/s`.
- Upload failed after `1061.3s`.
- Pod was manually terminated; runner recorded failure and teardown.

Future paid runs should not use the permissive `1 MiB / 0.15 MiB/s` transfer gate for full runtime
tar uploads.

## Decision

Cut this tile-refinement configuration as the hard-scene frontier branch. Keep the implementation
because it is the right substrate for speculation, but do not claim it as a speed win until a warm
image/resident protocol removes the remaining fixed costs and beats raw delivery.

Next frontier branches:

- warm image or persistent runtime volume;
- resident worker protocol for draft-score-crop without process boundaries;
- representative scene expansion beyond Blender examples;
- trained/predicted crop selection only if measured against raw delivery;
- token/spec-decode runner only as a separate measured multiplier.

## Continuation Goal Prompt

```text
/goal Continue the renderer frontier from:

- docs/research/RENDERER_FRONTIER_ITERATIVE_GOAL_PROMPT_2026-07-09.md
- docs/speed-lane-reports/spec-lab/RENDERER_FRONTIER_ITERATION_2026-07-09.md
- docs/speed-lane-reports/spec-lab/TILE_REFINEMENT_2026-07-09.md
- docs/speed-lane-reports/spec-lab/SPECULATIVE_RENDER_LADDER_2026-07-09.md
- docs/speed-lane-reports/spec-lab/RENDER_POLICY_TRAINING_2026-07-09.md

Do not repeat the per-crop or batch-crop tile branch unless the runtime is warm or resident. The
current measured frontier remains 14.3372x friendly and 7.8721x hard-scene delivery. Push the next
renderer-only branch that can plausibly beat hard-scene raw delivery, then stack the speculative
layer with measured receipts only. Before any RunPod work, verify tracked pods, live pods, balance,
and currentSpendPerHr. Use a 4 MiB transfer preflight and a stricter floor for any full runtime tar
upload, or use a warm image/volume instead.
```
