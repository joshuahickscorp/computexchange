# render/RACK-LOOP.md · v2 · the goal-iterative loop (run this on Opus)

Purpose: everything taste-bearing is DECIDED: archetype (D-ARCH.md), dimensions + fill map
(MEASUREMENTS.md RACK section · fill map v2 per RACK-DETAIL-AUDIT.md section 3), technique class
(real cut holes, bake-off in RACK-NOTES.md), rig defaults + tone pins + numeric oracle
(rack_verify.py), and the full detail ladders (RACK-DETAIL-AUDIT.md section 2 · read it FIRST,
it is the standard of done). What remains is MECHANICAL ITERATION, one bounded change per
iteration, judged by numbers and fixed acceptance images. Worktree: worktree-rack-oracle.
Builder: render/build_rack.py. Dash gate: middot only. Target: Apple/NVIDIA launch grade ·
an owner of this hardware finds nothing missing at any deliverable distance.

## INVARIANTS · never touch (escalate instead)
1. Pins never move to make a render pass. Evidence -> driver re-measures. (Finding-3 law.)
2. Desktop masters CLOSED (build_scene.py, desktop MEASUREMENTS rows, desktop portraits).
3. Technique class LOCKED: door mesh = REAL cut holes (cell boolean + array, _rack_bakeoff
   build_A). The fine FILTER layer behind it is a MAPPED micro-perf plane (ports-vs-maps
   distance rule · legit at 4mm setback). Class switches need a logged bake-off + sign-off.
4. Rig defaults (key 460 / rim 300 / fill 175 / expo -0.70 / void 0.006) + O_rack=0.0: any
   change is a LIGHTING-class commit that re-runs rack_verify on EVERY existing shot; if the
   shared trio rig is touched, the DESKTOP pins get re-verified too (O_desktop=-12 stands).
5. U-arithmetic places everything: u_z(n), fill map, 44.45n-0.79 panel rule. No eyeballing.
6. Git: one change class per commit (REMEASURE/GEOMETRY/MATERIAL/LIGHTING/CAMERA/RENDER/DOCS),
   class named in the message, no AI attribution ever.

## PORTED DESKTOP LAWS (new in v2 · learned the hard way on Studio/Spark)
7. FLIP-FLOP GUARD: any value already changed once on visual evidence moves again ONLY after a
   fresh measurement across TWO independent references, written to MEASUREMENTS.md first.
8. AUTOPSY PROTOCOL: any overturned pin/value gets an autopsy line in MEASUREMENTS.md · old
   value, why it was wrong (mechanism, not vibes), the replacing measurement. Precedents:
   led_x corner-arc autopsy; the gate-5a +6 offset strike.
9. CAMERA PHYSICS: DOF per shot class · detail f5.6 (TRUE macro camera aimed at the feature,
   NEVER a crop of a wide frame · the L18 lesson) · pair/trio f11 · heroes f16. Tone patches
   must stay sharp at gate time.
10. POST CHAIN: the desktop post (roll 0.3deg · CA +/-0.18% · bloom thr 0.88 · vignette ~0.80
    corner · grain 0.011) applies to DELIVERABLE frames only, AFTER the gate · the gate is
    always PRE-post. Copy post_chain.py usage from the desktop worktree; do not re-tune.
11. FIREFLY CLAMP: cycles indirect clamp 8.0 on portrait shots (desktop L13 lesson) · dark
    interiors + small bright LEDs is exactly the firefly recipe.
12. PANEL CALIBRATION: any blind panel run includes REAL photos of OTHER vendors' rack gear as
    controls; a clean-product FALSE-TELL is proven for desktops (real Apple press photos score
    3-5/5 render) · racks are handled hardware so the bias differs, MEASURE it before believing
    any verdict; two consecutive panel readings before acting on any delta.
13. TOOLING LIVES IN THE REPO: the mesh-pitch FFT check becomes render/rack_measure.py (port
    from scratchpad in wave 1) · acceptance numbers must be reproducible forever.

## THE LOOP · one iteration
1. Top unchecked box below · declare change class.
2. ONE bounded change in build_rack.py (or the named new file).
3. Render the part proof: `/Applications/Blender.app/Contents/MacOS/Blender -b -P
   render/build_rack.py -- --shot <s> --preview` (drop --preview for acceptance renders).
