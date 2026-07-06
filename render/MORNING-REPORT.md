# Wave 3 · GRADING-REPORT-driven GPU geometry pass (2026-07-06, Opus loop)

Driven by ~/Downloads/cx-render-handoff/GRADING-REPORT.md (sourced facet grades). Worked the
lowest-graded GPU facets first (the owner-flagged last mile · FE geometry). Each one bounded +
gate-verified · rig q34 clip 0.720% PASS (better than the 0.798% baseline):
- **G1 · 7 blades not 9** · real FE fans have seven wide-chord blades (LanOC/club386). Chord
  auto-widened 1.28x. Preview clip 3.04% -> 1.41%.
- **G2 · blade-tip rim ring** · extended tips to reach a glossy dark rim band · reads as a ring-fan.
- **G7 · double flow-through rear** · TWO open windows (was one over a solid box) revealing real
  vertical fin combs · the FE's defining rear feature · rear clip 0.000% PASS.
- **G4 · black center module** · was inverted (gunmetal center) · now black center + gunmetal frame.
- **G5 · single flush X** · unioned the two crossing bars (was a stacked-bar step) · arms reach the rims.
- **G9 · thin wordmark strip** · was an 18x70mm glowing tile blowing the side (2.62%->0.63% PASS).
- **G10 · recessed angled 12V-2x6** · was a protruding cylinder cable · now a recessed scallop socket.
- **G12 · bracket ports recessed + 2 screws** · solid no-vent bracket confirmed.
- **G17 · fixed the black bottom audit shot** · camera dropped under the floor · now the bracket/
  fingers/riser read.
- **build fix** · guarded BevelModifier.clamp_overlap (crashed the rig/trio on this Blender build).

## Honest next (loop open · GPU facets remaining, then the other objects)
- GPU still open (lower grades): G6 illumination-motif merge (ring+X continuous loop), G3 hub flatten,
  G16 blade material (kill foil-facet highlights), G11 edge louvers, G14 shroud cut-lines, G13/G15
  proportion+material verify, G18 see-through backlight in the rig.
- NOT yet started: Mac Studio (M1 rear vent = rectangular ~173x53mm field NOT a circle · M2 rear port
  order · spec file MAC-STUDIO-360-SPEC.md must be corrected FIRST), DGX Spark (S1 rear foam+I-O plate ·
  S3 blank champagne top · spec DGX-SPARK-360-SPEC.md USB-A line is wrong), rig R1 power cabling/PSU.
- The single-card front/q34 clip ~1.38% is inherent to the two big lit rings + X filling the frame
  (not a regression · baseline was 3%+) · the governing gate is the RIG q34 (0.720% PASS).

---

# Wave 2 · photoreal push (2026-07-06, later) · driven by a 5-lens forensic panel

