# render/CONCERNS.md · accuracy concerns + next steps (for the grader)

Date: 2026-07-02. Scope: the two procedural devices in `render/` (Apple Mac Studio,
NVIDIA DGX Spark) as shot in the phase-4 portrait set. Dash gate: middot only.

This report exists because the grader reopened the Studio on geometry (port corner radii;
flat-top edge) and said "there is more to it but we are close." I ran an exhaustive audit
(7 vision agents, one per rendered angle, each comparing against its reference photos, plus a
synthesis pass) and then reconciled every finding against the ACTUAL shader and geometry
constants in `build_scene.py`. That reconciliation matters: several "wrong colour" findings are
lighting artifacts of the deliberate void-black portrait rig, not base-material bugs, and I have
flagged those honestly rather than parroting them as albedo errors.

Status: neither device is closed. Both reopen. But the read is fair: silhouette, feature layout,
port count/order, and material FAMILY are all correct on both devices. What remains is a bounded
punch list, heaviest on the Spark front-face STRUCTURE, both devices' port CAVITY depth, the two
grader-confirmed Studio geometry items, and one lighting-direction decision. "Close" is accurate.

---

## 0 · The one decision that gates re-rendering: lighting direction

The audit's single largest cluster of findings is "too dark / reads charcoal / foam muddy." I
need to separate two things honestly:

- The Studio aluminium base IS silver: `blasted_aluminum()` is `(0.86, 0.87, 0.89)`
  (`build_scene.py:277`) · that is bright Apple silver, NOT charcoal. The dark read is the
  portrait rig: `expo=-1.15`, a dim single key, and a void-black world (`portrait_rig`,
  `:834`; Studio rig `key_e=34`). Under that rig, correct silver renders as a dark dramatic
  hero object. This is art direction, not an albedo bug.
- The Spark champagne base `(0.68, 0.55, 0.29)` (`champagne_gold`, `:452`) is a genuinely
  darkish/saturated gold; under the same dark rig it reads ochre/mustard. Here BOTH the base
  (slightly dark + saturated) AND the lighting contribute.

**Decision needed (D1):** keep the void-black dramatic hero lighting (stylish, but reads darker
than every reference press shot), OR relight bright and even so the metals read at their true
Apple-silver / champagne tone (literal accuracy, matches references). Most tone findings below
resolve automatically under a bright even relight; a few (Spark base, foam pore albedo, Spark
top material) are real and must be fixed regardless. Everything downstream re-renders once, so
this call should come first.

---

## 1 · Grader-confirmed geometry (Studio) · highest priority

| # | Concern | Where | Fix |
|---|---|---|---|
| 1 | **Port/slot corners too square.** USB-C + SD openings render as near-sharp rectangles. `cutter_box` rounds only the VERTICAL edges (`rounded_box(w,d,h,r,0,0)`, `:248`) so the slot's top/bottom ends stay dead sharp and the only rounding curves back into the hole depth where it is invisible. | `:528-532` | Re-cut USB-C and SD with the existing `stadium()` primitive (rounds in the X-Z / front-face plane, as the Spark pills already do). USB-C R = w/2 = 1.31 mm (full stadium ends); SD R = h/2 = 1.25 mm. No guessed value · the radius is the geometric half-width. |
| 2 | **Top-edge fillet too round/pillowy.** `top_fillet_build=8.9` puts an ~8.3 mm round-over on a 95 mm-tall body · the top reads as a soap-bar and the front face loses height. The "measured" `top_fillet_R=8.27` (`:135`) is contradicted by the grader's eye; it most likely caught a perspective-conflated footprint corner. | `:136` | Remeasure the top-edge radius off the dimensioned side elevation (`ref/mac-studio/dim_back-side.svg.png`) and the `apple_front` top edge, then tighten `top_fillet_build` to a small round-over (candidate ~2-3 mm pending remeasure). This also recovers the flat top and collapses the blown highlight band. |
| 3 | **Front face barrel-bowed.** The central front panel bows convex (brightest reflection mid-face) instead of staying planar. Entangled with #2 but distinct. | body build `:508-512` | Ensure only the vertical corner fillets curve; the central panel must be flat. Verify after the fillet fix. |
| 4 | **USB-C ports too short/stubby; SD slot too short.** USB-C should read ~3.2:1 tall (2.62 x 8.47); SD should be a long thin 26.85 mm slot clearly wider than the USB-C pair, with stadium ends. | `:528-532` | Confirm the modeled sizes render at the right aspect after the stadium re-cut (values are already correct in the dict; the square corners were making them read stubby). |

---

## 2 · Port + pocket CAVITY depth (both devices) · they read as flat black decals

Every opening on both devices is a flat near-black fill flush with the surface · no bezel, no
interior wall, no depth cue, no connector. This is the second-most-damaging class after tone.

- **Studio USB-C:** no recessed bezel ring, no lighter grey cavity wall with AO, no USB-C
  tongue/blade inside. Reference shows a chamfered bezel, a mid-grey recessed wall, and a
  centered striated tongue. Fix: inset ~0.4 mm + bevel, extrude cavity ~4-5 mm, dark-grey wall
  material, add the tongue blade (`:543-549` already builds a blade · verify it sits visible in
  the cavity, currently swallowed).
