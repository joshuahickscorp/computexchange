# CX ORACLES · MASTER LOOP · one document, three objects, 1:1 or nothing

THE GOAL: the final renders of the Mac Studio, the DGX Spark, and the 42U rack must look
MAJESTIC · indistinguishable from photographs of the real objects, as if Apple and NVIDIA shot
them for a launch page. Not "close" · one-to-one: geometry, color, micro-detail, light behavior.
It does not matter how many iterations this takes. The executor (Opus or Sonnet) grinds this
document box by box; taste and dispute go to the driver (Fable) or the owner. This document is
the ONLY loop file · it supersedes CX-ORACLES-GEOMETRY-TEN.md and subsumes RACK-LOOP.md (the
rack file remains as the detailed rack reference; its queue is mirrored here and state syncs
BOTH ways · check a box here, check it there).

## PART 1 · THE MAP (where everything lives)

| thing | path |
|---|---|
| desktops worktree (Studio + Spark, branch worktree-model-refinement) | `/Users/scammermike/Downloads/computexchange/.claude/worktrees/model-refinement` |
| rack worktree (branch worktree-rack-oracle) | `/Users/scammermike/Downloads/computexchange/.claude/worktrees/rack-oracle` |
| desktop builder / tone gate | `render/build_scene.py` · `python3 render/rig_patches.py --offset -12` (O_desktop = -12, FROZEN) |
| rack builder / oracle | `render/build_rack.py` · `python3 render/rack_verify.py <png> --shot <s>` (O_rack = 0.0, FROZEN) |
| blender | `/Applications/Blender.app/Contents/MacOS/Blender -b -P <builder> -- <args>` (4.2 LTS, Metal GPU) |
| desktop render args | `--portrait {studio|spark} --shot {front|q34|side|top|detail} --pw <px> --psamples <n> --pdir <dir>` · pair: `--only pair --samples <n> --out <dir>` |
| references | desktops: `render/ref/{mac-studio,dgx-spark}/` · rack: `render/ref/rack/{enclosure,enclosure-photos,node,switch,ups,accessories,homelab-gestalt,standards,context}/` + SOURCES.md |
| audits (the standards of done) | desktops: `render/GEOMETRY-AUDIT.md` + `render/geometry-audit-raw.json` · rack: `render/RACK-DETAIL-AUDIT.md` + `render/rack-audit-raw.json` (the DETAIL LADDERS live here · read before every part) |
| measurements + pins | each worktree's `render/MEASUREMENTS.md` (append-only + autopsies) |
| foam/mesh geometry cache | `render/cache/` (auto-keyed; material-only changes reuse, geometry changes rebuild) |
| post chain | `render/post_chain.py` (roll 0.3deg · CA +/-0.18% · bloom 0.88 · vignette ~0.80 · grain 0.011) |
| panel harness | `render/_panel_workflow.js` + `_panel_build.py` + `_panel_agg.py` + `_panel_save.py` |
| deliverable packer | `render/_checkpoint_export.py` (16-attachment folder in ~/Downloads) + `render/_collage.py` |
| reshoot scripts | `render/_photoreal_reshoot.sh` (both desktops) · `render/_spark_reshoot.sh` · rack: shots via build_rack args |

## PART 2 · THE LAWS (all of them · learned across ~30 commits of scar tissue · never violate)

