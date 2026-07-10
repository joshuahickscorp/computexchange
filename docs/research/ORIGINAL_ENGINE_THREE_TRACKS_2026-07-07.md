# Original-engine research: three tracks, investigate → plan → cross-track synthesis
### 2026-07-07 — owner ask: "build more of our own original code, rely less on named frameworks (Blender/Cycles/OIDN/ffmpeg)"

Produced by a 10-agent research wave (6 investigate: 1a/1b, 2a/2b, 3a/3b, WebSearch/WebFetch-heavy;
3 per-track plan syntheses; 1 cross-track synthesis). Zero GPU spend — pure research. Full agent
transcripts: workflow `w3fza52xm`, journal at
`~/.claude/projects/-Users-scammermike-Downloads-computexchange/8164f232-80d5-4d22-b50e-13e2599671ce/subagents/workflows/wf_761b2f72-280/journal.jsonl`.

---

## TRACK 1 — Fork Cycles: validated path reuse (ReSTIR) + GPU-native path guiding

**Verdict:** worth pursuing, gated behind a near-free Phase 0. Does NOT resurrect "skip rendering
frames" — it delivers per-frame variance reduction (fewer spp for the same SSIM tier), stacking
multiplicatively on the proven denoise-anchor. Deepest/slowest of the three tracks; least
"our own code" (stays a Cycles fork).

**Architecture:** a validated-reuse-plus-guidance reservoir layer inside a Cycles fork, our Rust
owning cross-frame policy.
- In-kernel (C++/CUDA, unavoidably in-tree): splat accepted-path radiance into a coarse world-space
  hash grid (SHARC-style) at shading time; mix a guided direction into BSDF sampling via MIS at
  `surface_bounce`. **Real prior art exists** — `weizhen/blender:restir` (Blender PR #121023,
  updated Oct 2025) already does CPU direct-lighting spatial ReSTIR. We build on it, not from
  scratch.
- **The key reframe:** our "failed" analytical reprojection (`analytic_reproject()` in
  `exp_render_stack_analytical.py:951`, identity-probe gate) is demoted from "the final answer" to
  a *proposer* — it reprojects frame N-1's reservoir as ONE candidate; ReSTIR re-evaluates the
  target function against frame N's real surface and collapses the weight if wrong. This is why it
  can't fail the way our hard warp did (worst-tile 0.27): a bad reused sample degrades gracefully
  to fresh path tracing instead of producing garbage. Our sunk reprojection investigation is not
  wasted — it becomes the proposer.
- Host-side (our Rust, `cx-reservoir` crate, thin C ABI): reservoir lifecycle, reuse schedule,
  history clamp, disocclusion detection, adaptive per-tile spp driven by reservoir confidence.
- ReSTIR PG (Zeng et al., SIGGRAPH Asia 2025) is the unification point: fit the guide FOR FREE from
  the reservoirs.
- **Our structural edge over upstream:** we gate on worst-tile SSIM, not provable unbiasedness — we
  can ship a coarser, cheaper, slightly-biased guide where academic work spends most of its effort
  staying unbiased.

**Phased milestones:**
- Phase 0 (hours-1 day, zero fork code): (a) Open PGL guiding ON vs OFF at equal SAMPLE COUNT
  (not wall-clock), scored AFTER our denoise anchor; (b) build `weizhen/blender:restir`, render the
  interior-dolly toy scene, does spatial-only ReSTIR clear a worst-tile tier equal-time PT misses?
  **This is the single most important gate** — does smarter sampling even help once the denoiser
  has already eaten the variance headroom?
- Phase 1 (days, no fork): stand up NVIDIA SHARC as a GPU-guide proxy; add ONE temporal candidate
  via our existing `analytic_reproject()` to the Phase-0b branch — **the decisive test of whether
  validated reuse beats the 0.27 wall**.
- Phase 2 (weeks): first real fork code — minimal GPU guiding cache kernel. Only if Phase 0+1 pass.
- Phase 3 (1-2 months): GPU wavefront reservoirs + the Rust host policy crate.
- Phase 4 (MVP): ReSTIR PG convergence — guide fit for free from reservoirs.

