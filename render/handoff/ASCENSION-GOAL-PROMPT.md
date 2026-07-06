# ASCENSION GOAL PROMPT · for a fresh FABLE planning+scaffolding session

> **How to use this file.** Paste the fenced `/goal` block at the very bottom into a **new Fable
> chat** (the model set to Fable). That session's entire job is to **produce the doctrine and build
> the scaffolding for the 8→10 photoreal ascension, then STOP** — it must NOT run the material
> iteration loop itself. After it stops, switch the model to **Opus** and let Opus be a thin
> **overseer** that simply drives the plan and the harnesses this Fable session leaves behind. The
> division is deliberate: Fable is the expensive, high-reasoning **architect** (write it once, write
> it well); Opus is the cheaper **operator** (run the loop many times against a fixed plan).

---

## 0 · Why this split exists (read this first, planner)
Getting these three products from "obvious render" to ~8/10 was the easy climb and it is DONE:
geometry is correct 360°, gate-clean, committed. The remaining 8→10 is the hard, expensive part —
**material and micro-surface realism** — and it is dominated by *many small, reference-locked
iterations* rather than by cleverness. That is exactly the wrong shape of work to spend a premium
reasoning model on turn-by-turn. So the owner wants the reasoning spent ONCE, up front, by you
(Fable), to lay down:
1. **The DOCTRINE** — an exhaustive, sequenced, per-surface plan to reach 10/10, so that execution
   becomes "follow the next unchecked step," not "figure out what to do."
2. **The SCAFFOLDING** — the reusable tools, material framework, reference library, gates wiring, and
   the forensic-panel workflow, so that each execution step is *one command + one judgement call*, not
   a fresh authoring problem.

Then you STOP. Opus, as overseer, opens your plan, runs your harness for the top unchecked surface,
looks at the A/B you made trivial to produce, tunes a parameter, commits, ticks the box, repeats.
**If Opus has to invent tooling or re-derive the approach, you under-delivered.** Over-scaffold.

## 1 · What to read before you plan (do not skip; internalize)
- `~/Downloads/cx-render-handoff/ASCENSION-HANDOFF.md` — the mission brief for the ascension: the
  per-surface deep-material punch-lists (esp. the 5090 FE), the reference-locked methodology, the
  gates, the LAWS, and the escalated owner-decisions. **Your doctrine expands this into an executable
  plan.**
- `~/Downloads/cx-render-handoff/GRADING-REPORT.md` — the sourced 360 research + facet grades, with
  the exact real-photo reference URLs per object/face (TechPowerUp Pictures/Disassembly for the 5090
  FE, ServeTheHome/ChargerLAB for the Spark, Apple press/iFixit for the Studio).
- `~/Downloads/cx-render-handoff/HANDOFF.md` — the original render→verify→improve methodology and the
  LAWS. All bind.
- The two live worktrees + their current builders, specs, gates, and MORNING-REPORT logs:
  - `computexchange/.claude/worktrees/rack-oracle/` → `render/build_rack.py` (5090 FE card + 6-GPU
    rig + `build_trio.py`), spec `render/ref/rack/RTX5090FE-SPEC.md`, gates `render/rack_verify.py` +
    `render/clipcheck.py`, log `render/MORNING-REPORT.md`, heroes `render/rack_previews/`.
  - `computexchange/.claude/worktrees/model-refinement/` → `render/build_scene.py`
    (`build_mac_studio` / `build_dgx_spark`), specs `render/MAC-STUDIO-360-SPEC.md` +
    `render/DGX-SPARK-360-SPEC.md`, tone gate `render/rig_patches.py --offset -18`, audit tools
    `render/_audit_desktop.py` + `render/_rear_detail.py`.
- Blender invocation: `/Applications/Blender.app/Contents/MacOS/Blender -b -P render/<builder>.py --
  <args>` (Metal GPU, Cycles, AgX). `--preview` = fast (40%/96spp); omit for full (100%/512spp). cwd
  persists between shell calls; `cd` into the correct worktree once.

