# render/RACK-BUILD-PLAN.md · the modeling plan (gates 5-10)

Date: 2026-07-05. Archetype LOCKED: D-ARCH A-prosumer (person-owned 42U homelab rack). This
plan is the driver's build architecture · what I own (compounding-risk geometry + the
dark-object lighting call), what I delegate (Sonnet measurement/panel throughput), and the
concrete approach to the two hard problems. Dash gate: middot only. Desktops are CLOSED; this
is a separate worktree and a NEW builder module · not one line of build_scene.py changes.

## 0 · The tone reality (measured 2026-07-05, reference-side, bright studio light)

| material | ref patch | L | a | b | source | note |
|---|---|---|---|---|---|---|
| powder-coat black | RM44 top lid | 14.8 | 0.5 | -1.3 | rm44_front_A.jpg | matte satin near-neutral, a HAIR cool · the whole rack |
| powder-coat black | RM44 ear/flange | 18.0 | 0.0 | 0.0 | rm44_front_A.jpg | flat flange catches more · L15-18 range |
| mesh interior read | RM44 mesh center | 16.6 | 0.0 | 0.0 | rm44_front_A.jpg | KEY FINDING · mesh+interior reads ~as dark as solid paint even in BRIGHT light · the depth sells on STRUCTURE (see-through holes + interior gradient), NOT tone |
| switch white/grey | CRS354 top rail | 76.7 | 0.6 | -2.7 | crs354_sth_front.jpg | the one bright face · cool white-grey · deliberate material variety |
| switch body | CRS354 mid | 55.0 | 1.5 | 2.6 | crs354_sth_front.jpg | shadowed body · pin the lit top band L~74 |
| port cavity | CRS354 RJ45 | 25.2 | 1.6 | 3.0 | crs354_sth_front.jpg | recessed port · dark-not-black, warm from contacts |

Candidate pins (re-measured IN-RIG in the material wave, these are the reference-side targets):
powder_black L16 a0.3 b-1.0 · switch_white L74 a0.6 b-2.5 · port_cavity L25 a1.5 b3.0.

## 1 · THE headline risk · a dark object on a void-black hero rig

The frozen PORTRAIT_RIG + global offset O=-12 were calibrated on BRIGHT albedo · silver alu
(L84) and champagne (L80); O is the midpoint of those two clean bright patches. The rack is
L~16 powder-coat · a fundamentally DIFFERENT lighting regime. Two consequences, both owned by
the driver:

