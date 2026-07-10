# Empirical Renderer 10x Path - 2026-07-09

## Short Answer

Yes, `10x` quality-preserving renderer-side speedup is now proven for a friendly scene class, but
not yet proven as a general renderer claim.

The current measured quality-preserving raw renderer ceiling is:

- `14.3372x` on `scene_world_volume.xml`, raw `2 spp` vs `4096 spp`;
- global SSIM `1.0`;
- worst-tile SSIM `1.0`.

This was measured on a live `NVIDIA L40S` RunPod run on 2026-07-09 after transfer preflight,
watchdog, runtime-root upload, binary smoke, render, scoring, and teardown.

Other measured raw delivery rows above `5x`:

- `scene_world_volume.xml`, raw `4 spp` vs `4096 spp`: `13.5495x`, global `1.0`,
  worst tile `1.0`.
- `scene_world_volume.xml`, OIDN `2 spp` vs `4096 spp`: `12.9695x`, global `1.0`,
  worst tile `1.0`.
- `scene_cube_volume.xml`, raw `32 spp` vs `4096 spp` on H100 NVL: `7.8721x`,
  global `0.99596516`, worst tile `0.965073782`.
- `scene_cube_volume.xml`, raw `64 spp` vs `4096 spp`: `7.3333x`, global `0.997972352`,
  worst tile `0.982018952`.
- `scene_monkey.xml`, raw `32 spp` vs `4096 spp`: `6.6154x`, global `0.999617997`,
  worst tile `0.975919427`.
- `scene_cube_volume.xml`, raw `128 spp` vs `4096 spp`: `6.4952x`, global `0.998970082`,
  worst tile `0.990779048`.
- `scene_caustics.xml`, raw `4 spp` vs `4096 spp`: `5.4658x`, global `1.0`, worst tile `1.0`.

This means `5x` is no longer aspirational and `10x` is no longer theoretical. The next question is
whether we can make `10x` boring, repeatable, and product-safe across representative scenes.

## What The Evidence Says

### Raw low-spp rendering already works on some scenes

Raw low-spp delivery works when the scene has a fast convergence knee:

- world volume and caustics examples are unusually forgiving in the current PNG/SSIM gate;
- cube volume stays delivery at `64 spp`, even with known EXR drift history;
- monkey needs at least `32 spp` raw for delivery because lower spp collapses worst-tile quality.

The lesson is not "always render less." The lesson is "find the scene-specific quality knee, then
ship the lowest sample count that clears global and worst-tile gates."

### Worst-tile gate is the truth serum

Monkey proves why global SSIM is dangerous:

- raw `4 spp` vs `4096 spp`: global `0.994308944`, worst tile `0.702331872`, fail;
- raw `8 spp` vs `4096 spp`: global `0.997478673`, worst tile `0.839460065`, fail;
- raw `16 spp` vs `4096 spp`: global `0.998963606`, worst tile `0.927667245`, preview;
- raw `32 spp` vs `4096 spp`: global `0.999617997`, worst tile `0.975919427`, delivery.

So any product claim must include worst tile, p5 tile, and failure/refinement policy.

### OIDN changes the branch shape

The live L40S run proved OIDN can turn a raw low-spp fail into delivery:

- `scene_monkey.xml`, raw `4 spp` vs `1024 spp`: global `0.994304903`, worst tile
  `0.702008074`, fail, `2.7937x` if shipped, but it cannot ship.
- `scene_monkey.xml`, OIDN `4 spp` vs `1024 spp`: global `0.999513701`, worst tile
  `0.991403939`, delivery, `2.4251x` including denoise.

That is not `10x`, but it proves the right algorithmic shape:

```text
low-spp draft -> denoise -> verify global/worst-tile -> ship or refine
```

## Where 10x Can Come From

Renderer-only `10x` likely requires stacking several renderer-side gains without letting quality
fall:

1. Low-spp quality knee:
   - already measured up to `14.3372x`;
   - still limited to friendly scene classes until harder scenes cross the same gate.

2. Denoise-assisted lower knee:
   - monkey proves OIDN can rescue `1/2/4/8 spp` against `4096 spp`;
   - cube volume proves OIDN `1 spp` can reach delivery at `7.6808x` on H100;
   - OIDN helps quality, but its CPU denoise overhead usually lowers the fastest shipped row.

3. Tile refinement:
   - failed rows often fail locally, not globally;
   - if only a small tile fraction needs higher spp, final work can be much less than full-frame
     rerender;
   - current speculative ladder models escalation but does not yet rerender and merge failed tiles.

4. Warm worker or image path:
   - current runs lose time to provisioning and tar transfer;
   - render-only speed can be hidden by cold logistics;
   - a pushed image, persistent volume, or warm pool is required before product claims.

