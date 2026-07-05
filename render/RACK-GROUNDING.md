# render/RACK-GROUNDING.md · the third oracle (NVIDIA GPU rack) · grounding note

Date: 2026-07-04. Scope: Part 0 of the rack shift · what the desktop-oracle apparatus already
gives us unchanged, and what is genuinely new for a rack. Written after reading build_scene.py,
MEASUREMENTS.md, NOTES.md (all waves), PHOTOREAL-LEDGER.md, PHOTOREAL-SHIFT-REPORT.md,
CONCERNS.md, panel/PANEL-LOG.md (16 loops), measure.py, clipcheck.py, rig_patches.py,
settle_sheet.py, overlay.py, ref/SOURCES.md. Dash gate: middot only. This shift lives in its own
worktree (worktree-rack-oracle, branched from worktree-model-refinement); the desktop masters
are CLOSED and untouchable.

## Authority hierarchy (inherited verbatim)

measurement-with-evidence > grader rulings > panel blind verdicts > own eye. The in-rig tone
gate is SENIOR to every photoreal move; post rescues nothing; FALSE-TELLs (true of the reference
device, or deliberate product choices) are logged, not chased. I never grade my own work as
done · I present sheets + numbers and the grader closes.

## REUSED UNCHANGED · tools we already own

1. **measure.py scale method** · border-flood-fill silhouette, Kasa circle fit with trimmed
   refit, connected-component blobs for features, sRGB->Lab patches, evidence crops under
   measure_evidence/. One known real dimension anchors each image; everything else follows from
   pixel scale. For the rack the anchor is even stronger (see NEW · the U-module).
2. **MEASUREMENTS.md discipline** · same columns (parameter, value, unit, conf, source image,
   evidence crop, note), spec-anchor rule, superseded rows struck with a written AUTOPSY naming
   what the old measurement actually read. The wave-3 foam-span double autopsy and the
   laundered-gate finding are the calibration for how honest this table must stay.
3. **The FROZEN PORTRAIT RIG + in-rig tone gate** · PORTRAIT_RIG (key 64/2.2, rim 16, fill
   28/2.7, expo -0.70, void-black world 0.006, glossy-only reflection world) with the global
   offset O = -12 L and rig_patches.py dE76 gating (tol 4, deep-texture 6). New rack materials
   get PINNED patches from one clean source each, spread recorded, gated in this rig. Any rig
   change (the interior fill, Part 5 Problem 2) is its own LIGHTING-class commit with ALL
   patches re-verified · alu 1.41 and the Spark pins must stay green.
4. **clipcheck.py** · device pixels >= 0.98 in any channel must stay under 1% or the render
   fails outright. Every rack frame ships clip-audited GREEN.
5. **Change-class commit discipline** · one class per commit (REMEASURE / GEOMETRY / MATERIAL /
   LIGHTING / CAMERA / POST / RENDER), declared before the change, tone gate green before every
   commit, evidence crop per claim.
6. **Settlement-sheet + turnaround + overlay formats** · settle_sheet.py-style render-vs-
   reference gates, turnaround_sheet (wireframe+shaded 4-angle Gate-1 proof), overlay.py 50%
   front overlays where a clean orthographic reference exists, contact sheets, verify_sheet.
7. **The photoreal tell taxonomy + ledger** · PHOTOREAL-LEDGER.md schema (OPEN /
   FIXED-UNCONFIRMED / CLOSED / REOPENED, autopsy on reopen). Standing lessons carried over as
   prior knowledge, not to re-learn: T7 bevel-with-noise-modulated radius on every metal edge
   (a uniform bevel highlight is itself a tell); T8 anodize/batch mottle; L5 anti-drift (added
   imperfection is the tell on clean product surfaces · real racks are the exception, they DO
   wear · see NEW); T3 per-shot physical DOF with the f-stop table actually applied (L18); T4
   one post chain, gate pre-post; L13 indirect clamp for cavity fireflies (a rack is ALL
   cavities · this lands on day one); the FOAM lesson that displaced heightfields cannot fake
   through-geometry depth (grader line 149 technique-class switch) · for the rack this maps to
   perforation and recess treatment.
8. **The blind five-agent camera-test panel** · _panel_build/_panel_agg protocol: 5 cold Sonnet
   agents, distinct forensic lenses, gate frames mixed with real controls under neutral names,
   PHOTOGRAPH/CG_RENDER + tells, >= 2-agent flags fail, calibrated against the controls'
   render-call rate, two consecutive clean = stop. Known instrument limits (loops 12 to 16):
   clean-product FALSE-TELL bias (a real Apple press photo drew 5/5 render unanimous) and high
   draw variance · verdicts are read against control calibration, never raw. The rack pool adds
   real datacenter/rack photography of OTHER vendors so device recognition cannot leak.
