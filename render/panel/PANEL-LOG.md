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