**Risks:** premise may die in hours (denoiser already ate the headroom); temporal reuse specifically
may still fail even validated (falls back to guiding-only, not a total kill); **Open PGL's GPU port
just got FUNDED** (joined ASWF April 2026, Intel maintainer moving to Blender) — upstream could
obsolete our custom guide, mitigated by keeping our edge in scene-specialization + bias-tolerance;
CUDA-only, dark on Apple Silicon/Metal.

**Resourcing:** Phases 0-1 solo/owner-driven, days, no hire. Phases 2-4 need dedicated
systems-graphics engineering (Cycles C++/CUDA wavefront kernels) — a real skill gap versus our
current Go+Rust team; budget a quarter-plus or a specialist.

### Track 1a preflight + launch, 2026-07-07

Before spending real money on `run_guiding_ab.py`/`pod/exp_guiding_ab.py` (Phase 0a, never run on
real hardware), ran a 2-agent parallel preflight: (1) verified Open PGL path guiding is genuinely
built into the official Blender 4.2.0 LTS Linux tarball (shipped since Blender 3.4, no custom build
flags needed, portable tarball bundles its own libs) with the one real caveat — guiding only works
on the CPU render device, which the script already forces unconditionally (`scene.cycles.device =
'CPU'`), so this is a non-issue; (2) a direct code audit that found ONE real bug in the SAME failure
class as the Track 2 OIDN incident: `load_exr_rgb()` in `exp_guiding_ab.py` silently substituted an
all-zero channel into the SSIM score if an R/G/B channel name lookup missed, instead of raising —
meaning a wrong channel-name assumption would have produced a quietly-wrong verdict after all 3
(expensive) renders completed, not a crash. **Fixed:** now raises if R/G/B aren't all found, letting
the existing imageio/cv2 fallback chain actually get a chance to read the file correctly instead of
masking the miss with zeros. Recommendation was GO after this one fix; launched
`python3 run_guiding_ab.py` on real hardware.

**RESULT (real hardware, A100-hosted CPU render, $0.07, clean teardown, 3 min wall):**
`guiding_helps_post_denoise=False`. `worst_tile_delta=-0.0002` (OFF=0.988, ON=0.9878) —
essentially zero/slightly negative, nowhere near the +0.005 verdict margin. Critically,
`guiding_active=True` — Open PGL genuinely engaged (the preflight's CPU-forcing and
attribute-name checks held up on real hardware), so this is a real negative, not a silent
no-op. Guiding also cost 67% more wall-clock at equal sample count
(`guiding_wallclock_overhead_x=1.671`) for zero quality benefit here.

**Interpretation:** this decisively answers the "does smarter sampling even help once the
denoiser has already eaten the variance headroom?" question for Open PGL's *guided
importance-sampling* mechanism specifically — no, not on this scene, not post-denoise.
It does **not** by itself refute Phase 0b (validated ReSTIR *sample reuse*, a different
mechanism — reuse imports already-computed samples rather than just steering new-sample
direction, closer in spirit to the reprojection work that hit the 0.27 worst-tile wall).
Phase 0b (`run_restir_branch_build.py`, building `weizhen/blender:restir`) is the
riskier/more expensive next step (~$0.8-5.6, 50-120 min, real risk of failure at the Cycles
build stage) — the experiment matrix explicitly recommended owner presence for that one's
sync_libs→build transition, so it was not auto-launched; flagged to the owner, who chose to
launch it fully unattended (accepting that a build-stage failure is money-safe either way and
would be diagnosed from the log afterward).

### Track 1b — ReSTIR fork build, 5 attempts, real bugs found+fixed, hit a deeper wall (2026-07-07)

Total spend across all attempts: **~$0.60**, all clean teardowns, zero orphaned pods.

