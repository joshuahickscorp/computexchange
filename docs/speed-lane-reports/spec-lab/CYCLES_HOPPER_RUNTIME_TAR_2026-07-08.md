# Cycles Hopper Runtime Tar Validation - 2026-07-08

## Run

- Driver: `scripts/spec-lab/run_cycles_sample_fanout_matrix.py`
- Purpose: make the no-rebuild worker path portable by exporting a Hopper `sm_90` runtime root and
  validating it on a fresh pod.
- GPU: RunPod community `NVIDIA H100 NVL`
- Runtime root: `/opt/cx-cycles`
- Artifact:
  `.artifacts/cycles/runtime/cx-cycles-hopper-sm90-runtime-20260708.tar.gz`
- Local artifact size: `192M`
- Tar contents: `install/` and `examples/`
- Remote extracted root size: `516M`
- Build: CUDA precompiled, OptiX disabled, `sm_90`
- Final independent safety check: `pods: []`, tracked pods `[]`, balance `$17.61`

## Export Pass

This pass did a normal build once, then packed and downloaded the runtime root.

| Stage | Time |
|---|---:|
| deps | `16.7s` |
| clone | `4.2s` |
| sync libs | `87.4s` |
| apply patches | `0.5s` |
| build | `388.0s` |
| binary smoke | `0.7s` |
| patch CLI smoke | `1.0s` |
| render smoke | `0.8s` |
| export runtime tar pack | `19.9s` |
| export runtime tar download | `9.4s` |
| tiny exact probe | `17.5s` |

The export probe was exact:

| Scene | Samples | Fan-out | Full | Actual wall | Quality | Merge |
|---|---:|---:|---:|---:|---|---:|
| `scene_world_volume.xml` | `64` | `2` | `0.85s` | `1.4148s` | exact, SSIM `1.0` | Python `0.31s` |

## Fresh-Pod Import Pass

This pass launched a new H100 NVL pod, uploaded the tar, extracted it to `/opt/cx-cycles`, and ran
smokes plus a tiny exact fan-out probe without clone, sync, or build.

| Stage | Time |
|---|---:|
| upload prebuilt root | `8.1s` |
| extract prebuilt root | `3.4s` |
| binary smoke | `0.7s` |
| patch CLI smoke | `1.0s` |
| render smoke | `0.9s` |
| tiny exact probe | `25.7s` |

Fresh-pod import probe:

| Scene | Samples | Fan-out | Full | Actual wall | Quality | Merge |
|---|---:|---:|---:|---:|---|---:|
| `scene_world_volume.xml` | `64` | `2` | `0.85s` | `1.3744s` | exact, SSIM `1.0` | Python `0.32s` |

## Interpretation

This is the first fresh-pod proof that the CX Cycles fork can run from a portable runtime root on
Hopper without rebuilding Cycles. It does not prove product fan-out speed yet; the probe is
deliberately tiny and remains process-overhead-bound. It does prove that normal Hopper
micro-experiments can stop paying the `~6-10 minute` build/sync tax when the runtime tar is reused.

The artifact is Hopper-oriented (`sm_90`). L40S or Ada lanes need a separate `sm_89` runtime tar or
a multi-architecture image.

## Next Use

Use this artifact for bounded Hopper tests:

```bash
python3 scripts/spec-lab/run_cycles_sample_fanout_matrix.py \
  --gpu-tier hopper \
  --prebuilt-root-tar .artifacts/cycles/runtime/cx-cycles-hopper-sm90-runtime-20260708.tar.gz \
  --remote-root /opt/cx-cycles \
  --scene scene_world_volume.xml,scene_cube_volume.xml \
  --samples 4096 \
  --fanouts 8,16 \
  --chunks-per-worker 4 \
  --device CUDA \
  --disable-adaptive-sampling \
  --merge-mode python \
  --execution-mode parallel \
  --parallel-slots 8 \
  --subset-retries 1 \
  --continue-on-probe-failure
```

Spend on future premium runs should be bounded and checked before crossing about `$5`.
