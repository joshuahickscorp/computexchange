# render/RACK-LOOP.md · the goal-iterative loop (run this on Opus)

Purpose: everything taste-bearing is DECIDED and pinned · archetype (D-ARCH.md), fill map +
all dimensions (MEASUREMENTS.md RACK section), technique class (real cut holes · bake-off
verdict in RACK-NOTES.md), rig values + tone pins + the numeric oracle (rack_verify.py).
What remains is MECHANICAL ITERATION: build each part to its goal state, judged by numbers
and fixed acceptance images, one change per iteration. This file is the protocol. Worktree:
worktree-rack-oracle. Builder: render/build_rack.py. Dash gate: middot only.

## INVARIANTS · never touch these (escalate instead)

1. **Pins never move to make a render pass.** The PINS/TOL in rack_verify.py and every value
   in MEASUREMENTS.md are frozen. If a pin seems wrong, STOP and present the evidence · the
   driver re-measures. (The desktops' finding-3 laundering autopsy is the law here.)
2. **The desktop masters are CLOSED.** build_scene.py, MEASUREMENTS desktop sections, the
   desktop portraits: read-only, always.
3. **Technique class is locked**: mesh perforation = REAL cut holes (cell-boolean + array,
   see _rack_bakeoff.py build_A). Switching class requires a logged bake-off + driver sign-off.
4. **Rig defaults in build_rack.py** (key 460 / rim 300 / fill 175 / expo -0.70 / void 0.006)
   and O_rack = 0.0 in rack_verify.py: changing ANY is a LIGHTING-class commit that re-runs
   rack_verify on EVERY existing shot and re-checks the desktop pins if the shared trio rig
   is involved.
5. **U-arithmetic places everything.** No eyeballed positions. u_z(n) + the fill map.
6. **Git discipline**: one change class per commit (REMEASURE / GEOMETRY / MATERIAL /
   LIGHTING / CAMERA / RENDER / DOCS), message states the class, no AI attribution ever.

## THE LOOP · one iteration

1. Pick the top unchecked box in the WORK QUEUE below. Declare the change class.
2. Make ONE bounded change in build_rack.py (or a new part function).
3. Render the part's proof shot(s):
   `/Applications/Blender.app/Contents/MacOS/Blender -b -P render/build_rack.py -- --shot <s> --preview`
4. Judge: `python3 render/rack_verify.py <render.png> --shot <s>` (exit 0 required) AND
   Read the image · compare against the part's ACCEPTANCE list + its reference photo.
5. Log one entry in RACK-NOTES.md (template at bottom). Commit if the gate is green.
6. A part is DONE when every acceptance box is checked; freeze it (comment "FROZEN" on its
   builder function) and move on. Three consecutive failed iterations on the same box ->
   STOP, write up the evidence, escalate to the driver.

## WORK QUEUE (in order · check boxes off in this file as they land)

### Part 1 · RM44 node face (gate 5b) · reference node/rm44_front_A.jpg
- [ ] body 440 x 176 x 468mm + ears to 482.6mm (EIA flange, thumbscrew bosses) · GEOMETRY
- [ ] mesh door: measured lattice P=2.87 R=2.59 shrink=0.24 THICK=1.2, cell-boolean+array
      method from _rack_bakeoff.build_A, door border frame ~8mm, cached like foam3d · GEOMETRY
- [ ] center lock cylinder ~9mm dia at face-center-x, ~14mm below face top (APPROX row ·
      re-measure against the photo when the door frame exists) + recessed badge zone · GEOMETRY
- [ ] top lid seam + bottom rail seam (from the photo's horizontal seams) · GEOMETRY
- [ ] interior: inward-normal box (albedo 0.032 · NEVER 0), 3x 120mm fan discs+hubs ~20mm
      behind the door, blades never crossing the door plane · GEOMETRY
- [ ] interior fill light inside the node: holes read as openings, exterior patches stay
      green (run rack_verify before/after) · LIGHTING
- [ ] node solo turnaround (front/q34/side, flat rig like turnaround_sheet) · RENDER
- ACCEPTANCE: (a) raking detail tile matches bake-A-raking's opening-read or better ·
  (b) rack_verify powder_black on the node front PASS · (c) mesh pitch on the render
  measures 2.87/2.59 +-5% via the FFT autocorr (scratchpad rm44 script method) ·
  (d) grazing shot holds the perforation read (compare bake-A-graze).

### Part 2 · CRS354 switch face · reference switch/crs354_sth_front.jpg + CAD pdf
- [ ] chassis 443 x 44.3 x 297mm WHITE powder + ears (+7mm projection) · GEOMETRY
- [ ] port block: ONE recessed RJ45 cavity (dark wall + AO + lit lower contact tab · the
      desktop USB-C treatment) instanced to the 2x24 grid with the photo's group gaps ·
      GEOMETRY
- [ ] 4x SFP+ + 2x QSFP+ cages right side (dark nickel, recessed) · GEOMETRY
- [ ] per-port LED dots row (emission OFF here · states set at assembly variance) · GEOMETRY
- ACCEPTANCE: switch_white patch PASS (add the shot box to rack_verify SHOTS first) ·
  port cavities read recessed at detail distance, never flat black · silhouette vs CAD
  dims +-1%.

### Part 3 · UPS face (2U) · reference ups/smt1500rm2u_amazon_cgi.jpg (geometry ONLY) 
- [ ] chassis 86 x 432 x 477mm black + ears · GEOMETRY
- [ ] face: recessed LCD window (emissive, dim · the ALIVE departure), button cluster,
      vent slot bank, badge blank · GEOMETRY/MATERIAL
- ACCEPTANCE: ups_black patch PASS (tol 6, low-conf pin flagged) · LCD emission does not
  clip (rack_verify clip gate) and reads at portrait distance.

### Part 4 · blanking panel + finger duct (1U each) · refs accessories/*
- [ ] blanking 483 x 45 flat + edge relief · duct: finger row + cover 43.7 x 482.6 x 67.7 ·
      GEOMETRY
- ACCEPTANCE: reads correct in a 3-unit stack test render with the U-gap rule (44.45n-0.79).

### Part 5 · assembly (gate 6) · the fill map in MEASUREMENTS.md
- [ ] place ALL units by u_z() per the fill map, linked duplicates for the 3 nodes · GEOMETRY
- [ ] variance system: per-instance seed drives LED palette {off,green,amber}, sub-degree
      seating jitter (<=0.4deg), micro-roughness/dust variation · node A on / B off / C on ·
      switch ports sparse-random · MATERIAL
- [ ] cage nuts at OCCUPIED holes only + a few spares (zinc, from the macro refs) · GEOMETRY
- [ ] restrained cabling: duct-to-switch patch cords only where the open front shows them ·
      GEOMETRY
- ACCEPTANCE: full-rack front + q34 · rack_verify ALL PASS on both · no two adjacent units
  read identical at portrait distance (Read the render and check explicitly) · empty rails
  U22-42 show square holes crisply.

### Part 6 · depth gate (gate 7) + materials (gate 8) + photoreal (gate 9) + portraits (10)
- [ ] raking-light detail of node face + one empty bay: openings-with-interior · the formal
      Problem-2 acceptance render, committed as evidence · RENDER
- [ ] tone table: every patch on front + q34 green at O_rack=0 · re-derive O_rack on the
      node's broad front face FIRST (one commit · see rack_verify autopsy note) · MATERIAL
- [ ] photoreal ledger rows T-RACK-1 (array uniformity) + T-RACK-2 (dead-black depth) +
      inherited taxonomy · then blind 5-agent panel loops per render/panel/PANEL-LOG.md
      protocol (pool = renders + homelab-gestalt/ + OTHER-vendor rack photos) · driver
      reads verdicts against control calibration
- [ ] portraits: front, q34, node detail, empty-bay detail + THE SCALE TRIO (Mac Studio +
      Spark + rack, one rig, true scale · per-object-class tone offsets) · RENDER

## ESCALATE TO THE DRIVER (stop the loop, write up, wait)

- Any pin or measured row looks wrong · any acceptance image needs a judgment call the
  checklist doesn't answer · technique class feels exhausted (3 failed iterations) · any rig
  change beyond the invariant values · anything requiring the desktops' files · the panel
  gate (driver runs it) · the trio composition (driver taste).

## NOTES ENTRY TEMPLATE

    ## <part> · iteration N · class <CLASS>
    Change: <one sentence>. Render: <file>. Verify: <PASS/FAIL + numbers>.
    Eyeball: <one sentence vs the reference>. Next: <the single next fix>.
