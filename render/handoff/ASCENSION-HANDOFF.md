# ASCENSION HANDOFF · 8 → 10 photoreal, the deep-material pass (2026-07-06)

You are inheriting three real products modeled to ~8/10 and asked to take them to **10 —
indistinguishable from official product photography, from every one of the 360 degrees.** The
geometry is now largely correct (the owner's earlier flagged gap is closed). What remains is the
**hardest and most important part: MATERIAL + MICRO-DETAIL REALISM.** Getting to 8 was the easy
climb. 8→9→10 is not more small facet tweaks — it is deep, reference-locked material work, and it is
where the render either becomes photographic or stays "a very good CG render."

**Read first:** `~/Downloads/cx-render-handoff/GRADING-REPORT.md` (the sourced 360 research + per-facet
grades) and `~/Downloads/cx-render-handoff/HANDOFF.md` (the original methodology + the LAWS). Both
still bind. Then open every current render in `~/Downloads/cx-render-handoff/images/` and the live
heroes (below) and study them **beside the real product photos** the GRADING-REPORT links.

## Owner's handover emphases (2026-07-06 · these three, in this spirit)
The owner reviewed the current state and is handing the deep pass to a fresh Fable session with three
explicit emphases. Plan and scaffold around these:
1. **RIG ACCURACY + RE-PROPORTIONING (newly elevated · was previously "leave the frame").** The owner
   now wants the 6-GPU rig itself audited for accuracy AND **resized/re-proportioned so it looks its
   best as a vessel for six RTX 5090 cards** — the six cards should read as the unmistakable hero, not
   float in the lower third of an oversized cart. This REOPENS the frame sizing (previously escalated as
   "owner-praised, do not touch"). See the dedicated RIG section below — it is now a first-class pass,
   not polish.
2. **ACTUALLY PHOTOREALISE THE GPUs.** The headline material goal (unchanged, still #1 for the money
   shot) — take the Dark Gun Metal shroud, molded blades, anodized fins, and light-guide LEDs to
   indistinguishable-from-photo. This is the largest single lift.
3. **CONTINUE ALL SIDES OF THE STUDIO AND SPARK.** Keep pushing the desktops' 360 material + detail
   (every face, not just the hero front) toward 10 — bead-blast anisotropy, port/tongue micro-geometry,
   champagne anodize zoning, foam, bottoms.

## The single most important MATERIAL goal (owner's priority)
**Make the 6× RTX 5090 FE cards as photoreal as the Mac Studio and DGX Spark already read.** The
GPUs are the money shot (12 fan-faces fill the rig/trio hero) and today they are the weakest
*material* read even though their geometry is now right. The Studio works because of its bead-blast
micro-texture; the Spark works because of its real 3D metal foam. **The GPU shroud now has curvature-
driven edge machining (wave-6) but still lacks scanned/measured PBR depth** — its Dark Gun Metal
shroud, its black fan blades, its anodized fins, and its white LEDs are still largely procedural. That
uniformity is the remaining tell. Close it and the whole hero lifts.

## Where things stand (what is DONE — do not redo)
- **Geometry, all 3 objects + rig, 360:** correct and gate-verified over ~33 commits this run.
  5090 FE: 7-blade ring fans, double flow-through rear w/ fin stacks, black center + gunmetal frame,
  single flush X, thin wordmark, recessed angled 12V-2×6, solid bracket, flat hub, angled louvers.
  Studio: rectangular ~173×53mm vent + correct rear port row + concentric bottom. Spark: metal-foam
  rear, correct 9-port I/O, blank champagne top, crisp machined-brick edges, green NVIDIA badge,
  inset foam, base cover. Rig: power leads + PCIe slots.
- **A critical bug was fixed:** the Studio body was collapsing on a rounded rear-boolean and rendering
  as an upright slab in the trio. Fixed (sharp r=0 recess cutter). **LESSON THAT STILL BINDS: after
  ANY boolean on the Studio/Spark fillet body, check body integrity from a LIT front/q34 — a bad
  cutter collapses the body to a flat plate and hides in the dark rear audits. Use sharp cutters for
  big recesses.**
- Rig q34 raw clip holds 0.72% PASS. Money shots re-rendered + correct: `images/trio/trio-q34.png`,
  `trio-front.png`, `rig/gpurig-*.png`, `DELIVERABLES-CONTACT-SHEET.png`.
- **Wave-6 (2026-07-06, this session) — do not redo:** GPU **curvature-driven edge machining** on the
  die-cast shroud (edge factor off geometry Pointiness in `machined_metal()`: convex chamfer sliver
  drops roughness −0.12 + lifts albedo toward 2× base capped 0.30 · shroud+rings+center bar · rig q34
  0.719% PASS · baked into trio/rig/gpu-front heroes). Studio **M2 dark rear-port inserts** (pockets
  were alu-lined). **R2 rig-side relight** (the side audit frame was near-black · camera-side softbox +
  fill gated on SHOT==side). **Full 360 audit sweep** — every face of all four objects now reads
  (gpu-bottom confirmed already lit; the two hook-flagged black frames resolved). **G18 see-through
  attempted + autopsied** (a dim backlight passed the gate but only spilled onto the base and eroded
  void-black · reverted to opt-in `--flow`, default off). VERIFIED already-correct (do not re-open):
  FE blades/hub near-black (read grey only from the studio key + LED fill), Spark front pods glossier
  than the shell, Studio bead-blast isotropic, LED GI via co-located point lights.

## THE RIG — accuracy + re-proportioning (owner emphasis #1 · now a first-class pass)
The 6-GPU rig is the frame-of-reference hero and the owner wants it **accurate and best-proportioned
for six cards**, not an oversized cart with the GPUs floating low. This is a real design pass, done
with fresh eyes and real references — plan it, don't dismiss it:
1. **Research real 6× RTX-5090 / multi-GPU open-frame rigs + AI workstations** (fetch photos: open-air
   mining/AI frames, Comino/Bizon-style multi-GPU builds, server GPU trays). Decide what the vessel
   *should* be — the current open two-shelf cart on casters is one interpretation; verify it against
   real builds and correct where it diverges.
2. **Card layout accuracy** — today: 6 cards PORTRAIT (137w × 304h × 40t), fan-face forward, side by
   side at 143 mm pitch (≈6 mm gap), hung from a top bar over a mobo tray + PSU + PCIe risers. Verify
   the pitch/gap, the mounting (how are six 2-slot cards actually seated + powered + risered in a real
   rig?), the power-lead routing (R1), and the base wiring against references. Fix inaccuracies.
3. **RE-PROPORTION the frame so the six cards are the hero — OWNER DECISION: a SNUG OPEN-FRAME RIG.**
   Current frame is 960×700×480 mm; the card wall is only 858×304 mm, so the cards fill under half the
   frame height → the "oversized cart" read the owner is rejecting. **Target (decided, do not
   re-litigate): tighten the frame to HUG the six-card wall — a purpose-built OPEN-AIR multi-GPU rig
   look (AI/mining-frame family), cards dominating, minimal empty metal, no oversized cart.** Bring the
   frame in close on all sides (a modest, even margin around the 858×304 card wall + the base tray/PSU),
   drop the excess height, keep it open-frame (no enclosure panels · void-black premium look holds).
   **HARD CONSTRAINT: the scale trio (`build_trio.py`) stands the Mac Studio + DGX Spark ON TOP of the
   rig, so the rig must still present a clean, flat top surface at a sensible height for the desktops** —
   achieve "snug around the GPUs" for the rig-alone hero WITHOUT breaking the trio composition (e.g. a
   clean top rail/plate the desktops sit on, sized to the tightened frame). Re-gate rig-q34/front +
   trio-q34/front after any resize (clip gate + tone pins).
4. Treat this as its own ordered checklist in the rack-oracle `ASCENSION-PLAN.md`, gated like every
   other pass. This SUPERSEDES the old "frame is owner-praised, leave it" escalation.

## THE ASCENSION METHODOLOGY (this is the whole job — internalize it)
The 8→10 loop is **reference-locked material iteration**, not facet-ticking:

1. **PAIR EVERY RENDER WITH THE REAL PHOTO.** Fetch the exact reference the GRADING-REPORT cites
   (TechPowerUp "Pictures & Cooler" + "Disassembly" pages for the 5090 FE; STH/ChargerLAB for the
   Spark; Apple press for the Studio), put your render and the photo **side by side at the same
   crop/lighting**, and ask the one question that matters: *"what material property is different?"*
   Not geometry — geometry is done. Roughness gradient? Anisotropy direction? Micro-scratches? Edge
   wear? Specular rolloff? Fresnel? Sub-surface in the plastic? The answer is always a **surface**
   answer now.
2. **CHASE MEASURED/SCANNED PBR where the procedural asymptotes.** The GRADING-REPORT explicitly flags
   the material asymptote: procedural noise reads uniform. For the hardest surfaces (the die-cast
   shroud, the molded blades, the anodized fins) consider **real scanned PBR sets** (albedo/roughness/
   normal/metallic/AO) or, at minimum, **multi-octave measured-value procedural stacks** driven by
   curvature (edge wear), position (cast-grain zoning), and a fine normal-detail map. Uniform roughness
   is the #1 CG tell on metal.
3. **ONE material property per iteration, gate-checked.** Change roughness OR the normal detail OR the
   anisotropy — not three at once. Render the single most relevant macro, compare to the paired photo,
   run the numeric gates (`rack_verify.py` / `rig_patches.py` / `clipcheck.py`). The dark-object tone
   pins are the arbiter (`rig_patches.py --offset -18`): keep only if the pin holds.
4. **FORENSIC MATERIAL PANEL every wave.** Spawn N vision agents, each Reading the paired render↔photo
   crops through ONE material lens (metal-roughness / plastic-sheen / anodize-anisotropy / LED-diffusion
   / edge-microbevel / grain-scale / specular-rolloff / dust-and-handling). Synthesize a ranked
   material punch-list + the single highest-leverage surface fix. **The panel is unreliable on hardware
   FACTS (it was wrong 3× on this project) — trust the sourced dossiers + the gates over its
   recollection. It IS reliable on rendering-quality tells (uniform roughness, missing GI, flat
   specular, plastic-that-reads-clay). Act on those.**
5. **VERIFY BEFORE PASS. Never fabricate completion. Autopsy every reverted value. One clean
   human-authored commit per bounded change.** All original LAWS bind (no git attribution; trademark
   gate = blank plates only; middot only; void-black is the look, no ambient-env reflection; keep the
   co-located LED point lights; check body integrity after booleans).

## THE 5090 FE DEEP-MATERIAL PUNCH-LIST (the priority · in order)
Study the render beside a TechPowerUp/LanOC macro before each one.
1. **Die-cast Dark Gun Metal shroud** — today a uniform (~0.13 albedo, ~0.45 rough) metal. Real:
   semi-matte cast aluminum with (a) fine sand-cast/bead grain (a subtle normal-detail map, ~40-80µm),
   (b) **curvature-driven edge brightening** (machined chamfers read bare-metal-bright; broad faces
   read matte), (c) faint roughness zoning so no two panels are identical, (d) the tight, slightly
   warm specular of anodized alu, not a plastic sheen. This is the biggest single lift.
2. **Fan blades — injection-molded black ABS**, not flat black. Real: a glossy clear-coat that travels
   a NARROW moving highlight along the cambered blade (you have a hint of this — deepen it), a faint
   mold parting-line down each blade, micro-flow-texture, and near-black albedo (0.03-0.05) so the
   gaps stay black. Kill any residual foil/facet look from the old geometry.
3. **The white LEDs — light-guide diffusion, not emissive tape.** Real FE LEDs glow through a frosted
   light-guide: soft, slightly volumetric, blooming into the shroud, brightest at the guide and
   falling off. Model the emitter as a frosted diffuser + keep the co-located point light for GI, and
   tune the post bloom so the rings/X read as *lit plastic*, not white paint. Watch the 1% clip gate.
4. **Anodized fin stacks (rear windows)** — give the black fins a raking anisotropic sheen + real
   depth AO so you look INTO a finned cavity (light passing through in the rig = G18). A dim backlight
   behind the rig cards makes the flow-through read (test against the clip gate).
5. **Micro-geometry** — real chamfer bevels on every shroud edge (a shading Bevel node is not enough at
   macro; add a small geometry bevel), panel-gap depth with contact shadow, the screw recesses, the
   die-cast draft. These catch the highlights that sell "machined metal."
6. **Product-photography lighting** — study how NVIDIA/TPU light the card: a large soft key + a hard
   rim that traces the top edge + the void-black falloff. The reflection of a softbox in the gunmetal
   is a huge realism cue (a studio env visible to GLOSSY rays ONLY, camera rays still void-black — this
   is allowed and distinct from the rejected ambient-env fill).

## STUDIO + SPARK — the last 10%, ALL SIDES (owner emphasis #3 · they are ~8, push to 10)
Push **every face**, not just the hero front — front, rear, sides, top, bottom each want the same
reference-locked material scrutiny. The Studio front is launch-grade and the rear now has the correct
rectangular vent + **M2 dark port inserts (wave-6, done)**; the remaining lift is material/detail on
the other faces. Per-object:
- **Studio:** the bead-blast is good; push the anodized-alu **roughness anisotropy = isotropic** (no
  brushed grain — bead blast has none), tighten the edge-roll highlight, and get the front USB-C
  **tongue** micro-geometry + the mirror-glossy status-LED. The rear port cavities want dark connector
  interiors (currently alu-lined). Consider a real bottom perforation on an X-Z-oriented mesh (the
  current annulus is a flat dark band because `perforated_band` needs an X-Z face).
- **Spark:** the foam is gate-pinned at its depth limit (**owner decision — do not churn**); the lift
  is the **champagne anodize** — measured roughness zoning + the glossier pods/I-O plate vs the matte
  shell (S13), and the micro-speckle STH shows on the flat faces. Verify the inset-foam side-hide (S5)
  landed.

## Gates, files, worktrees (unchanged)
- **rack-oracle** (`.claude/worktrees/rack-oracle/`): `render/build_rack.py` (GPU + rig + trio via
  `build_trio.py`), spec `render/ref/rack/RTX5090FE-SPEC.md`, gates `render/rack_verify.py` +
  `clipcheck.py`, log `render/MORNING-REPORT.md`, heroes `render/rack_previews/`.
- **model-refinement** (`.claude/worktrees/model-refinement/`): `render/build_scene.py`
  (`build_mac_studio` / `build_dgx_spark`), specs `render/MAC-STUDIO-360-SPEC.md` +
  `DGX-SPARK-360-SPEC.md`, tone gate `render/rig_patches.py --offset -18`, rear audit tool
  `render/_rear_detail.py` (lit head-on rear, world-light + glowports options), 360 tool
  `render/_audit_desktop.py`.
- Blender: `/Applications/Blender.app/Contents/MacOS/Blender -b -P render/<builder>.py -- <args>`
  (Metal GPU, Cycles, AgX). `--preview` = fast; omit for full 512spp/100%. cwd persists between shell
  calls — `cd` into the right worktree once.
- **Every ~5 commits:** full-quality hero reshoot + re-gate + rebuild the contact sheet.

## ESCALATED — needs the OWNER, do not churn (standing)
Spark foam DEPTH (gated at its tone limit), floor-reflection blur-vs-crisp, Studio TOP-logo plate
(trademark-gate blank vs Apple silhouette). **The rig frame is NO LONGER escalated — the owner reopened
its sizing (emphasis #1 above); re-proportion it deliberately against references.** If you hit a genuine
limit (3 fails on one surface, an unresolvable reference dispute, a taste call), **escalate in writing
in MORNING-REPORT.md and MOVE ON — do not block the loop.**

## The bar
An owner of the real hardware, handed your render and a real photo at any crop, cannot tell which is
which — the gunmetal reads as cast metal, the blades as molded plastic, the LEDs as lit light-guides,
the foam as foam, the aluminum as bead-blast. Geometry got you to 8. **Material gets you to 10.**
Leave an honest "next" every pause. Iterate as many times as it takes.
