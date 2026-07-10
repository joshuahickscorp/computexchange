# Render Lane as a SpecEngine Instance + Integrated-Benchmark Device-Bug Fix (Branch B)

Status: **DONE this wave** — design + local build only. No cloud provisioned, no money spent,
`scripts/spec-lab/.tracked_pods.json` is still `[]`. All writes are confined to
`scripts/spec-lab/` and this doc. Every file touched `py_compile`s clean; **14 local unit tests
pass** (7 adapter + 3 core + 4 integrated); the driver dry-run and the adapter dry-run both run
locally with zero hardware. See the "Verification (real command output)" appendix.

This serves the canonical plan `docs/research/CONSOLIDATION_PLAN_2026-07-09.md`:

```
SpecUnit -> DraftProducer -> Verifier -> AcceptancePolicy -> RepairPolicy -> SpecReceipt
```

Branch B expresses the **render lane** (the proven Cycles low-spp / tile / SSIM-gate mechanism)
through that contract, and fixes the device-selection bug that let the integrated benchmark burn
money on a CPU fallback with no receipt.

## Files

- **New** — `scripts/spec-lab/cx_render_spec_adapter.py` — the render adapter + honest bridges +
  a local `dry_run()` entrypoint.
- **New** — `scripts/spec-lab/test_cx_render_spec_adapter.py` — 7 SYNTHETIC unit tests (no GPU,
  no Blender): accounting logic + the canonical-contract shape.
- **Edited** — `scripts/spec-lab/run_integrated_production_benchmark.py` — the GPU-plan
  constraint + the Blackwell-rejecting `parse_gpu_plan()` + the `--allow-unsupported-gpu` hatch.
- **Edited** — `scripts/spec-lab/pod/exp_render_stack.py` — the fail-loud GPU enforcement (this is
  the runner the integrated driver actually calls; see the correction below).
- **This doc** — `docs/research/RENDER_SPEC_BRIDGE_DESIGN.md`.

`scripts/spec-lab/` is untracked in git (its `.gitignore` excludes only `.tracked_pods.json`), so
the new/edited runner code lives correctly inside the assigned path.

### Correction to the task framing (which runner has the bug)

The integrated driver does **not** call `pod/exp_cycles_render_prod.py`. It calls
**`pod/exp_render_stack.py`** (`run_integrated_production_benchmark.py:95` →
`cd /root/spec-lab && python3 pod/exp_render_stack.py '<json>'`). The device-selection code that
caused the B300 burn lives in `exp_render_stack.py`, so that is where the fix lands.
`exp_cycles_render_prod.py` carries the **same latent bug** but is out of the integrated path —
flagged as out-of-scope below, not fixed this wave.

---

## Deliverable 1 — Render adapter in the SpecEngine shape

Built as real, compiling, unit-tested code in `cx_render_spec_adapter.py`.

### The mapping

| SpecEngine trait | Render meaning |
|---|---|
| **SpecUnit** | a render unit — a tile (H×W pixel block) or a whole frame |
| **DraftProducer** | the cheap path: a low-spp (+adaptive+OIDN+guides+light-tree) trace, or a reprojected/warped tile → `draft_s` (MEASURED wall-clock) |
| **Verifier** | SSIM of the draft vs the reference/high-spp tile — global + worst-8×8-tile + p5 → `verify_s` (measurement time) |
| **AcceptancePolicy** | `QualityTier.clears()`: a unit passes iff `global ≥ 0.98 AND worst_tile ≥ 0.95` (optional p5 gate) |
| **RepairPolicy** | re-render the failed unit at reference quality → `repair_s` |

The acceptance thresholds are kept **in lockstep** with `cx_integrated_speculation.DELIVERY_GLOBAL`
/ `DELIVERY_WORST_TILE` (both `0.98` / `0.95`; a runtime assert in the verification appendix proves
they match), so the render lane and the integrated receipt gate on the same numbers.

### The canonical SpecReceipt contract (matches Branch A by CONTRACT, not by import)

`RenderSpecReceipt.to_dict()` exposes exactly the plan's canonical field set:

```
draft_cost, verify_cost, accepted_fraction, repair_cost,
total_product_time, quality_tier, speedup_vs_baseline
```

plus honesty fields (`baseline_cost, evidence, quality_gate, delivery_eligible,
global/worst/p5 ssim, claim_scope`). A module-level `assert_canonical()` guards the contract (used
by the tests, the dry-run, and available to the driver). The headline rule is enforced
**structurally**:

```
speedup_vs_baseline = baseline_cost / total_product_time     # ONE measured ratio
```