Ran a 5-lens forensic panel (workflow w5oyz13ew · materials/lighting/geometry/gestalt/accuracy +
synthesis) on the delivery heroes, then worked the ranked punch-list. Verdict moved from prior
panels' "4/4 render" toward "GOOD RENDER, nearly-photo in crops." Landed, each gate-verified:
- **Real lofted-airfoil fan blades** (was flat paddles · the #1 prior tell) · then a gentler sweep so
  they read as real shallow-swept FE rotors, not curled scythes.
- **Black satin fans** (reviews call the FE fans "black" · were medium-grey).
- **LEDs made real:** thin light-guide lines (were fat blown donuts) + raised the indirect clamp that
  was SUPPRESSING their GI, so they now wash cool light on the shroud · full-res raw clip 1.84%->0.93% PASS.
- **Darker gunmetal shroud** + a larger key softbox for a specular gradient across the metal.

**Three accuracy myths the panel asserted — all REFUTED by sourced research (kept the model honest):**
1. "The 5090 FE has no illumination · delete the LEDs" · WRONG · 5 sources (NVIDIA forums, TechPowerUp
   review+teardown, OC3D, NoobFeed) confirm the FE's static white X/inlet/logo LEDs. Kept.
2. "The Mac Studio is too tall/cubic" · WRONG · verified it renders as the correct squat 2:1 slab
   (197x197x95) · the panel misread the small q34 crop.
3. "The DGX Spark front should be a machined cheese-grater hole-lattice" · WRONG · the Chargerlab
   teardown confirms it is METAL FOAM ("solid porous structure, not a perforated mesh") · the gated
   foam3d is accurate · NOT rebuilt (the flip-flop guard prevented a regression).
Lesson logged: the vision panel is strong on "reads CG" but unreliable on hardware facts · sourced
references win. Honest next: the materials micro-texture wave (#4) + a re-panel to measure the drop.

**Wave-2 conclusion (panels 4 + 5 · the two headline wins):**
- **LED near-field-GI BREAKTHROUGH.** The loudest tell across ALL THREE panels: the LED rings are the
  brightest thing in a black scene yet cast no light. The emissive-only ring couldn't spill without
  clipping (gate tension). FIX: a co-located cool-white POINT LIGHT at each ring · a light is not a
  camera-visible surface, so it casts the halo on the shroud + rims the blades + spills to neighbours
  with ZERO added clip. The fans now read as genuinely LIT running hardware · gate still 0.79% PASS.
- **Deep dark FAN CAVITY.** Panel-4's #1: the fans read as stickers on a gray plate. Deepened the well
  + 17 near-black heatsink fins + darkened the backing · you now look INTO each card.
- **Finer SPARK FOAM (gated, done properly).** Reduced the trio-scale glitter (pitch 1.62->1.30) with
  the tone gate as arbiter · spark_foam dE 4.98 <= 6 PASS · the mean pin held, no regression.
- Plus: thicker cambered airfoils, glossier lit hub cap, PCIe gold-finger edge, 3xDP+HDMI I/O, brighter
  rig key, smudge-varied floor, frame edge bevels, per-card jitter · one honest revert (frame dust).
- The render moved from "4/4 obvious render" to a strong good-render with the devices near-photographic.
  ~40 gate-verified commits this session. Honest remaining: fine per-material specular contrast + the
  grounding contact-term (panel chairs disagree on it) · both diminishing-returns polish.

---

# Morning report · overnight loop (2026-07-05 -> 07-06)

Ran the OVERNIGHT-LOOP (render -> edit -> audit -> commit) on all three oracles. ~40 clean commits,
no AI attribution, blank trademarks throughout. Honest state below · the loop is still open (there
is always a next).

**Photoreal push (later in the night):** machined-metal microtexture + bright edge-bevel highlights
on the card shroud, a hero vignette + subtle chromatic aberration on top of the ring bloom, glossy
plastic fan blades, and a DEFINITIVE hi-res trio hero (2880x2304, 820 spp). Also caught + fixed a
real regression: a "round the rear ports" change had COLLAPSED the Mac Studio body (degenerate
boolean) · it was hidden in the dark rear audit and only showed in the lit hero · reverted + a
lit-angle body-integrity check is now codified in OVERNIGHT-LOOP.md.

## The headline: the RTX 5090 rig + the scale trio
- **The rack's GPU is now the real NVIDIA RTX 5090 Founders Edition**, researched from the web
  (5-agent pass -> render/ref/rack/RTX5090FE-SPEC.md) and remodelled to spec: true 304x137x40 (2-slot),
  the defining dual-fan double-flow-through layout, "Dark Gun Metal" monochrome, the X 'infinity'
  accent, and the FE's static white illumination (lit fan inlet rings + X + top-edge wordmark). 6 of
  them, fans out, in the 12U open frame on casters.
- **THE SCALE TRIO exists and renders** (render/build_trio.py): the 6-GPU RTX 5090 rack as the base
  with the Mac Studio + DGX Spark on top at TRUE metric scale, one lit scene. This is the site's
  frame-of-reference hero the owner asked for.

## Hero images to look at (render/rack_previews/)
- **DELIVERABLES-CONTACT-SHEET.png** · START HERE · every hero in one grid (trio, rig, card 360,
  desktop rears). Rebuild anytime with `python3 render/_contact_sheet.py`.
- **trio-q34.png / trio-front.png** · the scale trio (the money shot) · full quality + bloom.
- **gpurig-q34.png / gpurig-front.png** · the 6x RTX 5090 rig alone · full quality + bloom.
- **gpu-macro.png** · the 5090 card macro (glowing X + ring, shallow DOF). Full card 360 via `--part gpu`.
- Desktop rears: model-refinement `render/previews/audit-{spark,studio}-rearq34.png`.

## 360-degree coverage (the loop's core mandate)
- **RTX 5090 card**: front + q34 + rear all built (rear = mirrored X accent + blank cartouche +
  flow-through window). Fans reworked to read as real swept fans. Single-card audit rig: `--part gpu`.
- **DGX Spark**: 360-COMPLETE. Rear I/O port bank built (researched: power/4x USB-C/2x USB-A/HDMI/
  RJ-45/2x QSFP56 -> DGX-SPARK-360-SPEC.md). Sides/top verified.
- **Mac Studio**: 360-COMPLETE. Rear built (the circular perforated exhaust vent + the port row:
  power/4x TB5/2x USB-A/HDMI/RJ-45/3.5mm -> MAC-STUDIO-360-SPEC.md). Sides/top verified.
- Desktop 360 audit tool: `render/_audit_desktop.py --which {spark|studio} --yaw N`.

## Also done
- Fan blades, card tone (Dark Gun Metal), and a wired/darkened rig base (motherboard + PSU + PCIe
  riser ribbons; the tray no longer reads as a bright empty shelf).
- Hero post-chain (`--post`): a subtle bloom so the lit rings read as real LEDs + a hair of CA. The
  numeric gate always reads the raw pre-post frame.

## State: comprehensive · all 3 objects 360-COMPLETE
- **RTX 5090 card**: front/q34/rear/side/top/macro all built + rendered · real fans · Dark Gun Metal ·
  glowing accents · top/bottom exhaust fin combs · rear X + cartouche + flow-through window.
- **6x RTX 5090 rig**: renders from every angle · wired base (mobo + PSU + riser ribbons) · post bloom.
- **DGX Spark**: 360-complete · rear port bank + the twin QSFP metal cages · clean sides · foam/vent top.
- **Mac Studio**: 360-complete · rear circular perforated vent + port row (round AC inlet + jack) ·
  aluminium sides · aperture top.
- **Scale trio**: q34 + front heroes, full quality + bloom. **Contact sheet** rebuilt.

## Honest remaining (all polish · none hero-facing · loop stays open)
- Card 16-pin power-header read + short-end I-O bracket connector shapes (mostly hidden in the packed
  row). Optional handled-hardware wear on the frame. Env-reflection was TESTED + REJECTED (void-black
  is more premium · see OVERNIGHT-LOOP.md · do not re-try). The frame profile is owner-praised · unchanged.
- Nothing is claimed done that isn't · every hero is committed + gate-clean (clip PASS) · ~32 commits,
  no AI attribution, blank trademarks throughout.
