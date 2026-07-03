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

## Loop 4 pending · L3 cleanup (warpXZ 1.05->0.6 [un-distort cutouts], bevel 0.24->0.16, reflector
## broadened+raised+dimmed [kill the blobs]). Tone gate ALL PASS.
