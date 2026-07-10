# Blender Cycles CUDA Kernel Matrix — sm_90 / Blackwell Research (2026-07-10)

Status: COMPLETE (Branch E of `GENERALIZATION_PLAN_2026-07-10.md`, L4 lever; research-only,
no code changed). Serves task #21: remove the H100 first-render JIT tax + decide the
Blackwell availability rung. Evidence classes used below:

- **MEASURED** — we downloaded/ran the artifact ourselves this session (tarball listings
  verified against official sha256; executed bpy audits; campaign timings from our ledgers).
- **SOURCE** — read from the Blender source at the exact release tag (GitHub mirror of
  `projects.blender.org/blender/blender`), line numbers cited.
- **INFERRED / UNVERIFIED** — labeled inline. A real negative beats a massaged positive.

## TL;DR

1. **No official Blender release has EVER shipped an sm_90 (Hopper) Cycles cubin** — not
   4.2, not 4.5 LTS, not 5.0. H100/H200 always run the shipped `kernel_compute_75.ptx`
   through the CUDA driver's JIT on first render. The H100 JIT tax cannot be removed by
   any version pin; it can only be **amortized** (persist the driver's JIT cache).
2. **Surprise finding: A100 (sm_80) has no cubin either.** The shipped set covers consumer
   arches only (…sm_86, sm_89). Our "A100 passes in seconds" observation is NOT explained
   by a precompiled kernel — see §5 (open question, $0.02 decomposition test proposed).
3. **First Blackwell-capable release: 4.4.0 (2025-03-18), consumer-only** — ships
   `kernel_sm_120.cubin` (RTX 5090 / RTX PRO 6000 Blackwell class). **Datacenter Blackwell
   (B200 sm_100 / B300 sm_103) has no cubin in ANY release through 5.0.1** — the B300 rung
   stays closed.
4. **OptiX does NOT sidestep missing cubins**: Cycles' OptiX device subclasses the CUDA
   device and loads the same CUDA module. `device=GPU/OPTIX` + one-time JIT delay is
   exactly the expected behavior (SOURCE, §4).
5. **Recommended action (ONE)**: pin `blender-4.5.11-linux-x64.tar.xz` for the render lane
   AND persist the CUDA JIT cache on the `/models` volume in the same change (§7).
   API risk: **zero breaks** on our exact bpy surface — proven by an EXECUTED 45-check
   audit on real 4.2.1 and 4.5.11 binaries (§6). 4.2 LTS support ends THIS MONTH
   (released 16-Jul-2024; LTS = 2 years).

## 1. What the current pin (4.2.0) actually ships

Our lane pins `blender-4.2.0-linux-x64.tar.xz` (`exp_render_stack.py:208-211`,
`exp_cycles_render_prod.py:101`). We downloaded that exact tarball and listed
`4.2/scripts/addons_core/cycles/lib/` — **MEASURED**:

```
CUDA cubins : sm_30 sm_35 sm_37 sm_50 sm_52 sm_60 sm_61 sm_70 sm_75 sm_86 sm_89
CUDA PTX    : kernel_compute_75.ptx.zst          (the ONLY JIT fallback)
OptiX PTX   : kernel_optix.ptx.zst + osl/raytrace variants  (no precompiled OptiX-IR)
AMD         : gfx90x/gfx10xx/gfx11xx fatbins (irrelevant to our ladder)
```

- **sm_90 (H100/H200): ABSENT. sm_80 (A100): ABSENT. sm_100/sm_120 (Blackwell): ABSENT.**
- Matches the release build config exactly: root `CMakeLists.txt` at tag v4.2.0 line 666
  sets `CYCLES_CUDA_BINARIES_ARCH = sm_30…sm_89 compute_75` (SOURCE), and
  `build_files/cmake/config/blender_release.cmake` enables `WITH_CYCLES_CUDA_BINARIES`
  without overriding the arch list (SOURCE) — so the CMake default IS the shipped set.
  Identical list at v4.2.11 → later 4.2.x patches (through 4.2.22) don't add kernels
  (SOURCE, HIGH confidence; 4.2.x tarballs beyond 4.2.0 not individually downloaded).
- Why sm_86/sm_89 cubins can't serve the A100: cubins are minor-version-forward only —
  "a cubin object generated for compute capability X.y will only execute on devices of
  compute capability X.z where z≥y" (NVIDIA CUDA C++ Programming Guide 12.4.1, Binary
  Compatibility). sm_80 < sm_86, so A100 gets NO cubin.

