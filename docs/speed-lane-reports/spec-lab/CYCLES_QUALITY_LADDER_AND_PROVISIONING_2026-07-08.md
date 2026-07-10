# Cycles Quality Ladder And Provisioning - 2026-07-08

## Summary

This loop added a no-build standalone Cycles quality-ladder runner and ran it on real RunPod
H100 NVL hardware using the existing Hopper runtime tar:

```text
.artifacts/cycles/runtime/cx-cycles-hopper-sm90-batch-runtime-20260708.tar.gz
```

The runner uploads/extracts the runtime root, renders a high-sample reference plus lower-sample
drafts, computes global SSIM, worst-tile SSIM, p5 tile SSIM, MAE, max error, tiers each row, and
tears the pod down through the shared tracked-pod safety path.

New files:

- `scripts/spec-lab/run_cycles_quality_ladder.py`
- `scripts/spec-lab/test_cycles_quality_ladder.py`

Verification:

```bash
python3 -m py_compile scripts/spec-lab/run_cycles_quality_ladder.py
python3 -m unittest discover -s scripts/spec-lab -p 'test_cycles*.py'
```

Result: `51` Cycles tests passed.

Final safety check for this loop: `pods: []`, `tracked: []`, balance `$14.36`.

## Quality Ladder Results

All successful rows were no-build H100 NVL runs, fixed-sample CUDA renders, reference `4096 spp`.

Best delivery-tier rows by scene:

| Scene | Lowest/fastest delivery point | Global SSIM | Worst tile | Speedup vs 4096 spp |
|---|---:|---:|---:|---:|
| `scene_world_volume.xml` | `64 spp` | `1.000000000` | `1.000000000` | `7.4286x` |
| `scene_cube_volume.xml` | `64 spp` | `0.997972352` | `0.982018952` | `7.3333x` |
| `scene_monkey.xml` | `32 spp` | `0.999617997` | `0.975919427` | `6.6154x` |
| `scene_caustics.xml` | `4 spp` | `1.000000000` | `1.000000000` | `5.4658x` |
| `scene_sphere_bump.xml` | `4 spp` | `0.994660223` | `0.959727762` | `3.6575x` |
| `cx_many_glass.xml` | `64 spp` | `0.999854288` | `0.996755943` | `3.7216x` |
| `scene_cube_surface.xml` | `8 spp` | `0.995968115` | `0.979001121` | `2.9595x` |

The useful failure knee is `scene_monkey.xml`:

| Samples | Global SSIM | Worst tile | Tier | Speedup |
|---:|---:|---:|---|---:|
| `4` | `0.994308944` | `0.702331872` | fail | `6.8800x` |
| `8` | `0.997478673` | `0.839460065` | fail | `6.2169x` |
| `16` | `0.998963606` | `0.927667245` | preview | `6.2169x` |
| `32` | `0.999617997` | `0.975919427` | delivery | `6.6154x` |
| `64` | `0.999822485` | `0.988598461` | delivery | `6.3704x` |

Interpretation:

- Low-sample rendering is a real product lever on the standalone fork. The best current rows are
  `~6.6-7.4x` at delivery-tier quality on official examples.
- Worst-tile gating matters. Monkey at `4/8 spp` has excellent global SSIM but fails local quality,
  exactly the failure mode the tile gate was meant to catch.
- The synthetic `cx_many_glass.xml` scene rendered successfully, but it was not hard enough to be a
  decisive product proxy.
- Official scenes remain too small/easy for final product claims. They are now useful as fast tier
  calibration, not as the final benchmark.

## Standalone OIDN Status

The runner now has an optional `--with-oidn` scaffold that:

- renders raw draft EXRs;
- loads bundled `libOpenImageDenoise.so.2` through Python `ctypes`;
- uses the RT filter on RGB float buffers;
- writes an OIDN EXR;
- scores `variant=raw` and `variant=oidn` rows with denoise time included.

This scaffold is not validated yet.

Attempts:

- First attempt failed because `OpenImageIO` Python bindings were unavailable in the pod image.
- The shim was switched to `OpenEXR`/`Imath`, matching existing spec-lab code.
- The next attempt hit a transient SSH disconnect mid-stage before returning OIDN rows.

Conclusion: external standalone OIDN remains a medium-upside lane, but it needs one focused retry
with a reachable pod and possibly a background remote-log/poll wrapper so SSH drops do not erase the
stage result.

## Product-Chain Track 3 Status

`scripts/spec-lab/run_ultimate_no_reprojection.py` was updated to:

- prefer H200/H100 when requested;
- accept `--gpu-tier premium|value|any`;
- accept bounded overrides for `--frames`, `--ref-spp`, `--draft-spp`, `--resolution`, and `--scene`;
- extend the remote watchdog when the requested timeout is longer than the default.

Bounded Track 3 probe target:

```bash
python3 scripts/spec-lab/run_ultimate_no_reprojection.py \
  --gpu-tier value \
  --frames 4 \
  --ref-spp 1024 \
  --draft-spp 256 \
  --resolution 1280x720 \
  --timeout-s 3600
```

This did not reach workload execution. Premium and value-tier cold provisioning repeatedly failed
capacity or SSH reachability. The driver terminated unreachable attempts and cleanup was verified
after manual interrupts.

Provisioning evidence:

- H200 community: no capacity.
- Multiple H100/H200 secure/community attempts: unreachable.
- A100 community: no capacity.
- A100 secure, L40S community, A6000 community: unreachable in this window.
- Final check after interrupt: `pods: []`, `tracked: []`, balance `$14.36`.

Interpretation:

- The no-build runtime tar solves build tax, but not cold provisioning tax.
- Warm pool or pre-warmed worker image is now a product requirement, not a nicety.
- Do not spend more this loop searching ad hoc RunPod capacity; land a warm/reachable worker first.

## Score Impact

Positive:

- Adds a real no-build single-frame quality ladder.
- Establishes first strict tier policy with actual global/worst-tile failure and pass rows.
- Moves product speed evidence away from killed one-GPU fan-out toward low-sample quality tiers.

Negative:

- Still official/synthetic examples, not a representative CX scene.
- Standalone OIDN is scaffolded but not validated.
- Product-chain Track 3 did not execute because of provisioning/reachability.

Strict grade after this loop:

- Overall: `7.9/10`
- Product substrate: `7.6/10`
- Research scaffold: `9.7/10`
- Proven sample-parallel potential: `7.2/10`
- Proven single-frame quality-tier potential: `7.8/10`

The score rises because the substrate now has a product-relevant low-sample quality ladder. It does
not cross `8/10` because real CX scenes, warm workers, OIDN validation, and end-to-end product-chain
timing are still missing.