## 2 · What "the DOCTRINE" must contain (write these as committed markdown)
Produce a **`render/ASCENSION-PLAN.md` in each worktree** (GPU/rig plan in rack-oracle; Studio+Spark
plan in model-refinement), plus a top-level index in `~/Downloads/cx-render-handoff/`. Each plan is an
**ordered checklist of surface-passes**, GPU first (owner priority), each pass specified so completely
that execution is mechanical:

For **every surface** (GPU: die-cast shroud, black fan blades, anodized fin stacks, white LED
light-guides, PCB/bracket, gold fingers, cables; Rig: frame powder-coat, tray/PSU, casters; Studio:
bead-blast anodized alu, port cavities, status-LED glass, bottom perforation; Spark: champagne
anodize, metal foam, polished pods/I-O plate, base cover) write a row with:
1. **Reference** — the exact real-photo URL(s) to pair against for THIS surface, and the crop to use.
2. **Current state** — the material as it is built today (node setup, values), quoted from the builder.
3. **Target** — the measured/observed real material property, in words a shader can hit (albedo range,
   roughness value + its VARIATION/zoning, anisotropy direction+strength, normal-detail scale, edge-wear
   curvature response, Fresnel/coat, sub-surface for plastics, LED diffusion character).
4. **Approach** — scanned-PBR-texture-set vs measured-value procedural stack; which of the scaffolded
   material functions to call; which maps to author or source; the ONE bounded change per iteration.
5. **Gate** — which numeric gate arbitrates (`clipcheck` for LEDs, `rig_patches`/`rack_verify` tone
   pin for the metals/foam), the pass threshold, and the "keep only if the pin holds" rule.
6. **Estimated iterations + risk** — and the flip-flop/escalation note if the surface is gated at a
   limit or is an owner decision (Spark foam depth, floor reflection, Studio top-logo plate — DO NOT
   plan changes to these; list them as escalated).

