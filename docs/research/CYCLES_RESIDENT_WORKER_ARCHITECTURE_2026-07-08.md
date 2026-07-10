# Cycles Resident Worker Architecture - 2026-07-08

## Why This Exists

The no-build H100 NVL tests killed standalone subprocess sample fan-out as the product speed path:

- best no-build heavy row: `65536` samples, 4-way/4 chunks, exact, `0.8908x`;
- oversplit best: `65536` samples, 8-way/32 chunks, exact, `0.8533x`;
- fatter chunks reduced launch overhead, but every concurrent subset process still ran for roughly
  `90s` while the full render was about `80.8s`.
- a follow-up batch-manifest resident scaffold also stayed below `1x`: best row `65536` samples,
  4 resident workers / 16 chunks, exact, `0.8894x`.

The likely wall is not merge, build, or sample math. It is repeated per-worker Cycles scene/device
state plus GPU contention across independent processes. Removing shell launch overhead alone is not
enough.

## Source Hooks

The useful hooks already exist in upstream Cycles.

- `.artifacts/cycles/source/src/app/cycles_standalone.cpp`
  - `session_init()` creates `Session`, sets `OIIOOutputDriver`, reads XML/USD, adds the combined
    pass, calls `Session::reset(...)`, then `Session::start()`.
  - This is too coarse for product fan-out because every subset run repeats the whole standalone
    lifecycle.

- `.artifacts/cycles/source/src/session/session.cpp`
  - `Session::reset(...)` can push new `SessionParams` and `BufferParams` into an existing session.
  - `Session::set_samples(...)` can add work without reconstructing the process.
  - `Session::update_buffers_for_params()` calls `RenderScheduler::set_sample_params(...)`.
  - `Session::update_scene(...)` writes `use_sample_subset`, `sample_subset_offset`, and
    `sample_subset_length` into the scene integrator.

- `.artifacts/cycles/source/src/integrator/render_scheduler.cpp`
  - `RenderScheduler::set_sample_params(...)` maps the global sample count plus subset offset/length
    into the actual scheduled sample window.

- `.artifacts/cycles/source/src/integrator/render_scheduler.h`
  - The comment explicitly frames sample subsets as a way to distribute a single frame across
    multiple computers.

## Architecture Options

### Option A: Resident Standalone Worker

Add a new CX-only standalone mode, for example `cycles --cx-worker --listen /tmp/cx-cycles.sock`.

Lifecycle:

1. Start one process.
2. Load XML/USD once.
3. Build scene/BVH/device state once.
4. Receive JSON jobs:
   - output path;
   - total samples;
   - subset offset;
   - subset length;
   - adaptive on/off;
   - optional render dimensions.
5. For each job:
   - update `SessionParams`;
   - install a job-specific output driver;
   - call `Session::reset(...)`;
   - render;
   - return timing and artifact path.

Why this is first:

- It is the smallest step away from the current CLI.
- It preserves XML/USD ingestion and `Session` ownership.
- It can be run locally inside one pod before building a network service.

Risks:

- `Session::reset(...)` may still trigger scene/device reset work if parameters are treated as
  scene changes.
- Output driver swapping must be verified between jobs.
- Need a clean way to wait for one job to finish without destroying the process.

Status after the first implementation:

- Patch `0004-standalone-cx-batch-manifest.patch` landed a minimal non-socket form of this option.
- The two-job cloud smoke proved one process can render multiple sample-subset outputs and emit
  `CX_CYCLES_BATCH_OK`.
- The second smoke job skipped the full initial kernel-load/BVH-build block, so intra-process reuse
  is real.
- The multi-worker no-build H100 NVL speed probe was exact but negative: best `0.8894x`.
- Conclusion: Option A is useful as a scaffold and correctness harness, but not sufficient as the
  product speed lane unless it evolves into a true long-lived service with separate warm GPUs or a
  much deeper sharing model.

### Option B: In-Process Sample Scheduler

Patch Cycles so a single session renders multiple sample subsets internally and writes one merged
output.

Lifecycle:

1. Load scene once.
2. Split sample ranges inside the render scheduler.
3. Execute chunks through one `PathTrace`/device context.
4. Accumulate into one output buffer or per-chunk buffers without external EXR merge.

Why this might be the real product path:

- Avoids cross-process CUDA context contention.
- Avoids repeated scene/BVH setup.
- Avoids external EXR I/O for chunks.
- Gives the scheduler direct access to chunk timing for dynamic/LPT assignment.

Risks:

- This is a deeper fork and harder to keep upstream-rebaseable.
- Need to preserve exact/numeric equivalence.
- Could collide with Cycles' existing tile/sample scheduler assumptions.

### Option C: Product Pivot Without Sample Fan-Out

Use the fork as a fast, no-build, CUDA single-frame renderer and stack the already-proven product
levers around it:

- warm worker image or runtime root;
- denoise anchor;
- VP9/transcode delivery;
- warm-pool economics;
- optional OptiX or external GPU OIDN.

Why this is viable:

- The broader speed-lane already has proven denoise and transcode multipliers.
- The no-build root removes a large operational tax.
- It avoids deep renderer surgery while a resident scheduler is being built.

## Recommended Next Loop

1. Treat the batch-manifest proof as complete for Option A step zero.
2. Add finer resident instrumentation only if it supports Option B or service wrapping:
   - scene load;
   - first render;
   - second render;
   - subset render;
   - output write.
3. Do not spend more H100/H200 time on one-GPU manifest-worker fan-out. The new comparison is:
   - subprocess baseline: 65536 samples, 4-way/4 chunks, `0.8908x`;
   - resident-batch baseline: 65536 samples, 4 workers / 16 chunks, `0.8894x`;
   - target for a real win: at least `1.3x` on exact-clean world-volume before expanding the lane.
4. In parallel, create an `sm_89` runtime tar or pushed multi-arch image so L40S/Ada tests stop
   rebuilding.
5. Prioritize Option B (in-process scheduler) or Option C (single-frame Cycles plus denoise and
   transcode) before testing 8/16-way sample scheduling again on H100/H200.

## Stop Rules

- Do not run more premium subprocess fan-out on the same standalone architecture.
- Do not run more premium manifest-worker fan-out on one GPU unless the Cycles sharing model changes.
- Do not chase C++ merge before resident rendering; merge is already sub-second in the heavy rows.
- Do not attach spec decode to sample fan-out until resident/in-process scheduling clears `1x` on
  exact output.
