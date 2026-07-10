# Renderer Frontier 10x And Diminishing Returns - 2026-07-09

## Result

This run moved the renderer-side empirical ceiling from `7.4286x` to `14.3372x` on a live CUDA
quality ladder, with delivery-tier gates intact.

Best measured row:

- GPU: `NVIDIA L40S`, secure RunPod.
- Scene: `scene_world_volume.xml`.
- Reference: `4096 spp`, `12.33s`.
- Draft: raw `2 spp`, `0.86s`.
- Quality: global SSIM `1.0`, worst-tile SSIM `1.0`, p5 tile SSIM `1.0`.
- Speedup vs reference: `14.3372x`.
- Evidence: `docs/speed-lane-reports/spec-lab/cycles_quality_ladder_ledger.jsonl`, result at
  `2026-07-09T07:25:51`.

So the answer to "can we hit 10?" is now yes for the friendly scene class. It is not yet a general
renderer claim.

## Commands Run

Local harness gate:

```bash
python3 -m py_compile scripts/spec-lab/runpod.py scripts/spec-lab/run_cycles_quality_ladder.py scripts/spec-lab/cx_render_autoprobe.py scripts/spec-lab/run_speculative_render_ladder.py
python3 -m unittest scripts/spec-lab/test_runpod_safety.py scripts/spec-lab/test_cycles_quality_ladder.py scripts/spec-lab/test_cx_render_autoprobe.py scripts/spec-lab/test_speculative_render_ladder.py
```

Live L40S frontier sweep:

```bash
python3 scripts/spec-lab/run_cycles_quality_ladder.py \
  --gpu-tier ada \
  --scene scene_world_volume.xml,scene_cube_volume.xml,scene_monkey.xml,scene_caustics.xml \
  --ref-samples 4096 \
  --draft-samples 1,2,4,8,16,32,64 \
  --with-oidn \
  --oidn-device cpu \
  --min-balance 4 \
  --max-minutes 90 \
  --stage-timeout-s 2400 \
  --upload-timeout-s 900 \
  --transfer-preflight-mb 4 \
  --min-transfer-mbps 0.25
```

Live L40S representative/synthetic sweep:

```bash
python3 scripts/spec-lab/run_cycles_quality_ladder.py \
  --gpu-tier ada \
  --scene scene_sphere_bump.xml \
  --include-synthetic-scene \
  --synthetic-name cx_many_glass.xml \
  --ref-samples 4096 \
  --draft-samples 1,2,4,8,16,32,64 \
  --with-oidn \
  --oidn-device cpu \
  --min-balance 4 \
  --max-minutes 90 \
  --stage-timeout-s 2400 \
  --upload-timeout-s 900 \
  --transfer-preflight-mb 4 \
  --min-transfer-mbps 0.25
```

Live H100 hard-scene comparison:

```bash
python3 scripts/spec-lab/run_cycles_quality_ladder.py \
  --gpu-tier hopper \
  --scene scene_cube_volume.xml,scene_monkey.xml,scene_sphere_bump.xml \
  --include-synthetic-scene \
  --synthetic-name cx_many_glass.xml \
  --ref-samples 4096 \
  --draft-samples 1,2,4,8,16,32 \
  --with-oidn \
  --oidn-device cpu \
  --min-balance 4 \
  --max-minutes 75 \
  --stage-timeout-s 1800 \
  --upload-timeout-s 900 \
  --transfer-preflight-mb 4 \
  --min-transfer-mbps 0.25
```

Speculative render gate import:

```bash
python3 scripts/spec-lab/run_speculative_render_ladder.py --variants raw,oidn --min-rows 1
```

## Best Rows

| Class | GPU | Scene | Variant | Draft spp | Quality / worst / p5 | Speedup |
|---|---|---|---|---:|---:|---:|
| Friendly | L40S | `scene_world_volume.xml` | raw | 2 | `1.0 / 1.0 / 1.0` | `14.3372x` |
| Friendly | L40S | `scene_world_volume.xml` | raw | 4 | `1.0 / 1.0 / 1.0` | `13.5495x` |
| Friendly | L40S | `scene_world_volume.xml` | OIDN | 2 | `1.0 / 1.0 / 1.0` | `12.9695x` |
| Harder volume | H100 NVL | `scene_cube_volume.xml` | raw | 32 | `0.99596516 / 0.965073782 / 0.969909261` | `7.8721x` |
| Harder volume | H100 NVL | `scene_cube_volume.xml` | OIDN | 1 | `0.999190325 / 0.979622568 / 0.992000417` | `7.6808x` |
| Monkey | H100 NVL | `scene_monkey.xml` | raw | 32 | `0.999617996 / 0.975919427 / 0.997974547` | `6.88x` |
| Sphere bump | L40S | `scene_sphere_bump.xml` | raw | 16 | `0.999282615 / 0.994502681 / 0.997739905` | `4.5932x` |
| CX glass | H100 NVL | `cx_many_glass.xml` | raw | 8 | `0.997937312 / 0.967733036 / 0.979770975` | `3.8656x` |

