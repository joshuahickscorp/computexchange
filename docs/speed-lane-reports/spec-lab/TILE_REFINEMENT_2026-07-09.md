# Tile Refinement - 2026-07-09

## Summary

- Ledger: `docs/speed-lane-reports/spec-lab/tile_refinement_ledger.jsonl`
- Runtime: `.artifacts/cycles/runtime/cx-cycles-ada-sm89-batch-crop-runtime-20260709.tar.gz`
- Runtime SHA256: `79b5bfb41790a3f24618d48b8c8e10b81fc0b1340032421fef977dcef2c24728`
- Patch path: `--cx-batch-manifest` with optional per-row crop fields.
- Best valid tile-refine row: `3.4897x`, preview tier, `scene_cube_volume.xml`.
- Current hard-scene row to beat: raw `32 spp` cube volume, delivery tier, `7.8721x`.

## Best Valid Row

```json
{
  "scene": "scene_cube_volume.xml",
  "device": "CUDA",
  "pod_gpu": "NVIDIA L40",
  "crop_mode": "cx_batch_crop_manifest",
  "ref_samples": 4096,
  "draft_samples": 16,
  "refine_samples": 32,
  "grid": 8,
  "max_refine_tiles": 4,
  "ref_time_s": 7.1057,
  "draft_time_s": 1.0251,
  "crop_product_time_s": 1.011,
  "product_total_time_s": 2.0362,
  "product_speedup_vs_ref": 3.4897,
  "draft": {
    "quality": 0.991824361,
    "worst_tile_ssim": 0.931548839,
    "tier": "preview"
  },
  "final": {
    "quality": 0.99388874,
    "worst_tile_ssim": 0.946571643,
    "p5_tile_ssim": 0.965619041,
    "tier": "preview"
  },
  "selected_tile_count": 4,
  "failed_tile_count": 6,
  "refined_tile_fraction": 0.0625,
  "crop_vs_ref_tile_ssim_range": "0.965077219..0.96910515"
}
```

## What Improved

The first real crop implementation proved correctness but paid one Cycles process per tile:

- Per-crop CLI path: `1.617x`, final worst tile `0.946571643`.
- Batch-crop manifest path: `3.4897x`, same final quality.
- Crop product time dropped from `3.4463s` to `1.011s`.

The new `0006` patch keeps the old four-column batch manifest valid and adds:

```text
OUTPUT SAMPLES OFFSET LENGTH CROP_X CROP_Y CROP_W CROP_H FULL_W FULL_H
```

Build smoke proved:

- `CX_CYCLES_BATCH_CROP_SMOKE_OK=1`
- batch crop output: `64 x 64`
- legacy full-frame batch rows still render.

## Failed Follow-Up

A 6-tile delivery check was attempted:

- `max_refine_tiles=6`
- `transfer_preflight_mb=1`
- `min_transfer_mbps=0.15`
- preflight: `0.2244 MiB/s`
- full runtime upload failed after `1061.3s`
- pod was manually terminated and then verified gone.

No render row was produced. This is a logistics failure, not renderer evidence. The run shows that
`1 MiB` preflight is too weak for the `191M` runtime tar; future paid runs should use the `4 MiB`
preflight and a higher minimum transfer floor, or avoid tar upload with a warm image/volume.

## Decision

Keep the batch-crop manifest scaffold. Cut this tile-refinement configuration as a hard-scene
frontier branch for now: it improves cube-volume preview quality, but it does not beat raw `32 spp`
delivery at `7.8721x`.

The next honest branches are:

- warm image/persistent runtime to remove upload drag;
- resident worker protocol if draft scoring and crop refinement must stay in one process;
- trained/predicted crop selection only if it beats raw delivery on representative scenes;
- speculative render ladder now importing actual `tile_refine` receipts instead of modeling them.