Order the whole thing by **leverage × visibility**, with one structural exception up front:
- **PASS 0 (structural, do it first in rack-oracle): RIG ACCURACY + RE-PROPORTIONING (owner emphasis
  #1).** Because resizing the frame moves every rig + trio hero and re-bases the gates, plan it as the
  FIRST pass so all later material reshoots land on the final geometry. Research real 6× RTX-5090 /
  multi-GPU open-frame rigs (fetch photos), verify the card layout/pitch/mounting/power routing for
  accuracy, and **re-proportion the frame — OWNER DECISION: a SNUG OPEN-FRAME RIG that HUGS the six-card
  wall (purpose-built open-air AI/mining-frame look, cards dominating, minimal empty metal, NOT an
  oversized cart).** Do not re-litigate the vessel choice. HARD CONSTRAINT: the trio stands the Studio +
  Spark on top, so the tightened rig must still present a clean flat top surface at a sensible height
  for the desktops. Re-gate rig-q34/front + trio-q34/front after the resize. (This SUPERSEDES the old
  "frame is owner-praised, leave it" line — see the handoff's RIG section.)
- Then material by leverage × visibility: the 5090 **die-cast shroud + fan blades lead** (they fill the
  money shot · shroud already has wave-6 edge-machining — build on it toward scanned/measured PBR), then
  the LEDs, then the fins/see-through, then the **Studio + Spark last-10% on ALL SIDES** (owner emphasis
  #3 — every face, not just the front). Number the passes so Opus works top-down.

## 3 · What to SCAFFOLD (build it, prove it runs, but do NOT run the full material loop)
Build the machinery so each execution step is one command. At minimum:
1. **Reference-pairing / A-B compositor** — a tool (`render/_pair.py` in each worktree) that, given a
   surface tag, renders the matching macro at the matching camera/lighting AND fetches/loads the stored
   real reference crop, then writes a **side-by-side composite PNG** (render | photo) for the operator
   to judge. This is THE audit primitive for material work — without it, every step is manual. Prove it
   produces at least one composite (e.g., the 5090 shroud macro next to a TPU crop).
2. **A measured-material framework** — reusable, parametrized shader-builder functions layered over the
   existing `principled()` helper, e.g. `cast_metal(...)`, `molded_plastic(...)`, `anodized_alu(...)`,
   `light_guide_emit(...)`, each accepting the properties in §2.3 (roughness + zoning noise, normal
   detail, curvature-driven edge wear via Pointiness, anisotropy, coat, optional texture-map inputs).
   Refactor the current builders to CALL these (with today's values as the defaults) so behavior is
   unchanged now but every surface becomes a one-line parameter tune later. Prove the builders still
   render + gate-pass after the refactor.
3. **A PBR asset pipeline + directory** — decide and scaffold `render/pbr/<surface>/` for scanned map
   sets (albedo/rough/normal/metallic/AO), a loader that wires a map set into the material framework,
   and a written note on where to source or how to bake each set. You do NOT have to obtain all scans;
   you MUST leave the slots + loader so Opus can drop maps in.
4. **The FORENSIC MATERIAL PANEL workflow** — a reusable multi-agent Workflow script
   (`render/_material_panel.*`) that fans out N vision agents over the render↔photo composites, each
   through ONE surface lens (metal-roughness / plastic-sheen / anodize-anisotropy / LED-diffusion /
   edge-microbevel / grain-scale / specular-rolloff / dust-handling), and a synthesis step that emits a
   ranked material punch-list + the single highest-leverage fix. Wire it to READ the §3.1 composites.
   Document that its hardware-FACT claims are untrusted (it was wrong 3× on this project) but its
   rendering-quality tells are trusted.
5. **A one-command iteration harness** — a thin wrapper (`render/_iter.sh` or a documented sequence)
   that for a given surface: builds → renders the macro → runs the gate → makes the A/B composite, so
   Opus's per-step loop is: run harness, look, tune one value, commit, tick.
6. **Studio/Spark body-integrity guard** — since the fillet body collapses on bad booleans (a bug
   already fixed this project), scaffold a tiny check (reuse `_rear_detail.py --glowports`/a poly-count
   assert) that Opus can run after any boolean-touching change. Wire it into the harness for those two.
7. **Lighting/render upgrades as scaffolding** — set up (but leave OFF-by-default behind a flag) the
   studio-environment-visible-to-GLOSSY-rays-only reflection (allowed; distinct from the rejected
   ambient fill) and a tunable post-bloom pass for the LED light-guides, so Opus can dial them per hero
   without re-plumbing the compositor.

**Every scaffolded tool must be PROVEN to run** (one successful invocation each, output shown) and
committed. Leave clear `# TODO(opus):` markers at each parameter Opus will tune.

## 4 · The STOP condition (this is a hard boundary — respect it for usage)
STOP — do not begin the material iteration loop — once ALL of the following are true and committed:
- Both `ASCENSION-PLAN.md` files exist, are exhaustive per §2, ordered, and cross-linked from the
  handoff index.
- All §3 scaffolding is built, **each proven with one successful run**, and committed with clean
  human-authored messages (LAWS: no attribution, middot only, one bounded change per commit).
- The builders still render + gate-pass after the framework refactor (regression-free).
- `render/MORNING-REPORT.md` in each worktree has an honest "Opus starts here" section naming the
  first unchecked surface-pass and the exact command to run it.
Then write a short final summary of what the operator (Opus) does next, and STOP. Do not tune a single
material value beyond restoring current behavior through the new framework. **Leaving the loop for Opus
is the deliverable, not a shortcoming.**

## 5 · What Opus (the overseer) will then do — design FOR this
Opus opens the top `ASCENSION-PLAN.md`, takes the first unchecked surface, runs your `_iter` harness
(build → macro → gate → A/B composite), looks at the composite beside the real photo, tunes the ONE
flagged parameter via your material framework, re-runs, keeps it only if the tone pin holds, commits,
ticks the box, and moves to the next surface — periodically running your material panel and, every ~5
commits, the full hero reshoot + re-gate + contact-sheet rebuild you also scaffold. **Make that loop
require as little reasoning as possible.** If any step forces Opus to author a new material from
scratch or invent a tool, your scaffolding missed — anticipate it.

## 6 · LAWS + escalations that bind both sessions (never violate)
No git attribution of any kind (commits read as a human wrote them). Trademark gate = blank plates/
recesses only, never a real glyph. Dash gate: middot `·` only. Void-black is the look — no ambient-env
FILL (the glossy-only studio reflection in §3.7 is a different, allowed thing). Keep the co-located LED
point lights. After ANY boolean on the Studio/Spark fillet body, verify body integrity from a LIT
front/q34 (a bad cutter collapses it to a flat plate and hides in the dark rear audit). Verify gates +
pixels before claiming PASS. Autopsy every reverted value; flip-flop guard = re-measure against 2
references before re-changing a value. ESCALATED owner-decisions — do NOT plan or churn: Spark foam
DEPTH (gated at its tone limit), floor-reflection blur-vs-crisp, Studio TOP-logo plate. **The rig frame
is NO LONGER escalated — the owner reopened its sizing (PASS 0 above); re-proportion it deliberately
against references.** Escalate any genuine limit in writing and move on; never fabricate completion.

---

## THE PROMPT (paste this into a fresh FABLE chat)

The `/goal` field has a ~4000-character limit, so the full doctrine cannot be inlined. The prompt below
is a SHORT pointer: it tells Fable to READ the two documents (this file's sections 0-6 + the handoff),
which hold the complete instructions, and carries only the must-not-lose constraints inline as
insurance. Paste this verbatim:

```
/goal You are a FABLE ARCHITECT session for the 8→10 photoreal-material render ascension of three products (6× RTX 5090 FE GPU rig, Mac Studio, DGX Spark, + scale trio). Produce the DOCTRINE + build the SCAFFOLDING, then STOP — do NOT run the material iteration loop (a separate Opus overseer runs it after you).

READ THESE TWO DOCUMENTS IN FULL FIRST — they are your complete operating instructions (too long to inline here); do everything they specify:
1) ~/Downloads/cx-render-handoff/ASCENSION-GOAL-PROMPT.md — sections 0-6: exactly what to plan, what to scaffold, when to STOP, how to design for the Opus overseer.
2) ~/Downloads/cx-render-handoff/ASCENSION-HANDOFF.md — the mission brief, the RIG section, and the per-surface deep-material punch-lists (GPU-priority).
Then read the two they reference: ~/Downloads/cx-render-handoff/GRADING-REPORT.md (sourced research + reference-photo URLs + facet grades) and ~/Downloads/cx-render-handoff/HANDOFF.md (the LAWS). All four bind. (Also mirrored in the repo at render/handoff/.)

Must-not-lose (each expanded in the docs):
• OWNER'S 3 EMPHASES — (1) RIG: resize to a SNUG OPEN-FRAME rig hugging the six-card wall (open-air, cards dominating, minimal empty metal, NOT an oversized cart; owner decision, don't re-litigate), keep a clean flat top for the trio desktops; do it as PASS 0 (structural, first). (2) PHOTOREALISE THE GPUs (Dark Gun Metal shroud, molded blades, anodized fins, light-guide LEDs) — the #1 material goal. (3) STUDIO + SPARK on ALL SIDES, every face.
• DELIVER: render/ASCENSION-PLAN.md in each worktree (rack-oracle=GPU/rig, model-refinement=Studio/Spark) + a handoff-dir index; the scaffolding (A/B compositor, measured-material framework over principled(), PBR asset pipeline, forensic material-panel Workflow, one-command iteration harness, Studio/Spark body-integrity guard, off-by-default glossy reflection + LED bloom). Prove each runs once + commit. Refactor builders to the framework with TODAY'S values as defaults (regression-free). Leave # TODO(opus): at each tunable value.
• LAWS: no git attribution; trademark gate = blank plates only; middot · only; void-black look (no ambient-env fill); keep the co-located LED point lights; check Studio/Spark body integrity from a LIT front after any boolean; verify gates + pixels before claiming PASS; autopsy every reverted value; never fabricate completion.
• ESCALATED, do NOT churn: Spark foam depth, floor-reflection blur, Studio top-logo plate. The rig frame is NOW in scope (PASS 0).
• STOP when both ASCENSION-PLAN.md files are exhaustive + ordered, all scaffolding is built + proven + committed, builders are regression-free, and each render/MORNING-REPORT.md has an "Opus starts here" section naming the first pass + exact command. Then write a short "what Opus does next" and STOP. Leaving a mechanical loop + complete plan for Opus IS the deliverable.
```
