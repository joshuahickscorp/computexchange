# render/PHOTOREAL-LEDGER.md · the defect ledger (photoreal frontier)

Status key: OPEN · FIXED-UNCONFIRMED (commit landed, local evidence) · CLOSED (a full panel loop
passed with no agent naming it) · REOPENED (a CLOSED tell was named again; autopsy names the commit).
A tell named by >=2 agents in one loop, or in two consecutive loops, is priority one. FALSE-TELL =
true of the reference device (measurement beats panel), logged and not chased. Dash gate: middot only.

| id | tell | owning class | status | fix commit | evidence | panel loops named | notes |
|----|------|-------------|--------|-----------|----------|-------------------|-------|
| T1 | surface perfection · single roughness band, no smudge/dust/sparkle | MATERIAL | OPEN | - | - | - | 3-octave roughness, grazing sparkle, sub-threshold smudge+dust |
| T2 | foam depth uniformity + strut threading | FOAM-GEO-MAP | FIXED-UNCONFIRMED | foamgeo | foamgeo-dethread.png | - | coarse Voronoi cells + non-periodic fine noise (de-thread) + low-freq clouds depth hierarchy (bimodal pores). Residual: faint strut ribbing, a few over-flat shallow patches. Crush band + torn-cells deferred. |
| T3 | infinite focus | CAMERA | FIXED-UNCONFIRMED | camera | (reshoot) | - | physical DOF, focus on the front face · details f5.6 (strong falloff), pair f11 (far device softer), heroes f16 (far edge a breath soft, keeps tone patches sharp/gated) |
| T4 | missing image-formation layer | POST | FIXED-UNCONFIRMED | post | dgx-spark-q34-post.png | - | one PIL chain: roll, radial CA (+/-0.18%), specular bloom (thr0.88), gentle vignette, fine luminance grain (deterministic per frame). Post-delta on tone patches < 0.6 L; gate stays pre-post |
| T5 | empty reflections | LIGHTING | OPEN (tone-blocked) | - | - | - | the strip RIM draws a readable-edge line on the fillets; a defined SOFTBOX reflection on the matte champagne top desaturated its gold below the pin at every readable energy · tone gate SENIOR, so deferred. Revisit with a champagne-albedo compensation IF the panel names it |
| T6 | laboratory ground | LIGHTING+MATERIAL | FIXED-UNCONFIRMED | lighting | (reshoot pair) | - | floor micro-normal (~fine) + low sheen so the light reads as a broad smear, not a mirror; contact grades to a soft penumbra |
| T7 | CAD-sharp silhouettes | MATERIAL (bevel) | FIXED-UNCONFIRMED | material | material-bevel-edge.png | - | ShaderNodeBevel 0.30mm on every metal (alu, champagne, top-vent); edges now catch a hairline highlight |
| T8 | statistical placement perfection | MATERIAL+FOAM-GEO-MAP | FIXED-UNCONFIRMED | material | material-bevel-edge.png | - | anodize batch mottle (~60mm, low-amp) on the champagne shell + aluminium roughness. Intake/weave pitch-jitter deferred if the panel names them |
| T9 | perfect coplanarity | CAMERA+LIGHTING | FIXED-UNCONFIRMED | camera+post | (reshoot) | - | pair: Spark yawed -14.5 vs Studio -14.0 (a hair more). Sub-degree camera ROLL applied in the POST stage |

## own cold re-look findings (appended as discovered; same schema)
| id | tell | owning class | status | fix commit | evidence | panel loops named | notes |
|----|------|-------------|--------|-----------|----------|-------------------|-------|
| O1 | foam strut helical threading (two-displacement interference) | FOAM-GEO-MAP | FIXED-UNCONFIRMED | foamgeo | foamgeo-dethread.png | - | fine Voronoi replaced by non-periodic clouds noise · helical read much reduced (faint ribbing remains for the panel to judge) |
| O2 | champagne flat/plastic (no anodize grain) | MATERIAL | FIXED-UNCONFIRMED | material | - | - | anodize mottle added; directional extrusion grain deferred |