9. **Builder idioms** · rounded_box / stadium / cutter_box + apply_boolean + assign_interior
   (interior faces re-materialed from cutter bounding boxes), cavity treatment (wave 2: dark
   GREY walls 0.06 + AO, never albedo 0, a lighter internal element as the depth cue), the
   measured-constants dict pattern (every value traces to a MEASUREMENTS row), foam3d caching
   to render/cache/ for expensive geometry, --zaudit / --raking acceptance tooling.
10. **Packaging discipline** · one consolidated report, collaged evidence (contact sheets, not
    loose frames), under the attachment budget; export bundle for the grader.

## GENUINELY NEW · what a rack demands that the desktops never did

1. **Assembly, not shell.** Both desktops were single closed volumes; realism lived in material
   and edge. A rack is a FRAME holding many repeated units. The dominant realism problem moves
   from shader to STRUCTURE: uprights with the square-hole cage-nut pattern, rails, per-unit
   air gaps, a fill map. Modeling is hierarchical and INSTANCED (canonical part built once,
   placed many times, varied per instance) · new builder architecture, and the key to keeping
   the eventual glb small.
2. **The U-module anchor.** The desktops each anchored on one measured dimension. The rack has
   a repeating STANDARD module: 1U = 44.45mm exactly, EIA-310 rail opening 450.85mm, panel
   width 482.6mm. Vertical placement of every unit is U-ARITHMETIC, not pixel measurement ·
   MEASUREMENTS.md gets the U-module as the anchor row and a FILL MAP (what occupies each U)
   as a first-class measured artifact.
3. **Repetition variance (new first-class tell).** A stack of byte-identical units is a louder
   CGI signal than any single wrong material. Per-instance variance must be SYSTEMATIC:
   randomized LED states from a small palette, sub-degree seating jitter, per-instance
   micro-material variation (the anodize-mottle lesson applied ACROSS instances), non-repeating
   fill-map arrangement. Acceptance: no two adjacent units read identical at portrait distance.
   Goes into the ledger as a first-class row. Design of this system is an Opus-subagent task;
   the call on how far to push it stays with the driver (the L5 anti-drift lesson cuts BOTH
   ways here: racks are handled hardware, some wear is honest · but every variance knob must
   trace to something reference-visible, or the panel names it).
4. **Depth into darkness (new first-class tell, the hardest problem).** Perforated node faces,
   connector cages, drive bays, inter-unit gaps · every one must read as an opening with
   interior, not a black decal. This is the Spark-pill/wave-2 cavity lesson at 10x the count,
   plus a new lighting problem: the frozen rig has NO interior fill, and void-black recesses
   will crush (studio_intake read L4 in-rig · acceptable for one shadowed base band, fatal for
   a rack's entire face). Candidate techniques (bake-off, at least two, raking-light detail is
   the acceptance): real modeled recesses with interior walls at measured depth (interior
   albedo never 0), interior AO + a dedicated LOW INTERIOR FILL light that must not push any
   exterior tone patch out of tolerance, perforation as real cut geometry vs normal+opacity
   decided by camera distance (the ports-vs-maps rule). Owner: Opus subagent bake-off, driver
   decides.
5. **New material targets.** Rack steel is matte powder-coat (fine texture, NOT bead-blast alu,
   NOT anodize champagne) plus vendor accent faces, connector metals, rail zinc. Each gets a
   measured Lab pin from one clean source (spread recorded) and joins the tone-gate patch set.
6. **Archetype decision before anything (D-ARCH).** "GPU rack" is not one thing; the wrong
   pick is the corner-radius mistake at project scale. Options A (populated air-cooled node
   rack) / B (rack-scale flagship, NVL-class) / C (scale-drama prop) go to the grader with a
   reference-sufficiency assessment. The Spark precedent governs option B: if the newest
   platform is press-render-only, triangulate from multiple three-quarter shots, record the
   spread, never fabricate · and accept a declared reproduction ceiling.
7. **Cabling doctrine.** Front-facing-doctrine logic extends: minimal suggested bundles, only
   where the locked orbit sees them. Bad cables read worse than none.
8. **The scale trio.** The strategic payoff portrait: Mac Studio + DGX Spark + rack in one
   frame on the frozen rig at true relative scale (the U-module and the desktops' measured
   dimensions make the scale exact). The desktops' builders are consumed AS-IS · not one
   vertex or constant of theirs changes in this worktree.

## Sequence + gates (Part 7, restated as the working checklist)

1. this note · 2. D-ARCH grader sign-off · 3. reference hunt + coverage matrix (three named
failed avenues per declared gap) · 4. measurement layer, U-anchored, fill map · 5. canonical
parts, per-part look-fix loops · 6. assembly by U-arithmetic + repetition variance ·
7. depth-into-darkness raking gate · 8. materials tone-pinned, interior fill, exterior gate
green · 9. photoreal loop to two clean panels (rack tells added) · 10. portraits + scale trio ·
stop. I present; the grader closes.
