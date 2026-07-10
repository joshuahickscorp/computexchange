# Render Policy Training - 2026-07-09

## Summary

- Source ledger: `docs/speed-lane-reports/spec-lab/cycles_quality_ladder_ledger.jsonl`
- Tile refinement ledger: `docs/speed-lane-reports/spec-lab/tile_refinement_ledger.jsonl`
- Policy JSON: `docs/speed-lane-reports/spec-lab/render_policy_2026-07-09.json`
- Rows trained on: `187`
- Global best delivery: `14.3372`
- Hard-scene best delivery: `7.8721`

## Learned Policies

| Scene | Best delivery | Fastest failed draft | Learned action |
|---|---:|---:|---|
| `cx_many_glass.xml` | 3.8656x @ 8 spp raw | 3.929x @ 1 spp raw | `tile_refinement_or_trained_thresholds: low-spp rows are fast but fail worst-tile` |
| `scene_caustics.xml` | 5.4658x @ 4 spp None | none | `warm_worker_or_scene_specialization: quality passes but speed ceiling is below 10x` |
| `scene_cube_surface.xml` | 2.9595x @ 8 spp None | 3.1739x @ 4 spp None | `tile_refinement_or_trained_thresholds: low-spp rows are fast but fail worst-tile` |
| `scene_cube_volume.xml` | 7.8721x @ 32 spp raw | 8.6795x @ 1 spp raw | `tile_refinement_or_trained_thresholds: low-spp rows are fast but fail worst-tile` |
| `scene_monkey.xml` | 6.88x @ 32 spp raw | 7.1667x @ 1 spp raw | `tile_refinement_or_trained_thresholds: low-spp rows are fast but fail worst-tile` |
| `scene_sphere_bump.xml` | 4.5932x @ 16 spp raw | 3.9853x @ 2 spp raw | `tile_refinement_or_trained_thresholds: low-spp rows are fast but fail worst-tile` |
| `scene_world_volume.xml` | 14.3372x @ 2 spp raw | none | `ship_10x_policy: validate on more representative scenes and warm worker` |

## Interpretation

- This is not a synthetic benchmark or a modeled multiplier.
- It trains a routing policy from measured render receipts only.
- Actual tile-refinement receipts are included as `tile_refine` rows when present.
- Friendly scene classes can route to the `10x+` low-spp policy.
- Hard scenes route to tile refinement, higher-spp anchors, or trained threshold tuning.
- The next real implementation step is to replace the modeled tile-refinement action with
  actual crop/tile rerender and merge receipts.
