# Photoreal panel · ranked CG tells + status (forensic critique 2026-07-06)

A 4-lens forensic panel (materials / lighting / fans-geometry / layperson gestalt · run via Workflow)
read the final GPU-rig heroes and ranked what still reads as CG. Work these top-down. Full transcript:
task w3csvvvaf. This is the loop's panel mandate (L12) applied to the rig.

## Ranked tells (most impactful first) · status
1. **[DONE]** The rig FLOATS — no grounding (all 4 lenses). Fix applied: floor a hair reflective
   (rough 0.62->0.34) so the rig grounds with a faint reflection, staying near-black. Both the
   rack_rig floor + the trio floor.
2. **[DONE]** LEDs glow but EMIT NO LIGHT (all 4 lenses). Fix applied: boosted the emissive rings +
   X (2.6->4.0, 2.3->3.2) so they cast a real GI wash onto the shroud/frame · clip re-gated 0.70% PASS.
3. **[DONE · real bmesh rebuild]** Fan blades were FLAT PADDLES, not airfoils (macro + gestalt).
   FIXED: `_fan_blades()` now lofts each blade as a real cambered foil across 6 span stations —
   chord taper (root->mid bulge->tip), root-to-tip twist (38deg AoA at hub -> 16deg at tip), sickle
   sweep (LE trails back), ~1.4x-pitch overlap so you cannot see through to the well. One watertight
   mesh per rotor. Verified at macro + card-front + rig-q34: reads as a genuine 5090 FE dual-fan
   rotor. Clip-neutral (blades are dark/recessed · the 1.15% rig clip is the LED rings, unchanged).
4. **[TODO]** One flat CLAY material on everything — no dust/fingerprints/edge-wear, roughness uniform.
   Fix: split BSDFs further + a subtle grime/dust layer (dust in fan corners, faint smudges on the
   Studio aluminium, edge wear on frame corners). The rack is HANDLED hardware · honest wear allowed.
   Partly started: machined-metal microtexture + edge bevels on the card. Frame/desktops still clean.
5. **[DONE (grain) · PARTIAL (background)]** The infinite studio VOID. Grain applied (_grain.py, panel
   said 'no grain' is a render signature). Still open: a LIT curved seamless (floor-to-wall cove) so the
   bright patch pools radially, + a touch of atmospheric haze. (Void gradient kept · owner-praised look.)
6. **[PARTIAL]** No lens character — perfect edges, uniform sharpness. DOF + vignette + CA are in;
   edge bevels on the card are in. Still open: 0.5-1mm bevels on the FRAME rails/struts + blade cuts.

## Already convincingly photoreal — do NOT touch
- The DOF bokeh / blur falloff (macro) · the silhouette / proportions / composition · the soft
  key-light quality. Every critique was surface/light/grounding, not shape or arrangement.

