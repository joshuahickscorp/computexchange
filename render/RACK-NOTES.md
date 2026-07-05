# render/RACK-NOTES.md · the wave log for the third oracle (person-owned GPU rack)

Same discipline as the desktop NOTES.md: minimum three look-fix cycles per canonical part;
"done" per part requires a written pass; I present, the grader closes; every value traces to
a MEASUREMENTS row. Change class declared per commit. Dash gate: middot only.

## GATE 5a · enclosure frame + dark-object rig probe · class GEOMETRY + LIGHTING

Built render/build_rack.py (new self-contained module · desktop build_scene.py untouched).
The frame: 4 corner posts, top cap, base plinth, two 0U side channels, dark rear interior
panel, and 4 EIA rails · the front pair carries the 9.5mm square-hole pattern built as a 1U
flange segment (3 holes at the measured 6.35/22.25/38.10mm per-U offsets) + ARRAY modifier
x42. U-arithmetic spine: u_z(n) = 100 + (n-1)*44.45mm. Rendered on Metal GPU, ~7s/preview.

GEOMETRY VERDICT (frame-frame-front.png): proportions read as a true 42U 750mm cabinet
(1991 x 750 = 2.65:1), the arrayed square-hole rails are exact and evenly spaced · the
recognizable "anyone who has racked hardware" signature lands. Interior back + side channels
read. Frame geometry frozen for the empty-cabinet part; door/cage-nuts/leveling-feet detail
deferred to the assembly wave.

DARK-OBJECT RIG · the flagged HIGH risk, PROBED and RESOLVED:
- First probe at key=5200W blew the L~0.03-albedo powder-coat to WHITE (AgX over-exposure) ·
  I had overcorrected for the low albedo. The probe's value was exactly this read.
- Tuned to key=460 / rim=300 / fill=175 (metric, ~2m object): powder-coat lands lit-face
  L~22 (key side), shadow-side L~11, clean separation from void-black #060606, clip 0.000%
  peak 0.808. Reads as a dark satin cabinet carved out of black by EDGE + SHEEN (Apple
  dark-hero doctrine), not a silhouette, not a grey box. These are the frame-probe rig values
  (build_rack PROVEN defaults); the node-face wave refines them against big flat faces.

TONE-GATE TRANSFER · the second HIGH risk, QUANTIFIED:
- Reference powder-coat (rm44_front_A, bright flat studio) = L16. In-rig (dark hero) lit rail
  = L~22 · a natural offset of ~+6, OPPOSITE the desktops' -12 (silver L84 ref -> L73 in-rig).
- MECHANISM: a bright object under a dramatic key sits BELOW its flat-studio reference (the
  studio floods it more evenly than a single hard key); a DARK object sits ABOVE (the hard key
  concentrates onto the lit faces above the flat-studio flat read). Different regimes.
- RESOLUTION: the rack gets its OWN tone-gate offset O_rack ~ +6, NOT the shared desktop
  O=-12. For the scale trio (all three on one rig), each object's patches gate against their
  own offset · one rig, per-object-class offsets. This kills the "does O=-12 transfer" risk
  with a measured answer. Locked into the tone-gate design; powder_black joins the patch set.

NEXT (gate 5b): the RM44 node face · the hero part carrying Problem 2 (depth into darkness).
The measured truth (RACK-BUILD-PLAN section 0): the triangular mesh reads L~16 even in bright
studio light, so the target is STRUCTURE (see-through holes to a dark-not-zero interior), not
tone. Technique bake-off (real cut holes + interior box vs normal+opacity vs hybrid) on a
raking-light detail tile; I decide, raking acceptance render proves it.

## GATE 5b (part 1) · depth-into-darkness BAKE-OFF · class GEOMETRY+MATERIAL · VERDICT

Mesh lattice MEASURED first (2D FFT autocorr on rm44_front_A at 3.480 px/mm): triangle
period P=2.87mm (half-period 1.44 confirms alternating up/down), row pitch R=2.59mm
(equilateral check 0.87x2.97 agrees), full V repeat 5.17mm, open fraction ~0.5 LOW-CONF
(threshold-circular · geometric open at 0.4-0.5mm web = 0.33-0.40 · refine at part wave).
~7,500 holes per node face at true scale. Evidence rack-rm44-mesh-crop.png.

Bake-off on a 200x120mm true-scale door tile + interior (box, fan, faint interior fill),
judged under raking strip (~12deg) and 55deg grazing (_rack_bakeoff.py):
- **A · REAL cut holes** (boolean 2 tri-prisms on ONE P x 2R cell -> array 69x23 = 25,392
  tris, renders in ~4s): raking = crisp punched openings, web catches the rake, per-hole
  interior variation once the interior was fed (ifill 3.5, fan at true ~20mm depth, interior
  albedo 0.032) · grazing = holds the perforation read with true perspective compression ·
  the EXACT test the Spark's displaced heightfield failed before its technique-class switch.
- **B · alpha-mask shader**: the parity math produced zigzag banding (fragile), and the class
  has no hole-wall geometry -> no grazing glints, no thickness parallax. Its one advantage
  (cost) is moot at A's 25k tris.
**VERDICT: A · real cut holes, cell-boolean + array.** Locked as the technique class
(RACK-LOOP invariant 3). Iteration notes: first raking strip at 9W blew the exterior (same
over-exposure class as the frame probe · trimmed to 2.4); fan blades must never cross the
door plane (placement bug caught by eye). Evidence: bake-A-raking.png (acceptance-class),
bake-A-graze.png, bake-B-raking.png (failure documented).

## O_rack AUTOPSY · class REMEASURE (the oracle caught the driver)

rack_verify.py (the numeric oracle for the Opus loop) armed on the frame render CAUGHT the
gate-5a offset claim: "lit rail L~22 -> O_rack ~+6" came from a patch box CONTAMINATED by
the brighter side-channel wall behind the rail. Clean flange-only box reads L16.6 in-rig vs
reference L16.0 -> natural offset ~+0.6 ~= 0. Dark-regime L is compressive · a dark object
largely TRACKS its flat-studio reference tone under the hero rig (the desktops' -12 was a
bright-albedo phenomenon). **O_rack = 0.0 working value** (rack_verify default, autopsy note
in-file); final derivation lands on the RM44 broad front face at the part wave. Gate now:
powder_black dE 0.65 PASS · clip 0.000% · exit 0. The 5a NOTES claim stands SUPERSEDED by
this entry.

## HANDOFF · the loop goes to Opus

render/RACK-LOOP.md written: invariants (pins never move · desktops closed · technique class
locked · rig/O changes are LIGHTING commits), the one-iteration protocol, the per-part WORK
QUEUE with acceptance numbers, escalation triggers, notes template. The oracle
(rack_verify.py) is the judge; the driver returns for escalations, the panel, and the trio.