4. Judge: `python3 render/rack_verify.py <png> --shot <s>` exit 0 AND Read the render vs the
   part's reference + its ladder in RACK-DETAIL-AUDIT.md section 2.
5. One entry in RACK-NOTES.md (template at EOF). Commit if green.
6. Part DONE when every box checked -> mark FROZEN on its builder function. Three consecutive
   fails on one box -> STOP, write evidence, escalate.

## WORK QUEUE v2 (in order · audit evidence tags [rack-audit-raw.json])

### Wave 0 · FRAME CORRECTIONS (the built part is wrong before anything stacks on it)
- [x] W0.1 GEOMETRY · outer width 760 -> 600mm (DONE 608mm, rails held, gate PASS): move walls/posts inboard ~80mm/side, FREEZE
      rail x (rails measured CORRECT at c-c 464.5). Accept: rail-derived px/mm scale gives
      outer width 600 +/- 2% on a fresh front render.
- [~] W0.2 GEOMETRY · post faces to ~45mm DONE (gate PASS) · hinge/keeper DEFERRED to 3/4 hardware box
      Accept: post face ratio ~0.075 of width; silhouette no longer dead-straight.
- [x] W0.3 GEOMETRY (DONE, gate PASS) · top front band to ~45mm; base band floats: 4 leveling feet (pad ~45mm,
      30mm floor gap) + twin casters inboard. Accept: light gap under base reads; feet at
      corner footprints.
- [ ] W0.4 GEOMETRY · rear rail pair (holed, same pattern/c-c) at rear depth; corner gusset
      castle plates; brush strips inboard of posts. Accept: rear rails read between units
      from dead front.
- [ ] W0.5 GEOMETRY · U-tick strip on rail flange (blank ticks, 1/U) + hole size verified
      9.5mm in source. Accept: ticks resolve at 4K front.
- [x] W0.6 MATERIAL (DONE, gate dE2.42 PASS, tone re-verified L18) · orange-peel powder micro-texture (NEW target · not bead-blast, not
      anodize · scale from enclosure-photos macro) + RE-MEASURE the frame render tone with
      rack_verify (my eye says grey, committed number says L22 · one of them autopsies).
      Accept: rack_verify powder_black PASS on the corrected frame.

### Wave 1 · RM44 node face (hero · ladder in DETAIL-AUDIT sec 2)
- [ ] 1.1 GEOMETRY · body 440x176x468 + ears: folded flange to 482.6, 2 knurled thumbscrews
      each, fold radius per rm44_q34. Accept: silhouette vs photo +/-1.5%.
- [ ] 1.2 GEOMETRY · door: real tri punches P2.87/R2.59 (rounded corners, alternating
      orientation), border FADE ROWS (clipped partial triangles at frame edge), border ~8mm,
      cached like foam3d. Accept: FFT pitch on render 2.87/2.59 +/-5% (rack_measure.py) ·
      fade rows visible in the macro.
- [ ] 1.3 GEOMETRY · FILTER LAYER: mapped micro-perf plane 4mm behind door (fine hex ~0.9mm),
      then fan wall (3x120mm rings+hubs) behind it, interior albedo 0.032 never 0.
      Accept: raking + grazing tiles read door->filter->dark (beats bake-A) · this is the
      Problem-2 acceptance, committed as evidence.
- [ ] 1.4 GEOMETRY · keystone badge plate (proud, chevron bottom, BLANK) + bail-handle lock at
      photo-measured y + top lip seam + bottom sill with 2 corner screws + 4 witness dots.
      Accept: feature positions vs rm44_front_A +/-2% of face width.
- [ ] 1.5 LIGHTING · interior fill so holes read as openings; exterior patches stay green
      (rack_verify before/after). Accept: both.
- [ ] 1.6 RENDER · node solo turnaround + TRUE-MACRO detail (law 9). Accept: rack_verify PASS
      + pitch check on the macro.

### Wave 2 · CRS354 switch face
- [ ] 2.1 GEOMETRY · chassis 443x44.3x297 WHITE + ears (+7mm) + faceplate seam. Accept: CAD
      dims +/-1%.
- [ ] 2.2 GEOMETRY · port grid: ONE recessed RJ45 cavity (dark wall + AO + lit lower contact ·
      desktop USB-C law) instanced as 4 GANGS of 2x6 · TOP row latch-up, BOTTOM row MIRRORED
      latch-down · gang gaps > port gaps · per-port LED pipes (above top row, below bottom).
      Accept: gang structure + mirror flip read at detail distance; never flat black.
