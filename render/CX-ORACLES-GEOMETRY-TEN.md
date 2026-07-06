# CX ORACLES · GEOMETRY TEN · the exactness loop

Repo worktree: `/Users/scammermike/Downloads/computexchange/.claude/worktrees/model-refinement`
Builder: `render/build_scene.py` · Gate: `render/rig_patches.py --offset -12` · Blender:
`/Applications/Blender.app/Contents/MacOS/Blender -b -P render/build_scene.py -- <args>`
Evidence base: `render/geometry-audit-raw.json` (10-unit pixel-measured audit, 2026-07-05),
arbitrated in `render/GEOMETRY-AUDIT.md`. References: `render/ref/` (see SOURCES.md).

## Part 0 · Mission
Drive every graded aspect of BOTH devices to 10/10 geometric exactness · a machinist shown the
render beside the reference photo finds no shape, proportion, position, radius, or presence error.
Realism follows exactness: the loop is render -> reference-diff -> change -> verify -> re-gate,
repeated in small bounded steps. The current scorecard is in GEOMETRY-AUDIT.md · Studio lags Spark.

## Part 1 · Doctrine (unchanged, senior, non-negotiable)
1. Authority: measurement-with-evidence > this document > any panel/agent opinion > your own eye.
2. The TONE GATE is senior to every geometry move: after every commit, calib-render studio-front +
   spark-q34 (1500px/240s) and run the gate. ALL PASS or the change iterates/reverts. Pins never move.
3. ONE feature per commit. Each commit message names the feature, the reference evidence, and the
   measured before/after.
4. Any overturned pin gets an AUTOPSY line in MEASUREMENTS.md (what the old value was, why it was
   wrong, what measurement replaced it) · precedent: led_x 87.70 (measured through corner
   foreshortening) -> 64.60.
5. FLIP-FLOP GUARD: if a value has already been changed once on visual evidence (weave pitch, foam
   cell scale), it moves again ONLY after a fresh measurement across at least TWO independent
   references, written to MEASUREMENTS.md first.
6. Trademark gate: no Apple logo, no NVIDIA logo, no text · plates/zones stay blank. Not defects.
7. No em/en dashes in anything you write · middot or "to".
8. Foam rebuilds are cached (`render/cache/`) · geometry changes to the foam field change the cache
   key automatically; material-only changes reuse the cache (fast). Budget accordingly.
9. Deliverable discipline: the checkpoint folder (`~/Downloads/cx-oracles-checkpoint-*`) stays at
   16 attachments or fewer · report + per-angle heroes + collages, rebuilt by
   `render/_checkpoint_export.py`.

## Part 2 · The loop (each iteration, ~30-60 min)
1. PICK the top open item from the queue (Part 4) · or the next re-measure task if its target is one.
2. RE-MEASURE: open the named reference image(s), measure the feature as pixel ratios (of body
   width/height/face), convert to mm, write the pin + evidence line to render/MEASUREMENTS.md.
   If the new measurement disagrees with the audit finding, the MEASUREMENT wins · log and move on.
3. IMPLEMENT one bounded change in build_scene.py (dimension, radius, position, count, mask).
4. VERIFY CHEAP: calib render the ONE most relevant angle (1500-1700px / 240s), eyeball against the
   reference crop, re-measure the ratio in the render. Acceptance check from the queue must PASS.
5. GATE: rig_patches ALL PASS (pre-post, tolerances unchanged).
6. COMMIT (one feature, evidence in message). Update the scorecard row in render/GEOMETRY-AUDIT.md.
7. Every ~5 commits OR when a device's open queue empties: full 4K reshoot
   (`render/_photoreal_reshoot.sh`), regate delivered frames, rebuild collages + checkpoint
   (`render/_collage.py`, `render/_checkpoint_export.py`), commit deliverables.
8. OPTIONAL secondary signal (never the gate): a 5-agent cold panel via
   `render/_panel_workflow.js` + `_panel_build.py` + `_panel_agg.py`. Interpret with the known
   clean-product FALSE-TELL (real Apple press photos score 3-5/5 render · see PHOTOREAL-SHIFT-REPORT
   sections 10b-10e). Panel variance is high; two consecutive readings before believing any delta.
9. If a reference angle is missing for a queued feature (e.g. Studio rear port macro), do a targeted
   web image search, save to render/ref/<device>/ with a source line in SOURCES.md, then measure.

## Part 3 · Current scorecard
See `render/GEOMETRY-AUDIT.md` (2026-07-05 baseline, updated per commit). Summary: Studio front
aspects 4-8 (intake band + apertures + corner blend just improved, verify in the reshoot), Spark
side/top 2-7 (ribbing fixed; dish/radius/slot open), both rears 0 (scope-gated), pair scale flagged.

## Part 4 · Ranked queue (top first · each: evidence -> change -> acceptance)

