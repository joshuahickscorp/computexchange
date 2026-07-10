# Cross-Architecture Consistency Gates — Metal vs CUDA (2026-07-10)

Status: BUILT + stage 1 MEASURED locally. Branch B of `GENERALIZATION_PLAN_2026-07-10.md`
(objective 2, "Any silicon"). Predecessor context: `CONSOLIDATION_PLAN_2026-07-09.md`
(strict-delivery result + the honest number model this doc obeys).

## Why

The CX fleet is Apple Silicon — Cycles renders on **Metal**. Rented GPUs are **CUDA**.
Every product flow we care about mixes them: a Mac drafts and a CUDA box verifies, or a
CUDA box produces the reference a Mac-delivered frame is graded against. The strict
delivery gate (global SSIM >= 0.98, **worst-tile SSIM >= 0.95** — earned 2026-07-10,
DECISION=GROW) was measured on ONE architecture at a time. Cycles is **not** expected to
be byte-identical across device kernels (different compilers, float ordering, intrinsic
implementations). The honest questions are:

1. **How close are the two architectures, per grading tile**, on an identical
   hash-pinned config?
2. Does the cross-arch worst tile **clear the delivery gate** (>= 0.95)?
3. Is the cross-arch delta **within the Monte-Carlo noise floor** of one architecture
   reseeding itself — or is it a **systematic kernel bias**?

Without these numbers, any "Mac drafts / CUDA verifies" delivery claim is unearned.

## The three-stage gate tool

