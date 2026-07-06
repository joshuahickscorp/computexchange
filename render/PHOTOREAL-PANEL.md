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
3. **[DEFERRED · needs real rebuild]** Fan blades are FLAT PADDLES, not airfoils (macro + gestalt).
   A Simple-Deform bend on box blades merged them into a solid dome (worse). The real fix is a lofted
   airfoil blade MESH (bmesh: sickle sweep + root-to-tip twist + chord taper), denser overlap. Bounded
   but non-trivial · do it as its own wave.
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

## Next when the loop resumes here
Highest leverage remaining: #4 (material separation + subtle grime on the frame/desktops) and #3 (the
airfoil blade-mesh rebuild). Re-run the panel (resume Workflow wf_35dcad2f-4cb or a fresh one) after a
batch of fixes to confirm the tells actually dropped · two clean-ish panels = the rig is photoreal.
