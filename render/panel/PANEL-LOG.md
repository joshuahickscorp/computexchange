# render/panel/PANEL-LOG.md · forensic cold-agent panel loops (photoreal frontier)

Protocol: 5 fresh cold vision agents per loop, each a distinct forensic lens (reviewer / lookdev /
photographer / materials / buyer). Each agent is shown my post gate frames MIXED with real-photo
controls (actual Spark/Studio hardware) under neutral names in a neutral folder, and forced to call
each PHOTOGRAPH or CG_RENDER with confidence + up to 3 tells. Cold: no agent knows the mix or purpose.
CLEAN = no MINE gate frame flagged render by >=2 agents AND no tell named by >=2 on a gate frame.
Calibrated against the real controls' render-call rate. Two CONSECUTIVE clean panels = stop.

## Loop 1 (2026-07-03) · NOT CLEAN
- MINE render-call rate **0.97** vs REAL(control) **0.14** · the panel separates render from photo.
- Gate frames: studio-front 5/5 render (uniform4, reflect5, repeat2); spark-front 5/5 (foam5,
  uniform2, bevel2, shadow2); spark-detail 5/5 (foam5, clean2, edge3, uniform2); pair 4/5 (reflect4,
  foam5); [secondary] studio-q34 5/5 (dust2, edge5, reflect3); spark-q34 5/5 (foam5, grain2, bevel2).
- Real controls: spark-foam 0/5, studio-wiki 0/5, studio-3q 0/5, studio-apple 0/5, spark-sth2 0/5,
  spark-srv 1/5; spark-side(cl_side-matte) 4/5 render (a real photo that reads CG: matte side-on +
  clean bg · a calibration FALSE read, useful).
- Priority-one tells (>=2 agents): **foam** (procedural/tiled/no 3D pore occlusion, 5/5 every Spark
  frame) · **reflect** (empty/too-clean metal + too-symmetric floor, strongest on the SILVER studio)
  · **uniform/dust/clean** (surfaces too perfect) · **edge/bevel** (idealized) · **shadow** (contact/
  floating) · **grain** (image-formation still reads synthetic on q34).

## Loop 2 (2026-07-03) · NOT CLEAN · after pass-1 (foam warp+deeper+contrast, overhead reflector, grunge, bevel 0.42, floor)
- MINE render-call rate **0.90** (was 0.97) vs REAL **0.06** (was 0.14). My frames improved slightly;
  the panel also grew MORE confident on the reals, so the discrimination gap held (~0.84).
- Gate frames: pair 5/5 (foam5, uniform2, clean2); studio-front 5/5 (reflect5, edge2, perfect2);
  spark-detail 4/5 (foam4, bevel2); spark-front 4/5 (foam4, reflect2); [secondary] studio-q34 5/5
  (edge3, bevel2, perfect2, reflect2); spark-q34 4/5 (foam5, clean2, reflect2, edge3).
- Reals: spark-side 0/5(!, was 4/5 · a real photo now correctly read), studio-3q 0, spark-foam 0,
  studio-apple 0, studio-wiki 1, spark-srv 1, spark-sth2 0.
- Learnings: (a) foam still reads procedural at MACRO scale (uniform cell SIZE, not lattice) despite
  the warp; (b) the bevel BACKFIRED · bumping to 0.42 made a "razor-thin CG bevel line" (edge/bevel
  named) · anti-drift: dial it back past the original; (c) reflection still reads "synthetic gradient"
  and surfaces "too clean/clay" · the grunge is too subtle to register.

## Loop 3 (2026-07-03) · NOT CLEAN · after pass-3 (bevel 0.24, foam macro cell-size variation, wear up)
- MINE render-call rate **0.87** (0.97 -> 0.90 -> 0.87) vs REAL **0.14**. Slow asymptotic gain.
- Gate frames: spark-front 5/5 (bevel3, foam5, reflect2); studio-front 5/5 (reflect5, perfect3,
  grain2); spark-detail 4/5 (foam4, bevel4); pair 3/5 (shadow2, foam2); [secondary] studio-q34 5/5
  (reflect5, bevel3); spark-q34 4/5 (bevel3, shadow3, foam3).
- Reals now drawing a few false render-calls too: spark-sth2 2/5 (foam2), spark-foam 1/5 (foam2) ·
  the agents are primed on "foam == procedural", so even REAL foam photos take foam tells (the tell
  is becoming a panel bias, not purely a render defect).