5. Scene specialization:
   - product/material-variant scenes are the strongest owned-renderer opportunity;
   - the Rust `decoupled_micro` path already shows `4.365x` local speedup for cached structure
     re-shading;
   - that is not Cycles parity yet, but it is a real separate route to large multipliers for the
     "same scene, many variants" workload.

## Path To 100x

The `10x * 10x = 100x` idea is structurally sound only if the multipliers are independent enough.

A plausible stack:

| Layer | Evidence Today | Product Multiplier Target |
|---|---:|---:|
| Low-spp raw quality knee | `14.3372x` measured | `5-15x`, scene-dependent |
| OIDN/denoise assisted draft | delivery rescue measured | `1.2-3x` effective beyond raw knee |
| Tile refinement | scaffolded, not live | `1.2-5x` on local-failure scenes |
| Warm worker/image | runtime tars measured, upload wall remains | removes cold tax, not pure render speed |
| Decoupled material variants | `4.365x` local microbench | `2-10x` for variant batches |
| Spec decode/orchestrator | protocol scaffolded | `1.5-10x` depending modality and acceptance |

The honest `100x` story is not "one renderer magically becomes 100x faster." It is:

```text
quality-knee render + denoise + selective refinement + warm logistics + workload specialization
+ speculative scheduling
```

That can absolutely reach `20x+` for the right workload. `50x-100x` needs either many variants,
many frames, or very high draft acceptance where the expensive path is rarely invoked.

## Updated Methodology: Beat Ourselves First

The next run should treat stock Cycles as the reference, but not as the only opponent. The tougher
opponent is our own best receipt.

Required run order:

1. Establish or import the stock Cycles reference for the workload.
2. Import the current best CX receipt for the nearest comparable workload.
3. Push renderer-side gains first: raw spp knee, denoise-assisted knee, tile/frame refinement,
   representative-scene coverage, warm runtime/image path, and cost accounting.
4. Keep pushing until the next renderer-only improvement cannot be made without quality loss,
   unsupported modeling, or wasteful spend.
5. Only then attach speculative decode/orchestration, training, threshold tuning, classifier
   routing, and iterative optimization on top of the strongest renderer substrate.

The immediate target is no longer "prove `5x`" or "touch `10x`." Both are already in the ledger.
The target is:

```text
turn the measured 14.3372x friendly-scene ceiling into repeatable 10x+ delivery-tier renderer
receipts on harder representative scenes, then stack speculative adoption/training/iteration on top
of that stronger base.
```

This keeps the compounding story clean. A future `100x` claim must come from one of two honest
forms:

- one measured end-to-end workload receipt showing the full multiplier; or
- a staged receipt table where every multiplier is measured, the dependencies are explicit, and any
  modeled bridge is marked as modeled.

## What Would Make This Investable

The strongest claim we can back now:

- "CX has measured quality-preserving raw renderer rows above `5x`, with a current best of
  `14.3372x` on a friendly scene class, a hard-scene live ceiling of `7.8721x`, and OIDN lanes
  that rescue failed low-spp drafts into delivery quality."

The claim we need next:

- "CX repeatedly hits `10x` delivery-tier output on representative customer-like scenes with
  wall-clock, overhead, and teardown receipts."

The claim after that:

- "CX stacks renderer-side `10x` with speculative/orchestrated `N x` at the pipeline level, making
  rented/back-end compute cheaper than buying a workstation for most jobs."

## Immediate Branches

Keep:

- raw quality ladder;
- OIDN quality ladder;
- worst-tile gate;
- decoupled renderer microbench;
- no-build runtime tars.

Cut:

- one-GPU subprocess fanout speed claims;
- one-GPU manifest-worker fanout speed claims;
- global-SSIM-only claims;
- cold upload attempts that stall after reachability.

Add next:

- transfer-speed preflight before full runtime tar upload;
- pushed image or persistent-volume path;
- live OIDN `1/2/4/8/16 spp` vs `4096 spp`;
- actual tile rerender and merge;
- harder representative CX scene;
- spec decode anchor after renderer branch completes.

## Current Ceiling Statement

With current empirical evidence, the honest renderer-only quality-preserving point is:

- proven: `14.3372x` on friendly `scene_world_volume.xml`;
- hard-scene ceiling from this round: `7.8721x` on `scene_cube_volume.xml` H100 raw `32 spp`;
- near-term plausible: `10x` on harder scenes only after tile refinement, warm workers, or
  trained routing;
- not yet proven: general `10x` across representative customer scenes;
- longer-term plausible stack: `20x+` for selected workloads, `50-100x` for batch/variant/speculative
  workloads where multiple multipliers compose.
