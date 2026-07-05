# CX ORACLES · GEOMETRY DEEP AUDIT (2026-07-05)

10 vision agents, one per reference cluster, geometry only (shape / proportion / position / radius /
presence · never tone), pixel-ratio measurements against every reference in `render/ref/`. Raw
structured findings: `render/geometry-audit-raw.json` (85+ findings). This file is the ARBITRATED
synthesis: agent findings cross-checked against the frozen measured pins and my own direct frame
reads, because a single vision pass can mis-measure through perspective · every conflict is flagged
rather than blindly actioned.

## Scorecard (0-10 · 10 = a machinist could not tell render from reference)

### Mac Studio · the laggard (owner's read confirmed)
| aspect | score | arbitration |
|---|---|---|
| top flatness / top-edge fillet | 9 / 8 | best-in-model |
| proportions (front W:H) | 7 | dimensional unit's 3/10 OVERRULED · body is spec-exact 197x95 (ratio 0.482 vs drawing 0.481); agent measured through perspective. Ortho verify queued |
| port row datums (layout, spacing, heights) | 8 | reproduction-grade per audit |
| ports geometry (apertures) | 5 -> ~6.5 tonight | LED repositioned onto flat face (was on the corner ARC · bad old pin, autopsied), SD 2.25 + dark aperture. USB-C width 2.62 vs audit 4.2:1 CONFLICTS with a measured pin · re-measure queued |
| intake band | 4 -> ~7 tonight | mechanical bug found: perf threshold lost the 2.5mm zlift, rendering ~6.3mm of a 12.4mm band. Fixed; reshoot verifies |
| corner radii / blends | 4 -> ~6 tonight | segs 48/12 -> 64/24 kills the facet; true G2 corner-to-fillet blend queued |
| silhouette overall | 6 | base undercut profile queued; top logo inlay = TRADEMARK-GATED, excluded by doctrine (not a defect) |
| REAR | 0 | never built · full build spec cataloged (quality 7/10), scope-gated |

### DGX Spark · closer (owner's read confirmed) but the audit is harsher than the panel was
| aspect | score | arbitration |
|---|---|---|
| side proportion / cleanliness | 7 / 7 | solid |
| edge fillets (3/4) | 7 | side unit says side-face radii off (S4) · re-measure, units disagree |
| front (foam field, plateaus, slots, tabs) | 3-4 RAW · flagged | several findings CONFLICT with the L15 build verified directly against cl_front-foam (tabs exist but read "missing" at 1mm · sth refs suggest wider; plateau proud-vs-flush contested). Cross-reference re-measure FIRST, then fix. Genuine new catches: pill slot should be a smooth concave SCOOP (ours is flat-floor tub); the real pills are THROUGH-slots with a diamond mesh grille behind (ours are blind) |
| foam character | flagged | cell scale finding conflicts with the L19 fine build · re-measure across refs |
| top vent | 2-3 -> ribbing fixed tonight | zipper ribbing on recess wall KILLED (weave now normal-gated to the flat panel · acceptance: zero ribs). Queued: dish-slope transition (S5, 15mm annulus spec), recess corner radius ~19mm squircle, border widths, the missing 133x3mm slot, weave pitch (FLIP-FLOP GUARD: L17 read it fine, this audit reads 5mm bands · measure twice before a third change) |
| REAR / BOTTOM | 0 / 0 | never built · build specs cataloged (8/10, 7/10), scope-gated |

### Pair
| aspect | score | note |
|---|---|---|
| relative scale | 4 · flagged | S5: Spark:Studio width ratio off vs 0.761 expected · verify PAIR_SPAN/camera math before touching geometry |
| ground contact / composition | 7 / 7 | fine |

## Tonight's applied fixes (geo-audit pass 1, commit 3a3c344 · tone gate ALL PASS)
1. LED x 87.70 -> 64.60 (onto the flat face · old pin autopsied: measured through corner foreshortening)
2. Perf band to 12.4mm exposed (zlift bug found and fixed · the band was rendering at half height)
3. SD slot 2.50 -> 2.25 + interior lip recessed to 3.4mm (dark aperture)
4. Body segs 64/24 (corner facet)
5. Spark vent weave normal-gated off the recess walls (zipper ribbing -> zero)

## What drives the next loop
The full ranked queue with per-item acceptance checks lives in the goal document:
`~/Downloads/CX-ORACLES-GEOMETRY-TEN.md`. Raw evidence: `render/geometry-audit-raw.json`.