- Loop 3 exposed TWO self-inflicted regressions to fix: (a) the overhead reflector cast circular
  "ghost AO blob" artefacts on the silver top; (b) the foam warpXZ distorted the bezel/port cutouts
  ("warped L and O"). Also the shader bevel highlight reads as a "uniform CG specular line" at ANY
  size (its UNIFORMITY along the whole edge is the tell, not its radius).

## Loop 4 (2026-07-03) · NOT CLEAN but GAP COLLAPSING · after L3 cleanup
- MINE render-call rate **0.83** (0.97 -> 0.90 -> 0.87 -> 0.83) vs REAL **0.29** (up from 0.06-0.14).
  Discrimination gap 0.83 -> 0.84 -> 0.73 -> **0.54**. The panel is losing the ability to separate:
  real Apple lifestyle 3/5 render, real spark-sth2/foam 2/5, and MY spark-front dropped to 2/5.
- Persistent gate tells: foam (5/5 spark-detail, spark-q34), edge (edge6 spark-detail, edge4 studio-
  front · the bevel/rim edge highlight UNIFORMITY, size-independent), perfect/clean/uniform, ground/
  shadow (soft contact on seamless black · partly the deliberate site-match void = FALSE-TELL).
- CRITICAL DISCOVERY: the "soft dark circular blobs / identical dimples / AO artifacts" the panel
  named on the SILVER TOP across loops 2-4 were a SELF-INFLICTED bug in add_grunge, NOT a model
  defect: the smudge used a 45 mm VORONOI, which tiled the 197 mm studio top into a ~4x4 grid of
  roughness cells reading as a regular dimple grid on the reflective surface. Persisted at 1100
  samples (so not denoiser) and with the reflector OFF (so not the reflector). AUTOPSY -> commit L4:
  smudge Voronoi replaced by large ORGANIC NOISE (non-cellular), amplitude pulled back. The top now
  reads as real mottled aluminium. This regression had been inflating my studio render-calls for 3
  loops · the true post-fix rate is measured in loop 5.

## Loop 5 pending · L4 grunge fix, on the FINAL 4K/700-sample delivery frames (fairest test).
## Tone gate ALL PASS (alu 2.11, champ 3.16, top 2.99, foam 5.40).

## Loop 5 (2026-07-03) · NOT CLEAN · on the 4K delivery frames (grid-dimple fixed)
- MINE **0.97**, REAL **0.03**. MINE rose vs loop 4 (0.83) · TWO causes: (a) large loop-to-loop
  PANEL VARIANCE (loop 4 drew a soft panel · REAL 0.29; loop 5 drew a harsh one · REAL 0.03), and
  (b) the grunge FIX traded the "grid dimples" tell for a "cloudy blotchy procedural MARBLE/smudge"
  tell on the reflective studio top (flagged 88-95 conf). Any roughness variation on the big mirror
  top reads as fake.
- DECISIVE LESSON: every REAL studio control scored 0/5 render DESPITE being "flawless/clean" · so
  "too clean" was never the giveaway. Real Mac Studio tops ARE immaculate. Added imperfection is the
  tell, not its absence. -> commit L5: grunge REMOVED from aluminium + champagne (anti-drift, dialed
  to zero). The clean bead-blast/anodize stays.
- Persistent structural residual across all 5 loops: foam (procedural macro, improved), edge/bevel
  (uniform highlight), reflect (matte metal on void-black = site-match FALSE-TELL), soft contact
  shadow (site-match FALSE-TELL).

## Loop 6 pending · FINAL · clean surfaces (grunge removed), 4K delivery frames.

## Loop 6 (2026-07-03) · FINAL · NOT CLEAN · clean surfaces (grunge removed), 4K delivery frames
- MINE **0.87**, REAL **0.20**. The clean-surface revert WORKED on the studio: studio-front 5/5 -> 3/5,
  the marble tell gone, remaining tells all mild (clean2/reflect2/edge2/shadow2). Foam still 5-6/5 on
  every spark frame; edge-uniformity persists.