1. **Attempt 1 ($0.14):** failed at `build` (`gmake: Makefile: No such file`). Root cause was
   actually one stage earlier: `cmake_configure` had failed ("Could NOT find Epoxy") but its
   own success check (`test -f build_linux/CMakeCache.txt`) false-positived, because CMake
   writes a partial cache file even on a failed configure — masked by `cmake ... | tail -50`
   swallowing cmake's real exit code (`set -e` doesn't see failures inside a pipe without
   `pipefail`). **Fixed:** added `libepoxy-dev` to the apt deps list (confirmed via Blender's
   own CMake source that Epoxy is unconditionally required, a genuine gap in Blender's own
   Ubuntu docs) and added `-o pipefail` to every stage command that pipes into `tail`.
2. **Attempt 2:** failed before even provisioning — all 12 GPU deploy attempts hit transient
   RunPod capacity/network issues (same pattern seen earlier for Track 2's first launch).
   Not a code bug; simple retry.
3. **Attempt 3 ($0.17):** the pipefail fix worked correctly this time — `cmake_configure`
   correctly reported `ok=False` at the right stage instead of a stage late. New real error:
   CMake wants a `vulkan` pkg-config module. Verified against Blender's actual CMake source:
   `-DWITH_VULKAN_BACKEND` defaults ON on Linux (OFF on macOS), unconditionally requiring
   vulkan+shaderc pkg-config regardless of other options. We only need headless CPU+CUDA
   Cycles rendering, not Blender's Vulkan-backed viewport, so **fixed** by adding
   `-DWITH_VULKAN_BACKEND=OFF` to the cmake flags (no new packages needed).
4. **Attempt 4 ($0.15):** Vulkan fix worked; configure got further, then failed on
   "Could NOT find Freetype". Noticed a bigger clue in the same log:
   `WITH_LIBS_PRECOMPILED is disabled` / `Unable to find LIBDIR` — meaning the build had
   fallen back to needing every system dev library individually instead of Blender's normal
   precompiled third-party library bundle. Root-caused: `git-lfs` was apt-installed but its
   filter hooks were never registered (`git lfs install`), so `make update`'s internal
   `lib/linux_x64` submodule pull silently yielded empty content instead of the real
   precompiled binaries. **Fixed:** added `git lfs install` to the deps stage, plus a
   diagnostic (`du -sh lib/linux_x64`) at the end of the sync stage so a broken submodule
   would be visible immediately rather than surfacing as a confusing missing-package error
   two stages later.
5. **Attempt 5 ($0.14):** the diagnostic did its job — `LIBDIR_CHECK=0 lib/linux_x64`: the
   directory exists (git created it) but is empty even with git-lfs correctly configured.
   **This points to a deeper issue than a missing local setup step: this specific research
   fork's `lib/linux_x64` submodule pinning itself is likely broken or absent** (research
   branches often don't maintain the same precompiled-library linkage official release
   branches do) — same Freetype failure as attempt 4, now with the root cause narrowed down
   rather than newly-discovered.

**Where this stands:** the only remaining path is a full system-library build (skip the
precompiled bundle entirely), which carries a flagged, real risk: Ubuntu 24.04 almost
certainly ships Embree 3.x while Blender's current main HARD-requires Embree ≥4.0.0 (unlike
Freetype/Epoxy, this one has no soft-disable option) — a materially bigger detour
(building Embree from source, or auditing whether this specific fork's pinned Cycles version
actually wants 3.x since the fork diverged from main circa Oct 2025) than any fix applied so
far.

**DECISION (owner, 2026-07-07): stop here.** The build environment fragility of a paused,
diverged research fork is itself informative for Track 1's risk profile — not a dead end for
the underlying ReSTIR idea, but a real cost signal for THIS specific path (building on an
unmaintained WIP branch vs. e.g. targeting a more actively-maintained ReSTIR
implementation, or doing the reservoir work as an addition to our own render pipeline rather
than a Cycles source fork). No further pod spend on Track 1b this round. Net for the whole
Track 1 probe: Phase 0a (guiding) ran clean and answered its question (no); Phase 0b (ReSTIR
build) did NOT reach a working binary, but banked 3 real, reusable build-environment fixes
(libepoxy-dev, `-DWITH_VULKAN_BACKEND=OFF`, `git lfs install`) if this thread is revisited
later, plus the diagnosis that this fork's own library pinning is broken — worth knowing
before trying again, not worth re-discovering. Total Track 1 spend: ~$0.67 ($0.07 guiding +
~$0.60 across 5 build attempts).