**Runtime selection logic** (SOURCE: `intern/cycles/device/cuda/device_impl.cpp` @ v4.2.0,
`CUDADevice::compile_kernel`, lines 258-283): try exact `lib/kernel_sm_{major}{minor}.cubin.zst`;
if absent, walk DOWN from the device's compute capability to the nearest
`lib/kernel_compute_{XY}.ptx.zst` and hand it to the driver JIT. For sm_80, sm_90, sm_100,
sm_103 alike, the file found is `kernel_compute_75.ptx.zst` → **first-use driver JIT**.

## 2. First release shipping each kernel

| arch | GPUs | first official release with a shipped cubin | evidence |
|---|---|---|---|
| sm_90 (Hopper) | H100, H200 | **NONE — never shipped, through 5.0.1** | CMake defaults at v4.2.0/v4.3.2/v4.4.0/v4.5.0/v5.0.0/v5.0.1 all lack sm_90 (SOURCE); tarballs 4.2.0/4.4.0/4.5.11 lack it (MEASURED) |
| sm_100/sm_103 (Blackwell datacenter) | B200 / B300 | **NONE — never shipped, through 5.0.1** | same evidence; PR text below shows the exclusion was deliberate |
| sm_120 (Blackwell consumer) | RTX 5090, RTX PRO 6000 Blackwell | **4.4.0 (released 18-Mar-2025)**; also in 4.5.x LTS and 5.0.x | `kernel_sm_120.cubin.zst` present in the 4.4.0 AND 4.5.11 tarballs (MEASURED); added by PR [blender/blender#134170](https://projects.blender.org/blender/blender/pulls/134170), merged 2025-02-10 into `blender-v4.4-release` |
| sm_89 (Ada: L40S/L4/4090) | our historical L40S band | already in 4.2.0 | tarball (MEASURED) |
| sm_86 (Ampere consumer: A40/A6000) | — | already in 4.2.0 | tarball (MEASURED) |
| sm_80 (A100) | A100 | **NONE — never shipped** | tarballs + CMake (MEASURED/SOURCE) |

PR 134170 body, verbatim (fetched via the projects.blender.org API): *"Enables building of
a Cubin for GPUs based on Blackwell architecture if CUDA toolkit version 12.8 or higher is
installed. Only added sm_120 to the default set, since it is the one relevant for consumer
GPUs (RTX 5090 etc.) that are generally used with Blender."* — datacenter Hopper/Blackwell
are explicitly not a Blender target; do not expect sm_90/sm_100 in future defaults.
Corroborated by the 4.4 release notes: "Support GeForce RTX 50x0 series (Blackwell)"
(developer.blender.org/docs/release_notes/4.4/cycles/). Build guard (SOURCE:
`intern/cycles/kernel/CMakeLists.txt` @ v4.4.0 line 616): sm_100/sm_101/sm_120 require
CUDA toolkit ≥ 12.8 at build time — and the shipped 4.4.0 tarball proves the buildbot had
it (sm_120 cubin present, MEASURED). Blender 5.0 drops Kepler (sm_30/35/37) but adds
nothing new for us (SOURCE, v5.0.0/v5.0.1 CMakeLists).

## 3. The JIT tax: mechanics and how to amortize it

NVIDIA CUDA C++ Programming Guide 12.4.1 (Just-in-Time Compilation + CUDA Environment
Variables), verbatim:

- The driver "automatically caches a copy of the generated binary code in order to avoid
  repeating the compilation in subsequent invocations" — the **compute cache** — and it
  "is automatically invalidated when the device driver is upgraded."
- `CUDA_CACHE_PATH` default on Linux: `~/.nv/ComputeCache`.
- `CUDA_CACHE_MAXSIZE` default 1073741824 (1 GiB) on desktop/server; max 4294967296 (4 GiB).
- Forward compat: "to be able to execute code on future architectures with higher compute
  capability... an application must load PTX code that will be just-in-time compiled for
  these devices" — this is exactly Blender's `compute_75` PTX strategy.

Consequences for our lane:

- The JIT is paid **once per pod** (cache lives in `~/.nv`, dies with the pod), which is
  why the 1500s `gpu_probe_timeout_s` headroom "fixed" H100: the probe render eats the JIT,
  then every subsequent render in the pod hits the cache. Our two H100 300s probe timeouts +
  subsequent pass with 1500s headroom (MEASURED, 2026-07-09 ledger) are fully consistent.
- **Amortization across pods**: point `CUDA_CACHE_PATH` at the persistent `/models` volume
  (e.g. `/models/spec-lab/nv-compute-cache`) and set `CUDA_CACHE_MAXSIZE=4294967296`.
  Caveat (honest): the cache invalidates on driver upgrade and is keyed per PTX/driver, so
  a fleet with heterogeneous driver versions re-JITs per driver version — still a large
  expected-case win on repeat H100/H200/A100 rentals. Cache safety on a shared volume is
  NVIDIA-managed (single-writer per pod in our one-driver-at-a-time policy).
- Building our own sm_90 cubins (Cycles source + nvcc) is possible but is a heavy new lane;
  not recommended while the cache-persistence lever is untried (it captures most of the win).

## 4. Does OptiX sidestep missing cubins? NO (and this reconciles our observation)

SOURCE, all at tag v4.2.0:

- `intern/cycles/device/optix/device_impl.h:66` — `class OptiXDevice : public CUDADevice`.
- `intern/cycles/device/optix/device_impl.cpp:240` — `OptiXDevice::load_kernels()` calls
  `CUDADevice::load_kernels(...)`: the OptiX device loads the SAME CUDA module (cubin or
  PTX-JIT) for the non-raytracing kernels, in addition to its own OptiX modules.
- The OptiX modules themselves ship as PTX (`kernel_optix*.ptx.zst`, MEASURED in tarball;
  no precompiled OptiX-IR) and are runtime-compiled per-arch by the driver's OptiX runtime
  (`optixModuleCreateFromPTXWithTasks`, lines 358-389), with the driver-side OptiX cache.

So on an sm_90 pod our runner truthfully reports `CX_CHOSEN_DEVICE=GPU/OPTIX` while the
first render still pays the CUDA `compute_75` PTX JIT (plus OptiX module compilation).
Observation reconciled — no contradiction, no silent CPU involvement (the fail-loud guard
verifies that separately).

## 5. Open question (honest): why did A100s probe "in seconds"?

A100 = sm_80 has NO cubin in 4.2.0 (MEASURED) and takes the same PTX-JIT path as H100
(SOURCE). Yet our campaign observed A100 probes passing in seconds while two fresh H100s
timed out at 300s (MEASURED, our ledgers). The kernel matrix does NOT explain the
asymmetry. Candidate explanations, all currently UNVERIFIED:

- (a) The A100 pods' first GPU touch wasn't the probe — pods reused across rungs already
  had a warm `~/.nv/ComputeCache` (our runners deliberately reuse `/root/blender` and
  scene caches; plausible from campaign history).
- (b) The driver's ptxas backend JITs `compute_75` PTX substantially faster to sm_8x than
  to sm_90 targets.
- (c) Driver-version differences between RunPod A100 and H100 hosts.

**$0.02 decomposition test** (next A100 pod, no extra rental): `rm -rf ~/.nv/ComputeCache`,
then time the 64x64@1spp probe cold, then again warm. If cold ≈ minutes, (a) is confirmed
and the §7 cache-persistence lever matters on A100 too; if cold ≈ seconds, (b)/(c).

Related single datapoint: the B300 (sm_103, datacenter Blackwell) incident — Blender 4.2
enumerated the GPU but the trace silently fell back to CPU ($0.58, no receipt; the origin
of the fail-loud guard). Per §3 forward-compat, `compute_75` PTX *should* JIT on Blackwell
with a current driver, so the failure mechanism is UNDECOMPOSED (possible driver/OptiX
interaction, possible very-long JIT read as a hang). With no sm_100/103 cubin in any
release (§2), B200/B300 stay excluded from the ladder regardless — do not spend to
decompose it now.

## 6. API-break audit for the 4.5.11 pin — EXECUTED, zero breaks

Method: extracted every `bpy` attribute the embedded scripts in
`scripts/spec-lab/pod/exp_render_stack.py` touch (`BLENDER_SCENE_SCRIPT` +
`GPU_PROBE_SCRIPT`), wrote a 45-check set-and-assert audit, and RAN it headless on real
binaries: local Blender **4.2.1** (our current macOS baseline) and **4.5.11** (official
`blender-4.5.11-macos-arm64.dmg`, sha256 verified `1fad76c7…04ca4f13`). Result — MEASURED:

```
4.2.1 LTS : CX_BPY_AUDIT_SUMMARY fails=0 total=45
4.5.11 LTS: CX_BPY_AUDIT_SUMMARY fails=0 total=45   (check-for-check identical output)
```

Surface covered (all PASS on both): `scene.cycles.{samples, seed, use_adaptive_sampling,
adaptive_threshold, adaptive_min_samples, use_light_tree, use_denoising, denoiser,
denoising_input_passes='RGB_ALBEDO_NORMAL', denoising_prefilter='ACCURATE', max_bounces,
diffuse_bounces, glossy_bounces, transmission_bounces, volume_bounces, device}`; cycles
preferences `{compute_device_type, get_devices(), get_devices_for_type, devices[].type/.use}`;
`view_layer.use_pass_{vector,z,combined,normal}`; `scene.render.{use_motion_blur,
resolution_x/y/percentage, filepath, use_border, use_crop_to_border, border_min/max_x/y}`;
`image_settings.{file_format='OPEN_EXR_MULTILAYER', color_mode='RGBA', color_depth='32',
exr_codec='ZIP'}`; `frame_start/end/frame_set`; camera `animation_data_clear /
keyframe_insert(location, rotation_euler)`; `bpy.ops.wm.open_mainfile` /
`bpy.ops.render.render`. Audit script: session scratchpad `bpy_surface_audit.py`
(reproducible; not committed — this branch is docs-only).

Notes from the audit + release-notes sweep (4.3/4.4/4.5 `python_api.md`, clean markdown via
the projects.blender.org API):

- `denoiser='OPTIX'` fails on macOS builds in BOTH versions (enum item is platform-
  conditional, Linux/Windows only) — expected, not a version break; our Linux pods have it.
- `view_layer.use_pass_denoising_data` exists in NEITHER 4.2 nor 4.5 (verified against the
  shipped 4.2.0 addon tree, MEASURED) — the script's `hasattr` guard already no-ops today;
  harmless legacy (Cycles wires denoising guides automatically from
  `denoising_input_passes`). Optional cleanup, zero urgency.
- CLI: `-noaudio` (which our runners pass) still registered at v4.2.0, v4.5.11 AND v5.0.1
  (`source/creator/creator_args.cc`, SOURCE) — no launch-flag break.
- 4.4's slotted-Actions rework changed direct Action *assignment* semantics only;
  `keyframe_insert` is unchanged (the release notes' own examples use it). We only
  keyframe-insert — unaffected.
- 4.5 breaking changes touch `gpu` module shaders, VSE, `frame_path()` — none on our surface.

**Render-OUTPUT changes (not API) that matter to receipts** — 4.4 changed bump-mapping
shading ("changes the look of some renders", 4.4 Cycles notes) and updated the OptiX
denoiser; therefore **4.2-rendered references are NOT comparable to 4.5 renders. Never mix
binaries inside one receipt; re-baseline banked references after the pin.** Our pipeline
renders ref+draft with the same binary per run, so it is structurally safe — the rule
applies to reuse of HISTORICAL banked EXRs. Also: 4.5 raised the minimum NVIDIA driver for
OptiX to 535 (4.5 Cycles release notes) — the existing fail-loud probe surfaces any
too-old-driver pod; RunPod images generally ship ≥535 (UNVERIFIED fleet-wide).

## 7. Recommended action (ONE)

**Pin `https://download.blender.org/release/Blender4.5/blender-4.5.11-linux-x64.tar.xz`
(sha256 `05ed7bd41bf3e61ae4f4a7cdc364c43088bf8b3fed702c2269c018fdf63a2188`, MEASURED
against the official `blender-4.5.11.sha256`) in both runner bootstrap constants
(`exp_render_stack.py:208-211`, `exp_cycles_render_prod.py:~101`), and in the SAME change
export `CUDA_CACHE_PATH=/models/spec-lab/nv-compute-cache` +
`CUDA_CACHE_MAXSIZE=4294967296` in the pod env so the sm_80/sm_90 PTX JIT is paid once per
driver version instead of once per pod. Keep `gpu_probe_timeout_s=1500` as the fail-safe.
Then re-baseline references before the next receipt (§6 output caveat).**

Why 4.5.11 and not alternatives: 4.2.x support ends this month (4.2.0 released 16-Jul-2024;
LTS = 2 years per the Blender release-cycle handbook) and never gains kernels; 4.4.x is
EOL non-LTS; 5.0.x has the same NVIDIA kernel set but a much larger API/behavior delta and
no LTS guarantee; 4.5 is the current LTS (4.5.11 released 23-Jun-2026, patched into
mid-2027), ships the sm_120 cubin, and is a proven zero-break drop-in for our scripts (§6).
URL pattern for future patches: `.../release/Blender4.5/blender-4.5.<N>-linux-x64.tar.xz`
(+ `blender-4.5.<N>.sha256` alongside).

What the pin does and does not buy:

| GPU | 4.2.0 today | on 4.5.11 |
|---|---|---|
| A100 (sm_80) | PTX JIT (see §5) | same (no sm_80 cubin exists) — cache persistence is the lever |
| H100/H200 (sm_90) | PTX JIT, 300s+ first render | same JIT, amortized by the cache env; NOT removed |
| L40S/L4/4090 (sm_89), A40/A6000 (sm_86) | native cubin | native cubin |
| RTX 5090 / RTX PRO 6000 Blackwell (sm_120) | no cubin (unsupported; unverified JIT behavior) | **native cubin — new availability rung unlocked** |
| B200 (sm_100) / B300 (sm_103) | silent-CPU incident (guarded now) | still NO cubin — **rung stays closed**; keep the Blackwell-reject guard for datacenter SKUs, relax it only for sm_120 SKUs |

GPU-policy memory update implied: "until a Blackwell-capable Blender ships" is now
satisfied for CONSUMER Blackwell by 4.4.0+/4.5.11 — the render ladder may add sm_120 SKUs
after the pin lands; datacenter Blackwell remains excluded.

## Sources

- Tarballs (MEASURED, listed this session): `download.blender.org/release/Blender4.2/blender-4.2.0-linux-x64.tar.xz`, `.../Blender4.4/blender-4.4.0-linux-x64.tar.xz`, `.../Blender4.5/blender-4.5.11-linux-x64.tar.xz` (+ official `.sha256` files; 4.5.11 Linux + macOS checksums verified).
- Build config (SOURCE, exact tags via `raw.githubusercontent.com/blender/blender`): root `CMakeLists.txt` `CYCLES_CUDA_BINARIES_ARCH` at v4.2.0 (l.666), v4.2.11, v4.3.0, v4.3.2, v4.4.0 (l.683, first sm_120), v4.5.0, v5.0.0, v5.0.1; `build_files/cmake/config/blender_release.cmake` at v4.2.0/v4.5.0; `intern/cycles/kernel/CMakeLists.txt` at v4.4.0 (l.616, CUDA 12.8 guard).
- Runtime logic (SOURCE, v4.2.0): `intern/cycles/device/cuda/device_impl.cpp` (compile_kernel l.258-283), `intern/cycles/device/optix/device_impl.h` (l.66), `intern/cycles/device/optix/device_impl.cpp` (l.240, l.358-389); `source/creator/creator_args.cc` at v4.2.0/v4.5.11/v5.0.1 (`-noaudio`).
- Blackwell PR: https://projects.blender.org/blender/blender/pulls/134170 (via Gitea API; merged 2025-02-10, base `blender-v4.4-release`).
- Release notes (projects.blender.org/blender/blender-developer-docs, raw API): `release_notes/4.4/cycles.md` (Blackwell, bump mapping, OptiX denoiser, sample subset), `release_notes/4.5/cycles.md` (OptiX min driver 535), `release_notes/4.3|4.4|4.5/python_api.md` (breaking-changes sweep); release-cycle handbook (LTS 2 years): developer.blender.org/docs/handbook/release_process/release_cycle/.
- NVIDIA CUDA C++ Programming Guide 12.4.1 (docs.nvidia.com/cuda/archive/12.4.1/cuda-c-programming-guide/): Binary Compatibility (cubin X.y→X.z z≥y), Just-in-Time Compilation (compute cache, driver-upgrade invalidation), CUDA Environment Variables (`CUDA_CACHE_PATH` `~/.nv/ComputeCache`, `CUDA_CACHE_MAXSIZE` default 1 GiB / max 4 GiB), forward-compat PTX JIT statement.
- Release dates (download.blender.org listings): 4.2.0 16-Jul-2024; 4.4.0 18-Mar-2025; 4.5.0 15-Jul-2025; 5.0.0 18-Nov-2025; 5.0.1 16-Dec-2025; 4.2.22 & 4.5.11 both 23-Jun-2026.
- Our campaign ledgers (MEASURED): two H100 SECURE 300s probe timeouts + pass at 1500s headroom; A100 probes passing in seconds; B300 silent-CPU incident ($0.58) — `CONSOLIDATION_PLAN_2026-07-09.md` step-1 log.

## Confidence summary

| claim | confidence |
|---|---|
| 4.2.0 ships no sm_80/sm_90/Blackwell cubin; only compute_75 PTX fallback | HIGH (MEASURED tarball + SOURCE) |
| No release through 5.0.1 ships sm_90 or sm_100/sm_103 | HIGH (SOURCE at every tag; MEASURED at 4.2.0/4.4.0/4.5.11) |
| 4.4.0 is the first release shipping sm_120 (consumer Blackwell) | HIGH (MEASURED tarball + PR + release notes) |
| OptiX device still requires the CUDA module → JIT applies under GPU/OPTIX | HIGH (SOURCE, direct code read) |
| H100 300s = one-time compute_75→sm_90 driver JIT, amortizable via CUDA_CACHE_PATH | HIGH mechanism (SOURCE+NVIDIA docs+our timings); cache-persistence win UNVERIFIED until tried |
| A100-fast asymmetry explanation | OPEN — hypotheses only, $0.02 test defined (§5) |
| B300 failure mechanism | UNDECOMPOSED (single datapoint); rung closed on kernel absence regardless |
| 4.5.11 pin = zero bpy API breaks for our scripts | HIGH (EXECUTED 45-check audit on both real binaries) |
| RunPod fleet drivers ≥535 for 4.5 OptiX | UNVERIFIED (probe fail-loud covers it) |
