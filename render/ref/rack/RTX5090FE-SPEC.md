# RTX 5090 Founders Edition · modeling spec (researched 2026-07, 5-agent web pass)

The rack's GPU is the **NVIDIA GeForce RTX 5090 Founders Edition** · the current consumer AI
flagship (32GB GDDR7, Blackwell, $1,999), the sibling to the Studio + DGX Spark. FE = the
reference design (model this, not an AIB card). Full research: task wyf4ukki4 output.

## Dimensions (build to these · high confidence)
- **304 mm** long (the long axis, along the PCIe edge) · **137 mm** tall (bracket edge to top)
  · **40 mm** thick = **2-slot** (compact · vs the 3-slot 4090 FE). Weight ~1.8 kg (mass ref).
- Discard: 48 mm (outer envelope incl. protrusions), 61 mm (that is NVIDIA case CLEARANCE, not
  the card), any silver/chrome two-tone (that is the 3090/4090, wrong generation).

## Cooler / fans (the defining geometry)
- **2 axial fans**, identical, **~115 mm** dia, **7 blades** each. BOTH on the SAME broad face
  (the intake face). One fan at EACH END of the 304 mm span; the small PCB hides in the CENTER
  third between them.
- **Double flow-through**: an open pass-through window under EACH fan (front face -> fin gaps ->
  back face open). Light passes clean through both ends. Signature look.
- Internal (short-end/silhouette): 1 vapor chamber (center) + 5 heatpipes + 3 fin blocks (a
  middle one + two larger end stacks under the fans). Fin surface concave (dished) under each hub.

## Colorway (CORRECTION · high confidence) · "Dark Gun Metal"
Deliberately DARK, near-monochrome, all-metal, MATTE. NO silver/chrome edges (that was 40-series).
Keep the whole card dark. Per-surface PBR (RGB 0-1 linear · placements high-conf, exact nums med):
- shroud frame/body: (0.165, 0.173, 0.188) gunmetal · rough 0.60 · metallic 0.9 (machined alu)
- fans + center caps: (0.110, 0.114, 0.125) -> CORRECT DOWN to ~(0.050, 0.052, 0.060). Reviews call
  the fans flatly "black" (NoobFeed, PC Gamer 2026-07 pass) and the render at 0.110 read medium-GREY,
  not black · a proper black plastic is ~0.03-0.06 linear · the glossy coat still gives edge/spec
  highlights so the blades read against the dark well. rough 0.55 · metallic 0.0 · coat 0.35 (matte plastic)
- heatsink fins: (0.082, 0.086, 0.098) black · rough 0.50 · metallic 0.90 (anodized alu)
- X / infinity accents: (0.14, 0.15, 0.16) · rough 0.40 · metallic 0.90 (slightly glossier, catches key)
- PCI bracket: (0.078, 0.078, 0.078) black · rough 0.50 · metallic 0.90 (anti-fingerprint)
- illumination (below): EMISSIVE cool white (0.933, 0.957, 1.0) · static, non-RGB

## Signature form
- **X-shaped accent** on BOTH front + back faces (mirror = "infinity loop"). A dark-grey X-shaped
  bracket sits BETWEEN the two fans on the front. Corners more rounded than the 4090.
- **Static white illumination** (non-RGB, always on · model emissive): (1) wordmark on the TOP
  EDGE, (2) the V/X accents on both faces, (3) rings around the fan inlets on both faces.
  CONFIRMED 2026-07 (NVIDIA GeForce forums + TechPowerUp review + TEARDOWN photos + OC3D + NoobFeed ·
  5 sources): the FE genuinely lights the GeForce RTX side logo, the air-inlet area, the X (front +
  near the power connector), and the top logo · static WHITE, non-RGB, NON-adjustable, cannot be
  turned off · "the metal frame houses white LED illumination" (teardown). So the lit rings + X +
  wordmark are ACCURATE · KEEP them.
  FLIP-FLOP GUARD (do NOT re-open): the forensic panel (w5oyz13ew) asserted "the FE has NO
  illumination · delete the emission." That is FACTUALLY WRONG · overruled by the 5 sources above.
  The agents worked from imperfect training memory · the live reviews + teardown win. The LEDs stay.
  The panel's VALID point (keep): the emitters must cast real GI (cool spill onto blades/shroud) ·
  addressed by raising the indirect clamp + thinning the emitters · NOT by deletion.

## Trademark gate · logos/wordmarks = BLANK plates only (shape + placement, no glyphs)
- top-edge "GeForce RTX" wordmark: a blank recessed backlit strip (emissive cool white), centered.
- back X etched logo: a blank shallow recessed cartouche inside the rear X.
- "RTX 5090" top-cover text: a blank small etched plate near the power connector.

## I/O · power · backplate
- bracket: **3x DisplayPort + 1x HDMI** = 4 connectors. NO vent grille (flow-through exhausts
  top/back). Black anti-fingerprint. Connectors reversed vs prior FE.
- power: **1x 16-pin 12V-2x6**, recessed into the shroud on the TOP-REAR edge near the "RTX 5090"
  plate, **angled** (shallow oblique ~15-30 deg off vertical · exact deg not published).
- backplate: full dark metal, mirrored rear X + blank etched cartouche, a LARGE open flow-through
  cutout window over the REAR-fan region (fin stack visible behind, ~120mm-fan-sized opening).

## Rack mounting (our use · 6-card row)
Cards stand PORTRAIT (304 vertical, 137 wide, 40 thick), fan-face forward (-Y): 2 fans top+bottom,
X accent + glowing inlet rings on the front. 6 in a row · pitch ~148mm · needs a ~950mm-wide rig.
The glowing inlet rings + X carry the "running" read from the front; the top-edge wordmark shows
on the end card + in q34.
