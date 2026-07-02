# render/NOTES.md · the verify loop for the tabletop oracles

Minimum three look-fix cycles per device. "Done" requires a written pass stating what a
device owner would check and why it now passes. Dash gate: middot only.

## Combined scene · tabletop hero

### iter 1 (128 spp, 25% preview)
Feeling is right the moment it lands: real desk, ~36 degree down-angle, contact shadows,
the size contrast reads. Faults against reference:
- Floor reads as a studio sweep, not a matte desk · strong light gradient, far floor lifts to
  grey instead of falling into void #060606. Too reflective.
- Exposure too hot (-0.15): DGX Spark top champagne blows toward white; real Spark top is matte
  anodized, not glossy gold.
- Mac Studio top slightly mirror-glossy; bead-blast should stay satin.
Fix (environment + material class): darker higher-roughness floor so it goes matte and the far
edge falls to black; exposure to -0.35; Spark champagne roughness 0.30 to 0.44 (anodized matte);
key energy trimmed so nothing clips.

### iter 2 (env fix)
Void reads black, desk matte. Studio strong. DGX Spark top still blew near-white (champagne base
too light for the flat-top specular). Fix: champagne base 0.58/0.45/0.27 to 0.44/0.33/0.16,
roughness 0.46 to 0.52.

### iter 3 (material + compose)
Champagne now a muted anodized gold, not jewelry. Turned the Spark to yaw 16 so more of the foam
front + both pills face camera. Solos rendered for close inspection.

## Mac Studio · PASS (iter 3 solo)
A device owner checks: generous vertical corner radius (~28 to 30 mm) · the continuous aluminum
band with the tighter top-edge fillet · the front row of two USB-C slots, the wider SD slot, and
the tiny power LED · a uniform satin bead-blast, not a mirror and not a blotchy roughness. All
present and correct in mac-studio-iter3. The top reads satin aluminum under the soft key. PASS.

## DGX Spark · PASS (iter 3 solo)
A Spark owner checks: the open-cell metal-foam FRONT face reading as thousands of dark cavities,
not stucco or glitter · two champagne pill cutouts set into that foam · a MUTED anodized champagne
shell (warm gold, not bright jewelry) · the exact flat-square proportion against the taller
Studio. All present in dgx-spark-iter3. The foam is genuine porous geometry (Voronoi displacement),
the pills read, the champagne is anodized-matte. PASS.

## Scene · PASS (iter 3)
The two machines sit on a matte desk at a standing eye line looking down ~36 degrees, size contrast
exact, contact shadows real, void black behind. This is the "walked up to a table" brief. Finals
render at 1024 spp / 3200 px from this locked scene.

## PASS 3 · reproduction grade (overlay verification)

### Mac Studio · overlay loop 1 (render/verify/mac-studio-front-overlay.png)
Measured: reference device AR 2.047 · render AR 2.075 (+1.4%), height delta -4px after
width-match. Silhouette and top corner radii agree. Defects from the 50% overlay:
- PORT ROW too low by ~15px · the two USB-C + SD slot sit nearer the bottom edge than the
  reference row. Fix (geometry): raise the port z.
- BASE PERFORATION wrong · sparse random dots on a flat band vs the reference's fine dense
  mesh following the circular base. Fix (material): a real perforation pattern, denser + finer,
  not scattered geometry.
- Port tongue detail is CORRECT (checklist wants the internal tongue visible).
Order: geometry (port z) then material (perforation), re-overlay after each.

### Mac Studio · overlay loop 2 (perforation fix)
Base intake rebuilt: Voronoi F1 at ~1.3 mm pitch (was 2.4 mm sparse), sharp ramp for small
round holes, Bump 0.35 to sink them · reads as a fine dense perforated mesh, matching the
reference at the base. Re-overlay: ports + SD slot now coincide with Apple's row (ghosting
gone), silhouette 2.075 vs 2.047 (+1.4%), perforation character matched. Remaining nitpick:
the circular base arc reads a touch flatter than the reference · candidate for loop 3.
Mac Studio front: reproduction-grade close. Pushing the improved base into the live finals.