---

## TRACK 2 — Our own temporally-stable neural denoiser

**Verdict:** pursue — for ownership + pipeline-specialization, not to out-research NVIDIA/Intel.
Temporal stability is solved in principle in the literature (motion-warped history + kernel
prediction + explicit temporal loss); **OIDN 3's own temporal denoiser ships H2 2026** and will
erase our "OIDN flickers" story for generic content. What's defensible: a denoiser co-designed with
OUR adaptive sampler + light-tree, trained on OUR exact noise/AOV distribution, gated on OUR
worst-tile SSIM metric, owned end-to-end. **Bonus: this is a second, more promising attack on the
animation-speedup wall** — it renders a fresh cheap frame every time and only accumulates history;
it never synthesizes unseen geometry, so it degrades gracefully instead of hard-failing like
reprojection did.

**Architecture:** a shallow (~0.5-3M param) albedo-demodulated, kernel-predicting two-branch
U-Net — architecturally a fork of **OIDN 3's own public temporal design (Apache-2.0)** — trained in
PyTorch on Noise2Noise pairs minted from our own pipeline.
- Inputs: our exact AOV vector (`{noisy RGB, albedo, normal, depth, motion}`) — already a superset
  of what these nets need; `exp_cycles_render_prod.py` already emits albedo+normal denoising passes,
  `exp_render_temporal.py` already emits real Vector+Z. We are missing nothing.
- **The moat is the training distribution, not the architecture**: unlimited perfectly-matched pairs
  from OUR pipeline (same adaptive sampler, same light-tree noise, same scenes) — the single
  biggest data-efficiency lever a general tool like OIDN structurally cannot exploit.
- Temporal variant = same net + 3 additions: warp previous DENOISED OUTPUT by our motion vectors as
  extra input; explicit temporal loss; reject bad history on geometry consistency (falls back to
  spatial-only on disocclusion instead of ghosting).
- **Real integration points found in our actual code:** `JobRunner` trait in `agent/src/runners.rs`
  (clean slot for a new `DenoiseRunner`, beside Embed/Whisper/BatchInfer/Hawking); `apply_denoiser()`
  in `exp_render_denoise.py`; the `CX_DENOISER` ladder (`oidn|optix|none`) in
  `exp_cycles_render_prod.py` — a `denoiser="cx"` rung drops in on the same one-JSON-line honesty
  contract, directly comparable to banked OIDN/OptiX numbers.