- **Studio SD slot:** plain black bar. Reference shows interior contact teeth + a lower
  retaining lip. Fix: recess + add the contact comb.
- **Spark pills:** filled with a same-tone plug (read as bosses, not holes). The pill tubs
  (`:622`) sit too shallow / too bright. Fix: deepen the recess, darken the interior wall, add
  contact shadow so they read as real finger-slots.

---

## 3 · Spark front-face STRUCTURE · the biggest miss, three linked geometry errors

The reference front is NOT foam-edge-to-edge with floating ovals. It is: solid smooth champagne
**end-cap panels** at both ends, a **bounded foam field** inset in the center span, and **deep
recessed finger-slots** carved into those solid end-caps · with the NVIDIA mark on the left cap.

| Concern | Current | Reference | Fix |
|---|---|---|---|
| Missing end-cap panels | Foam runs full-width (`foam_field` 148 x 46, `:627`); pills float in foam | Solid champagne vertical end-caps (~one pill-width) frame a center foam band | Add solid champagne caps L+R; inset foam to the center span; bound it with champagne margins on all four sides |
| Hand-holds are flush ovals | Flat champagne ovals on the foam surface, no depth | Deep finger-slot recesses in the solid caps, visible interior wall + raised rim | Model real pocket depth (several mm) with a champagne interior wall/floor + occlusion |
| **NVIDIA logo + wordmark missing** | Absent | Green NVIDIA eye + vertical "NVIDIA" on the left cap, above the lower slot (`ref/dgx-spark/cl_front-lower-logo.jpg`, `sth_front-1`) | Add the decal on the left cap once it exists. Note: prior loops kept pills BLANK as a trademark gate · adding the real mark is a deliberate reversal of that gate and needs your OK (**D2**). |

---

## 4 · Spark TOP panel · wrong material AND wrong feature

