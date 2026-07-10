# LOCAL METAL RENDER LANE — the anchor stack on fleet hardware

**Date:** 2026-07-10
**Status:** BUILT + unit-tested; real-run result recorded below and in the ledger.
**Driver:** `scripts/spec-lab/run_local_metal_anchor.py`
**Tests:** `scripts/spec-lab/test_local_metal_anchor.py` (no Blender required)
**Ledger:** `docs/speed-lane-reports/spec-lab/local_metal_ledger.jsonl` (evidence label `MEASURED/local-metal`)

## Why this lane exists

The CX fleet is Apple Silicon Macs. Every render-lane receipt to date was measured on
rented CUDA hardware (A100/H100 pods). For the render lane to be a CX *product* — work
the fleet itself can sell — the anchor stack has to run and be **measured on Metal**,
on the machines CX actually owns. This lane is the zero-dollar local driver that does
that: the **same** anchor-vs-reference protocol, the **same** pod runner
(`pod/exp_render_stack.py`), the same honesty contract — pointed at the local Mac's
Blender + Metal GPU instead of a pod.

Hard rules honored:

- **LOCAL ONLY.** The driver never provisions a pod and never calls the RunPod API
  (unit-enforced: `LocalOnlySafetyTest`). Only network use: the one-time public scene
  download from download.blender.org (classroom.zip, ~70 MB, cached) and pip wheels
  for the EXR reader if missing.
- **Honesty-first.** The receipt is fully `MEASURED` (see "modeled" below); a missing
  Blender or missing GPU produces an honest `{"error": ...}` — never a fabricated or
  silently-CPU number.

## How it reuses the pod runner (no fork, no copy)

`pod/exp_render_stack.py` hardcodes pod paths (`BLENDER_DIR=/root/blender`,
`WORK_DIR=/tmp/render_stack`, `_CACHE_ROOT=/models|/root/spec-lab`). The driver does
NOT copy or fork the runner. It launches a tiny **shim subprocess** that:

1. imports `exp_render_stack` from `scripts/spec-lab/pod/`,
2. patches those module constants to local-Mac paths (from env vars) — including
   `SCENES_DIR`, which is derived from `_CACHE_ROOT` at import time,
3. sets `sys.argv[1]` to the config JSON and calls the runner's `main()` under the
   runner's own contract (last stdout line = ONE JSON object; failures emit
   `{"error":...}` and exit 0).

Because `BLENDER_BIN` is patched to an existing local binary, the runner's
`ensure_blender()` returns immediately — the Linux tarball download never triggers.
`ensure_system_libs()` (apt-get) fails non-fatally on macOS by design.

## The METAL rung (the one guarded edit to the pod runner)

The runner's embedded scene script and its functional GPU probe picked devices via
the `OPTIX > CUDA > HIP > ONEAPI` ladder. **METAL was not a rung.** Per the branch
mandate, the ONLY edit made to `pod/exp_render_stack.py` is the guarded addition of
`METAL` as the **last** rung of both ladders:

- On Linux Blender builds the enum is CUDA-family only; `compute_device_type='METAL'`
  raises and the existing `try/except: continue` skips it → **zero behavior change on
  every existing pod path** (verified: on macOS the enum is `('NONE','METAL')`, so the
  reverse is also true — the four CUDA rungs raise/continue there).
- **macOS headless gotcha** (documented in memory `blender-asset-render`, observed on
  this M3 Pro): `prefs.get_devices()` alone can leave `prefs.devices` EMPTY for METAL,
  which silently falls back to CPU (~9 min/image vs ~1 min). The rung therefore calls
  `prefs.get_devices_for_type('METAL')`, guarded by `hasattr` + `try` so it is a
  strict no-op everywhere else.
- The fail-loud paths are untouched: with `require_gpu=True` (the local default), no
  enumerated GPU still raises `CX_DEVICE_ERROR` / probe `SystemExit(3)`, and CPU
  devices stay disabled so a broken kernel errors instead of silently tracing on CPU.
  `MetalLadderGuardTest` pins all of this in the unit tests.

