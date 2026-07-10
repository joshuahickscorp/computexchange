# Phase 1 design sketch — decoupled shading across variant batches

*Track 3, `docs/research/ORIGINAL_ENGINE_THREE_TRACKS_2026-07-07.md`. This is a
concrete sketch to build against, not an implementation. It defines (1) what a
**path-structure cache** entry holds and (2) how re-shading N material variants
against one cached structure is scheduled as GPU compute passes.*

## The target case (and why it's the clean one)

Not the animated camera dolly — that case failed three ways this session
(worst-tile pinned ~0.27). The Phase-1 target is **N near-duplicate material
variants of ONE static frame** (e.g. 8 colorways of a product). Across variants:
camera, geometry, BVH, visibility, light positions, and the *geometric* structure
of every indirect bounce are **byte-identical**; only the surface shading term
changes. **Zero disocclusion** — nothing moves, so there is no reuse artifact to
fight. This is the structurally cleanest reuse case that exists, and it is exactly
what a pixel-level interface to Cycles can never expose.

## Two reuse regimes

- **Albedo/color swap only (the headline case).** All variants share one lobe
  shape (same roughness/metalness), differing only in reflectance color. The
  sampled bounce *directions* are then identical for every variant, so the cached
  path is EXACT for all of them — re-shading is a per-vertex color multiply. Zero
  added variance, the maximal win.
- **Lobe change (glossy ↔ rough, dielectric ↔ metal).** Directions would differ
  per material. To keep ONE shared path we sample bounce directions from a
  fixed, **material-agnostic proposal** (cosine-weighted, or a mid-roughness GGX
  reference) recorded in the cache, then per variant re-weight by that variant's
  BSDF value ÷ the proposal pdf (MIS-style). Unbiased as long as the proposal
  has support wherever any variant's BSDF does; variance grows only for a very
  glossy variant proposed by a diffuse ref — bounded, and acceptable inside our
  deliberately tiny feature envelope (Lambertian + one GGX lobe).

## Path-structure cache entry — one per primary path (pixel × sub-sample)

Stored **material-free**: geometry + visibility + the geometric part of throughput
only; no BSDF value, no color, no material-dependent pdf baked in.

```
PathStructure {
  pixel:        u32          // linear pixel index (packs sub-sample id)
  cam_wo:       vec3         // direction back toward the camera at the primary hit

  primary: HitVertex         // the primary hit (see below)

  // Indirect bounce chain, material-agnostic. Variable length; stored flat.
  chain_offset: u32          // start index into the shared HitVertex[] pool
  chain_len:    u32          // number of bounce vertices (<= MAX_DEPTH)
  terminated_on_env: u32     // 1 if the last segment escaped to the environment
  env_radiance: vec3         // uniform/HDRI radiance the escaping ray saw
}

HitVertex {
  p:            vec3         // hit position
  ns:           vec3         // shading normal (geometric normal packed alongside)
  material_slot:u32          // index into THIS batch's material table (same slot,
                             //   different table contents, per variant)
  uv:           vec2         // for textured PBR blocks later
  wi:           vec3         // sampled outgoing bounce direction (the proposal)
  proposal_pdf: f32          // pdf of wi under the material-agnostic proposal
  geom_weight:  f32          // cos(theta) / free-flight factors — NO BSDF term
  // Per-vertex direct-light connection (NEE), material-free:
  light_dir:    vec3
  light_pdf:    f32
  light_radiance: vec3
  light_G:      f32          // geometry term: cos*cos / dist^2
  light_visible:u32          // 1 = shadow ray unoccluded (a single visibility bit)
}
```

Everything above is computed **once per batch**. The only thing a variant supplies
is its **material table** (a small array of PBR blocks indexed by `material_slot`:
`{base_color, roughness, metalness, ...}`), and the only thing a variant computes
is `f_bsdf(vertex, wo, wi)` and, for the lobe-change regime, `pdf_bsdf`.

## GPU schedule — compute passes on the M1 wgpu/WGSL substrate

All buffers are **Structure-of-Arrays** storage buffers (coalesced gather in the
re-shade pass) — the exact buffer/pipeline round-trip already proven by
`examples/wgpu_smoke.rs` on Metal (and, unchanged, Vulkan on RunPod).

- **Pass A — Trace structure (ONCE per batch).** One invocation per path.
  Software BVH traversal in WGSL (portable default — RunPod A100s have no RT
  cores; hardware ray-query stays behind a feature flag for RT pods / Apple
  Silicon). Writes `PathStructure[]`, the flat `HitVertex[]` pool, and the
  visibility bits. Cost ≈ a normal integrator pass. **This is the amortized
  cost.**
- **Pass B — Re-shade variant v (per variant; N dispatches or one N×paths grid).**
  Reads the cache + variant v's material table. For each cached vertex: evaluate
  `f_bsdf` (albedo-swap: a multiply; lobe-change: `f_bsdf * cos / proposal_pdf`),
  fold in the pre-computed `geom_weight`, `light_*` connection, and
  `light_visible` bit, accumulate radiance into `image_v`. **Pure ALU + gather;
  no traversal, no ray casts, no shadow rays** — those results are already in the
  cache. This is the work that should be >90% cheaper than an independent render.
- **Pass C — Resolve.** Average sub-samples → per-variant framebuffer; hand off to
  the proven denoise-anchor / SSIM grading.

## Phase-1 success instrumentation (feeds Gate G1)

- **GPU atomic counters:** `traversal_steps` (incremented in Pass A) and
  `bsdf_evals` (Pass B), read back to *prove* the structure was traced once and
  that per-variant work is dominated by re-shading — success = **>90% of
  per-variant work is pure re-shade**.
- **Wall-clock:** time(1 × Pass A + N × Pass B) vs time(N independent full
  renders). Target for Gate G1 = **>2–3× speedup**.
- **Correctness:** grade each variant's re-shaded image against an *independent
  full-trace* render of the same variant using the shared
  `compute_ssim_global_and_tiles()` harness (global + 8×8 worst/p5 tile SSIM) —
  same metric as everything else measured this session, so the numbers compose.
  Re-shade must be within noise of the independent render, or the reuse is
  silently biased.

## What this deliberately is NOT

No hair/volumes/OSL/SSS; no moving geometry (that's the ReSTIR-reservoir layer
that comes *later*, on top, for turntables/small camera deltas). The bet Phase 1
tests is narrow and falsifiable: **does tracing the shared skeleton once and
re-shading N variants beat N independent renders by a worthwhile margin?** If the
counters say <90% re-shade or the speedup is <2×, Gate G1 fails and we keep
orchestrating the proven Cycles stack.
