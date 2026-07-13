<!-- CLAIM-SCOPE: internal-engineering-non-authoritative -->
# cx-render-handoff · start here

Photoreal render effort for three products (RTX 5090 FE rig · Mac Studio · DGX Spark). We are **right at
the cusp** — the Studio + Spark read near-photographic; the **GPU geometry** is the last mile.

## Order of operations for the next session
1. **Open `images/`** — the current best renders, by object. `trio/` = the money shots. `rtx5090fe/` =
   the GPU (the priority). Glance through to see where we are.
2. **Start a fresh session on `claude-fable-5`** and paste **`FABLE5-RESEARCH-PROMPT.md`**. Fable 5 will:
   research all three products exhaustively (360°: front/rear/bottom/top, exact geometry + materials,
   cited), grade each object on many facets vs our renders, give a path to 10/10 per facet, write
   **`GRADING-REPORT.md`** here, and emit a ready-to-paste **`/goal` loop**. Then it stops (no rendering).
3. **Switch the model to `claude-opus-4-8`** and paste the `/goal` loop from the report. Opus 4.8 runs
   the continuous render → verify → re-render → research → improve loop, working the lowest grades first.

## Files
- `HANDOFF.md` — the mission, the file/worktree map, and the METHODOLOGY (read in full; it's the point).
- `FABLE5-RESEARCH-PROMPT.md` — paste into Fable 5 (step 2).
- `GRADING-REPORT.md` — Fable 5 writes this (research + grades + the /goal loop).
- `images/` — current renders (trio, rig, rtx5090fe 360, mac-studio 360, dgx-spark 360, contact sheet).

The code lives in two git worktrees under
`/Users/scammermike/Downloads/computexchange/.claude/worktrees/` (rack-oracle, model-refinement) — see
HANDOFF.md for the builders, the numeric gates, and the sourced spec files.