- **Material bug (genuine, not lighting):** the top face uses the SAME `champagne_gold` shader
  as the shell · there is no separate dark material. The reference top is a distinctly DARK
  slate/charcoal matte-satin panel that contrasts against the gold front. Fix: assign the top a
  separate dark-slate material (~sRGB #2b2c2e, low saturation, satin) · do not share the gold.
- **Feature/geometry:** our top has a large central recessed rounded-rect tray with a pillowy
  12 mm-radius border (`cutter_box(top_panel_w, top_panel_h, 3.0, r=12)`, `:612-613`). The real
  top vent is an EDGE-ALIGNED recessed panel carrying a fine hex mesh / ~45deg diagonal weave
  behind a smooth bezel (SOURCES confirms a weave-vent panel exists; `cl_side-profile.jpg`).
  Fix: move the recess to the edge, tighten the border, and add the hex/weave texture · not a
  soft central basin.

---

## 5 · Foam realism · reads as reptile-skin crackle, not open-cell metal foam

Multiple axes fail at once; this is the Spark's signature material so it carries a lot of weight.

- **Cells ~3-4x too fine and too uniform.** `foam_cell_cm=13.75` (~0.73 mm cells) vs a coarser,
  irregular reference (~6-7 cells/cm, ~1.5 mm, with strong size variance). Fix: coarsen
  `FOAM_CELL`, add per-cell size variance (randomized Voronoi spacing / log-normal radii).
- **Cell shape wrong.** Angular/faceted Voronoi shards vs rounded blown-bubble voids. Fix:
  relax toward circular voids, round the strut junctions.
- **No real depth / through-porosity.** Reads as a bump-mapped skin on a solid core · grazing
  light produces no cell-wall highlight/shadow separation. Fix: drive real displacement /
  2-3 strut layers so front cells occlude deeper struts (the settlement's pore-depth PASS was
  measuring contrast spread, which survives, but the eye still reads it flat · honest).
- **Pores near-black by design.** Pore albedo is literally `(0.012, 0.008, 0.004)` (`:475`),
  L~2, vs reference soft L~41. I darkened pores hardest to pull the mean to the sth_front-1
  target · it overshot. Fix: lift pore albedo to a soft grey/champagne and let AO + geometry
  carry the depth instead of black albedo.
- **Struts wrong colour + finish.** Uniform saturated olive-gold `(0.585, 0.44, 0.155)` (`:476`)
  with a machined/threaded micro-texture (reads like tiny screws). Reference struts are a
  desaturated grey-champagne with sparse pinpoint glints. Fix: desaturate + lower value, move
  the gold into narrow speculars, remove the threaded normal.

---

## 6 · Proportions, base, and intake mesh (Studio)

- **Both bodies read too tall / near-cubic.** Verify absolute heights render at 95 mm (Studio) /
  50.5 mm (Spark) so the footprint dominates and the pair shows a clean ~2:1 height delta.
  (Partly a consequence of the pillowy top eating apparent height · re-check after #2.)
- **Studio base reveal missing.** No recessed foot/undercut · the body meets the ground flush.
  Add the dark base ring so it reads as a floating slab (`reveal_gap=2.5` exists in the dict but
  is not reading in the portrait).
- **Intake mesh wrong on three counts + one open contradiction.** Holes too coarse/large,
  square-packed instead of hex-staggered, band too tall. `perforated_band()` (`:359`, `ph=1.70`):
  shrink hole diameter, tighten to a hex offset lattice (~3-4x more holes), reduce band height,
  tuck under the bottom overhang. **Open question (needs a measurement, not a guess):** the
  front audit says the mesh must NOT wrap the vertical corners (corners stay solid aluminium);
  the detail audit says the corner-wrap is correct. Resolve against `apple_front` + a lower-corner
  macro before locking the mesh.

---

## 7 · Staging (tabletop pair) + finish polish

- **Pair not coplanar.** The Spark is tilted (top pitches up, front edge lifts) and sits on a
  different plane than the Studio · reads as floating/inclined. Fix: zero the Spark's X/Y tilt,
  re-seat both on Z=0, match camera-relative yaw, bring the Spark inward to share the baseline.
- **Spark grounding/shadow unbelievable.** Long hard cast, no contact AO. Add contact occlusion,
  shorten/soften the cast, confirm one shared key drives both objects.
- **Studio front microtexture too sandy** (`blasted_aluminum` roughness noise 0.34-0.42 +
  bump 0.02, `:284-293`) · reduce amplitude for bead-blast, not sandpaper.
- **Spark champagne top lip too wide/blown** · narrow to a pinstripe, kill the gloss bloom.
- **Studio LED reads as a smudge** · render a clean small dot, remove the adjacent artifact.

---

## 8 · Verification gaps · faces we have NEVER rendered (render before final)

These are unverified because no portrait exists at that angle · not passes, just blind spots:

1. **Studio REAR I/O** · power inlet, Ethernet, Thunderbolt/USB-A, HDMI · never modeled-checked.
2. **Studio BOTTOM** · the circular foot / base intake reveal is only inferred (and found
   missing on the 3/4). No bottom portrait. (Also the one documented reference GAP.)
3. **Spark TOP vent** · every top view so far shows the WRONG tray; the real edge-aligned
   hex/weave vent has never been rendered. Needs a dedicated top-down after the #4 rebuild.
4. **Spark REAR I/O** · power, networking (QSFP), USB/display · unverified.
5. **Both device SIDES** · only glimpsed obliquely; the Spark side showed speckle-noise failure
   on the pair. No straight side profile exists.
6. **Spark end-caps + logo placement** · can only be verified after #3/#4 add the caps.
7. **Port cavity interiors** · re-macro after #2 adds real depth.

---

## 9 · Strengths · correct, do NOT regress

- Studio front I/O layout, count, order (2x USB-C then SD left, LED far right, one baseline).
- Studio silhouette family: square-footprint slab, rounded vertical corners; USB-C vertical, SD
  horizontal; round-hole intake band concept in the right place.
- Spark silhouette: elongated foam-clad strip, two vertical hand-holds near the ends in correct
  pill/stadium shape; two-material (foam front / smooth top) split concept; warm-gold hue family.
- Foam applied as a non-tiling stochastic field (no repeating seam).
- Camera framing on every angle (dead-on fronts, valid 3/4s, ~35deg tabletop eye line).
- Pair footprint ratio (~0.6-0.75, near the 0.76 target); Studio correctly the larger device.

---

## 10 · Recommended sequence (proposal · your call on scope)

0. **D1 lighting decision** (void-black vs bright-even) · gates all re-renders.
1. Studio confirmed geometry: stadium port cuts + tighten top fillet (+ flatten front) · §1.
2. Port/pocket cavity depth on both · §2.
3. Spark front-face structure: end-caps + bounded foam + recessed slots · §3. Then **D2** on the
   NVIDIA logo, then add it.
4. Spark top: dark-slate material + edge hex/weave vent · §4.
5. Foam realism: coarser irregular cells, lift pore albedo, desaturate struts, real depth · §5.
6. Proportions + base reveal + intake mesh (resolve the corner-wrap question first) · §6.
7. Staging + finish polish · §7.
8. Render the un-shot faces · §8. Re-audit against references. Present again · no self-grading.

Each wave is one change class, committed on its own, clip-checked, with an evidence crop, per
the standing loop protocol. I will not declare closure · I will present compare sheets and let
you grade.

## Decisions I need from you
- **D1:** void-black dramatic hero lighting, or bright-even literal-accuracy lighting?
- **D2:** add the real green NVIDIA logo + wordmark to the Spark front (reverses the earlier
  blank-pill trademark gate)?
- **D3:** scope · do you want the full sequence above, or a subset for this next pass?

---

## Export manifest (this folder)
- `portraits/` · all 7 4K angles: mac-studio-front / -q34 / -detail, dgx-spark-front / -q34 /
  -detail, oracles-pair@3x.
- `sheets/` · phase4-portrait-set (contact), settle-studio-q34, settle-spark-q34, and the four
  compare-* nearest-reference sheets.
- `CONCERNS.md` · this report.
