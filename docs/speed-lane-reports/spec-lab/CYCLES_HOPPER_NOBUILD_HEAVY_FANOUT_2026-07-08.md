# Cycles Hopper No-Build Heavy Fan-Out - 2026-07-08

## Run

- Driver: `scripts/spec-lab/run_cycles_sample_fanout_matrix.py`
- Purpose: test whether no-build H100 execution plus much heavier sample counts can make
  process-level sample fan-out beat a single Cycles render.
- GPU: RunPod community `NVIDIA H100 NVL`
- Runtime root: uploaded from
  `.artifacts/cycles/runtime/cx-cycles-hopper-sm90-runtime-20260708.tar.gz`
- Scene: `scene_world_volume.xml`
- Quality: all probes strict EXR exact, SSIM `1.0`, worst-tile SSIM `1.0`
- Merge: Python/OpenImageIO
- Final independent safety check after both runs: `pods: []`, tracked pods `[]`, balance `$16.65`
- Total spend for the two heavy no-build runs: about `$0.86`

## No-Build Setup

Both runs skipped clone, sync, and build.

| Run | Upload | Extract | Smokes |
|---|---:|---:|---:|
| oversplit 16k/65k | `7.7s` | `3.4s` | `2.5s` |
| fatter 65k | `7.9s` | `3.3s` | `2.5s` |

This confirms the runtime tar is fast enough for premium micro-experiments. The limiting factor in
these rows is renderer execution, not build tax.

## Oversplit Run

This run used `--chunks-per-worker 4`, `--parallel-slots 16`.

| Samples | Fan-out / chunks | Full | Subset wall | Merge | Actual wall | Actual speedup | Scheduler gain |
|---:|---:|---:|---:|---:|---:|---:|---:|
| `16384` | `8 / 32` | `20.78s` | `31.4790s` | `0.61s` | `32.0890s` | `0.6476x` | dynamic `1.0507x`, LPT `1.0959x` |
| `16384` | `16 / 64` | `20.77s` | `35.8794s` | `0.80s` | `36.6794s` | `0.5663x` | dynamic `1.2669x`, LPT `1.2936x` |
| `65536` | `8 / 32` | `80.86s` | `94.2264s` | `0.53s` | `94.7564s` | `0.8533x` | dynamic `1.0746x`, LPT `1.0842x` |
| `65536` | `16 / 64` | `80.91s` | `97.1801s` | `0.79s` | `97.9701s` | `0.8259x` | dynamic `1.1841x`, LPT `1.2417x` |

## Fatter-Chunk Run

This run used `--chunks-per-worker 1`, `--parallel-slots 16`.

| Samples | Fan-out / chunks | Full | Subset wall | Merge | Actual wall | Actual speedup |
|---:|---:|---:|---:|---:|---:|---:|
| `65536` | `4 / 4` | `80.83s` | `90.3808s` | `0.36s` | `90.7408s` | `0.8908x` |
| `65536` | `8 / 8` | `80.83s` | `90.6475s` | `0.40s` | `91.0475s` | `0.8878x` |
| `65536` | `16 / 16` | `80.82s` | `93.5747s` | `0.43s` | `94.0047s` | `0.8597x` |

## Interpretation

This is the strongest negative result so far for shell-launched process sample fan-out:

- removing build tax works;
- increasing samples helps;
- reducing chunk count helps;
- Python/OIIO merge is no longer the bottleneck;
- output is exact;
- but separate Cycles processes still do not beat one full H100 render.

The best row was `65536` samples, 4-way/4 chunks, at `0.8908x`. The fatter rows are especially
diagnostic: each concurrent subset process ran for roughly `90s`, while the full render was only
`80.8s`. That means the processes are contending or redoing enough scene/BVH/device work that sample
division does not translate into throughput.

## Decision

Do not spend more premium GPU time on the same subprocess fan-out architecture unless the
architecture changes. The next sample-parallel lane must be one of:

- resident warm workers with scene/BVH/device state kept alive;
- in-process scheduling inside Cycles;
- a custom harness that avoids full standalone startup per sample subset.

For product speed, prioritize the proven per-frame chain while that work happens:

- prebuilt/warm root;
- CUDA single-frame render;
- denoise anchor;
- transcode delivery;
- warm-pool economics.