never a product of per-tile ratios. `baseline_cost` is a **real single-lane** reference-quality
render of the same delivered unit — the honest denominator. This wave the module does **not**
import Branch A's `spec-engine/` crate; it matches the schema by contract so render + token
receipts compose in the plan's staged-multiplier table (a code merge into `spec-engine/` is a
sequenced follow-up).

### Three honest bridges (usable now, composable later)

- **`receipt_from_measurements(units)`** — turns per-tile/frame `TileMeasurement`s into a canonical
  receipt. A unit that clears the tier is accepted at draft+verify cost; a unit that fails is
  repaired (its `repair_s` re-render is charged) and then delivered at reference quality.
- **`from_stack_metrics(metrics)`** — maps a **real** `exp_render_stack.py` output onto the
  canonical receipt: the whole animated shot is one delivered unit, `baseline_cost = T_ref_s`,
  `total_product_time = T_stack_s`, `speedup = T_ref_s / T_stack_s = net_speedup`. The
  disocclusion crop is **already inside** `T_stack`, so it is reported as `repair_cost` but not
  re-added — `total_product_time` stays `== T_stack` (no double-charge). `evidence = MODELED` iff
  `metrics['modeled']`.
- **`from_speculative_receipt(core.SpecReceipt)`** — proves the shared `cx_speculative_core`
  engine output IS the same canonical shape (render/token parity).
- **`build_engine(...)`** — a live `SpeculativeEngine` instance over render callables (the
  modality-general trait shape, instantiated for pixels).

### Honesty discipline (load-bearing)

Every second carries `MEASURED` (real Cycles wall-clock) / `MODELED` (a derived cost, e.g. the
area-scaled crop re-render) / `SYNTHETIC` (a fixture / dry-run number). A receipt with **any**
MODELED cost is **not** `delivery_eligible` — it PARKS, mirroring
`cx_integrated_speculation.RenderVerifier`. `_combine_evidence()` makes a shot only as clean as its
dirtiest unit (SYNTHETIC dominates, then MODELED).

### Local dry-run entrypoint (no cloud, no Blender, no money)

`python3 cx_render_spec_adapter.py` runs `dry_run()`, which exercises all three paths on SYNTHETIC
inputs and prints the canonical receipts under a money-safety banner
(`cloud_touched: false, blender_invoked: false, evidence: ALL SYNTHETIC`). It demonstrates:

1. a per-tile receipt (3 accept, 1 repaired) → headline `= baseline / total`, one ratio;
2. a classroom-shaped integrated receipt with `modeled: true` → **PARKS**
   (`delivery_eligible: false`), `total_product_time == T_stack` (crop not double-charged) — the
   seconds are an arbitrary fixture, so the ratio is **not** any published headline;
3. the live shared engine → same canonical shape (render/token parity).

All three pass `assert_canonical()`. Because a local Blender/GPU is not available (and would cost
money on cloud), the dry-run uses SYNTHETIC inputs by design — it proves the **shape**, never a
performance number.

---

## Deliverable 2 — Diagnosis + fix of the integrated-benchmark device bug

### Root cause

Blender 4.2 (pinned at `exp_render_stack.py:149-152`, `blender-4.2.0-linux-x64`) predates Blackwell
and ships **no Cycles CUDA/OptiX kernel** for `sm_100` (B200/B300) or `sm_120` (RTX 50xx); its
kernels cap at Hopper `sm_90` / Ada `sm_89`. The failure had **three compounding defects**, all of
which the old code let pass silently:

1. **Device chosen by ENUMERATION, not kernel-load.** The in-Blender ladder only called
   `prefs.get_devices()` and checked `d.type != 'CPU'`. On a B300 the GPU *enumerates*, so the
   ladder "passed" and printed `CX_CHOSEN_DEVICE=GPU/OPTIX` **before** any pixels were traced. The
   kernel then failed to load at `bpy.ops.render.render()` time and Cycles silently fell back to
   CPU. The sentinel reflected the *requested* device, not the device the samples ran on.
2. **CPU fallback was a NOTE, not a failure.** `fell_to_cpu` only appended `" NOTE: ran on CPU"` to
   the metrics note and **still emitted a receipt** — the "$0.58 burned, silent CPU render" path.
3. **The GPU plan actively steered INTO the broken SKU.** `DEFAULT_GPU_PLAN` ended
   `…,NVIDIA B200,NVIDIA B300 SXM6 AC`, and the monotonic ladder *escalates on capacity failure*,
   so exhausting A100/H100/H200 marched it straight onto Blackwell.