L1 AUTHORITY: measurement-with-evidence > this document > panel/agent opinion > your own eye.
L2 TONE GATES ARE SENIOR: after EVERY commit, re-render the calib shots and run the gate for the
   worktree you touched (desktops: studio-front + spark-q34 at 1500px/240s then rig_patches;
   rack: rack_verify on the part's shot). ALL PASS or iterate/revert. Pins NEVER move to make a
   render pass. Per-object-class offsets: O_desktop=-12 · O_rack=0.0 · the scale trio runs one
   rig with BOTH gates green in the same frame set.
L3 ONE FEATURE PER COMMIT · class named (REMEASURE/GEOMETRY/MATERIAL/LIGHTING/CAMERA/RENDER/
   DOCS) · measured before/after in the message · no AI attribution in git, ever.
L4 AUTOPSY PROTOCOL: any overturned pin/value gets an autopsy line in MEASUREMENTS.md · old
   value, the MECHANISM of the error, the replacing measurement. (Precedents: Studio led_x was
   measured through corner foreshortening; rack O_rack +6 was a contaminated patch box.)
L5 FLIP-FLOP GUARD: any value already changed once on visual evidence moves again ONLY after a
   fresh measurement across TWO independent references, written to MEASUREMENTS.md FIRST.
   (Live cases: Spark weave pitch · Spark foam cell scale · Studio usbc_w.)
L6 TRADEMARK GATE: no Apple/NVIDIA/SilverStone/MikroTik/APC logos or text, ever · model the
   SHAPED BLANK (keystone plate, badge recess, label zone) exactly, leave it empty.
L7 DASH GATE: no em/en dashes in any file you write · middot or "to".
L8 CAMERA PHYSICS: detail shots = TRUE macro camera at f5.6 aimed at the feature (NEVER a crop
   of a wide frame) · pair/trio f11 · heroes f16 · tone patches sharp at gate time.
L9 POST CHAIN: post_chain.py on DELIVERABLE frames only, AFTER the gate (gate is pre-post,
   post rescues nothing). Do not re-tune post parameters.
L10 FIREFLY CLAMP: cycles indirect clamp stays on for portraits (dark interiors + small bright
   emitters = firefly recipe).
L11 TECHNIQUE LOCKS (switching any requires a logged bake-off + driver sign-off):
    Spark foam = REAL 3D open-cell geometry (voxel-remeshed sphere union, cached) ·
    rack mesh door = REAL cut holes (cell boolean + array) with the fine FILTER LAYER as a
    mapped micro-perf plane ~4mm behind (ports-vs-maps distance rule) ·
    recessed ports/cavities = real geometry with AO walls, never flat black paint.
L12 PANEL CALIBRATION: blind panels are a SECONDARY signal, never the gate. Controls MUST
    include real photos of comparable gear. Known bias: clean product photography scores
    3-5/5 "render" (proven · real Apple press photos flagged unanimously). Two consecutive
    readings before believing any delta. The driver reads verdicts.
L13 U-ARITHMETIC (rack): u_z(n) + fill map v2 places everything · panel heights 44.45n-0.79 ·
    no eyeballed positions. FRONT-FACING DOCTRINE: model what the locked orbit sees.
L14 VERIFY-THEN-PIN: a single agent's single-photo claim (e.g. CRS354 tan bezel) needs a second
    reference before it becomes a pin.
L15 HANDLED-VS-PRISTINE: desktops are pristine launch units (added wear reads FAKE · proven by
    panel autopsy: dimples then marble). The rack is HANDLED hardware · light wear/dust/cable
    slack is honest, but every knob cites a homelab-gestalt photo.
L16 TOOLING IN THE REPO: every acceptance number must be reproducible by a script that lives in
    render/ (port the mesh-pitch FFT to render/rack_measure.py at rack wave 1).
L17 DELIVERABLE DISCIPLINE: refresh the 16-attachment checkpoint folder(s) after every full
    reshoot · per-angle heroes + collages + report, JPG ~2000px, upload-friendly.
L18 CACHE DISCIPLINE: expensive geometry (foam, mesh doors) is cached and keyed on shape params
    + position · material-only iterations are near-free · budget iteration order accordingly.

## PART 3 · THE LOOP (one iteration, any object)

1. PICK the top open box for the object with the LOWEST current grade (rack first until it
   catches up, then Spark polish, then Studio verify items · but never leave a broken state).
2. DECLARE the change class. If the item is tagged RE-MEASURE, do the measurement FIRST:
   open the named reference(s), measure pixel ratios, convert against the anchor dimension,
   write the pin + evidence to MEASUREMENTS.md. Measurement disagrees with the audit? The
   measurement wins (log it).
3. IMPLEMENT one bounded change in the builder.
4. VERIFY CHEAP: render the ONE most relevant angle at calib res (1500-1700px / 240s) ·
   compare against the reference crop · re-measure the ratio in the render · the item's
   ACCEPTANCE check must pass.
5. GATE (L2). 6. COMMIT (L3) + tick the box here (and in RACK-LOOP.md if a rack item) +
   update the audit scorecard row. 7. Every ~5 commits or at wave end: full 4K reshoot for
   that worktree, regate delivered frames, rebuild collages + checkpoint, commit deliverables.
8. THREE consecutive fails on one box -> STOP, write evidence in NOTES, escalate.

ESCALATE (stop, write up, wait): pin disputes · acceptance judgment the ladder cannot answer ·
technique-class exhaustion · rig changes beyond invariants · desktop-file changes from the rack
loop · panel verdict reading · trio composition · new reference needs (driver hunts/approves).

## PART 4 · MAC STUDIO QUEUE (closest to done · verify + polish)

State: ~30 commits deep · tone-locked, layout datums reproduction-grade (audit) · geo pass 1
landed (LED to flat face 64.6 autopsied, perf band 12.4mm zlift-bug fix, SD 2.25 + dark
aperture, corner segs 64/24). Scorecard: render/GEOMETRY-AUDIT.md.

- [ ] ST-V1 VERIFY (INGEST GATE) · fresh 4K front: intake band reads ~0.13 of height with
      9-10 hole rows crossing the fillet tangent, no blank lip, no hard seam · LED at 0.172W
      from right edge on the flat face · SD aperture uniformly dark. If any fails, one
      follow-up commit each. Accept: pixel ratios within 5% of apple_front.
- [ ] ST-V2 VERIFY · corner facet: top-corner silhouette at 4K shows no straight segment.
      If it still facets -> S1 G2 blend work (bevel profile, not just segments).
- [ ] ST1 GEOMETRY · corner-to-fillet G2 blend (if ST-V2 fails) · Accept: no polyline at 4K zoom.
- [ ] ST2 REMEASURE+GEOMETRY · usbc_w flip-flop-guarded re-measure across apple_front +
      wikimedia_front + mac-studio-front-ref (pin 2.62 vs audit ~2.0-2.2/aspect 4.2:1) ·
      change only if 2+ refs agree · Accept: rendered aspect within 5% of the reconciled pin.
- [ ] ST3 GEOMETRY · base reveal: inset base puck producing the crisp ~1.5mm constant dark
      reveal line under the band, head-on · Accept: reveal reads at 4K, ratio matches ref.
- [ ] ST4 RENDER+VERIFY · ortho front (long lens): face H/W within 1% of 95/197 · P1 port
      center ~32mm from left silhouette · closes the dimensional unit either way.
- [ ] ST5 MATERIAL · powder/blast sheen final polish pass ONLY if the trio frame shows a
      mismatch vs the Spark's finish under the shared rig.
- SCOPE-GATED: Studio REAR build (full port-row spec in geometry-audit-raw.json) · only if
  the owner adds rear angles to deliverables.

## PART 5 · DGX SPARK QUEUE (small list · mostly re-measure discipline)

State: the deep-work object · real 3D foam (P1.62 pitch, voxel 0.14, per-strut color), L15
front layout (edge-to-edge foam, satin plateaus, end tabs), L17 fine weave, L18 true macros,
tone-locked. Audit conflicts flagged rather than blindly applied (several audit reads
contradict L15's direct verification · hence the RE-MEASURE tags).

- [ ] SP1 GEOMETRY · pill slot concave SCOOP: replace the flat tub floor with a swept concave
      finger dish inside each plateau slot (refs: sth_front-1/2 + cl_front-foam highlight
      gradients) · relief stays strictly concave · Accept: single smooth highlight gradient
      in the dish at macro, no flat plane.
- [ ] SP2 REMEASURE · front layout reconciliation across sth_front-1 + sth_front-2 +
      cl_front-foam + nv_hero_3q TOGETHER: end-tab width, plateau size/position, proud-vs-
      flush relief, foam margins · weight the cleanest orthographic view · then AT MOST ONE
      reconciled change-set commit · Accept: rendered ratios within 5% of reconciled pins.
- [ ] SP3 GEOMETRY · top vent dish transition: ~15mm tangent dish annulus, depth 1.2-1.5mm,
      G2 at both ends, replacing the stepped wall · Accept: side-on halo band gradient, no
      step shadow (cl_side-profile).
- [ ] SP4 GEOMETRY · vent recess corner radius to ~0.17 of panel width (squircle character) ·
      Accept: ratio within 10% at 4K top.
- [ ] SP5 REMEASURE+GEOMETRY · the 133x3mm stadium slot near the foam-side top edge
      (centerline ~15mm from edge) · reconcile exists-vs-shape first · Accept: within 10%.
- [ ] SP6 REMEASURE · weave pitch (flip-flop-guarded · history 4.6 -> 1.5 · audit says ~5mm
      satin bands): two references minimum, implement as SOFT satin bands gated to the flat
      panel · Accept: pitch within 10% of reconciled pin.
- [ ] SP7 GEOMETRY · through-slot pills + diamond-mesh grille behind the plates (the real
      pills are open handles/intakes) · Accept: darkness through the slot with faint regular
      mesh glint (cl_front-intake).
- [ ] SP8 GEOMETRY · side plinth foot (recessed dark plinth under the body, sth_side refs) ·
      Accept: thin under-shadow band reads side-on.
- [ ] SP9 VERIFY · pair relative scale: measured Spark:Studio width ratio within 3% of the
      perspective-corrected 0.761 · fix PAIR_SPAN/camera math if off, never device dims.

## PART 6 · RACK QUEUE (the gap · full detail in render/RACK-DETAIL-AUDIT.md sections 2-3,
## mirrored from RACK-LOOP.md v2 · run in the RACK worktree, rack_verify gates)

Wave 0 · FRAME CORRECTIONS (built part is wrong before anything stacks):
- [ ] R0.1 GEOMETRY · outer width 760 -> 600mm: walls/posts inboard ~80mm/side, rail x FROZEN
      (rails measured correct: c-c 464.5 vs 465.1 spec) · Accept: rail-derived scale gives
      width 600 +/-2% on a fresh front.
- [ ] R0.2 GEOMETRY · post faces ~45mm + 3 hinge bosses + latch keeper · Accept: face ratio
      ~0.075 of width, silhouette not dead-straight.
- [ ] R0.3 GEOMETRY · top band ~45mm · 4 leveling feet (pad ~45mm, 30mm floor gap) + twin
      casters inboard · Accept: light gap under base reads.
- [ ] R0.4 GEOMETRY · rear rail pair (holed, same pattern) at rear depth · corner gusset
      castle plates · brush strips · Accept: rear rails read between units from dead front.
- [ ] R0.5 GEOMETRY · U-tick strip on rail flange + hole size verified 9.5mm in source ·
      Accept: ticks resolve at 4K.
- [ ] R0.6 MATERIAL · orange-peel powder micro-texture (NEW target) + RE-MEASURE the frame
      tone (eye says grey, committed number says L22 · one autopsies) · Accept: rack_verify
      powder_black PASS.

Wave 1 · RM44 NODE (hero · two-layer mesh):
- [ ] R1.1 body 440x176x468 + folded ears to 482.6 + 2 knurled thumbscrews each · +/-1.5%.
- [ ] R1.2 door: real tri punches P2.87/R2.59 rounded-corner alternating + border FADE ROWS +
      ~8mm frame, cached · Accept: FFT pitch +/-5% via render/rack_measure.py (port the tool).
- [ ] R1.3 FILTER LAYER (mapped micro-perf ~0.9mm at 4mm setback) + fan wall (3x120mm rings/
      hubs) + interior albedo 0.032 · Accept: raking + grazing read door->filter->dark ·
      the formal Problem-2 evidence render.
- [ ] R1.4 keystone badge plate (blank, chevron bottom) + bail-handle lock at measured y +
      lid lip seam + bottom sill 2 screws + 4 witness dots · +/-2% of face width.
- [ ] R1.5 LIGHTING · interior fill · holes read open, exterior patches stay green.
- [ ] R1.6 RENDER · node turnaround + TRUE macro (L8) · rack_verify PASS + pitch check.

Wave 2 · CRS354 SWITCH: chassis 443x44.3x297 WHITE + seam + ears (+7mm) · port grid 4 GANGS of
2x6, bottom row MIRRORED (latch flip), per-port LED pipes, gang window bezel (VERIFY-THEN-PIN
color) · SFP+ 2x2 + QSFP+ 2x1 cages w/ bale latches · console/MGMT stack · status LEDs +
pinhole · switch_white patch added to rack_verify and PASS. [4 boxes · R2.1-R2.4]

Wave 3 · UPS: 432x89 bezel grille-LEFT/control-RIGHT + louver count from photo + scallop
pillars + recessed control plate · power bar + 2x2 buttons + dim emissive LCD (clip gate
holds) + ears + blank badge · photo outranks CGI on every disagreement. [2 boxes · R3.1-R3.2]

Wave 4 · ACCESSORIES: blanking w/ edge returns + relief notches + mounting slots · duct
fingers/pitch/cover · cage nut CANONICAL (square cage + collar + castellated wings, zinc) at
OCCUPIED holes + 2-3 spares + M6 screws two head styles · shelf + generic blank mini-PC.
[4 boxes · R4.1-R4.4]

Wave 5 · ASSEMBLY (fill map v2 FIRST as a REMEASURE commit: bottom-heavy, deliberate voids
U4/U9, network band TOP: duct U37 + switch U38 + shelf U40, honest U21-36 void run) ·
place by u_z(), linked dupes · variance: LED budget ~60% lit mixed green/amber 2-3 bright,
node states A-on/B-off/C-on, seating jitter <=0.4deg, per-instance roughness/dust (every knob
cites a gestalt photo) · cage nuts occupied-only · patch catenary arcs duct->switch->columns ·
full front + q34 + TWO true macros · rack_verify ALL PASS both wide shots · no two adjacent
units identical. [5 boxes · R5.1-R5.5]

Wave 6 · PHOTOREAL + TRIO: post chain on deliverables + firefly verify · ledger rows T-RACK-1
array uniformity / T-RACK-2 dead-black depth / T-RACK-3 filter-layer read · panel per L12 with
real-rack controls (driver reads) · THE SCALE TRIO: Studio + Spark + rack, one rig, per-class
offsets, true scale by U-arithmetic · BOTH gates green in the same frame set · driver owns
composition. [3 boxes · R6.1-R6.3]

## PART 7 · FINAL DELIVERABLES (the definition of "majestic, done")

1. Per-object hero sets (4K, post-chained): Studio 4 angles + macro · Spark 5 angles + macro ·
   rack front/q34/node-macro/empty-bay-macro.
2. THE SCALE TRIO hero (the site's money shot · ~20:1 scale story, desktops at the rack's foot).
3. Refreshed 16-attachment checkpoint folder(s) with collages + the consolidated report.
4. Every tone gate green · every acceptance ratio in tolerance · audits updated to final
   scores · all worktrees committed clean.
Stop condition: every box above checked, every non-scope-gated audit aspect at 9+ · then
present to the owner · the owner closes.

## PART 8 · INGEST GATE (state at handoff · 2026-07-05)

- Desktops geopack (final 4K reshoot with geo-pass-1 fixes): [PENDING at doc-write · the
  executor's FIRST action in the desktop worktree is ST-V1/ST-V2 verification on
  render/portraits/*.png, then tick this line] -> on ingest: gate ALL PASS re-confirmed,
  checkpoint repacked, ST-V1/ST-V2 verdicts recorded here.
- Rack: audit + loop v2 committed (384471f) · nothing running · Wave 0 is open and FIRST.
- Panel history + calibration: PHOTOREAL-SHIFT-REPORT.md sections 10b-10e (desktop worktree).