- Train PyTorch (fork OIDN's own training toolkit — it expects exactly our EXR/AOV layout), deploy
  via `ort` ONNX runtime into Rust first (zero Python at inference), migrate to native Burn/CubeCL
  later for the owned end-state, hand-write the kernel-apply gather as a custom Metal/CUDA kernel
  (the latency-critical, most-worth-owning piece).

**Phased milestones (table, cheapest → MVP):**
| Phase | What | Proves/kills | Cost |
|---|---|---|---|
| M0 Data harness | Mint matched pairs via existing `runpod.py`+`exp_cycles_render_prod.py`, Noise2Noise (2 cheap renders, no clean target needed for training) | Can we produce in-distribution data on demand | cents-$5, an afternoon |
| M1 Single-frame denoiser (**the go/no-go**) | Train the small net (1-3 GPU-hr), drop in as `denoiser="cx"`, run on held-out scene | Match/beat OIDN 2.5 on OUR worst-tile SSIM at speedup-preserving latency | **<$10, one session** |
| M2 Rust inference bridge | ONNX export, `ort`-based `DenoiseRunner`, measure real latency on target HW | Owned deploy path works, fits the latency budget (make-or-break number) | ~1-2wk eng, ~$0 GPU |
| M3 Temporal variant | Add warped-history input + temporal loss + history rejection, train on short animated clips | Beats single-frame AND beats OIDN-3-temporal on flicker without regressing spatial quality; tests the animation-wall bonus | multi-week, low-$ GPU |
| M4 Owned end-state | Native Burn/CubeCL, hand-written kernel gather, sampler↔denoiser co-design | Removes third-party runtime entirely; the one edge OIDN can never have | multi-week, dedicated |

**The first real commitment is only M0+M1: <$10, one session, no infra beyond one A100 + PyTorch.**

**Risks:** OIDN 3 ships strong/general H2 2026 (largely priced in — we're not competing on generic
denoising; M1's decision rule already routes to a fallback: fork OIDN's inference into Rust, still
delivers ownership); latency eats the speedup (the make-or-break number, answered at M2); N2N noise
may not match true MC statistics (detected at M1's held-out eval; mitigation: nonlinear-N2N); kernel-
prediction gather may not port cleanly to Burn/CubeCL (mitigation: hand-write it, which we want to
own anyway); **honest ceiling: a legitimate product/ownership win, not a moat against NVIDIA/Intel.**

**Resourcing:** M0+M1 owner-solo, days, no hire. M2+ needs someone who can do PyTorch training AND
Rust inference AND (for M4) custom GPU kernels — rare combined skillset; realistically owner +
one dedicated ML/systems engineer, or the owner wearing both hats over months.

### Track 2 M0+M1 — LAUNCHED on real hardware, 2026-07-07

**Run 1 (cross-scene, the default/honest M1):** classroom (4 frames, 768 crops, 40 epochs,
1.115M params) -> eval on held-out bmw27. Real hardware (NVIDIA L40S), cost **$0.26**, clean
teardown. Result: **`oidn_wins` decisively** — cx worst_tile=0.5918 vs OIDN worst_tile=0.964
(delta -0.372), cx quality=0.92 vs OIDN quality=0.9954. This was the FIRST trustworthy run of
this comparison: OIDN itself had been silently reporting `oidn_unavailable` because
`eval_cx_denoiser.py`'s original `denoise_oidn()` used an unreliable standalone `import oidn`
pip binding; fixed this session to invoke OIDN via Blender's real bundled Cycles/compositor
`CompositorNodeDenoise` node (the same mechanism every other OIDN number this session actually
came from) — see `scripts/spec-lab/eval_cx_denoiser.py`.

**Triage (multi-agent investigation before spending more):** launched a 3-way parallel
diagnosis — data-scale calibration, a direct code-bug audit, and costed next-action options —
into a synthesis. Findings:
- The `oidn_wins` result IS trustworthy (OIDN fix is real, bmw27 eval uncontaminated) but does
  NOT mean the core thesis is dead: 768 patches from 4 frames of ONE diffuse-material scene is
  2-3 orders of magnitude short of the scene/material diversity a denoiser would need to
  cross-scene-generalize (classroom is diffuse-only; bmw27 stresses specular/glossy transport
  the net never saw).
- **A real bug was found and fixed:** `train_cx_denoiser.py`'s train/val split was done at the
  individual-PATCH level (shuffle all `.npz` files, take 10%), not the source-FRAME level.
  Since `exp_mint_denoise_pairs.py` draws crops via unconstrained independent `rng.randint`
  per frame with no tiling/min-distance constraint, and reuses one fixed `noisy_b` array
  across all 192 crops of a frame, overlapping train/val crops could share literal target
  pixels — meaning the reported `best_val_loss=2e-06` measured memorization of the 4 training
  frames, not real validation. **Fixed:** the split now reads `manifest.json`'s per-patch
  `"frame"` field and holds out whole frames, never individual patches, for val.
- **Decision rule adopted:** before funding a bigger multi-scene "M2" run (~$0.70-1.50, tests
  whether unlimited own-pipeline data actually closes the cross-scene gap), run a much cheaper
  same-scene sanity check first (~$0.15-0.20) — train AND held-out-eval on DIFFERENT FRAMES of
  the SAME scene (bmw27, the fast-render scene). If in-distribution worst-tile SSIM is still
  far below OIDN's ~0.96, that's a capacity/architecture ceiling more data can't fix — close
  the gate, log `oidn_wins` as final, move to Track 1a with $0 further Track 2 spend. If
  in-distribution is competitive, that isolates the bmw27 loss as pure cross-scene
  generalization and justifies the M2 multi-scene run.
- Same-scene sanity check launched: `python3 run_denoiser_experiment.py --same-scene
  --train-scene bmw27` (with the frame-split fix in place). Real hardware (L40S), **$0.12**,
  clean teardown. Result: `best_val_loss=0.07372` (sane and non-degenerate now, vs. the
  leaked 2e-06 before the split fix — confirms the fix works) but **still `oidn_wins`,
  decisively, in-distribution**: cx worst_tile=0.6332 vs OIDN worst_tile=0.9633 (delta
  -0.3301) — essentially the SAME gap size as the cross-scene run (-0.372). Global quality is
  fine (cx=0.9717 vs OIDN=0.9954); the failure is concentrated in the worst 8x8 tiles, i.e.
  specific hard regions (specular highlights / fireflies), not the whole image.

**CONCLUSION — gate closed, per the pre-committed decision rule:** a same-scene, held-out-
frame gap this large (still ~0.33 worst-tile SSIM below OIDN, nowhere near "competitive")
rules out "just needs more scene diversity" as the primary explanation — this is a
capacity/training-recipe ceiling (1.115M params, 40 epochs, ~98-132s of training, 576-768
patches), not a cross-scene generalization problem that a bigger multi-scene M2 run would fix.
**Track 2's M0/M1 result stands as `oidn_wins`, final, for this architecture/scale.** No
further Track 2 pod spend this round. Total Track 2 cost across both runs: **$0.38** (well
under the <$10 budget). This is a legitimate, informative negative result, not a failure of
the gate design — it correctly and cheaply prevented over-investing in a bigger training run
that the same-scene check shows would likely not have closed the gap. Revisit only with a
materially different recipe (bigger model, far more epochs, or a fundamentally different
kernel-prediction/receptive-field design) if Track 2 is revisited later. Per the sequencing
plan, next: **Track 1a** (Open PGL guiding A/B test), $0 further Track 2 spend.

---

## TRACK 3 — Rust-native, reuse-first renderer

**Verdict:** pursue the cheap de-risking rungs (~3 weeks) now; hold the expensive build (6-12
months + a senior graphics hire) behind Gate G1 — and **re-point the reuse target** before spending
a dollar of graphics time.

**The critical correction:** don't target the animated-camera-dolly case — that's the exact case
we already failed three ways this session. Target **decoupled shading across near-duplicate variant
batches** instead (e.g., 8 colorways of one product): geometry, camera, lights, BVH traversal,
visibility, and the geometric structure of every indirect bounce are byte-identical across variants;
only the material shading term changes. **Zero disocclusion** — the structurally cleanest possible
reuse case, and exactly what a pixel-level interface to Cycles can never expose.

**Architecture:**
- **Substrate: wgpu + WGSL compute** — one Rust codebase → Vulkan (RunPod NVIDIA) / Metal (owner's
  Apple Silicon) / DX12 / WebGPU. **Not aspirational — already proven in our own repo:**
  `agent/Cargo.toml` already has `default = ["metal"]` (candle on Metal) + a mirrored `cuda` feature
  — the exact "one binary, GPU backend at build time" pattern this would reuse verbatim.
- **Traversal: software BVH in WGSL as the portable default**, hardware ray-query behind a feature
  flag. Forced by a real, verified fact: **RunPod A100s have NO RT cores** (NVIDIA strips them from
  the compute die) — while Apple Silicon M3/M4 DOES have hardware ray intersection, and RunPod
  RTX/L40S/A6000 pods do too. Software BVH is the only common denominator across our whole rental
  fleet. **Operational takeaway for right now: if rendering becomes the product, host on RT-core
  pods, not A100s** — A100 is a poor renderer host (no RT hardware, no NVENC either).
- **First-class primitive: a path-structure cache**, not a pixel. Trace the shared geometric
  skeleton once per variant batch (primary hits, visibility, shadow rays, indirect reconnection
  vertices) into GPU storage buffers; re-evaluate only the material/BSDF term per variant. Maps
  directly onto our existing Go control-plane's draft→verify→gate framing.
- ReSTIR/GRIS reservoirs layer on top later for GI noise reduction + the intermediate cases
  (turntables, small camera deltas) where some geometry does move.
- Tiny feature envelope on purpose: Lambertian + one GGX lobe, area+HDRI lights, PBR material block.
  No hair/volumes/OSL/SSS — the 15-year Cycles breadth we get to explicitly NOT build.

**Phased milestones:**
- Phase 0 (~1-2wk): M0 pure-Rust CPU path tracer, one hard-coded Lambertian sphere, correctness
  gated by the **white furnace test** (100%-albedo sphere in uniform radiance must render invisible
  to within MC noise) — catches every classic integrator bug against a known answer, zero assets.
  M1: port to WGSL compute via wgpu, same furnace test passing identically on BOTH the owner's Mac
  (Metal) AND a RunPod NVIDIA box (Vulkan), bit-comparable within noise.
- Phase 1 (~1wk): decoupled-shading reuse PoC on a still-trivial static scene, swap BSDF albedo
  across N colorways. Success = an instrumentation counter shows the shared structure traced once,
  >90% of per-variant work is pure re-shade, measured speedup over independent rendering.
- **★ Gate G1 (make-or-break):** green-light the expensive Phase 2 ONLY if (a) Phase 1 shows
  decoupled reuse beats brute force by a worthwhile margin (target >2-3x) AND (b) Track 1's fork
  experiment has shown reuse is worth real money post-denoiser. **You fork Cycles to discover
  whether reuse pays; you don't build a renderer to find out.** If G1 fails: stop, keep
  orchestrating the proven Cycles stack (5.84x@0.95-tier, ships today, zero build risk).
- Phase 2 (~3-6mo, needs a specialist): glTF ingest, GPU BVH build+traverse (the largest single
  chunk), GGX lobe, and — the unglamorous likely killer — **color management + look-match to
  Cycles** within customer signoff tolerance. Internal stop gate at ~6 weeks if we can't look-match
  one easy studio scene.
- Phase 3 (~3-6mo further): headless batch pipeline + one real design partner accepting their
  actual catalog renders as final. Layer ReSTIR reservoirs to extend reuse to turntables/small
  camera deltas. Stop if no design partner accepts non-Cycles output — that's a tech demo, not a
  product.

**Risks (ranked by likelihood of actually killing it):** reuse has no commercial value (detected
cheaply in Track 1's Gate G1, not here); dual-backend wgpu toolchain doesn't hold (detected Phase 0,
~1wk); **trust/look-match barrier — a brand won't accept catalog output from a non-Cycles/V-Ray/
KeyShot engine** (flagged as the MOST LIKELY KILLER, not the ray tracer itself — detected at Phase
2's six-week look-match gate, definitively at Phase 3 with a real design partner); GPU BVH harder
than budgeted; ReSTIR-PT's research-grade subtleties (shift-mapping bugs, moving-shadow artifacts)
bite the *secondary* reservoir layer, not the decoupled-shading primary win; fleet mismatch (A100
has no RT cores — a provisioning decision to flag now, not a blocker).

**Resourcing:** Phases 0-1 (~3wk) existing in-house Rust capability, no hire needed to reach Gate
G1. Phases 2-3 (6-12+ months) need a DIFFERENT specialty we don't have in-house — senior graphics
engineering (GPU BVH, BSDF importance sampling, color management/look-match). Precedent cited:
appleseed called a production renderer "a truly herculean team effort"; Cycles is 15+ years. Budget
a dedicated senior graphics hire, full-time, 6-12 months — and do not start that phase, or the hire,
until Gate G1 passes.

**Relation to Track 1:** partly a substitute — if Track 3 fully succeeds, Track 1 becomes redundant
(native reuse in our own integrator is the pure version of what forking Cycles for ReSTIR
approximates). But Track 1 is 3-10x faster to a verdict, so it runs FIRST and its result decides
Track 3's Gate G1.

---

## CROSS-TRACK SYNTHESIS

**The floor, never lose sight of it:** the proven Cycles orchestration stack ships TODAY at
5.84x @ 0.95-tier with zero build risk. Everything below is upside, sequenced so we spend <$50 and
no hire to decide which bet is worth real money before committing a dollar of graphics-engineer
time.

**Two honest rankings, both matter:**
- **By near-term risk-adjusted leverage:** Track 2 > Track 1 > Track 3. Track 2's go/no-go is <$10,
  one session, owner-solo, produces an OWNED artifact, no gating dependency on anything else.
- **By ultimate ceiling if fully built:** Track 3 > Track 1 > Track 2. Track 3 *is* the actual "own
  engine"; Track 1 is faster to ship but forever a fork; Track 2 is a bounded component, not a moat.

**Sequencing:**
- **Weeks 0-4, three cheap probes in parallel, no hire, <$50 total, ALL scored on the SAME shared
  worst-tile SSIM harness** (`compute_ssim_global_and_tiles`, 8x8 grid) so the numbers actually
  compose: Track 2 M0→M1 (fire first); Track 1 Phase 0 (guiding checkbox + build the ReSTIR branch,
  scored after our denoiser); Track 3 Phase 0 (furnace test + dual-backend proof).
- **Weeks 4-8, the reuse-value verdict:** Track 1 Phase 1b (the decisive validated-temporal-
  candidate test) + Track 3 Phase 1 (decoupled-shading PoC) together = **Gate G1**. Track 2 M2
  proceeds independently regardless.
- **Gate G1 (~month 2-3)** governs ALL expensive spend and any graphics hire. Fails → stop both
  renderer bets, keep the proven stack, ship the owned denoiser, done — a good outcome, not a
  failure.
- **Later:** Track 2's M4 (sampler↔denoiser co-design, the one edge OIDN can never copy) only
  unlocks once we own a sampler — schedule it onto whichever of Track 1/3 clears G1.
- **Hard dependency to respect:** Track 1's entire value is measured THROUGH the denoiser — if
  Track 2 replaces OIDN, Track 1's Phase-0 gate must be re-run against the new denoiser. One shared
  harness is non-negotiable.

**The single cheapest next action, today:** Track 2 M0→M1 — mint Noise2Noise pairs from the
existing `exp_cycles_render_prod.py`+`runpod.py` harness (cents), train the small
albedo-demodulated kernel-predicting U-Net (1-3 GPU-hr), drop it in as `denoiser="cx"`, grade
against OIDN 2.5 on our worst-tile SSIM on a held-out scene. **Total <$10, one session, owner-solo.**
Free same-day companion: Track 1 Phase 0a (Open PGL checkbox on/off, equal-sample) — zero cost,
fastest raw signal, but produces no owned code, so it's the sidecar not the headline.

**The honest big picture — "own the engine" gets built OUTSIDE-IN, never renderer-first:**
1. Own the denoiser (Track 2 M1-M2) — achievable this quarter, squarely inside our current
   systems-Rust muscle.
2. Own the reuse/scheduling POLICY in our Go+Rust control plane (the host-side orchestration both
   render tracks push down to us, regardless of which renderer wins).
3. Own the renderer CORE only later, and only if Gate G1 says the reuse dollars are real — this is
   the one place our current in-house strength (systems Rust/Go) doesn't reach; it needs a
   dedicated graphics specialist, hired or ramped, and that gap — not the ambition — should govern
   the pace.

Two genuine research gambles to name plainly: (1) does validated reuse actually beat the 0.27
reprojection wall — genuinely unknown, could still be a structural "no" even with the ReSTIR dodge;
(2) can we build a from-scratch renderer customers accept as final catalog quality — the tech is
tractable, but the trust/look-match barrier is the more likely killer and it's a business problem,
not an engineering one.