Probe evidence (MEASURED, 2026-07-10, this Mac): the ladder enumerated
`[('Apple M3 Pro','CPU'), ('Apple M3 Pro (GPU - 18 cores)','METAL')]`, picked
`METAL/Apple M3 Pro (GPU - 18 cores)`, and traced a real frame on it.

## Local Blender discovery

Order (first runnable wins): `--blender` arg → `$CX_LOCAL_BLENDER` →
`/Applications/Blender.app/Contents/MacOS/Blender` (the repo convention — every
`scripts/cx_*.py` asset renderer uses this path; Blender is NOT on PATH on the fleet
Macs) → `blender` on PATH. If none exists the driver emits
`{"error": ..., "status": "PENDING-OWNER-HARDWARE"}` with the install step
(Blender 4.2 LTS macOS Apple Silicon from blender.org) and exits 0.

This Mac: **Blender 4.2.1 LTS** at the `.app` path — same 4.2 LTS line as the pod
lane's pinned 4.2.0 tarball, so numbers are protocol-comparable (not
silicon-comparable; see caveats).

## Python deps (user environment never mutated)

The runner needs `numpy/PIL/skimage` plus an EXR reader. **Measured constraint
(2026-07-10):** this Mac's python lacks the `OpenEXR` bindings and the runner's
imageio fallback cannot open Blender MULTILAYER EXRs without extra backends
(`ValueError: Could not find a backend`). Verified fix: `pip install OpenEXR` reads
the multilayer EXR correctly (channels `ViewLayer.Combined.*`, `.Depth.Z`,
`.Vector.*`). The driver provisions this WITHOUT touching the user's env, tiered:

1. base python already imports everything → used directly;
2. only `OpenEXR` missing → `pip install --target ~/.cache/cx-spec-lab/pysite OpenEXR`,
   prepended to `PYTHONPATH` for the shim subprocess only;
3. more missing → private venv at `~/.cache/cx-spec-lab/venv`.

## Default config (TINY — sized for ~5–15 min on an M3 Pro)

| knob | value | why |
|---|---|---|
| scene | classroom | same production scene as the cloud lane; ~70 MB one-time download, cached at `~/.cache/cx-spec-lab/scenes/` |
| resolution | 960x540 | tiny tier |
| frames | **2** | the runner clamps `frames` to >=2 (a single frame cannot form the animated protocol) — the task's "1 frame" is honestly delivered as the 2-frame minimum |
| keyframe_every | 1 | all-anchor, zero reprojection — the pure anchor-vs-reference measurement, the same mode as cloud RUN 3 |
| ref_spp / draft_spp | 512 / 64 | tiny tier |
| repair | OFF | as mandated |
| hole_fill | inpaint | kf=1 has no reprojected frames → no holes ever exist; this skips the pointless fixed-overhead calibration render and keeps `modeled=false` — the receipt is FULLY measured |
| require_gpu | **True** | fail-loud: a run that cannot land the Metal GPU errors instead of benchmarking the CPU |

