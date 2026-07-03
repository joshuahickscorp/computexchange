# render/PHOTOREAL-LEDGER.md · the defect ledger (photoreal frontier)

Status key: OPEN · FIXED-UNCONFIRMED (commit landed, local evidence) · CLOSED (a full panel loop
passed with no agent naming it) · REOPENED (a CLOSED tell was named again; autopsy names the commit).
A tell named by >=2 agents in one loop, or in two consecutive loops, is priority one. FALSE-TELL =
true of the reference device (measurement beats panel), logged and not chased. Dash gate: middot only.

| id | tell | owning class | status | fix commit | evidence | panel loops named | notes |
|----|------|-------------|--------|-----------|----------|-------------------|-------|
| T1 | surface perfection · single roughness band, no smudge/dust/sparkle | MATERIAL | OPEN | - | - | - | 3-octave roughness, grazing sparkle, sub-threshold smudge+dust |
| T2 | foam depth uniformity + strut threading | FOAM-GEO-MAP | FIXED-UNCONFIRMED | foamgeo | foamgeo-dethread.png | - | coarse Voronoi cells + non-periodic fine noise (de-thread) + low-freq clouds depth hierarchy (bimodal pores). Residual: faint strut ribbing, a few over-flat shallow patches. Crush band + torn-cells deferred. |
| T3 | infinite focus · zero DOF | CAMERA | OPEN | - | - | - | per-shot focus plane + physical DOF; far device softer on the pair |
| T4 | missing image-formation layer · no grain/bloom/aberration/vignette | POST | OPEN | - | - | - | one post chain, each effect below conscious notice |
| T5 | empty reflections · metals reflect abstract gradients | LIGHTING | OPEN | - | - | - | readable softbox EDGE in each metal, consistent between devices |
| T6 | laboratory ground · pasted contact, no micro-texture/bounce | LIGHTING+MATERIAL | OPEN | - | - | - | AO-to-penumbra gradient, ground micro-sheen, champagne warmth into contact |
| T7 | CAD-sharp silhouettes · mathematically sharp edges | MATERIAL (bevel) | OPEN | - | - | - | 0.2 to 0.4mm bevel shader on every metal, hairline edge highlight |
| T8 | statistical placement perfection · perfect periodicity | MATERIAL+FOAM-GEO-MAP | OPEN | - | - | - | sub-perceptual breaks in intake/weave/foam, low-amplitude anodize mottle |
| T9 | perfect coplanarity · everything axis-aligned | CAMERA+LIGHTING | OPEN | - | - | - | sub-degree camera roll, one device yawed a hair more |

## own cold re-look findings (appended as discovered; same schema)
| id | tell | owning class | status | fix commit | evidence | panel loops named | notes |
|----|------|-------------|--------|-----------|----------|-------------------|-------|
| O1 | foam strut helical threading (two-displacement interference) | FOAM-GEO-MAP | FIXED-UNCONFIRMED | foamgeo | foamgeo-dethread.png | - | fine Voronoi replaced by non-periodic clouds noise · helical read much reduced (faint ribbing remains for the panel to judge) |
| O2 | champagne reads a touch flat/plastic on the smooth shell (no anodize grain) | MATERIAL | OPEN | - | - | - | folded into T1/Spark-champagne anodize directional structure |