Honest caveat: the exact internal mechanism (enumerate-then-kernel-fail vs. enumerate-empty) cannot
be disambiguated from the artifacts — the B300 stderr was never ledgered (itself a gap). Both
mechanisms share this root cause and this fix; the fix is robust to both.

### The fix (implemented — exact locations)

**(b) Constrain the GPU plan — `run_integrated_production_benchmark.py`:**

- `:33-43` — a `HARD DEVICE CONSTRAINT` comment stating the sm-level rationale.
- `:44` — `DEFAULT_GPU_PLAN` is now `A100 80GB PCIe → H100 80GB HBM3 → H200` (all ≤ sm_90;
  Blackwell dropped).
- `:48-51` — `UNSUPPORTED_GPU_MARKERS` (`B200, B300, GB200, GB300, BLACKWELL, RTX 5090/5080/5070,
  RTX PRO 6000 B`). Substring-safe: `H200` / `A100` / `L40S` never match.
- `:62-80` — `parse_gpu_plan(..., allow_unsupported=False)` raises a clear, bug-citing `ValueError`
  on any Blackwell SKU (this also guards manual `--gpu-plan` overrides).
- `:150-154, :157` — a `--allow-unsupported-gpu` escape hatch (default off; only after a
  Blackwell-capable Blender is proven on-box).

**(a) Verify GPU and FAIL LOUD — `pod/exp_render_stack.py`:**

- `:417` + `:582-593` — the in-Blender scene script reads `CX_REQUIRE_GPU`; when set and no usable
  GPU device is selected, it prints `CX_DEVICE_ERROR=…` and `raise SystemExit(3)` **before** the
  costly trace. CPU devices are already disabled in the ladder (`:565`), so if the GPU kernel then
  fails to load, Cycles has no CPU to fall back to and the render errors instead of silently
  tracing on CPU.
- `:752-808` (`GPU_PROBE_SCRIPT`) + `:811-855` (`require_gpu_probe`) — upgraded from
  enumeration-only to a **functional micro-render**: 64×64 @ 1 spp on the GPU with CPU disabled,
  requiring `rc==0 AND CX_GPU_PROBE_RENDERED=1 AND device!=CPU AND output file exists`. This
  catches the enumerate-but-no-kernel case for pennies, **before** the reference frame. Fails
  closed.
- `:658-749` — `run_blender_frame(..., require_gpu=False)` threads the flag, sets `CX_REQUIRE_GPU`
  (`:689`), parses the `CX_DEVICE_ERROR=` sentinel and raises a clean reason (`:730-733`), and
  refuses any frame whose reported device is `CPU`/`unknown` under `require_gpu` (`:736-740`).
- `:1300, :1358, :1400` — `require_gpu` threaded into all three render call sites (reference,
  anchor, calibration), and `require_gpu_probe()` is invoked before the reference frame
  (`:1235-1236`).
- `:1520-1525` — final belt-and-suspenders: under `require_gpu`, a CPU-containing device set raises
  instead of emitting metrics — no CPU-rendered receipt ever leaves the runner.

The driver already sends `require_gpu: True` (`run_integrated_production_benchmark.py:163`), and
`exp_render_stack.py:1195` parses it, so the enforcement binds **end-to-end**.

---

## The EXACT scene set the next cloud run will report (honest, not cherry-picked)

Two distinct render lanes feed the canonical receipt; both report on the sets below, every row
labeled.

### Lane 1 — single-frame low-spp / SSIM-gate (already measured, live L40S)

Source: `docs/research/RENDERER_FRONTIER_ITERATIVE_GOAL_PROMPT_2026-07-09.md:30-49` — live RunPod
`NVIDIA L40S`, delivery tier (global ≥ 0.98, worst-tile ≥ 0.95). Reference is 4096 spp throughout.
The **headline is the hard/representative band**; the forgiving scene is reported as a labeled
convergence-trivial outlier, never as the number.

| scene | draft | speedup vs 4096 | label |
|---|---|---|---|
| `scene_cube_volume.xml` | 32 spp | **7.8721×** | MEASURED — representative ceiling |
| `scene_monkey.xml` | 32 spp | **6.88×** | MEASURED — representative |
| `scene_sphere_bump.xml` | 16 spp | **4.5932×** | MEASURED — representative |
| `cx_many_glass.xml` | 8 spp | **3.8656×** | MEASURED — representative (hard floor) |
| `scene_world_volume.xml` | 2 spp | 14.3372× | MEASURED — **convergence-trivial outlier, labeled, NOT the headline** |

