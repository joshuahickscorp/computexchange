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