## Diminishing Returns Boundary

The frontier now splits into two classes.

Friendly scenes:

- `scene_world_volume.xml` cleared delivery at `1-64 spp`.
- The best row was `2 spp`; lower does not beat it because fixed render/launch cost dominates.
- This branch is already above `10x`; repeating the same low-spp sweep is unlikely to teach much.

Harder/product-like scenes:

- `scene_cube_volume.xml` improved on H100, but still topped at `7.8721x` delivery.
- `scene_monkey.xml` topped at `6.88x` raw delivery; OIDN rescued `1-4 spp`, but not to `10x`.
- `scene_sphere_bump.xml` topped at `4.5932x`.
- `cx_many_glass.xml` topped at `3.8656x`.

That is the honest saturation point for brute renderer-side low-spp/OIDN sweeps. More identical
sweeps are not the next multiplier. The next multiplier has to come from actual selective
refinement, trained scene/workload routing, warm images/workers, or renderer integration that avoids
the remaining fixed floor.

## Speculative Adoption Status

The speculative render ladder was rerun over the refreshed measured quality ledger:

- Report: `docs/speed-lane-reports/spec-lab/SPECULATIVE_RENDER_LADDER_2026-07-09.md`
- Ledger: `docs/speed-lane-reports/spec-lab/speculative_render_ladder_ledger.jsonl`
- Gate rows after refresh: `183`
- Delivery rows after refresh: `141`
- Best gate: `14.3372x`, delivery, `scene_world_volume.xml`, raw `2 spp`

This is render-side speculative gating, not vLLM token speculative decoding. A ready vLLM
spec-decode runner was not present under `scripts/spec-lab`; the current adoption boundary is a
scaffold/receipt boundary.

## Trained Routing Policy

A receipt-derived render policy was trained from the same measured quality ledger:

- Script: `scripts/spec-lab/train_render_policy.py`
- Report: `docs/speed-lane-reports/spec-lab/RENDER_POLICY_TRAINING_2026-07-09.md`
- Policy JSON: `docs/speed-lane-reports/spec-lab/render_policy_2026-07-09.json`
- Policy ledger: `docs/speed-lane-reports/spec-lab/render_policy_training_ledger.jsonl`
- Global best learned policy: `scene_world_volume.xml`, raw `2 spp`, `14.3372x`.
- Hard-scene best learned policy: `scene_cube_volume.xml`, raw `32 spp`, `7.8721x`.

This is deliberately lightweight training: a measured-receipt policy table, not a fake neural claim.
It converts the frontier into routing decisions:

- friendly volume-like scenes can route to a `10x+` low-spp policy;
- cube/monkey/glass/sphere-like hard scenes route to tile refinement, higher-spp anchors, or
  trained threshold tuning before any general `10x` claim.

## Cloud Safety

Safety receipts after the final H100 run:

- Live pods: `[]`
- Tracked pods: `[]`
- Balance: `$43.3189284859`
- Current spend per hour reported by API: `$0.01`
- Runtime tars still present:
  - `cx-cycles-ada-sm89-batch-runtime-20260708.tar.gz`
  - `cx-cycles-hopper-sm90-batch-runtime-20260708.tar.gz`
  - `cx-cycles-hopper-sm90-runtime-20260708.tar.gz`

The new transfer preflight worked:

- L40S secure: `4 MiB` preflight at `0.8789 MiB/s`, upload `57.6s`.
- L40S community: `4 MiB` preflight at `0.8897 MiB/s`, upload `35.9s`.
- H100 community: `4 MiB` preflight at `0.2586 MiB/s`, upload `429.8s`.

The H100 result proves the preflight should probably be raised above `0.25 MiB/s` for expensive
pods unless the branch is uniquely valuable.

## Next Methodology

Stop repeating brute low-spp sweeps as the main branch. Continue with:

1. Tile refinement implementation:
   - identify failed or low-confidence tiles from the same scoring pass;
   - rerender only those tiles or exact crops;
   - merge and re-score final output.

2. Trained/tuned routing:
   - learn scene class, sample knee, denoise usefulness, and GPU tier from the ledger;
   - route friendly volume-like scenes directly to the `10x+` policy;
   - route hard scenes to tile refinement or higher-spp anchors.

3. Warm worker/image:
   - avoid repeated runtime tar uploads;
   - increase transfer preflight floor for premium pods;
   - turn render timing into product timing.

4. Spec decode integration:
   - keep Hawking/vLLM integration thin;
   - use the renderer ledger as the training/acceptance dataset;
   - do not claim `100x` until the stack is measured end-to-end or the staged multipliers are
     explicitly labeled.