### DGX Spark · overlay loops 1-2 (ref render/ref/dgx-spark-front-ref.jpg, StorageReview front)
The reference is a near-front desk photo (busy background), so this is feature/material
verification, not a strict silhouette overlay. Loops:
- L1: foam too coarse + too regular vs the reference's fine dense foam. Fix (geometry+shader):
  two overlapping Voronoi scales · coarse FOAM_CELL 2.2 to 1.8 mm + a finer field at 1/3 scale,
  and a second displace modifier at 1/3 scale for real two-scale geometry pores.
- L2: foam read near-BLACK (pores dominated) where the reference reads golden-brown. Fix
  (material): champagne web now dominates (ramp element0 at 0.08, element1 pushed to 0.56 so
  only deep pore centers darken, and even they stay dark champagne 0.11/0.08/0.05, not soot).
  Re-render: the face now reads as golden two-scale open-cell foam, matching the reference.
Note: under the flat verify light the metallic pill tubs read cream (they reflect the bright
neutral world); in the dark-world hero they render as recessed dark champagne, verified in the
tabletop finals. Champagne shell tone confirmed muted/warm, not jewelry.

## PASS 3 · Phase 3-4 delivery (docs/PERF.md)
Shipped + live-verified: glb 2.3MB to 1.0MB (foam maps 512px+crush), fonts self-hosted +
subset (Geist 10KB + Cormorant 18KB, 0 Google requests), brotli JS (three.js 1.27MB to 201KB
wire), control-plane .br serving + Accept-Ranges on the glb + immutable cache for hashed names +
woff2/ktx2 whitelist. gltfpack/toktx uninstallable here (documented); KTX2 server path pre-wired.
Remaining Phase 4: content-hash filenames, still-first crossfade, LCP/waterfall measurement, SW.

### Mac Studio · overlay loop 3 (high-res 758px reference)
Silhouette +1.1% aspect (753x367 ref vs 803x387 render). Top edge, corner radii, both USB-C
tongues, SD slot, LED, and the perforation band all coincide in the 50% overlay · the front is
reproduction-grade indistinguishable. Sole residual: the circular-base arc reads a hair flatter
than the reference foot (sub-pixel at hero distance). Bead-blast micro-roughness is carried by
the shader noise (0.34 to 0.42) in the still. Front angle: DONE.

### DGX Spark · reference ceiling
All available Spark press imagery is 3/4 on a wooden desk (StorageReview) · no clean
front-orthographic exists for this new device, so a pixel overlay like the Studio's is not
possible. The Spark is verified reference-FAITHFUL on the measurable signatures: two-scale golden
open-cell foam density + tone, the two recessed pill cutouts and their placement, the muted
champagne shell, the flat-square proportion against the taller Studio. This is the reproduction
ceiling the references permit.

## MODEL REFINEMENT worktree · DGX Spark accuracy loops (side-by-side vs StorageReview front)
The flat verify light blew champagne metal to white (misleading); relit the verify with a soft
directional key + dark world (like the reference studio light) so metal reads as a champagne
gradient · render/verify/dgx-spark-compare.png (ref left, render right).
- pass 1 (geometry+material): brighter champagne body base 0.44 to 0.60, wider champagne rails
  (pocket 136 to 124mm, lips 44.5 to 38mm), deeper foam displacement 3.2 to 4.2mm, recessed
  champagne pill tubs (set 2.2 to 4.0mm, tub 0.36 to 0.52 champagne not cream).
- pass 2 (foam tone): the reference foam is bright GOLD, mine was dark brown · web ramp element0
  to (0.92,0.74,0.42), web area up (element1 pos 0.52 to 0.62), ridges glossier (rough 0.30 to
  0.22) for the gold catch, cells slightly larger (1.8 to 2.0mm). Foam now reads as golden
  open-cell metal matching the reference. Verified in the hero lighting too (preview iterr1).

## MODEL REFINEMENT · Mac Studio aluminium tone (per-device verify lighting)
The dim directional verify hid a real gap: aluminium base 0.58 read as dark anodized gunmetal,
not Apple's bright silver. Metals show their surround, so the verify now lights each device to
match ITS reference: Studio under a bright high-key white studio (Apple product shot), Spark under
the dim directional desk key (StorageReview). Fix: aluminium base 0.58 to 0.86 (real ~0.9 alu
reflectance) · the Studio now reads as bright Apple silver in both the verify compare and the hero
(preview iterr2). Both devices confirmed accurate in the hero lighting.
