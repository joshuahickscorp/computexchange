# RACK DETAIL AUDIT · the deep re-audit before scaffolding (2026-07-05)

8 audit units (one vision agent per reference cluster + the process docs), pixel-ratio evidence,
raw findings in `render/rack-audit-raw.json` · arbitrated here against the measured pins and my own
direct reads (frame render + rm44_front_A studied by eye). Standard: **Apple/NVIDIA launch grade** ·
a person who owns this hardware finds nothing missing, nothing off, at any deliverable distance.
Dash gate: middot only. This file feeds RACK-LOOP.md v2 (the Opus grind queue).

## Verdict in one line
The plan's skeleton is right (U-arithmetic, real-holes technique, tone pins, numeric oracle) but the
DETAIL LADDER was ~40% specified · the audit found one severity-5 dimensional error in the only built
part, a structural misread of the hero mesh, an untruthful fill map, and six desktop scar-tissue
lessons missing from the loop protocol. All fixed on paper below · nothing here is expensive to
build, it was just unplanned.

## Scorecard (arbitrated)

| unit | grades | headline |
|---|---|---|
| frame vs refs | proportions 2 · holes 8 · posts 3 · overall 4 | **S5: frame 27% too wide** (rails prove it) · feet/casters missing · posts half-weight, featureless |
| RM44 node (hero) | refs 6 · plan coverage 4 · ladder 4 | **S5: mesh is TWO-LAYER** (coarse tri punches + fine filter screen behind) · lock has a bail handle · badge zone is a keystone plate · border fade rows |
| CRS354 switch | refs 7 · plan coverage 5 · ladder 4 | port grid is 4 GANGS of 2x6 with mirrored bottom row (latch flip) · console/MGMT stack · gang window bezel |
| UPS | refs 4 (photo gap known) · coverage 6 · ladder 5 | face is grille-left / control-right SPLIT with scallop pillars · CGI-vs-photo disagreements logged |
| accessories | refs 6 · coverage 5 · ladder 5 | cage nut = square cage + collar + castellated spring wings · blanking edge returns + relief notches · M6 screw two head styles |
| enclosure detail | refs 7 · coverage 4 · ladder 3 | casters + levelers + corner gusset castle plates + hinge blocks + brush strips · orange-peel powder texture |
| homelab gestalt | rules 7 · variance coverage 3 · **fill map 2** | fill map too neat: needs deliberate voids, top network band, shelf gear, front cable arcs, LED budget |
| process | loop 6 · oracle 4 · lesson transfer 5 | six desktop lessons absent (see section 4) |

## 1 · Arbitration rulings (measurement > agent > eye, conflicts resolved)

1. **Frame width S5 · CONFIRMED, elegant proof.** The rails' own hole pattern gives the px/mm scale
   (1U = 16.84px) · height back-computes to 2005mm (right) but outer width to ~760mm vs 600 real.
   The rails are CORRECT (c-c 464.5 vs 465.1 spec) · the walls/posts sit ~80mm too far out per side.
   FIX CLASS: geometry, move walls/posts inboard, FREEZE rail x. (My frame eyeball missed this ·
   the pixel arithmetic caught it. This is why the oracle audits every part.)
2. **Frame tone reads mid-grey, not powder-black** (my own eye on frame-frame-front.png; the
   committed L≈22 claim does not match the washed read of the png). RE-MEASURE the committed frame
   render with rack_verify before gate 8; suspect the preview exposure or the patch box. Autopsy
   rule applies: if the L numbers were read off a contaminated patch, strike and re-derive.
3. **RM44 two-layer mesh S5 · CONFIRMED TWICE** (my eye + the unit independently). The depth story
   is door punches -> FINE FILTER SCREEN (~0.8-1mm micro-perf, visibly textured) -> darkness. The
   bake-off's real-holes verdict STANDS for layer 1, but the interior plan changes: the filter is a
   mapped micro-perf plane ~3-5mm behind the door (maps are legit at that scale · ports-vs-maps
   distance rule), fans/dark box behind THAT. This kills the flat-void read cheaply.
4. **Lock**: bail/wing handle on a cylinder body, sitting where the keystone badge plate dips the
   mesh · not a bare cylinder. Badge = proud keystone plate with chevron bottom, blank per
   trademark gate.
5. **CRS354 "tan monoblock bezel"**: plausible but VERIFY-THEN-PIN · one agent, one photo · confirm
   the connector-block color against a second reference before pinning a material.
6. **"Rear rails missing"**: front-facing doctrine says model them · they read through the open
   front between units (severity kept at 2, build at assembly wave).
7. **Fill map 2/10 truthfulness · ACCEPTED.** The D-ARCH fill was designed for build convenience,
   not gestalt truth. Revised fill (section 3) keeps every verified part, adds the gestalt rules.
   D-ARCH archetype itself unchanged (grader-locked).

