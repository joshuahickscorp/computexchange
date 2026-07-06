# ASCENSION INDEX · the 8→10 doctrine + scaffolding (built 2026-07-06 by the Fable architect)

The planning+scaffolding session specified by ASCENSION-GOAL-PROMPT.md ran to its STOP
condition. Everything below exists, ran once successfully, and is committed with clean
human-authored messages. The material iteration loop was deliberately NOT started · that loop
is the Opus overseer's job. Dash gate: middot only.

## The two executable plans (the DOCTRINE · work top-down)

1. **GPU + rig (owner priority)** · `computexchange/.claude/worktrees/rack-oracle/render/ASCENSION-PLAN.md`
   PASS RIG-0 (snug open-frame re-proportioning · structural · FIRST) → G-A die-cast shroud →
   G-B molded blades → G-C light-guide LEDs (owns the solo-frame clip debt: gpu-front 1.375% /
   gpu-macro 2.596% FAIL at baseline) → G-D fins/see-through → G-E micro-geometry → G-F
   glossy-env photography → G-G bracket/gold/cables → R-H frame materials.
2. **Mac Studio + DGX Spark (all sides)** · `computexchange/.claude/worktrees/model-refinement/render/ASCENSION-PLAN.md`
   MS-0 gate repair FIRST (fresh calib exposed: spark_top pin obsolete post-S3 · spark_foam box
   drift -17.5 · offset re-pin ~-8.5 class) → MS-A..MS-D Studio (bead-blast/edge-roll · front
   macro · rear cavities · bottom annulus) → SP-A..SP-E Spark (champagne speckle+side · rear
   I/O plate · top strips · bottom cover · foam verify-only).

Each pass row carries: reference URL+crop · current state quoted from the builder · measured
target · one-knob approach · gate + threshold · estimated iterations + risk. Every tunable in
the builders carries a `# TODO(opus):` marker naming its pass.

## The scaffolding (per worktree · all proven once)

| tool | what it does | proof |
|---|---|---|
| `render/_materials.py` | the measured-material framework · today's values as defaults · PBR/aniso/frost/speckle scaffolds default OFF | selftests 14/14 + 13/13 · old-vs-new pixel diff mean 0.0001/255 · gates identical |
| `render/_iter.sh <surface>` | ONE command: render → gates → A/B pair | gpu-shroud + spark-side ran green |
| `render/_pair.py` + `render/ref_photos/manifest.json` | the render-vs-photo composite (the audit primitive) | 4 composites written (gpu-shroud · studio-alu · spark-champagne · spark-side) |
| `render/pbr/<surface>/` + loader | scanned-map slots (albedo/rough/metallic/ao/height · BOX/object projection) + sourcing notes | placeholder set wires 3 maps |
| `render/_material_panel.mjs` | 8-lens forensic panel Workflow + synthesis · hardware-fact claims untrusted BY PROMPT | 2-lens run independently named G-A1 + G-C2 as the top fixes |
| `render/_reshoot.sh` | the every-~5-commits hero bank + re-gate + contact sheet / calib + integrity + tiles | committed (plumbing = the proven pieces it sequences) |
| `render/_integrity.py` (model-refinement) | the fillet-body collapse guard: poly floors + bbox + LIT-front fractions · exit-coded · wired into boolean-adjacent gates | INTACT run: 12717/1769 polys · 30.8%/17.9% fractions |
| `--glossenv S` + `--post --bloom-*` (build_rack) | glossy-only studio reflection + tunable LED bloom · default OFF · camera rays stay void-black | bg pixel identical at S=0.5 · device pixels gain the reflection · bloom args move the frame |

## References on disk
- 5090 FE: `rack-oracle/render/ref_photos/` (8 driver-inspected LanOC/HWCooling photos ·
  SOURCES.md has provenance + the gallery URL patterns · TPU blocks plain fetchers).
- Desktops: the existing `model-refinement/render/ref/{mac-studio,dgx-spark}/` library.
- STILL TO FETCH (first Opus errands · the manifests mark them): RIG-0a open-frame rig photos ·
  a LIT-LED club386/TheFPSReview crop for G-C · the iFixit Studio bottom photo · the STH Spark
  top-down photo.

## What Opus does next (the whole loop · designed to need no invention)
1. Open `rack-oracle/render/ASCENSION-PLAN.md` · take the first unchecked box (RIG-0a).
2. For material passes: `./render/_iter.sh <surface>` · look at `render/pairs/<surface>-AB.png`
   · turn the ONE `TODO(opus)` knob the pass names · re-run · keep only if the gates hold ·
   commit (no attribution · middot · one bounded change) · tick the box.
3. Every ~5 commits: `render/_reshoot.sh` + the panel Workflow on the fresh pairs · act on
   rendering-quality tells only.
4. Escalate in writing after 3 fails · never churn the owner-escalated items (Spark foam depth ·
   floor reflection · Studio top-logo plate) · never fabricate completion.

Binding docs (unchanged · all four still law): ASCENSION-GOAL-PROMPT.md · ASCENSION-HANDOFF.md ·
GRADING-REPORT.md · HANDOFF.md (this directory + mirrored in the repo at render/handoff/).