The reference cache (`~/.cache/cx-spec-lab/ref_cache`) makes repeat local sweeps over
draft-side knobs cheap, with T_ref honestly reported as the original measured render
time (the runner's own cache semantics).

## RESULTS (MEASURED/local-metal, 2026-07-10, Apple M3 Pro)

Host: `macOS-26.6-arm64`, Blender 4.2.1 LTS, device string from the runner:
**`GPU/METAL`** (Cycles enumerated + traced on `Apple M3 Pro (GPU - 18 cores)`).
The official row is in `docs/speed-lane-reports/spec-lab/local_metal_ledger.jsonl`
carrying the full config, host info, and the runner's standard metrics JSON verbatim.

**Official tiny-tier run (LEDGERED):** classroom 960x540, 2 frames, kf=1, ref 512 /
draft 64, repair off —

| metric | value |
|---|---|
| net_speedup | **2.881x** |
| quality (global SSIM) | 0.9470 (per-frame 0.9464 / 0.9475) |
| worst_tile_ssim | 0.7989 (per-frame 0.7989 / 0.8160) |
| p5_tile_ssim | 0.8773 |
| T_ref | 69.06 s (per-frame 34.58 / 34.48) |
| T_stack | 23.97 s (per-keyframe 13.04 / 10.93) |
| modeled | **false** (fully measured; kf=1 + inpaint = no modeled step) |
| device | GPU/METAL |
| total driver wall | ~2.5 min once the scene is cached (~70 MB download on first run) |

**Smoke micro-tier run (NOT ledgered, `--no-ledger`; honest negative):** 480x270,
ref 64 / draft 16 → net_speedup **0.757x** (T_ref 8.77 s vs T_stack 11.59 s),
quality 0.8033, device GPU/METAL. At a 64-spp reference the anchor stack LOSES:
OIDN + guide passes are a near-fixed per-frame cost that dwarfs the tiny trace.
Real negative, kept — it maps where the anchor stack's win region STARTS on Metal.

Reading the official number honestly: 2.881x @ global 0.947 on Metal is the
tiny-tier anchor ratio, NOT the product number. It does not clear the published
0.95-tier gate (global >= 0.95 misses by 0.003; worst-tile 0.80 < 0.90) — at 512-spp
the "reference" is itself still noisy, so the SSIM comparison is against a noisier
truth than the 4096-spp cloud protocol, and per-frame fixed costs (Blender start +
BVH, ~2 s of every wall time) weigh ~6x heavier at 34 s frames than at 20+ min 4K
frames. What this run PROVES is the lane itself: the identical runner, protocol, and
honesty gates execute on fleet Apple Silicon with a real Metal GPU trace and produce
a fully-measured receipt. Scaling ref_spp/resolution toward the cloud tiers is now a
`--config-json` flag away, budgeted only in Mac-hours.

## Honest caveats

- **Protocol-comparable, not silicon-comparable.** The local number and the A100/H100
  numbers share the protocol (same runner, same scene, same SSIM grading) but the
  ratio is hardware-relative — cloud RUN 4 already showed the ratio moves between
  A100 and H100. A Metal ratio is a *fleet capability* number, not a refutation or
  confirmation of the cloud ones.
- **Tiny tier ≠ 4K tier.** 960x540 @ ref 512 is far below the 4K/4096 headline runs;
  fixed per-frame overhead (Blender start + BVH) is a larger fraction of every wall
  time here, which compresses net_speedup. Do not read the tiny-tier ratio as the
  product number.
- **OIDN on Apple Silicon** runs (Blender 4.2 ships it for arm64); an unavailable
  denoiser fails loud by the runner's own guard rather than mislabeling the anchor.
- Blender version skew: local 4.2.1 vs pod 4.2.0 (same LTS line).
- **Cold-cache probe flake (observed once, 2026-07-10):** the very FIRST functional
  GPU probe on this Mac — before Blender had ever compiled its Metal render kernels
  here — died with SIGABRT (rc=-6), and the runner correctly fail-louded
  ("refusing a CPU-fallback GPU benchmark") instead of falling back to CPU. The
  immediate rerun compiled the kernels ("Loading render kernels (may take a few
  minutes the first time)") and passed; every probe since passes in ~2 s. If the
  first run on a fresh fleet Mac errors this way, rerun once before investigating —
  the fail-loud is the guard working, not a broken lane.

## Usage

```bash
# the official tiny run (appends to the ledger):
python3 scripts/spec-lab/run_local_metal_anchor.py

# plumbing check only:
python3 scripts/spec-lab/run_local_metal_anchor.py --dry-run

# smoke / sweeps (no ledger append):
python3 scripts/spec-lab/run_local_metal_anchor.py --no-ledger --ref-spp 128 --draft-spp 32

# unit tests (no Blender needed):
python3 scripts/spec-lab/test_local_metal_anchor.py
```