1. **Contrast/separation.** A black object on void-black (#060606) under a dim hero key can
   VANISH · merge into the background as a silhouette. Resolution: light it like an Apple
   dark-hero (black Mac Pro on black) · the KEY reveals the powder-coat SATIN SHEEN across the
   top/upright faces, the RIM draws every metal edge + rail, and the frontal fill (which made
   silver read as silver on black) now carves the black uprights out of the void. The object
   is defined by EDGE + SHEEN, not albedo. This is "tone lives in the key" for a dark object.
2. **Tone-gate transfer.** O=-12 may not hold for L16 patches (it was set by L80+ patches).
   Plan: measure powder_black IN-RIG, read its natural offset; if it agrees with the -12
   group, keep O; if the dark object needs a different global exposure, that is a LIGHTING-
   class commit that RE-VERIFIES the desktop pins too (the scale-trio shares one rig · the
   rig cannot drift the closed desktops out of tolerance). The rack rig may end up as a
   SEPARATE portrait rig from the pair rig, gated independently · decided when the frame +
   one node render and I can read the in-rig numbers. Flagged, not guessed.

This risk is why the enclosure frame (mechanical, exact) is built + lit FIRST · it is the
cheapest way to read the dark-object rig numbers before committing to the expensive parts.

## 2 · Builder architecture (the compounding-risk decision I own)

- **New module `render/build_rack.py`**, self-contained (mirrors build_scene.py's proven
  structure · args, reset_scene, enable_gpu, principled/rounded_box/add_bevel helpers COPIED
  in, not imported · the frozen file stays untouchable). If it grows past ~1500 lines I factor
  a shared `rack_lib.py`, but self-contained is the proven idiom.
- **U-arithmetic is the spine.** `u_z(n)` returns the Z of rack-unit n's bottom face above the
  rail datum: `RAIL_DATUM + (n-1)*44.45mm`. A unit spanning Us [a..b] has face height
  `(b-a+1)*44.45 - 0.79` mm (the EIA undersize · this GAP between units is load-bearing
  realism, not an error). Every occupant places by U, never by eyeball.
- **Hierarchical + INSTANCED.** Canonical part built ONCE as a clean object; placed by linked
  duplicate (`obj.copy()` sharing mesh data) so the .glb stays small and the scene tractable.
  The 3 RM44 nodes are linked dupes; per-instance variance rides object-space seed (Problem 1).
  The rail square-hole pattern is a 1U segment + ARRAY modifier x42 (not 126 booleans).
- **Front-facing doctrine** (desktop precedent): model what the locked orbit sees. Rear I/O,
  zero-U PDU, interior guts modeled ONLY if the orbit shows them. Front door OPEN/REMOVED so
  the fill reads.
- **Geometry-freeze per part**: build, measure-verify, freeze. Each canonical part closes on
  its own look-fix loop before placement; then assembly is mechanical.

## 3 · Canonical parts + build order (gate 5)

Build in dependency order; each gets a per-part turnaround (Gate-1 wireframe+shaded) + a
cavity/mesh detail before it is frozen:

1. **Enclosure frame** (FIRST · exact, mechanical, and the dark-rig probe): 4 corner posts,
   top + base, 750mm-wide body with the two 0U side channels; 4 EIA rails inset, front pair
   carrying the 9.5mm square-hole pattern (1U segment + array x42, holes at 6.35/22.25/38.1mm
   per-U offsets); perforated front door built but shot open/removed; optional side panels.
   The recognizable signature is the square-hole rail · get the hole pitch exact, it reads
   instantly to anyone who has racked hardware.
2. **RM44 GPU node face** (the hero part · Problem 2 lives here): 482.6mm ears + 440mm body,
   176mm tall; full triangular-perforation mesh door with a center cylinder lock + recessed
   SilverStone badge; ears with the mounting thumbscrews. The mesh is the depth-into-darkness
   bake-off (section 5).
3. **CRS354 switch face** (1U, WHITE · material variety + small-scale cavity depth): 48 RJ45
   in the grouped 2x24 grid with grouping gaps + 4 SFP+ + 2 QSFP+ cages + link LEDs; each
   RJ45 a recessed cavity (desktop USB-C-pocket treatment · dark-grey wall + AO + a lit lower
   contact, never flat black) instanced x48; white powder-coat chassis.
4. **UPS face** (2U): black chassis, recessed LCD (LEGITIMATELY emissive · see section 4),
   button cluster, vent slots, badge.
5. **Blanking panel** (1U): flat black toolless panel with the slight edge relief + optional
   vent slot.
6. **Finger duct** (1U): black cable manager fingers + cover.
7. **Cabling**, restrained: a few suggested bundles only where the open-door orbit shows the
   rail channels · bad cables read worse than none (desktop blank-rear logic).

## 4 · Problem 1 · repetition variance + the ALIVE departure

The desktop doctrine was "nothing glows." The rack REVERSES this deliberately · the acceptance
standard is "the LED field looks populated-and-alive." LEDs, link lights, and the UPS LCD are
LEGITIMATELY EMISSIVE (dim, physically-scaled). Variance is baked per-instance on the 3 RM44
nodes (and read across the whole fill, which is heterogeneous by construction so most variance
comes free):
- **LED states** from a small palette: node A power-on (green), B idle (off/amber), C on. Switch
  ports: sparse random link/activity from {off, green, amber}. No two units' LED field identical.
- **Sub-degree seating jitter**: each node yaw/pitch +/- <0.4deg and a z micro-offset within its
  U gap · real hand-racked gear is never perfectly seated.
- **Per-instance material variation**: object-space seed feeds each node's powder-coat
  micro-roughness + a faint wear/dust field (the anodize-mottle lesson applied ACROSS
  instances). Acceptance: no two adjacent units read identical at portrait distance.
Anti-drift caveat (L5): added imperfection is the tell on CLEAN product · but a homelab rack
is HANDLED hardware · light wear/dust is honest here. Every variance knob still traces to
something reference-visible (the homelab gestalt photos show real dust, cable dress, mixed LED).

## 5 · Problem 2 · depth into darkness (the bake-off · driver decides)

Measured truth (section 0): the RM44 mesh reads L~16 even in bright studio light · nearly as
dark as solid paint. So the target is NOT a tone · it is STRUCTURE: the triangular holes must
read as OPENINGS you see partway through, to a dark-but-not-zero interior with a readable
gradient. Same failure mode as the Spark dead-black pill · at 10x the count, plus the empty
rails revealing the cabinet interior.

Test at least TWO techniques on a raking-light detail tile, keep what survives (foam3d
precedent):
- **A · real cut geometry + interior box**: triangular perforation as real boolean/array holes
  through the door panel, a dark interior volume behind at measured depth (fans/drives
  suggested), interior albedo NEVER 0 (0.02-0.04 so wall gradients read), interior AO + the
  dedicated LOW INTERIOR FILL light (section 6). Hero-distance nodes.
- **B · normal+opacity map**: perforation as an alpha/normal map on a plane with a dark box
  behind · cheap, for units the camera never gets close to. The ports-vs-maps distance rule.
- **C · hybrid**: real holes on the camera-nearest node + one empty bay (the hero depth read),
  mapped for the rest.
Acceptance image: a raking-light detail of one node face + one empty bay where the perforation
and the open rail unambiguously read as openings-with-interior, not black paint. If they read
flat -> switch class, log the bake-off (grader line-149 precedent). Interior fill must not push
any exterior tone patch out of tolerance.

## 6 · Rig · inherit + the one addition (gates 7-8)

Frozen PORTRAIT_RIG + void-black doctrine carry over, tuned for the dark object (section 1),
plus ONE addition: a dedicated LOW INTERIOR FILL light living inside the cabinet/nodes so the
mesh throats + empty bays are not crushed to zero · it must NOT push exterior powder-coat/white
patches out of the exterior tone gate. This is a LIGHTING-class commit with all patches (incl.
the desktop pins, since the scale trio shares a rig) re-verified. New material patches join the
gate: powder_black, switch_white, port_cavity, rail_zinc.

## 7 · Materials (tone-pinned, one clean source each, spread recorded)

powder-coat black (matte satin, fine texture · NOT bead-blast, NOT anodize · a NEW target) ·
switch white/light-grey · recessed port cavity dark-grey+AO · RJ45 gold contacts · SFP+ cage
dark nickel · rail/cage-nut zinc (semi-bright) · UPS black plastic + emissive LCD · LED
emitters (green/amber palette) · cable jackets (restrained). Each pinned + gated like the
desktop metals.

## 8 · Assembly, photoreal, portraits (gates 6, 9, 10)

- **Assembly (6)**: instance the fill map (MEASUREMENTS) by U-arithmetic; bake variance; gate =
  full-rack front + 3/4, no adjacent units identical.
- **Photoreal (9)**: full tell taxonomy + the TWO rack tells (array uniformity, dead-black
  depth) as first-class ledger rows. Blind 5-agent panel, real-photo pool now includes real
  datacenter/homelab rack photos of OTHER gear so recognition cannot leak. Same pass condition.
- **Portraits (10)**: front, 3/4, node-face detail, empty-bay depth detail, and THE SCALE TRIO
  · Mac Studio + DGX Spark + rack in one frame at TRUE relative scale (U-module + desktop
  measured dims make it exact · the rack ~1991mm vs the ~95mm Studio is a ~20:1 story, the
  desktops tiny at the rack's foot). The strategic payoff image the site wants most.

## 9 · Risk register

| risk | severity | mitigation |
|---|---|---|
| dark object vanishes on void-black | HIGH | edge+sheen lighting (Apple dark-hero); frame-first rig probe before expensive parts |
| tone-gate O=-12 doesn't transfer to L16 | HIGH | measure in-rig, separate rack rig if needed, LIGHTING-commit re-verifies desktop pins |
| dead-black mesh reads flat (Problem 2) | HIGH | technique bake-off + raking acceptance, real holes on hero node |
| 3 identical nodes read CGI (Problem 1) | MED | per-instance variance system + emissive LED palette |
| square-hole rail wrong pitch | MED | exact EIA offsets, 1U array, measure.py verify on the render |
| scale-trio rig drifts closed desktops | MED | shared-rig changes re-verify desktop pins every time |
| glb bloat from 3 nodes x mesh holes | LOW | linked instances + array modifier + foam3d-style cache |
