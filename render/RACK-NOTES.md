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