- [ ] 2.3 GEOMETRY · gang window recess + bezel (color VERIFY-THEN-PIN vs 2nd ref) · SFP+ 2x2 +
      QSFP+ 2x1 cages with lips + bale latches · console/MGMT stack far right · status LEDs +
      reset pinhole. Accept: every zone present at correct x-ratio +/-2%.
- [ ] 2.4 MATERIAL · switch_white patch into rack_verify SHOTS, PASS. Accept: exit 0.

### Wave 3 · UPS face (2U)
- [ ] 3.1 GEOMETRY · 432x89 bezel · grille-LEFT / control-RIGHT split · louvered grille
      (photo-counted pitch, recessed) · scallop pillars flanking recessed control plate.
      Accept: massing split ratio vs photo +/-3% (photo outranks CGI on every disagreement).
- [ ] 3.2 GEOMETRY/MATERIAL · power bar + 2x2 buttons + LCD window (dim emissive · the ALIVE
      departure · clip gate must hold) + ears + blank badge. Accept: ups_black PASS · no clip.

### Wave 4 · accessories
- [ ] 4.1 GEOMETRY · blanking panel: face + edge returns + end relief notches + mounting slots.
      Accept: 3-stack test with 44.45n-0.79 gaps reads.
- [ ] 4.2 GEOMETRY · duct: photo-counted fingers/pitch/profile + cover. Accept: same stack.
- [ ] 4.3 GEOMETRY · cage nut CANONICAL: square cage + collar + castellated spring wings, zinc
      + M6 screws (two head styles). Placed at OCCUPIED holes + 2-3 spares ONLY. Accept: macro
      matches cagenut_single_macro.
- [ ] 4.4 GEOMETRY · shelf + generic blank mini-PC (gestalt shelf rule · scope note in commit).

### Wave 5 · assembly (fill map v2 · REMEASURE commit adopts it into MEASUREMENTS.md first)
- [ ] 5.1 REMEASURE · write fill map v2 (DETAIL-AUDIT sec 3: bottom-heavy, deliberate voids,
      network band TOP: duct U37 + switch U38 + shelf U40, honest U21-36 void run).
- [ ] 5.2 GEOMETRY · place all units by u_z() · linked dupes for nodes. Accept: front + q34,
      rack_verify ALL PASS both.
- [ ] 5.3 MATERIAL · variance: LED budget ~60% lit mixed green/amber 2-3 bright, node states
      A-on/B-off/C-on, seating jitter <=0.4deg, per-instance roughness/dust (handled-hardware
      law · every knob cites a gestalt photo). Accept: no two adjacent units identical + LED
      field reads alive-not-decorated.
- [ ] 5.4 GEOMETRY · cabling: patch catenary arcs duct->switch->near columns (gestalt rule),
      velcro loops, restrained elsewhere. Accept: arcs read as slack physics, not splines.
- [ ] 5.5 RENDER · full-rack front + q34 + empty-bay TRUE-MACRO + node TRUE-MACRO.

### Wave 6 · photoreal + portraits (driver-shared)
- [ ] 6.1 post chain onto deliverables (law 10) + firefly clamp verify (law 11).
- [ ] 6.2 photoreal ledger rows T-RACK-1 (array uniformity) T-RACK-2 (dead-black depth)
      T-RACK-3 (filter-layer read) · panel per law 12 (driver reads verdicts).
- [ ] 6.3 THE SCALE TRIO: Studio + Spark + rack, one rig, per-class offsets (law 4), true
      scale by U-module arithmetic. Driver owns composition. Accept: both desktop pins AND
      rack pins green in the same frame set.

## ESCALATE (stop, write up, wait)
Pin disputes · acceptance judgment calls the ladder does not answer · 3 fails on one box ·
any rig change beyond invariants · anything touching desktop files · panel verdict reading ·
trio composition · any NEW reference need (driver hunts or approves web fetch).

## NOTES TEMPLATE
    ## <part> · iteration N · class <CLASS>
    Change: <one sentence>. Render: <file>. Verify: <PASS/FAIL + numbers>.
    Eyeball: <one sentence vs the reference>. Next: <single next fix>.