Code (Branch B's assigned paths, all built this wave):

- `scripts/spec-lab/pod/exp_cross_arch_gate.py` — the runner (works on a CUDA pod AND
  locally via the shim; reuses `exp_render_stack.py` verbatim for Blender bootstrap,
  scene cache, the deterministic camera path, money-safe render guards, EXR reader and
  the 8x8 grading-grid SSIM — no divergent copy).
- `scripts/spec-lab/run_cross_arch_gate.py` — driver with one subcommand per stage
  (`local` / `cuda` / `report`).
- `scripts/spec-lab/test_cross_arch_gate.py` — 44 local unit tests (no Blender, no
  cloud, no money): hashing, manifest tamper-refusal, kwargs parity, gate logic on
  synthetic matrices, real-SSIM-path tiling, driver plumbing, report assembly.
- Ledger: `docs/speed-lane-reports/spec-lab/cross_arch_gate_ledger.jsonl`.

### What is rendered (the canonical recipe)

`exp_render_stack.py`'s EXACT **reference recipe** (`run_blender_frame(is_ref=True)`):
fixed spp, adaptive OFF, denoiser OFF, guides OFF, light-tree left at the scene's
`.blend` default, the same deterministic camera path. No denoiser means no OIDN-backend
confound — what is compared is **the path tracer itself**. The anchor-stack (denoised
draft) cross-arch comparison is a named FOLLOW-UP, not silently mixed into this gate.

Tiny canonical config (finishes in minutes, $0 locally): `classroom` @ 960x540,
frame 1/2, **ref_spp 512**, bounces 12, cam_motion 1.0, seeds (0, 12345).

### Stage 1 — `local`: Metal-vs-Metal self-consistency (LOCAL, $0, RUN FOR REAL)

Three renders of the identical config on this M3 Pro's Metal GPU, each a fresh Blender
subprocess (fail-loud `require_gpu`; a receipt is refused unless the traced device is
Metal):

| render | seed | purpose |
|---|---|---|
| A | 0 | the canonical render (exported) |
| A2 | 0 | SAME-SEED repeat -> determinism **ceiling** (+ pixel-exact check) |
| B | 12345 | CROSS-SEED -> Monte-Carlo noise **floor** at this spp |

Per-tile SSIM on the SAME 8x8 grading grid as the delivery gate (same `_tone`, same
`_tile_rects`). Stage 1 also **exports the canonical EXR (render A) + a tamper-evident
manifest**: canonical config (9 knobs), `config_hash` (sha256, key-order/type
normalized), EXR sha256, producer arch/device/Blender version, and the measured
baselines. Export lands in `~/.cache/cx-spec-lab/cross_arch_export/<config_hash>/`.

### Stage 2 — `cuda`: the replica half (scaffold built; FIRED LATER by the orchestrator)

`run_cross_arch_gate.py cuda` is the standard money-safe driver (the adversarially
verified pattern of `run_reference_consistency_probe.py`): $-estimate printed before
anything cloud-touching, refuses to start if any tracked/live pod exists (one driver at
a time), `register_cleanup()` before provision, `arm_remote_watchdog()` FIRST on the pod
(TTL 2h), policy GPU ladder A100->H100->H200 (Blackwell rejected — no Blender 4.2
kernel), replica run detached (`ssh_detached`, peer-reset-proof), teardown in `finally`
+ tracked-pods-empty assertion. `--dry-run` prints the full manifest + estimate with
**zero** RunPod API calls.

Pod-side (`mode="replica"`): validates the shipped manifest BEFORE any spend
(`config_hash` recomputed, EXR sha256 matched — a tampered/corrupted export refuses),
re-renders the IDENTICAL config (same `seed_a`; the render kwargs are built by the SAME
function `render_kwargs_from_config` that built the producer's — structural parity),
scores per-tile SSIM(shipped Metal EXR, CUDA replica) = the **cross-arch delta**, plus
(default on) one seed_b render for the verifier-side cross-seed floor.

**Cost estimate (MODELED from MEASURED basis, printed by the driver):** 2 renders x
<= 60s (M3 Metal measured 34.6s/render at this config; every measured A100 render of
this scene beat the M3 at equal settings) + 8-18 min pod overhead (measured band:
provision + Blender tarball + scene + pip + functional GPU probe) => ~9-20 min =>
**~$0.47 on the A100 rung** (worst-case ladder climb to H100: ~$1.01). Never a receipt.

### Stage 3 — `report`: the gate verdict (pure, unit-tested)

`gate_report()` combines the halves (refusing halves whose `config_hash` differ):

- **`gate_pass` = cross-arch worst-tile SSIM >= 0.95** (`DELIVERY_WORST_TILE`, mirrored
  from `cx_integrated_speculation` with a unit test pinning the mirror). The gated
  comparison is the SAME-SEED cross-arch pair — exactly the "Mac produced the
  reference, CUDA reproduces it" delivery question.
- The same-seed same-arch baseline is the comparison **ceiling**; the cross-seed
  same-arch baseline is the **floor**. Cross-arch between them = the architectures
  behave like different noise realizations of the same estimator. Cross-arch **below
  the floor** = `systematic_arch_bias_suspected` (worse than reseeding) — flagged
  explicitly even when the gate still passes.
- If the same-arch cross-seed floor itself fails the gate at this spp, a cross-arch
  fail is labeled noise-dominated: `gate_pass=false AT THIS CONFIG`, with the honest
  note that the arch-vs-noise split needs a higher-spp canonical render.
- Until stage 2 runs, status is `PENDING-CUDA-HALF` and `gate_pass` is `null` — the
  same-arch half never predicts the cross-arch number.

## MEASURED FINDING (2026-07-10, local, $0): `cycles.seed` is INERT on Blender 4.2.1

The first stage-1 run returned cross-seed worst-tile SSIM 1.0 — suspicious, so it was
diagnosed on raw pixels before being believed:

- At the canonical config (512 spp), seed 0 vs seed 12345 renders differ by
  **max|d| = 1.43e-06** (float32 epsilon) — the same magnitude as the same-seed
  run-to-run jitter (1.19e-06). MEASURED.
- Decisive control at **8 spp** (visibly noisy: 3x the pixel gradient energy of
  512 spp, mean|d| = 0.088 vs the converged frame): seed 0 vs seed 777 still differ by
  only **max|d| = 2.4e-07**. The noise is real; the seed does not re-realize it.
  MEASURED.
- Holds for **every sampling pattern this build offers**: AUTOMATIC, TABULATED_SOBOL,
  BLUE_NOISE (Blender 4.2.1; SOBOL_BURLEY is not in the 4.2 enum). MEASURED.

Consequences, wired into the tool (not just documented):

1. The "cross-seed Monte-Carlo noise floor" is **DEGENERATE on this build** — it
   re-measures determinism, not statistics. The runner detects this structurally
   (`classify_seed_effect` on raw max-abs diffs: float-jitter vs >= ~1e-2 real MC
   re-realization) and flags `degenerate_seed_inert` in the manifest baselines.
2. `gate_report` refuses to compute `cross_arch_within_same_arch_noise` /
   `systematic_arch_bias_suspected` from a degenerate floor (it would have classified
   ANY real cross-arch delta as "systematic bias"). With an inert seed, the same-arch
   baseline is the **determinism ceiling only**, and any cross-arch gap below it is a
   kernel-level difference by construction. gate_pass semantics are unchanged.
3. **Retro-implication for the banked reference-consistency probe** (4K/4096spp,
   CUDA, 2026-07-10, "SSIM 1.0 — the gate is REAL"): different-seed renders coming out
   pixel-identical there is the SAME inert-seed behavior — that probe measured
   run-to-run **reproducibility**, not statistical convergence of the reference.
   Operationally its conclusion survives (the delivered frame is graded against THE
   reference, and re-rendering the reference reproduces it), but "the reference is
   converged" was never actually tested. Flagged to the orchestrator; that probe is
   outside this branch's write paths.

## Stage 1 result — MEASURED 2026-07-10 (local M3 Pro, Metal GPU, Blender 4.2.1, $0)

Ledger: `cross_arch_gate_ledger.jsonl` (`event=cross_arch_gate_local_selfconsistency`,
evidence `MEASURED/local-metal`); the corrected (seed-inertness-aware) row is the
operative one.

| measurement | value | meaning |
|---|---|---|
| same-seed (A vs A2) worst-tile / global SSIM | **1.0 / 1.0** (unrounded) | determinism ceiling: same config+seed reproduces itself across fresh subprocesses |
| same-seed pixel-exact | **False** — max abs diff 1.43e-06 (run 1: 1.19e-06) | Metal is deterministic only to float epsilon (atomics/scheduling), invisible to SSIM |
| cross-seed (A vs B) worst-tile / global SSIM | 1.0 / 1.0, max abs diff 1.19e-06 — **DEGENERATE (seed inert)** | NOT a noise floor; flagged `degenerate_seed_inert` |
| render walls (3 x 512spp @ 960x540) | ~29-33 s each | matches the 34.6s local-metal basis |
| canonical export | EXR (sha256-pinned) + manifest at `~/.cache/cx-spec-lab/cross_arch_export/<config_hash>/` | what the CUDA half reproduces |

The useful reading: on this Blender build the same hash-pinned config is **bitwise-
stable to ~1e-06 on one architecture**. Therefore the upcoming cross-arch delta is
attributable to kernels, not sampling — an unusually clean experiment, courtesy of the
inert seed.

## Honesty contract

- Every number is labeled MEASURED / MODELED / SYNTHETIC; the runner emits
  `modeled: false` only on real pixels.
- The gate (0.95) is the strict delivery constant — never loosened here; a cross-arch
  FAIL is reported as a fail with its decomposition, not massaged.
- `require_gpu` is fail-loud end to end (functional 64x64@1spp probe with CPU devices
  disabled; every render refuses CPU fallback; the local driver refuses to ledger a
  non-Metal device).
- LOCAL-ONLY wave: the `local` and `report` subcommands structurally cannot touch the
  RunPod API (`runpod` is imported ONLY inside `cmd_cuda`; unit-tested). Stage 2 is
  sequenced by the orchestrator, one pod driver at a time.
- The cross-arch comparison is valid ONLY because the manifest is hash-pinned and both
  halves build their render kwargs through one shared code path; both are enforced in
  code and unit-tested, not just documented.

## Known limits / follow-ups (named, not hidden)

1. **Reference recipe only.** The anchor stack (adaptive + OIDN + guides + light-tree)
   adds denoiser-backend and adaptive-scheduling confounds; measuring its cross-arch
   delta is a separate follow-up once the reference-level gate number exists.
2. **One scene, one config.** A gate number at classroom/960x540/512spp does not
   certify 4K/4096spp; the scene-sweep branch (A) owns config diversity. The tool takes
   arbitrary configs — re-run with a bigger config when the budget says so.
3. **spp-limited floor.** At 512 spp the cross-seed floor may itself sit below 0.95;
   the report says so explicitly (noise-dominated verdict) rather than letting a small
   config fake a cross-arch failure (or pass).
4. **Same Blender version on both sides** (4.2.x) is assumed and recorded in the
   manifest; a version-skew gate (4.2 vs newer sm_90-capable builds, Branch E) would be
   a config knob on top of this tool, not a rewrite.
