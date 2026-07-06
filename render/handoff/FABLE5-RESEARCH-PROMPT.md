# PROMPT FOR FABLE 5 (paste this into a fresh session running claude-fable-5)

You are the **research + grading** phase of a photorealistic 3D-render effort. Your entire job this
session is to (1) research the three real products exhaustively on the web, and (2) grade our current
renders against reality on as many facets as you can find, with a concrete path to 10/10 for each.
**You do NOT render or edit any 3D code — you research, grade, and hand off. Stop cleanly after the
grades + the goal loop.** A separate Opus 4.8 session will do the iterating.

## Read first (context — do not skip)
- `~/Downloads/cx-render-handoff/HANDOFF.md` — the mission, the file map, and the render→verify→improve
  METHODOLOGY that got us here. Internalize it.
- `~/Downloads/cx-render-handoff/images/` — our CURRENT best renders, organized by object:
  `trio/` (the money shots), `rig/`, `rtx5090fe/` (the 5090 FE card, 360°), `mac-studio/`, `dgx-spark/`.
  Open every one. This is what you are grading against reality.

## The three objects (same premium style, same ideology — dark void-black studio, true scale)
1. **NVIDIA RTX 5090 Founders Edition** (in the 6-GPU rig) — **this is where we are weakest.** NVIDIA's
   FE geometry is subtle: ours reads *close* but a real one differs. This is the priority.
2. **Mac Studio** (197×197×95 mm).
3. **NVIDIA DGX Spark** (150×150×50.5 mm, gold metal-foam) — already close; verify.

## STEP 1 — RESEARCH (web, exhaustive, cite everything)
For EACH of the three, research the real product **fully 360°** and pin exact facts with sources:
- **Front** — every port, button, vent, logo placement, cut-line, proportion.
- **Rear** — every port (type, count, order, exact shape/size), vents, connectors, backplate features.
- **Bottom** — the face that rests on the rig/desk (feet, vents, labels, base reveal).
- **Top** — the face pointing up (vents, logo, exhaust fins, power connector, texture).
- **Sides** — profile, thickness, cut-lines, fin stacks.
- **Geometry** — exact dimensions (mm, against a known anchor), fan blade count + curvature + pitch,
  slot count, radii, chamfers, the X-accent shape, the flow-through windows.
- **Materials/finish** — exact colour (name + approx PBR), roughness/anisotropy, the LED elements.
Use manufacturer spec pages, TechPowerUp, iFixit/Chargerlab teardowns, GamersNexus, reviews WITH
measurements, official product photography, dimensioned drawings. Prefer teardowns + measured sources.
When sources conflict, pick the best-supported and log the conflict. **Cite every source URL.**
Watch for the traps we hit: the 5090 FE genuinely HAS white LEDs; the Spark front IS metal foam (not a
cheese-grater); the Mac Studio IS a squat 197×95 slab. Confirm, don't assume.

## STEP 2 — GRADE (find as many facets as possible)
For EACH object, invent as many distinct grading facets as you can (aim for many — e.g. front-port
accuracy, rear-port accuracy, bottom face, top face, fan-blade geometry, shroud cut-lines, fin-stack
read, proportions, colour/finish, LED behaviour, material identity, edge treatment, grounding, 360
completeness, …). For each facet give:
- **Current grade X/10** (judged from our render vs the real reference you researched).
- **What a 10/10 looks like** (the specific real-world truth, with the source).
- **The concrete path to 10/10** (the exact bounded change(s), in priority order).
Then an **overall grade per object** and the single highest-leverage fix for each.

## STEP 3 — OUTPUT, then STOP
Write everything to `~/Downloads/cx-render-handoff/GRADING-REPORT.md`:
1. The sourced research (per object, per face), with URLs.
2. The full facet-by-facet grades + paths to 10/10.
3. A ready-to-paste **`/goal` loop** for the Opus 4.8 session — a single `/goal` command that instructs
   Opus 4.8 to run the continuous render→verify→re-render→research→improve loop from HANDOFF.md, working
   the lowest-graded facets first (GPU geometry leads), one bounded gate-verified change per commit,
   cycling all 360 angles, researching + citing before modelling, never fabricating completion, and
   leaving an honest next each pause. Make the `/goal` self-contained enough that pasting it (with the
   model switched to Opus 4.8) starts the loop against this report + HANDOFF.md.

**Then stop.** Do not render, do not edit builders, do not start the loop. Hand off with the report +
the goal loop. That is the clean finish line for this session.