### STUDIO (the laggard · work these first)
S1. Corner-to-fillet G2 blend. Evidence: apple_front/wikimedia silhouettes show a continuous curve
    where the 31.4mm plan corner turns through the top edge; ours still faintly facets after the
    seg bump. Change: bevel profile/segments or a custom blend so no straight segment survives at 4K.
    Accept: zoom the top-left silhouette at 4K · no polyline visible.
S2. USB-C aperture width RE-MEASURE (flip-flop guard applies · pin 2.62 vs audit 4.2:1 aspect).
    Measure on apple_front AND wikimedia_front AND mac-studio-front-ref. If ~2.0-2.2mm confirmed,
    change usbc_w + cutter radius; keep height 8.47 and positions. Accept: rendered aspect within
    5% of the measured ratio.
S3. Base reveal / undercut profile. Evidence: crisp ~1.5mm dark reveal line under the band in
    apple_front; 3q unit S4 wrong_shape on the wall-to-floor termination. Change: model the inset
    base puck so a constant-height dark reveal reads head-on. Accept: reveal line visible and
    near-constant across the front at 4K, matches ref ratio.
S4. Intake band VERIFY (fixed tonight, zlift bug): in the fresh 4K front, band height ratio should
    read ~0.13 of total height with 9-10 hole rows and holes crossing the fillet tangent (no blank
    lip, no hard seam). If not, adjust threshold/texture origin.
S5. Front W:H ortho check: render an orthographic-ish front (long lens) and confirm face H/W within
    1% of 95/197. Closes the dimensional unit's S5 either way (expected: false alarm).
S6. Port group x-positions: confirm P1 center ~32mm from left silhouette edge in the ortho frame
    (audit says within noise · verify once, then close).
SCOPE-GATED: Studio REAR build (full spec in geometry-audit-raw.json: exhaust field, port row
    order, cloverleaf inlet, 4x vertical TB, 2x USB-A, HDMI, jack, power button) · build only if the
    owner adds rear angles to the deliverables.

### SPARK
K1. Pill slot cross-section: real slot is a smooth concave SCOOP (finger dish), ours is a flat-floor
    tub. Evidence: sth_front-1/2 + cl_front-foam highlight gradients. Change: replace tub floor with
    a swept concave profile inside the plateau slot. Accept: single smooth highlight gradient in the
    dish at 4K, no flat floor plane, relief still strictly concave (commit-1 rule).
K2. Front layout cross-reference RE-MEASURE (conflicts audit-vs-L15): end tab width, plateau size /
    position / proud-vs-flush relief, foam margins above/below plateaus. Measure across sth_front-1,
    sth_front-2, cl_front-foam, nv_hero_3q TOGETHER (they disagree with each other · reconcile by
    weighting the cleanest orthographic view). Write pins, then apply at most ONE reconciled change
    set. Accept: rendered ratios within 5% of the reconciled pins.
K3. Top vent dish transition (audit S5): border descends into the panel via a ~15mm tangent dish,
    depth ~1.2-1.5mm, not a stepped wall. Change: re-profile the recess cutter. Accept: side-on
    (cl_side-profile angle) shows the halo band gradient, no step shadow line.
K4. Recess corner radius ~19mm squircle (audit: ours ~3.5% of panel width, ref ~17%). Accept: ratio
    within 10% at 4K top view.
K5. The missing 133 x 3mm stadium slot near the foam-side top edge (centerline ~15mm from edge).
    Cross-check first (side unit saw a slot-like feature · reconcile exists-vs-shape). Accept:
    slot present, dimensions within 10%.
K6. Weave pitch RE-MEASURE (flip-flop guard · history: 4.6mm -> 1.5mm on L17's read; audit now says
    ~5mm satin bands). Two references minimum (cl_side-profile + nv_hero_3q crop + any new top
    photo found via web search). Whatever the number, implement as SOFT satin bands, not corduroy
    threads, gated to the flat panel only. Accept: pitch ratio within 10% of the reconciled pin.
K7. Through-slot pills + diamond mesh grille behind the plates (audit S4: the real pills are
    open handles/intakes with a mesh screen behind). Structural: cut through, add mesh plane.
    Accept: darkness through the slot with a faint regular mesh glint, matches cl_front-intake.
K8. Side plinth foot (audit S4 missing: recessed dark plinth under the body). Measure on
    sth_side-1-vertical/cl_side-matte, add. Accept: thin dark under-shadow band reads side-on.
SCOPE-GATED: Spark REAR + BOTTOM builds (full port/panel specs cataloged in geometry-audit-raw.json).

### PAIR
P1. Relative-scale verify: measure Spark:Studio width ratio in the pair frame (expect 0.761
    corrected for the two yaws/depths). If off, fix PAIR_SPAN/camera math, not device dims.
    Accept: measured ratio within 3% of projected expectation.

## Part 5 · Reporting
Maintain render/GEOMETRY-AUDIT.md scores per commit. At every full reshoot, refresh the checkpoint
folder (16 attachments max) and sync PHOTOREAL-SHIFT-REPORT.md's deliverables section. When all
non-scope-gated aspects reach 9+, write the closing summary and STOP · present, owner closes.