Honest representative band: **3.87× – 7.87×**. A `receipt_from_measurements` receipt over these
tiles is MEASURED and delivery-eligible.

### Lane 2 — integrated animated compound stack (NONE-YET, this is the target)

The device-fixed `run_integrated_production_benchmark.py` default job:

- scene **classroom**, **3840×2160**, **4 frames**, keyframe_every 4, bounces 12;
- **ref_spp 4096**, **draft_spp 512**, anchor stack = adaptive + OIDN + albedo/normal guides +
  light-tree; hole_fill `rerender` (so `modeled: true`).

Its combined `net_speedup = T_ref_s / T_stack_s` is the **first honest combined render receipt** and
does **not exist yet** — it is `NONE-YET` in the plan's staged table and will only exist after the
sequenced GPU run. Because `rerender` mode carries the one MODELED crop step, that receipt PARKS
(not delivery-eligible) until an `inpaint`/`nearest` (fully measured) variant clears the gate. No
number here is invented, and Lane-1 multipliers are **not** multiplied into Lane-2.

---

## Verification (real command output)

```
$ python3 -m py_compile cx_render_spec_adapter.py test_cx_render_spec_adapter.py \
      run_integrated_production_benchmark.py pod/exp_render_stack.py
PY_COMPILE OK (4 files)
# embedded Blender scripts compiled via compile(m.BLENDER_SCENE_SCRIPT|GPU_PROBE_SCRIPT):
EMBEDDED SCRIPTS COMPILE OK (BLENDER_SCENE_SCRIPT + GPU_PROBE_SCRIPT)

$ python3 -m unittest test_cx_render_spec_adapter      # Ran 7 tests ... OK
$ python3 -m unittest test_cx_speculative_core         # Ran 3 tests ... OK
$ python3 -m unittest test_cx_integrated_speculation   # Ran 4 tests ... OK
# lockstep tier: integrated (0.98, 0.95) == adapter (0.98, 0.95) -> LOCKSTEP OK

# --- driver dry-run matrix (money-safe; runpod.py is import-safe, --dry-run returns
#     before any cloud call) ---
[1] default:            gpu_plan = A100 80GB PCIe / H100 80GB HBM3 / H200
                        require_gpu = True ; device = GPU        -> PASS
[2] --gpu-plan "...B300 SXM6 AC...":  exit=1
    ValueError: GPU 'NVIDIA B300 SXM6 AC' matches unsupported-arch marker(s) ['B300']:
    Blender 4.2 has no Cycles kernel for Blackwell (sm_100/sm_120) and silently renders
    on CPU there (this is the 2026-07-09 B300 $0.58-no-receipt bug)...   -> PASS (rejected)
[3] ...B300... --allow-unsupported-gpu:  exit=0                  -> PASS (escape hatch)
[4] H200 / A100 / L40S:                  exit=0                  -> PASS (no false-flag)

# --- adapter local dry-run (no cloud, no Blender) ---
$ python3 cx_render_spec_adapter.py
{ "cloud_touched": false, "blender_invoked": false, "money_safe": true,
  "evidence": "ALL SYNTHETIC ...",
  "receipts": [
    { per-tile:  speedup_vs_baseline 2.580645, delivery_eligible true  },  # 32.0 / 12.4
    { classroom: speedup_vs_baseline 4.0, evidence MODELED, delivery_eligible false,
                 total_product_time 30.0 == T_stack },                     # PARKS (modeled)
    { live-engine: same canonical shape, evidence SYNTHETIC } ] }
```

`scripts/spec-lab/.tracked_pods.json` = `[]` before and after (no pod provisioned).

---

## Next cloud experiment (sequenced — do NOT run this wave)

`python3 run_integrated_production_benchmark.py` with the default (now Blender-4.2-safe) plan →
`require_gpu_probe()` functionally gates the reference frame on a real GPU → the first honest
combined render receipt (`T_ref_s / T_stack_s`), every row labeled measured/modeled. Run it money-
safe (arm_remote_watchdog + register_cleanup + verify `.tracked_pods.json` empty before/after), one
driver at a time. **Kill clause:** if no honest combined workload beats the best single-lane 7.87×,
report that plainly rather than massaging it.

## Out-of-scope flag (not touched this wave)

`pod/exp_cycles_render_prod.py` carries the identical enumeration-only silent-CPU-fallback pattern
(around its device-selection block). If any other driver renders GPU-required jobs through it, port
the same `require_gpu` / functional-probe guards before doing so.
