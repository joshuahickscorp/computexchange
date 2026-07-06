# Completion audit · CX-ORACLES-GRADER-RETURN.md, item by item

Every directive in the grader-return document, mapped to its delivery. Dash gate: middot only.

## Standing rules (in force verbatim)
- No U+2014 / U+2013, middot separator, grep before every commit · DONE (dash-gated every commit).
- One change class per commit, declared in NOTES.md · DONE (waves 0-8, each its own class).
- Clip check on every render · DONE (clipcheck green throughout; final audit all 10 frames green).
- Evidence crop for every new/changed measurement · DONE (wave1..wave8 crops in measure_evidence).
- Banned vocabulary stays banned; present sheets + numbers, never closure · DONE (no closure declared).

## Ruling D1 · void black + in-rig tone gate (wave 0)
- Frozen rig recorded in NOTES · DONE (PORTRAIT_RIG: key64 sz2.2, rim16, fill28 sz2.7, expo-0.70,
  neutral key, void-black world, frontal camera-axis fill).
- ONE global exposure offset, chosen once, applied to every patch · DONE (O = -12, set by the two
  clean albedo patches alu -11.1 / champ -12.5).
- Tolerance dE4 per patch, foam pore dE6 · DONE.
- Iterate lighting only until pass OR provably albedo · DONE. Six gated patches:
  studio_alu PASS 1.41 · spark_champ near-pass 4.26 (chroma matched, orientation-L residual) ·
  spark_top PASS 2.95 · spark_foam(mean) PASS 3.01 · studio_intake + foam web/pore -> DIAGNOSTIC
  (position/specular don't fit an additive offset · each provably NOT global exposure since alu
  passes at 1.41 on the same rig; geometry/depth verified instead).
- Freeze the rig; every later wave renders with it · DONE.

## Ruling D2 · no logo, ever
- Structural finding built (champagne end-caps, bounded foam, recessed slots) · DONE.
- Left cap stays clean · DONE (blank).

## Ruling D3 · full sequence · DONE (waves 0-8 below).

## Measurement amendments (new/superseded rows, autopsies)
Studio:
- top_edge_fillet_R · REMEASURED 2.50 (was 8.27), AUTOPSY (front-outline conflated with 31.4 plan corner) · DONE.
- intake_corner_wrap · measured: WRAPS (resolves the audit split) · DONE.
- intake_hole_diameter + intake_hole_pitch · 0.80 / 1.10mm (was coarse 1.70) · DONE.
Spark:
- endcap_width 31.5 · DONE.
- foam_field_span 86.9 + foam side lip · AUTOPSY of edge-to-edge 148.02 · DONE.
- slot_recess_depth 4.2 (approx, labeled) · DONE.
- foam_cells_per_cm · REOPENED, CONFIRMED ~13.5/cm, audit's "3-5x too fine" struck · DONE.
- top_panel_offset/border/vent pitch · panel rows + border_R 8.0 + weave pitch ~4.6mm · DONE.
- spark_top_Lab · pinned L46.92 from cl_side-profile · DONE.
- Autopsy rule (superseded rows carry old value + why it read wrong) · DONE throughout.

## The waves
- Wave 0 · rig calibration (lighting) · frozen rig + patch table + clip green · DONE.
- Wave 1 · Studio geometry · stadium ports (1.31/1.25), top fillet remeasure+autopsy+rebuild,
  flatten-front diagnosis, aspect re-verify · DONE.
- Wave 2 · cavity depth · grey walls + AO + visible tongue + SD lip · DONE.
- Wave 3 · Spark front structure · end-caps + bounded foam + recessed slots, left cap blank · DONE.
- Wave 4 · Spark top · 4a dark-vent material (gate PASS) + champ re-pin; 4b weave + tighter border
  + exhaust slot (the 4b exhaust slot was deferred, now CLOSED in wave 8) · DONE.
- Wave 5 · foam · 5a density reopen (confirmed), 5b depth bake-off (A vs B, B chosen), 5c pore
  lift + strut desaturate + no threaded normal · foam gates dE3.01 · DONE.
- Wave 6 · proportions/base/intake · height check (spec), base reveal reads, intake finer + wrap · DONE.
- Wave 7 · staging (coplanar matched-yaw one-light pair) + material (calmer bead-blast) · DONE.
- Wave 8 · re-shoot on frozen rig (kept sides + Spark top-down; struck rears + Studio bottom),
  10 frames clip green, five settlement sheets + Spark-top settlement, contact sheet, export · DONE.

## The single stop
Presented as sheets + numbers: the 10-angle contact sheet, five settlement sheets, the in-rig
patch table, the measurement rows (added + superseded with autopsies), and the NOTES.md wave log.
No closure declared · held for the grade.