- STOP DECISION. Six genuine loops. MINE 0.83-0.97 vs REAL 0.03-0.29 across the run · consistently
  distinguished, never near clean, with large panel-draw variance. The residual is structural:
  * FOAM (dominant, most-cited, device-specific) is a TECHNIQUE CEILING · heightfield displacement,
    however warped/deepened/size-varied, cannot make true open-cell topology (struts visible BEHIND
    struts, light passing through). Reads procedural to expert cold agents. Would need actual 3D foam
    geometry (voxel/scanned), a different pipeline.
  * EDGE/BEVEL uniform highlight · structural to a shader bevel + the frozen-rig rim on the fillet.
  * REFLECT + SHADOW/GROUND · FALSE-TELLs under the SENIOR constraints (void-black = deliberate
    site-match; soft contact on a seamless plane). Chasing them breaks the site integration or the tone.
- Per the authority hierarchy (measurement > grader > panel > eye) and the restraint doctrine
  ("dial back when named", "FALSE-TELLs not chased", "post rescues nothing"), the loop stops here and
  the result is reported honestly. Two-consecutive-clean is not reachable within the senior
  constraints (measurement tone-lock + void-black site-match) on a procedural heightfield pipeline.

## Summary trajectory
| loop | MINE | REAL | note |
|---|---|---|---|
| 1 | 0.97 | 0.14 | baseline |
| 2 | 0.90 | 0.06 | foam warp+deeper, reflector, grunge, bevel 0.42 |
| 3 | 0.87 | 0.14 | bevel 0.24, foam macro variation |
| 4 | 0.83 | 0.29 | un-distort cutouts, bevel 0.16 (soft-panel draw) |
| 5 | 0.97 | 0.03 | grunge->marble regression + harsh-panel draw |
| 6 | 0.87 | 0.20 | grunge removed (clean) · FINAL |

## Loops 7-8 (2026-07-03) · resumed iteration after re-reading the grader's T5/part-4 specs
- Found two UN-DONE grader acceptance criteria and implemented them:
  * T5 (line 50-51): metals must reflect a softbox with a READABLE EDGE (not a gradient). Added a
    glossy-only studio reflection WORLD (bg + diffuse stay void-black, tone-safe) THEN a defined
    rectangular SOFTBOX (Apple dark-hero style) gate-tuned to energy 4.2 · the studio top now shows
    a real soft-edged softbox reflection with a specular fillet line.
  * part-4 foam (line 96): torn/merged deep cells · added a third coarse non-harmonic Voronoi (4.7mm)
    + the large foam tonal variation. Also gentled the post vignette (the "letterbox" read).
- Loop 7 (reflection env): MINE 0.93, REAL 0.17. Loop 8 (softbox + torn cells): MINE 0.90, REAL 0.20.
- The aggregate held ~0.90. The reflect tell softened on the studio (the softbox reads), but EDGE
  rose to co-dominant (edge5 studio-q34 · "razor-thin uniform specular line") and FOAM stayed 5/5 on
  every spark frame.
- DECISIVE, GRADER-ALIGNED CONCLUSION (8 loops, every grader-specified material/lighting technique
  now implemented and tone-gated): the panel does not reach two-consecutive-clean because the residual
  is (1) a FOAM TECHNIQUE CEILING · a displaced heightfield cannot be reticulated open-cell foam with
  true through-strut self-shadow, no matter the cell hierarchy/torn-cells/de-thread (the grader's own
  part-4 recipe, fully applied) · it needs real 3D/scanned foam geometry, a different pipeline; (2)
  EDGE uniformity, structural to a perfect bevel; (3) FALSE-TELLs · the void-black dark-hero aesthetic
  is GRADER-MANDATED (line 105) and the "gold metal foam looks CG" read now flags REAL foam photos too
  (spark-foam control 2-3/5). Per the grader's own rule (line 173) "if the panel dislikes reality, the
  panel loses to the measurement." The tone gate stayed SENIOR and GREEN through all 8 loops.

## Summary trajectory (8 loops)
| loop | MINE | REAL | note |
|---|---|---|---|
| 1 | 0.97 | 0.14 | baseline |
| 2 | 0.90 | 0.06 | foam warp+deeper, reflector, grunge, bevel 0.42 |
| 3 | 0.87 | 0.14 | bevel 0.24, foam macro variation |
| 4 | 0.83 | 0.29 | un-distort cutouts, bevel 0.16 (soft-panel draw) |
| 5 | 0.97 | 0.03 | grunge->marble regression + harsh-panel draw |
| 6 | 0.87 | 0.20 | grunge removed (clean) |
| 7 | 0.93 | 0.17 | glossy reflection env + foam tonal variation |
| 8 | 0.90 | 0.20 | readable-edge softbox + foam torn cells |
