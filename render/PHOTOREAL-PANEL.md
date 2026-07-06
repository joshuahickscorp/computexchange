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
  fans grey and clipping hard. Tempered ring 16->9, X 12->7 · re-measured: rig q34 clip 1.15%->0.87%
  (PASSES the 1% gate for the first time), fans read black, rings still glow, and spill is PRESERVED
  (shroud between rings 174.6 > far border 109.1 · no inversion · guarded per the panel-2 lesson).
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

## Next when the loop resumes here
Two hard tells remain: the **airfoil blade-mesh rebuild (#3)** and **material texture/grime (#4)**.
These are real waves, not tweaks - reaching 4/4-photo is a large effort. Re-run the panel after each
to measure the drop. Blunt: the render is meaningfully more photoreal than panel-1 (real emitters +
grounding) but is NOT yet passing as a photograph · the fan geometry + material uniformity are the wall.