## PANEL 2 (task wd9qnb03m · after grounding+LED+grain) · MEASURED with a colour picker
Verdict: still 4/4 RENDER, but the graders MEASURED my panel-1 fixes as off-target, and I re-fixed:
- **[NOW DONE] LEDs (#1, all 4).** Panel-1 boost was too weak: metal adjacent to a ring measured
  DARKER (~53) than far metal (~90) - the inversion a real emitter never makes. Re-fixed: ring
  emission 4->16, X 3.2->12 · now measured adjacent ~76 > far ~66 (inversion corrected). The rings
  light the shroud. Cost: clip ~2.5% (the intentional bright LEDs · realistic · gate was set for the
  no-emitter dark cabinet).
- **[TEMPERED 2026-07-06] LED brightness re-balanced.** Emission 16/12 was FLOODING the (now black)
  fans grey and clipping hard. Tempered ring 16->9, X 12->7 · fans read black, rings still glow, spill
  PRESERVED (shroud between rings 174.6 > far border 109.1 · no inversion · guarded per panel-2).
  HONESTY CORRECTION: the 40%-preview clip read 0.87% PASS but the FULL-RES RAW re-gate reads
  **1.839% FAIL** · the preview downsampling was BLURRING the crisp ring pixels under 0.98. Verified
  the clip is 99.7% pure-white and confined to the fan-ring bands (y 0.31-0.67) · it is ENTIRELY the
  accurate white LEDs, zero metal blow-out. NEXT: thin the emissive rings (real FE inlet rings are thin
  light-guide LINES, not fat donuts) · this cuts the clipped AREA toward the 1% gate AND is more photoreal.
- **[NOW DONE] Grounding (#2, all 4).** Panel-1 landed on the tabletop objects, NOT the rack legs.
  Re-fixed: floor roughness 0.34->0.14 (glossy) · it now reflects the legs + the bright rings, grounding
  the whole rig.
- **[OPEN · biggest remaining] Fan blades (#3).** Still flat constant-width paddles with black voids,
  not 7-11 overlapping swept airfoils. Box-blade hacks fail (see autopsy). Needs a real LOFTED AIRFOIL
  BLADE MESH (bmesh: taper + sickle sweep + root-to-tip twist + cup), dense overlap. A dedicated wave.
- **[OPEN] Materials (#4, all 4).** Everything reads uniform/pristine · needs per-material texture
  break-up + subtle handled-hardware grime (dust in fan corners, edge wear, aluminium anisotropy).
- **[NOTE] Spark foam-front (#5).** Graders read the champagne foam as a 'procedural glitter' tell ·
  it is ACCURATE to the real DGX Spark (foam front, smooth sides) but the foam MATERIAL could read
  more like real foam. The Spark is gated/proven · touch only with care.

## Grounded next changes (web reference pass 2026-07-06) · queued behind the current reshoot
- **Black fans.** Reviews call the FE fans flatly "black" (NoobFeed, PC Gamer); the render at blade
  albedo 0.110 reads medium-GREY. Correct down to ~0.050 · the glossy coat keeps edge/spec highlights
  so blades still read against the dark well. (Spec updated: RTX5090FE-SPEC.md.)
- **LEDs are ACCURATE · keep them.** Confirmed the real FE lights the inlet rings + X (both sides) +
  side logo + top logo, static cool-white, non-adjustable. So the rings/X in the model are correct,
  NOT artistic licence · the clip at the emitter is photographically honest. Temper only the razor
  thinness if it still reads game-y, do NOT remove.
- **Spark cheese-grater (tell #5).** The gold front is a STRUCTURED dimple-lattice (mini DGX Station /
  Mac-Pro grater), not random glitter · a real hole-array + gold anodised alu. Queued for the Spark
  worktree (touch the gated Spark with care · DGX-SPARK-360-SPEC.md has the research).

## PANEL 3 (workflow w5oyz13ew · 5 lenses + synthesis · on the airfoil+black-fan+LED9 delivery heroes)
Verdict: "GOOD RENDER, bordering on nearly-photo in crops · ~90% still reads CG at a glance." The
airfoil blades + grounding + bead-blast Studio are acknowledged as real gains. Ranked master punch-list:

1. **LEDs · [FACT DISPUTE RESOLVED · KEEP].** Panel claimed the FE has NO illumination + urged deleting
   the rings/X. That is FACTUALLY WRONG · 5 sources (NVIDIA GeForce forums, TechPowerUp review + TEARDOWN
   photos, OC3D, NoobFeed) confirm the real FE has static WHITE LEDs on the X, the inlet area, the top
   GeForce logo + the side logo (non-RGB, non-off). The LEDs STAY (accurate signature). BUT the panel's
   real observation holds across all 5 lenses: they read as UNSHADED GLOWING GEOMETRY · no cool GI spill
   onto the black blades/hub, no bounce onto the shroud, no along-length ripple, no diffuser tint. FIX =
   make the accurate LEDs BEHAVE like emitters: raise diffuse bounces >=4, add along-length brightness
   noise + a slight cool tint, ensure the shroud/hub pick up a visible cool gradient. (Not delete.)
2. **DGX Spark front · [HIGH].** Confirmed the glitter tell + NEW: the speckle WRAPS around the rounded
   corner (impossible for a real panel) + two fabricated oval cutouts flank it + the gold is too
   jewelry-yellow. FIX = real countersunk-hole lattice geometry (array of beveled holes ~2-3mm pitch),
   confined to the flat face, edge-to-edge, delete the ovals, satin anodised gold (desat ~30%). GATED ·
   now supported by panel + web research (2 refs) · still verify foam-vs-grater against a real photo first.
3. **Fans · [HIGH · REFINE not rebuild].** The new airfoils read as too-long, too-curled 'scythe' blades
   on an oversized hub · panel wants 9 SHORT, shallow-swept blades, hub ~30% of fan dia, +root fillet +
   a slightly domed hub cap. FIX = reduce sweep (0.52->~0.28), shrink hub, add blade-root fillet.
4. **CG clay (materials, all lenses) · [HIGH].** One flat frontal key + one uniform roughness everywhere.
   FIX = 3-light studio (key45 + quarter fill + cool top rim, softbox >= object) + per-material rough
   variation (blade flow lines + gloss leading edge · shroud cast grain · frame orange-peel bump) + a
   light dust/AO pass on up-faces + a faint smudge mask on grip faces. Diffuse bounces >=4 so wells darken.
5. **Card bottom + shroud tone · [MED].** Bottom edge is a dummy block · add the PCIe I/O bracket (3x DP +
   HDMI cutouts) + gold-finger edge + recessed 16-pin. And darken the shroud albedo (reads silver under
   the cool key · should be clearly darker gunmetal ~0.10-0.14 than the frame).
6. **Clone lockstep · [MED].** 6 pixel-identical cards · randomise each yaw/pitch +/-0.3deg, seat +/-0.5mm.
7. **Mac Studio · [MED].** Reads nearly CUBIC · real is a squat ~2:1 slab (197w x 95h). Verify the render
   proportions · add the recessed base foot ring + correct front I/O (2 USB-C + SDXC slot + power LED).
8. **Floor/bg · [LOW].** Floor is featureless black glass · give it a dark dielectric (rough ~0.3) with
   faint smudges + a slight vertical near-black->less-black background gradient. (Contact shadows already OK.)

## PANEL 4 (workflow w8sp521x8 · after the LED/fan/shroud/lighting wave) · MEASURED THE DROP
Verdict moved: prior panels "4/4 render" -> now "GOOD RENDER, short of nearly-photo · trio-q34 the
strongest frame · the Spark foam + Studio slab NEARLY read as photographed." Real progress. Ranked new
tells + what I did (this wave, all gate-verified rig q34 raw clip 0.79-0.94% PASS):
1. **[DONE] Fans read as solid discs on a lit gray plate (consensus #1).** Fixed: deepened the well
   12->26mm + 17 near-black heatsink fins + darkened the finstack backing 0.082->0.030 · the blade gaps
   now reveal DARK receding fin depth (you look INTO the card).
2. **[DONE] LED strips razor-uniform, weak GI.** Raised the indirect clamp (GI spill) earlier + now a
   subtle along-length emission ripple (0.80-1.12x) so they read as diffused strips. (KEEP the LEDs · verified real.)
3. **[DONE] Rig-alone frames underexposed vs the trio.** rack_rig key 72->96, rim 52->60, fill 16->22.
4. **[DONE] Floor a perfect mirror.** floor_mat() smudge-varied roughness 0.13->0.42 · reflection blurs.
5. **[DONE-ish] Bevel-less razor frame edges.** powder_coat shading bevel 0.20->0.70mm · edges catch a line.
6. **[OPEN · MED] Material contrast.** blades/shroud/hub share one plastic · split finishes (matte blades +
   glossier hub + distinct shroud). Small remaining materials polish.
7. **[GATED] Spark foam is a flat 2D texture (no self-shadow relief).** Panel wants pore depth · the
   foam3d is accurate + gated (SP10/SP11) · touch only with a measured before/after · deferred with care.
Panel accuracy myths REFUTED this wave: none new (panel-4 was told the verified facts up front · it
stayed on rendering-quality · confirms the sourced-references-win discipline).

## ESCALATION (owner taste call · loop escalation rule) · the Spark trio-scale glitter
The ONE remaining lever on the money-shot trio is the DGX Spark reading a touch glittery at trio scale.
VERIFIED: the foam3d is ACCURATE metal foam + reads as real 3D pores at MACRO (front macro audit) · the
trio glitter is a SCALE artifact (0.7mm relief doesn't resolve on the tiny trio Spark). To reduce it I
would make the foam CELLS FINER · but that LOWERS the per-pixel contrast (std) that the gated spark_foam
tone pin (SP10/SP11, loops 17/19) was tuned to HOLD. So the panel's ask (less glitter) and the gated
tuning (hold the contrast pin) directly CONFLICT · this is a premium-look TASTE decision. Per the loop's
escalation rule I am NOT risking the gated tuning · OWNER CALL: keep the current sparkly-premium foam, or
authorise a finer/matter foam (which needs a re-tune of the spark_foam gate). Everything else is addressed.

## Execution order (this wave · rack-weighted, but advance a desktop too)
A. LED GI realism (keep · #1 valid core) + gate: thin the X lit bar (it is 55% of the clip) → under 1%.
B. Fan refine (#3): less sweep + smaller hub + root fillet.
C. Shroud darken + card bottom I/O bracket (#5).  D. Clone lockstep jitter (#6).
E. Material micro-texture + 3-light + dust (#4 · the big materials wave).  F. Floor/bg (#8).
G. DESKTOP: verify+fix Mac Studio proportions (#7) · then the Spark hole-lattice (#2, gated, re-verify).
Re-run the panel after B/E and after the desktop wave to measure the drop.
