# HANDOFF · photoreal render of three objects (RTX 5090 FE rig · Mac Studio · DGX Spark)

You are inheriting a long, disciplined effort to make three real products render **indistinguishable
from official product photography**, correct from **every one of the 360 degrees**. This document
instills the METHODOLOGY that got us "right at the cusp" so you can carry it with fresh eyes. Read it
in full before touching anything.

## The mission
Three objects, one premium look (dark void-black studio, AgX, subtle bloom + film grain), true metric
scale (1 Blender unit = 1 m):
1. **The home GPU rig** — a 12U open-frame cart on casters holding **6× NVIDIA RTX 5090 Founders
   Edition** cards, fans out. + **the scale trio**: the rack as the base with the Mac Studio + DGX
   Spark on top (the site's frame-of-reference hero, the money shot).
2. **Mac Studio** — 197×197×95 mm aluminium desktop.
3. **NVIDIA DGX Spark** — 150×150×50.5 mm gold "metal-foam" AI box.

Standard: Apple / NVIDIA launch grade. An owner of the real hardware finds nothing missing, nothing
wrong, at any distance, from any angle — **front ports, rear ports, the bottom face (the side that
sits on the rig), and the top face (facing up)** all correct.

**Where we are:** the Studio and the Spark are *really close* to photographic. The one thing lacking is
the **look of the GPUs** — NVIDIA's exact FE geometry is subtle and the current 5090 FE reads *close*
but a real one differs. The last mile is GEOMETRY accuracy, not more material tricks. Get the geometry
exactly right, 360°, on all three — then the materials + our lighting will carry it home.

## Where everything lives
Two git worktrees under `/Users/scammermike/Downloads/computexchange/.claude/worktrees/`:
- **`rack-oracle`** (branch `worktree-rack-oracle`) · the rack + the 5090 FE card + the scale trio.
  - Builder: `render/build_rack.py` · `--part {gpu|gpurig|assembly|frame} --shot {front|q34|rearq34|side|top|macro} [--preview]`
  - Trio composer: `render/build_trio.py -- --shot {q34|front} [--hires --post]`
  - Card spec (SOURCED): `render/ref/rack/RTX5090FE-SPEC.md`
  - Numeric gate: `render/rack_verify.py <png> --shot q34` (clip + Lab tone) · `render/clipcheck.py`
  - Loop manual: `render/OVERNIGHT-LOOP.md` · panel log: `render/PHOTOREAL-PANEL.md` · report: `render/MORNING-REPORT.md`
- **`model-refinement`** (branch `worktree-model-refinement`) · the two desktops.
  - Builder: `render/build_scene.py` · entry fns `build_mac_studio(loc_x, yaw_deg)`, `build_dgx_spark(loc_x, yaw_deg)`
  - `--only {studio|spark|pair|none} --shot {front|q34|detail}` · 360 audit tool: `render/_audit_desktop.py --which {studio|spark} --yaw N --name X`
  - Specs (SOURCED): `render/MAC-STUDIO-360-SPEC.md`, `render/DGX-SPARK-360-SPEC.md`
  - Tone gate: `render/rig_patches.py --offset -18` (Lab patches: studio_alu, spark_champ, spark_foam …)
- **Blender:** `/Applications/Blender.app/Contents/MacOS/Blender -b -P render/<builder>.py -- <args>` (Metal GPU, Cycles, AgX).
- **cwd resets between shell calls** — always `cd` into the right worktree or use absolute paths.
- Deliverable heroes: `rack-oracle/render/rack_previews/` (trio-*, gpurig-*, gpu-*, DELIVERABLES-CONTACT-SHEET.png).
- A copy of the current best frames is in `~/Downloads/cx-render-handoff/images/`.

## THE METHODOLOGY (this is the whole point — internalize it)
Every iteration is a tight **render → verify → re-render → research → improve** loop. One bounded
change at a time, always proven against the real reference AND a number before you believe it.

1. **PICK** the top open item for the LOWEST-graded object/facet (this session: the GPU look). Guarantee
   every object + every one of its 360 faces (front/rear/side/top/macro) gets built and audited over time.
2. **RESEARCH before you model.** Never guess a dimension, port, vent pitch, connector, material, or
   finish. Search the web — manufacturer spec pages, TechPowerUp, iFixit/Chargerlab teardowns, reviews
   with measurements, official product photography, dimensioned drawings. Convert to mm against a known
   anchor. **Cite the source URL into the object's spec file.** For a whole object or face, fan out a
   research Workflow across facets (dimensions / a specific face / materials / I-O).
3. **ONE bounded change** in the builder. No batching unrelated changes.
4. **RENDER** the single most relevant angle at preview res (`--preview`), cycling angles across
   iterations so all 360 faces get audited, not just the front.
5. **AUDIT HONESTLY.** Open the render. Compare it to the REAL reference photo. Ask "what still reads
   fake or wrong here?" Run the numeric gate (`rack_verify.py` / `rig_patches.py`). **VERIFY BEFORE YOU
   CLAIM PASS** — read the tool's real exit code and look at the actual pixels. A blown highlight, a
   collapsed body, a wrong tone is a NUMBER, not an opinion. If it regressed or reads fake, fix or revert
   before moving on.
6. **COMMIT** one clean, human-authored change. Tick the item. (Commit hygiene laws below.)
7. **Every ~5 commits:** full-quality reshoot of the hero angles, re-gate, rebuild the contact sheet.
8. **FORENSIC PANEL** (periodic deep audit): a multi-lens Workflow where N vision agents each Read the
   hero PNGs through a distinct lens (materials / lighting / geometry / layperson gestalt / product-
   accuracy) and rank what still reads CG, then a synthesis agent produces a ranked punch-list + the
   single highest-leverage next change. Run it after each wave to MEASURE the tell-drop.
   - **CRITICAL — the panel is unreliable on hardware FACTS.** It works from imperfect memory. This
     session it was WRONG three times, each of which would have been a regression: it said the 5090 FE
     has no LEDs (it does — 5 sources), that the Mac Studio is too tall (it's the correct 197×95 slab),
     and that the Spark front should be a machined cheese-grater (it's genuinely metal foam — Chargerlab
     teardown). **Always verify the panel's accuracy claims against sourced research. Trust the gates +
     the teardowns over the panel's recollection.** The panel IS reliable on rendering-quality tells
     (CG-clay materials, missing GI, flat lighting) — act on those.
9. **AUTOPSY on any reverted value** — write WHY the old value was wrong, so it is never re-tried.
   **Flip-flop guard:** re-measure against 2 references before re-changing a value you already changed.
10. **Gated (tone-pinned) parts** (the Spark foam, the champagne, the Studio alu carry Lab pins in
    `rig_patches.py`): change them WITH the gate as arbiter — render, run the gate, keep only if the pin
    holds; if it breaks, revert + autopsy. Escalate-in-writing and MOVE ON (do not block the loop) on a
    genuine limit: 3 fails on one item, an unresolvable reference dispute, or a taste call needing the owner.

## LAWS (never violate)
- **No git attribution of any kind.** Never add `Co-Authored-By`, `Generated with`, or any Claude/AI
  trailer. Commits read exactly as a human wrote them. (Standing owner rule.)
- **Trademark gate:** never model a real logo/wordmark/text glyph. Model each as a BLANK plate/recess of
  the correct shape + placement (emissive or etched as the real one is).
- **Dash gate:** middot `·` only in comments/messages — no em/en dashes.
- **One bounded change per commit.** Honest messages. Verify before claiming PASS.
- **After any BOOLEAN cut, check body integrity from a LIT angle** (not just a dark 3/4). A near-full-
  round port cut once collapsed the Studio body to a flat plate; it hid in the dark rear audit and only
  showed in the lit hero. Round cuts: keep r a hair under half; re-render a lit front/q34 to confirm.
- **Clean premium stays clean.** Added imperfection reads FAKE on a Studio/Spark/GPU face. The rack is
  HANDLED hardware → honest wear at handled edges is allowed, but every knob cites a photo. (We reverted
  frame "dust" this session — it over-lightened the horizontals and read fake.)
- **Void-black is the look.** A subtle ambient-env reflection was TESTED and REJECTED — it flattened the
  premium dark-object contrast. Do NOT re-add it. Grounding comes from a faint glossy-floor reflection.

## What already works — do NOT undo (verified accurate)
- **The 5090 FE lit elements are REAL** (static white LEDs on the inlet rings, the X on both faces, the
  side + top GeForce logos). Keep them. The look-of-light comes from a **co-located POINT LIGHT at each
  ring/X** (a light is not a camera-visible surface, so it casts a cool halo on the shroud + rims the
  blades + spills to neighbours with ZERO added clip — this resolved the loudest tell across 3 panels).
- **Fan blades are a real lofted-airfoil bmesh** (`_fan_blades`): cambered cross-sections, chord taper,
  root-to-tip twist, gentle sickle sweep, ~1.4× overlap; roots tuck under the hub; a deep dark finned
  cavity sits behind so you look INTO the card.
- **The Spark front is METAL FOAM** (real 3D `foam3d` geometry), not a cheese-grater. It is gate-pinned
  (`spark_foam` mean, dE<6) and at its depth limit (deeper pores overshoot the pin — verified).
- **The Mac Studio proportions are correct** (197×197×95) and it has bead-blast micro-texture.

## Current state (2026-07-06, ~55 gate-verified commits this run)
The render moved from "4/4 obvious render" to a strong good-render with the devices near-photographic.
Rig q34 raw clip holds **0.798% PASS**. Money shots: `images/trio/trio-q34.png`, `images/trio/trio-front.png`.
Contact sheet: `images/DELIVERABLES-CONTACT-SHEET.png`.

### The honest remaining gap (this is your job)
- **The GPU geometry is the last mile.** The 5090 FE reads *close* but a real one differs — the exact
  fan-blade count/curvature/pitch, the shroud cut-lines, the fin stacks, the 2-slot proportions, the
  X-accent shape, the backplate window, the bracket. Re-research the exact geometry from real close-ups
  and dimensioned teardowns and correct it, 360°. This is the highest-value work.
- **Two open decisions** (owner taste / unresolvable by us): the Spark foam depth is at its gated tone
  limit (re-tune the `spark_foam` gate for deeper pores?); the floor grounding — panels conflict on
  blur-vs-crisp reflection.
- **Material asymptote:** procedural materials read slightly uniform; closing the last gap may want
  scanned/measured PBR, but FIRST get the geometry exactly right — that is what the owner flagged.

Leave an honest "next" every time you pause. Never fabricate completion. Iterate as many times as it
takes. The honesty of the audit is the whole point.
