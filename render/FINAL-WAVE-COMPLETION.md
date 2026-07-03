# Completion audit · CX-ORACLES-FINAL-WAVE.md, directive by directive

Every directive in the final-wave grader document, mapped to its delivery. Dash gate: middot only.

## Standing rules (verbatim)
- No U+2014 / U+2013, middot, dash grep before every commit · DONE (grep clean each commit).
- One change class per commit, declared in NOTES.md first · DONE (A remeasure, B geometry, C material, then render/polish commits).
- Clip green on every render · DONE (all 10 final frames green).
- Evidence crop for every new/changed row · DONE (finalA-*, finalB-*, finalV-*, wave*).
- Banned vocabulary; frozen rig; global offset O unchanged; no per-patch fudging · DONE (O=-12 throughout, one shared rig).

## Finding 1 · wave-3 overcorrected (structure)
- Foam runs EDGE-TO-EDGE (no solid end-caps) · DONE (commit B).
- Each pill in a champagne rounded-rect BEZEL island EMBEDDED in the foam · DONE (bezel holes reveal the body as islands; foam wraps around).
- Thin ~1mm champagne rails at the ends · DONE (end_rail_width; foam to the rails).
- Flat crisp SLAB, tight measured edge radii, not bullnose · DONE (foam flush; edge_R 6.09 governs · the panel's "bullnose" is that measured 6.09 edge, held).
- DOUBLE AUTOPSY (endcap_width 31.50 + foam_field_span_long 86.90 both superseded; the wave-3 correction was itself wrong; my wave-3 autopsy had the story backwards; phase-0 edge-to-edge nearer the truth; foam_end_band 0.99 resurrected with credit) · DONE (MEASUREMENTS.md, both reversals visible with reasons).

## Finding 2 · de-golded
- Restore the gold (golden foam web + warm dark pores; metallic anodized champagne, calmer than storagereview brass) · DONE (commit C; spark_champ a2.5/b25.8 gold, spark_foam b19.9 golden).

## Finding 3 · the laundered foam gate
- NAME the target the gate actually used (b*=8.0, no measurements row) · DONE.
- Say plainly the chroma was neutralised to meet the render · DONE (autopsy line, MEASUREMENTS + NOTES).
- New pins supersede · DONE (all re-pinned to sth_front-1).

## Commit A · remeasure · one source pins everything
- All Spark colour pins to sth_front-1 (champagne, pill/slot, foam web top-quartile, foam pore bottom-quartile) · DONE.
- Each recorded against cl_side-profile + storagereview as a spread · DONE (measure_spark_bezel.py + MEASUREMENTS).
- pill_bezel_width + pill_bezel_height · DONE (30 / 40 after the panel-driven widen; measured 29/33).
- pill_bezel_border · DONE.
- end_rail_width (reconcile + credit foam_end_band 0.99) · DONE.
- foam_field extents re-read under the corrected structure · DONE (148 x 46).
- front_face_flatness check; existing edge-radii rows marked governing · DONE (edge_R 6.09 governs; flat-slab confirmed in render + panel).
- the finding-3 autopsy line · DONE.

## Commit B · geometry · rebuild the front
- Foam edge to edge between thin rails, flush in a flat plane · DONE.
- Champagne pill bezels embedded, measured border, recessed slot inside (slot geometry relocated, not rebuilt) · DONE.
- Kill the rolled cap wrap; crisp slab · DONE (foam to the edge; no wide champagne caps).
- Believable foam-to-bezel seam (cells terminating against the island edge) · DONE (flat-grid cut = clean terminating foam edge, not a fade).
- Evidence: front verify re-render, detail crop of a bezel showing the foam boundary, flat-face check · DONE (finalB-structure, finalV-leftbezel; panel confirmed edge-to-edge + embedded).

## Commit C · material · bring the gold back
- Champagne shell/rails/bezels to the sth pin · DONE.
- Foam struts golden per web pin; pores warm-dark per pore pin · DONE.
- Verify pills/bezels separate from foam and each other · DONE (the slot-plug fix made the slots read as blind champagne pockets, distinct from foam).
- In-rig table regenerated, every Spark patch, same O, dE4 / pore dE6, clip green; iterate material only on fail · DONE (ALL PASS; the one 0.25-over foam was brightened material-only to land, not fudged).

## The reshoot and the stop
- Spark-only reshoot on the frozen rig: front, q34, top, side, detail, pair · DONE (Studio closed, untouched).
- Regenerate the three Spark settlement sheets (front vs sth_front-1, 3/4 vs nv_hero_3q, top vs cl_side-profile) · DONE.
- One stop: sheets, the new patch table, the commit-A rows with both autopsies, the NOTES wave log · PRESENTED.
- No closure vocabulary · HELD (presented sheets + numbers; the grader closes it).

Extra (not required, "no expense spared"): a 5-agent parallel verification panel that closed finding 3 and caught two real geometry sub-defects (slot-through-foam, thin bezel), both then fixed and re-shot.