## 2 · The detail ladders (what Apple/NVIDIA-grade requires, per part)

**Enclosure**: width 600 fix · post faces to ~45mm with hinge bosses x3 + latch keeper · top band
to ~45mm · 4 leveling feet (pad ~45mm, 25-40mm floor gap) + twin-wheel casters inboard · corner
gusset castle plates · rear rail pair (holed) · U-number strip on rail flange (blank tick band ·
numerals only if legible-neutral) · brush/air-dam strips inboard of posts · orange-peel powder
micro-texture (NEW material target, not bead-blast) · base band slot row + pan screws.

**RM44 x3**: door = real tri punches (P2.87/R2.59, rounded-corner triangles, orientation
alternating) with BORDER FADE ROWS (partial triangles clipped into the frame, a 1-2 row dimmer
transition) · fine filter layer behind (mapped micro-perf, ~4mm setback) · fan wall behind filter
(3x 120mm rings + hubs, blades never crossing) · keystone badge plate (blank) + bail-handle lock at
measured y · top lid overhang lip + bottom sill with 2 corner screws + 4 witness dots · EARS:
folded flange to 482.6, 2 thumbscrews each (knurled, proud), ear-to-body fold radius · per-instance
LED/roughness/seating variance.

**CRS354**: massing + faceplate seam · 48 ports as 4 GANGS of 2x6 (gang gaps > port gaps), TOP row
latch-up, BOTTOM row MIRRORED latch-down, per-port LED pipe pair above/below respectively · gang
window recess with its bezel (color: verify-then-pin) · SFP+ 2x2 cage cluster + QSFP+ 2x1 stacked
(cage lips, bale latches) · console + MGMT RJ45 stack far right · status LED column + reset pinhole
· white powder chassis + ears (+7mm projection) · silkscreen zones as blank shapes.

**UPS**: 432x89 bezel, grille-LEFT / control-RIGHT split massing · louvered vent grille (count/
pitch from photo, recessed) · recessed rounded-square control plate flanked by concave scallop
pillars · power bar at panel top · LCD window (dim emissive, AsX-safe) + 2x2 button cluster ·
ears + badge blank. CGI-vs-photo disagreements resolved toward the PHOTO.

**Accessories**: blanking panel with edge returns + end relief notches + mounting slots (not bare
rectangle) · duct finger count/pitch/slot profile + cover · CAGE NUTS: square cage, extruded
collar, castellated spring wings, zinc · placed at OCCUPIED holes only + 2-3 spares · M6 screws
two head styles with flanged washers · bare square holes stay empty elsewhere.

**Assembly/gestalt (fill map v2)**: bottom-heavy mass gradient · UPS at U1-2 · nodes low-mid with
ONE deliberate 1U void between repeated chassis · network band PINNED TOP (switch high, duct
directly below it, patch-cable catenary arcs crossing the front to the nodes' near columns) · one
shelf with a non-rackmount box (scope-decision: generic black mini-PC, blank) · LED budget: ~60%
of possible LEDs lit, mixed green/amber, 2-3 blinking-bright, exposure-correct dimness · light
dust/wear at handled edges only (rack is HANDLED hardware · desktop clean-product law inverts,
but every knob traces to a gestalt photo).

## 3 · Fill map v2 (replaces the MEASUREMENTS row · REMEASURE-class commit when adopted)
U1-2 UPS · U3 blank · U4 VOID (deliberate) · U5-8 node A · U9 VOID · U10-13 node B · U14 blank ·
U15-18 node C · U19 duct (patch slack) · U20 switch... MOVED: switch to U38-40 zone per gestalt
top-band rule -> final: U37 duct · U38 switch · U39 blank · U40 shelf+minipc · U41-42 empty ·
U21-36 empty rails (the honest void run). Exact map written to MEASUREMENTS.md on adoption.

## 4 · Process gaps to port into RACK-LOOP v2 (all six now included)
flip-flop guard (2-ref re-measure before re-changing any once-changed value) · autopsy-on-
overturned-value protocol (codified template) · DOF-per-shot-class table + true-macro rule (L18
lesson: detail shots get a REAL close camera, never a crop) · post chain (grain/CA/bloom/vignette/
roll) applied post-gate, gate is PRE-post · firefly clamp (indirect clamp value from desktop L13)
· panel FALSE-TELL calibration (controls MUST include real rack photos of other vendors' gear +
the known clean-product bias; two consecutive readings before believing any delta) · per-object-
class tone offsets for the scale trio (O_desktop=-12, O_rack=0, re-verify BOTH on any shared-rig
change) · mesh-pitch FFT tool moved into repo as render/rack_measure.py (out of scratchpad).
