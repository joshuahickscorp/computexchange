# render/MEASUREMENTS.md · measured geometry & tone

Every value produced by `render/measure.py` from `render/ref/`. One known real
dimension anchors each device (Mac Studio front width 197 mm · DGX Spark front
long edge 150 mm); all other values follow from the pixel scale. Evidence crops
under `render/measure_evidence/`. Dash gate: middot only.

## Mac Studio

| parameter | value | unit | conf | source image | evidence crop | note |
|---|---|---|---|---|---|---|
| front_width_anchor | 197.00 | mm | anchor | apple_front.jpg | mac_front_silhouette.png | known real width (Apple spec 197 mm); the one absolute this image scales from |
| front_aspect_W:H_meas | 2.05 | ratio | high | apple_front.jpg | mac_front_silhouette.png | image aspect vs spec 2.074; image reads taller by intake-band inclusion |
| front_corner_R_tl | 8.35 | mm | high | apple_front.jpg | mac_corner_tl.png | Kasa fit rms=0.59mm |
| front_corner_R_tr | 8.20 | mm | high | apple_front.jpg | mac_corner_tr.png | Kasa fit rms=0.37mm |
| top_edge_fillet_R | 2.50 | mm | high | dim_back-side.svg.png | wave1-fillet-fit | REMEASURED wave 1 from the dimensioned side/rear elevation top corner (Kasa nb24 R2.20 rms0.19mm, nb32 R2.91; both top corners agree ~3.6 at nb40, R grows with nb = tight fillet). ~SUPERSEDES 8.27~ AUTOPSY: the old value came from an apple_front FRONT-OUTLINE fit that conflated the tight top-edge fillet with the 31.4mm plan corner turning through the silhouette (the rounded vertical edge reads as a large arc in the 2D outline). The real top is flat with a ~2.5mm edge. |
| usbc_orientation | vertical | axis | settled | apple_front.jpg | tiebreak_usbc.png | SETTLED by two photographers: Apple H/W 3.23, Wikimedia H/W 1.87 (3/4, foreshortened) · both vertical |
| usbc_long_axis_vert | 8.47 | mm | high | apple_front.jpg | mac_usbc_zoom.png | = USB-C receptacle 8.4mm dimension, oriented vertical |
| usbc_short_axis_horiz | 2.62 | mm | high | apple_front.jpg | mac_usbc_zoom.png | = USB-C 2.6mm dimension |
| usbc_pair_spacing | 14.79 | mm | high | apple_front.jpg | mac_ports.png | center-to-center, along the width |
| usbc_left_x_from_center | -66.16 | mm | high | apple_front.jpg | mac_ports.png | feature position: + is right of center |
| usbc_right_x_from_center | -51.36 | mm | high | apple_front.jpg | mac_ports.png | feature position |
| port_row_center_from_base | 24.36 | mm | high | apple_front.jpg | mac_ports.png | z of the port row above the base |
| sd_slot_orientation | horizontal | axis | high | apple_front.jpg | mac_ports.png | wide slot, long axis horizontal (opposite of the USB-C ports) |
| sd_slot_width | 26.85 | mm | high | apple_front.jpg | mac_ports.png | horizontal long axis |
| sd_slot_height | 2.50 | mm | high | apple_front.jpg | mac_ports.png |  |
| sd_center_x_from_center | -24.41 | mm | high | apple_front.jpg | mac_ports.png | feature position: left of center |
| led_diameter | 2.94 | mm | approx | apple_front.jpg | mac_ports.png | power dot core (glow-inclusive) |
| led_from_right_edge | 10.80 | mm | med | apple_front.jpg | mac_ports.png |  |
| led_from_base | 27.50 | mm | med | apple_front.jpg | mac_ports.png | LED height above device base |
| led_x_from_center | 87.70 | mm | med | apple_front.jpg | mac_ports.png | feature position: far right |
| intake_band_height | 8.55 | mm | high | apple_front.jpg | mac_intake_band.png | perforated hex mesh, front-face bottom edge to silhouette bottom (NOT the ground gap) |
| intake_hole_pitch | 1.10 | mm | med | apple_front.jpg | wave6-intake-pitch.png | wave-6 remeasure at verified scale (12.27 px/mm, 1cm bar): ~9-10 holes/cm -> ~1.1mm hex pitch. The old builder 1.70mm was too coarse (audit right). |
| intake_hole_diameter | 0.80 | mm | med | apple_front.jpg | wave6-intake-pitch.png | wave-6: hole diameter ~0.8mm (hole:pitch ~0.73), thin bright web between holes |
| intake_corner_wrap | wraps | axis | high | apple_front.jpg | wave6-intake-corner.png | wave-6: the perforation CONTINUES around the rounded vertical corner (does NOT stop at solid aluminium). Resolves the audit split (detail-audit right, front-audit wrong); the render already wraps (mesh on the bottom fillet) |
| front_height_spec | 95.00 | mm | spec | apple_front.jpg | mac_front_silhouette.png | absolute from Apple spec; image measures 96.20 (+1.3%) = intake-band inclusion, not scale error |
| alu_Lab_L | 84.32 | L* | high | apple_front.jpg | mac_alu_patch.png | diffuse mid-face patch |
| alu_Lab_a | 0.03 | a* | high | apple_front.jpg | mac_alu_patch.png |  |
| alu_Lab_b | -1.12 | b* | high | apple_front.jpg | mac_alu_patch.png |  |
| intake_mesh_Lab_L | 71.81 | L* | med | apple_front.jpg | mac_intake_band.png | wave-0 tone gate; PERFORATED-MESH-ONLY region (y 0.60-0.93 of crop). SUPERSEDES full-crop L77.68 which was ~60% bright front-face silver above the mesh (autopsy) |
| plan_corner_R | 31.40 | mm | high | dim_top-front.svg.png | mac_plan_corner.png | footprint / vertical-edge radius (distinct from 7.6mm top fillet); vector arc rms=0.040mm |
| base_reveal_gap | 2.5 (INFERRED) | mm | inferred | apple_desk-setup.jpg | mac_reveal_gap.png | declared design parameter, NOT measured; recess conflates with cast shadow (~23mm). Tune in phase 3 vs the 3/4 blend |

## DGX Spark

| parameter | value | unit | conf | source image | evidence crop | note |
|---|---|---|---|---|---|---|
| front_long_anchor | 150.00 | mm | anchor | cl_front-foam.jpg | dgx_front_silhouette.png | NVIDIA spec 150 mm; the one absolute this image scales from |
| front_short_edge_spec | 50.50 | mm | spec | cl_front-foam.jpg | dgx_front_silhouette.png | absolute from spec; this source reads 51.4 (+1.8%, incl. thin visible side); sth_side reads 48.6 (-3.8%, foreshorten) |
| front_aspect_long:short_meas | 2.92 | ratio | high | cl_front-foam.jpg | dgx_front_silhouette.png | image aspect vs spec 2.97; a ~3:1 STRIP, NOT square |
| front_edge_R_tl | 7.05 | mm | high | cl_front-foam.jpg | dgx_corner_tl.png | Kasa rms=0.17mm, left edge |
| front_edge_R_bl | 5.13 | mm | high | cl_front-foam.jpg | dgx_corner_bl.png | Kasa rms=0.17mm, left edge |
| front_edge_R_mean | 6.09 | mm | high | cl_front-foam.jpg | (see corners) | clean left edge |
| pill_orientation | long axis || 50.5mm short/depth axis | axis | high | cl_front-foam.jpg | dgx_pills.png | each pill wider across the short edge; the two pills are arrayed along the 150mm long axis |
| pill_long | 31.41 | mm | high | cl_front-foam.jpg | dgx_pills.png | hand-hold cutout long axis (runs along the 50.5mm short/depth edge), 2 present |
| pill_short | 12.96 | mm | high | cl_front-foam.jpg | dgx_pills.png | pill width along the 150mm long axis |
| pill_center_from_end | 15.21 | mm | high | cl_front-foam.jpg | dgx_pills.png | near 50mm-end -> first pill center, along the 150mm axis |
| pill_pitch | 112.90 | mm | high | cl_front-foam.jpg | dgx_pills.png | center-to-center along the 150mm long axis |
| ~foam_field_span_long~ | ~86.90~ | mm | med | cl_front-foam.jpg | wave3-spark-front-struct.png | ~SUPERSEDED~ DOUBLE AUTOPSY (final wave): the wave-3 "bounded center span" was ITSELF WRONG. The per-row std band read the pill BEZEL + rail as one solid smooth block (they blur together at source resolution), and the wave-3 autopsy line blaming the original 148.02 for "model-assumption leakage" had the story BACKWARDS. The foam is EDGE-TO-EDGE; the phase-0 148.02 read was nearer the truth. See foam_field_long below. |
| ~endcap_width~ | ~31.50~ | mm | med | cl_front-foam.jpg | wave3-spark-front-struct.png | ~SUPERSEDED~ there are NO 31.5mm solid end-caps. That band was the pill bezel (~29mm) plus the ~1mm rail plus their blur, mistaken for one cap. Replaced by pill_bezel_* + end_rail_width below. |
| foam_field_long | 148.02 | mm | med | cl_front-foam.jpg | finalA-sth-pill.png | RESURRECTED (final wave): foam runs edge-to-edge between the thin end rails, flush in the flat front slab, flowing around the bezels. |
| foam_field_short | 46.34 | mm | med | cl_front-foam.jpg | finalA-sth-pill.png | RESURRECTED; ~2.5mm champagne lip top/bottom (foam_border_margin) |
| pill_bezel_width | 29.00 | mm | med | sth_front-1.jpg | finalA-sth-pill.png | champagne rounded-rect island (along 150), embedded in foam, holding the recessed slot (12.96 wide); border ~8mm on the logo side, foam on the inboard side |
| pill_bezel_height | 33.00 | mm | med | sth_front-1.jpg | finalA-sth-pill.png | bezel island along 50.5 (slot 31.41 tall + ~1mm border), foam above/below |
| pill_bezel_border | 5.00 | mm | med | sth_front-1.jpg | finalA-sth-pill.png | champagne margin slot-edge to foam (blank D2 bezel uses a symmetric ~5mm; the reference's ~8mm side margin carried the logo) |
| end_rail_width | 1.00 | mm | med | cl_front-foam.jpg | finalA-clfoam-pill.png | thin champagne rail at each 150-axis end (RECONCILES with + credits the phase-0 foam_end_band 0.99, which was nearer the truth than the wave-3 correction) |
| slot_recess_depth | 4.20 | mm | low | cl_front-foam.jpg | wave3-spark-front-struct.png | pill finger-slot pocket depth (accepted; the slot relocates into the bezel, geometry unchanged) |
| bezel_foam_relief | 0.60 | mm | low | sth_front-1.jpg | commit1-bezel-relief.png | photoreal commit 1: champagne bezel sits ~FLUSH (a hair proud) of the foam plane. Shadow width ~0.7mm at the bezel inboard edge, key elevation ~40deg -> relief ~0.7*tan40 ~ 0.6mm. Foam lowered to sit flush in the face; bezel (body face 0) flush with foam mean (+0.14). |
| foam_border_margin | 2.53 | mm | med | cl_front-foam.jpg | dgx_border.png | champagne lip, long edge (thin) |
| foam_end_band | 0.99 | mm | med | cl_front-foam.jpg | dgx_border.png | champagne band at short/pill ends (phase-0; = end_rail_width, credited) |
| top_width_anchor | 150.00 | mm | anchor | cl_side-profile.jpg | dgx_top_silhouette.png | NVIDIA spec 150 mm; the one absolute this image scales from |
| top_depth_spec | 150.00 | mm | spec | cl_side-profile.jpg | dgx_top_silhouette.png | absolute from spec; this source reads 148.6 (-0.9%) |
| top_aspect_W:D_meas | 1.01 | ratio | high | cl_side-profile.jpg | dgx_top_silhouette.png | top face is square (spec 1.00) |
| top_plan_corner_R | 21.04 | mm | med | cl_side-profile.jpg | dgx_top_corner.png | footprint corner (foam-edge softens fit), rms=0.64mm |
| spark_top_border_Lab_L | 77.75 | L* | med | cl_side-profile.jpg | (top patches) | champagne top BORDER (neutral light: a0.0 b5.56 · desaturated, far less golden than the warm-lit storagereview shell b42.78 · lighting, not a different metal) |
| spark_top_vent_Lab_L | 46.92 | L* | med | cl_side-profile.jpg | (top patches) | the recessed diagonal-WEAVE vent panel · distinctly DARKER than the champagne border (a3.55 b11.33). This is the "dedicated dark top material" (wave 4a). OVERRIDES the audit's dark-slate #2b2c2e/L17 and edge-aligned-hex assertions: the measured top is champagne + a CENTERED darker weave panel, NOT slate. Measurement wins. |
| spark_top_vent_pitch | diagonal ~45deg ribbed weave | axis | med | cl_side-profile.jpg | (top patches) | the vent panel carries a fine diagonal (~45deg) ribbed weave, not a hex mesh; builder ~4.6mm rib pitch |
| spark_top_panel_border_R | 8.00 | mm | low | cl_side-profile.jpg | settle8-dgx-spark-top.png | recessed vent panel corner radius (builder, tightened from 12); the reference panel corner reads moderate |
| spark_top_exhaust_slot | front-edge thin slot | axis | med | cl_side-profile.jpg | settle8-dgx-spark-top.png | a thin recessed exhaust slot runs along the front edge of the vent panel (wave-8 addition, closing the 4b defer) |
| top_panel_width | 114.15 | mm | med | cl_side-profile.jpg | dgx_top_panel.png | recessed vent panel |
| top_panel_height | 105.06 | mm | med | cl_side-profile.jpg | dgx_top_panel.png |  |
| top_panel_inset_margin | 17.92 | mm | med | cl_side-profile.jpg | dgx_top_panel.png | frame edge -> panel, mean L/R |
| foam_cells_per_cm_A | 13.00 | cells/cm | med | storagereview_front.jpg | dgx_foam_A.png | 26 ridges / 20mm strip |
| foam_cells_per_cm_B | 14.50 | cells/cm | med | storagereview_front.jpg | dgx_foam_B.png | 29 ridges / 20mm strip |
| foam_cells_per_cm_C | 13.50 | cells/cm | med | cl_front-foam.jpg | wave5-foam-scale.png | wave-5 REOPEN at verified scale (45.3 px/cm anchor): ~13-14 pores across the 1cm bar CONFIRMS 13-14/cm (~0.74mm mean pitch). OVERTURNS the audit's "3-5x too fine" claim: the real foam IS this fine · the density was never the problem. Size variance ~0.4-1.2mm (real spread). The misses are SHAPE (rounded bubble voids, not angular Voronoi shards), DEPTH (open cells, not a flat displaced skin), and MATERIAL (wave 5b/5c) |
| spark_champ_pin | 80.0 / 2.8 / 29.0 | Lab | med | sth_front-1.jpg | finalA-sth-pill.png | FINAL PIN (one source). Bright metallic anodized champagne, visibly GOLD (b29), calmer than the storagereview brass (b42.8) and far more gold than the wave-4 pale re-pin (b12). Spread: cl_side-profile champ b3-12, storagereview b42.8. sth_front-1 is THE pin (one photo, one light, zero cross-source drift). Supersedes champagne_Lab_* 72.52/7.78/42.78 AND the wave-4 re-pin 77.75/1.0/12.0. |
| spark_foam_mean_pin | 52.8 / 4.2 / 18.8 | Lab | med | sth_front-1.jpg | finalA-sth-pill.png | FINAL PIN. Golden foam, MUCH brighter+warmer than the render carried (~L26 b5). FINDING-3 AUTOPSY: the wave-5c gate "spark_foam PASS dE3.01" was measured against a b*=8.0 target that had NO measurements row · the wave-5c pore/web work quietly NEUTRALISED the foam chroma target (foam_mean b20.5 -> a fabricated 8.0) to make the grey render pass. That is the laundering the rules exist to kill. Superseded: pinned honestly to sth_front-1 b18.8. |
| spark_foam_web_pin | 76.9 / -- / 19.6 | Lab | med | sth_front-1.jpg | finalA-sth-pill.png | golden web, top-quartile L. Supersedes foam_web 67.98. |
| spark_foam_pore_pin | 15.2 / 5.3 / 12.3 | Lab | med | sth_front-1.jpg | finalA-sth-pill.png | WARM dark pore (b12.3), not neutral charcoal. Supersedes foam_pore 10.66. |
| side_thickness_persp_check | 48.63 | mm | med | sth_side-1-vertical.jpg | dgx_side_silhouette.png | smooth side reads 48.6 vs spec 50.5 (-3.7%); a per-source perspective indicator, NOT the absolute (spec 50.5 governs) |

## RACK (third oracle · D-ARCH A-prosumer · person-owned 42U)

Anchor discipline inverts for the rack: the absolute anchor is the EIA-310 U-MODULE, a
standard, not a photo · vertical placement of every unit is U-arithmetic, not pixel-measured.
Spec rows cite the archived evidence PDFs under ref/rack/ (integrity-checked, provenance in
ref/rack/RECON-D-ARCH.md). Pixel rows (per-part face features · RM44 perforation pitch, lock
position, seam heights · CRS354 port-grid positions) land with measure.py runs + evidence
crops in each part's modeling wave, anchored on that part's spec width. Dash gate: middot.

| parameter | value | unit | conf | source | evidence | note |
|---|---|---|---|---|---|---|
| U_module | 44.45 | mm | anchor | eia-rs310-1968.pdf | ref/rack/standards/ | THE anchor · 1.75in exactly · every U position = (n-1)*44.45 above rail datum |
| hole_offsets_per_U | 6.35 / 22.25 / 38.10 | mm | spec | eia-rs310-1968.pdf | ref/rack/standards/ | hole centers from U boundary · pattern 12.70/15.88/15.88 pitch · U boundary bisects the 12.7mm pair |
| square_hole | 9.5 | mm | spec | intel-rack-compat-guide-r24.pdf | ref/rack/standards/ | cage-nut square holes (EIA-310-D) · round alt 7.1mm |
| panel_height_rule | 44.45n - 0.79 | mm | spec | eia-rs310-1968.pdf | ref/rack/standards/ | equipment faces undersized 1/32in · the inter-unit AIR GAP every real rack shows |
| rail_opening_W | 450.85 | mm | spec | eia-rs310-1968.pdf | ref/rack/standards/ | 17.750in min (450mm-min camp recorded in RECON · spread noted) |
| panel_W | 482.60 | mm | spec | eia-rs310-1968.pdf | ref/rack/standards/ | 19in preferred width · ear-to-ear |
| hole_center_span | 465.12 | mm | spec | eia-rs310-1968.pdf | ref/rack/standards/ | 18.312in rail-to-rail hole centers |
| enclosure_HWD | 1991 x 750 x 1070 | mm | spec | apc-netshelter-sx-manual.pdf | ref/rack/enclosure/ | NetShelter SX 42U 750mm class (AR3150 table row) · donor SKU |
| enclosure_flange_range | 292.10 to 787.40 | mm | spec | apc-netshelter-sx-manual.pdf | ref/rack/enclosure/ | 750-wide flange adjustability · factory position fits 737mm-deep equipment |
| enclosure_door_open_area | 593018 (600w) | mm2 | spec | apc-netshelter-sx-manual.pdf | ref/rack/enclosure/ | perforated front door open-area table · door shown OPEN/REMOVED in the portrait (D-ARCH) |
| node_HWD (RM44) | 176 x 440 x 468 | mm | spec | vendor feed (RECON) | ref/rack/node/rm44_front_A.jpg | SilverStone RM44 4U GPU chassis · 4U nominal 177.8 -> 1.8mm reveal |
| node_face | full triangular-perforation mesh door + center lock | axis | high | rm44_front_A.jpg | ref/rack/node/ | REAL 1600px dead-front photo (driver-inspected) · mesh reads through to interior darkness |
| switch_HWD (CRS354) | 44.3 x 443 x 297 | mm | spec | mikrotik-crs354-dimensions-cad.pdf | ref/rack/switch/ | official CAD · +7mm ear projection · WHITE chassis (real photo crs354_sth_front.jpg) |
| switch_face | 48x RJ45 grouped grid + 4x SFP+ + 2x QSFP+ | axis | high | crs354_sth_front.jpg | ref/rack/switch/ | EXIF-verified straight-on photo, 800x483 |
| ups_HWD (SMT1500RM2UC) | 86 x 432 x 477 | mm | spec | smt1500rm2uc-datasheet.pdf | ref/rack/ups/ | official Schneider datasheet · face layout from CGI + 440px photo (tone LOW-CONF, ledger-flagged) |
| blanking_1U | 483 x 45 | mm | spec | apc-ar8136blk-blanking-spec.pdf | ref/rack/accessories/ | toolless plastic class · photos blankb1_*.jpg (1500px, real) |
| duct_1U_HWD | 43.7 x 482.6 x 67.7 | mm | spec | tripplite-srcableduct1u-spec.pdf | ref/rack/accessories/ | finger-duct cable manager |
| pdu_0U_HWD | 1829 x 56 x 51 | mm | spec | apc-ap8868-pdu-spec.pdf | ref/rack/accessories/ | vertical zero-U · rear channel · likely invisible in the front orbit |
| cage_nut_body | ~13.5 x 13.5 | mm | med | RECON (RS PRO/ITA/Chatsworth) | ref/rack/accessories/cagenut_single_macro.jpg | M6 in 9.5 sq hole · macro photo for texture |

### RACK FILL MAP · first-class artifact (bottom -> top, U1 datum at rail bottom)

Grader-ruled archetype: person-owned, homelab-real. Populated U1-U21, honest empty rails
U22-U42 (real homelabs grow into oversized racks · shows the square-hole rail signature).
No two adjacent units identical; unit types alternate; variance system (LED states, seating
jitter, wear) is the assembly wave's Problem-1 system.

| U range | occupant | face H (mm) | note |
|---|---|---|---|
| U1-U2 | APC SMT1500RM2UC UPS | 86.0 | the iconic bottom-of-rack · LCD lit |
| U3 | 1U blanking panel | 43.7 | |
| U4 | EMPTY (open rails) | · | deliberate gap · square holes + cage nuts read |
| U5-U8 | RM44 GPU node A | 176.0 | power LED on |
| U9 | 1U finger duct | 43.7 | patch slack |
| U10-U13 | RM44 GPU node B | 176.0 | LED off (idle/earning states differ) |
| U14 | 1U blanking panel | 43.7 | |
| U15-U18 | RM44 GPU node C | 176.0 | LED on · sub-degree seating jitter differs per node |
| U19 | 1U finger duct | 43.7 | cords up to the switch |
| U20 | MikroTik CRS354 (WHITE) | 44.3 | sparse random port LEDs · the one white face |
| U21 | 1U blanking panel | 43.7 | |
| U22-U42 | EMPTY rails | · | 21U honest headroom · rails, cage nuts, side channels visible |

Front door: OPEN or REMOVED in the portraits (fill must read). Zero-U PDU + rear cabling:
rear channel, modeled ONLY if the locked orbit shows them (front-facing doctrine).

### RM44 node face · pixel rows (rm44_front_A.jpg, anchor W=440mm, 3.480 px/mm)

| parameter | value | unit | conf | source | evidence | note |
|---|---|---|---|---|---|---|
| mesh_tri_period_H | 2.87 | mm | high | rm44_front_A.jpg | rack-rm44-mesh-crop.png | 2D FFT autocorr: fundamental 10px, half-period 5px (alternating up/down triangles) |
| mesh_row_pitch | 2.59 | mm | high | rm44_front_A.jpg | rack-rm44-mesh-crop.png | 9px row; full V repeat 5.17mm (up-row/down-row) · equilateral check 0.87x2.97 OK |
| mesh_open_fraction | ~0.5 | frac | LOW | rm44_front_A.jpg | rack-rm44-mesh-crop.png | threshold-circular read (shadowed web counts dark) · geometric open at web 0.4-0.5mm = 0.33-0.40 · refine against macro at part wave |
| lock_center | face-center x · ~9 dia | mm | med | rm44_front_A.jpg | rack-rm44-front-regions.png | x offset -0.8mm from center; z ~37mm below silhouette top (CONTAMINATED · see autopsy) |
| face_height_read | 199.2 vs spec 176 | mm | autopsy | rm44_front_A.jpg | rack-rm44-front-regions.png | +23mm = the visible foreshortened TOP LID plane in the silhouette · NOT a scale error (W anchor clean) · lock z below FACE top ~ 37-23 = ~14mm APPROX |
| door_seam | ~34 below silhouette top | mm | low | rm44_front_A.jpg | rack-rm44-front-regions.png | strongest upper horizontal gradient · re-derive after top-face contamination split |

## R0.2 gate AUTOPSY (2026-07-05) · false-FAIL from a drifted patch box, corrected
Commit a5564a4 (R0.2 post widening) shipped with a message claiming "rack_verify PASS" · it did
NOT: powder_black read L58.2 dE42 FAIL. Cause: the wider 45mm post inner edge moved into the
(0.655..0.683) patch band, sampling the bright post face, not the L16 rail flange. Material and
tone UNCHANGED · a measurement artifact, the same class as the gate-5a +6 autopsy. FIX: patch box
re-derived to x0.618-0.640 (clean flange, probed L15.8 · on the L16 pin) in rack_verify.py.
Post-fix gate: powder_black on-pin, ALL PASS. Process note: R0.2's commit message was wrong to
claim PASS pre-verified · this corrective commit sets the record straight (the geometry was fine).

## W0.5 · hole-size verification (2026-07-05) · source CORRECT, render resolution-limited
SQ_HOLE = 9.5mm exact in build_rack.py (EIA-310 square cage-nut hole) · HOLE_OFF (6.35, 22.25,
38.10) = the exact per-U EIA hole-center offsets. The frame-front preview measured ~8.5mm/hole, but
at 0.944 px/mm a 9.5mm hole is ~9px and AA/threshold shrinks the dark region ~1px · a measurement
floor, NOT a geometry error (confirmed against the source constant). VERDICT: hole size PASS.
U-tick strip DEFERRED to the close-shot pass (with the hinge hardware): blank ticks on black rails
do not read at frame distance, and adding raised flange geometry risks re-contaminating the fragile
powder_black patch (R0.2/W0.4 lesson). Both belong on a dedicated 3/4 or 4K-detail hardware box.

## SP10 · foam cell frequency · METHOD NOTE (2026-07-05 · no pin yet · do not guess one)
Auto-measurement attempt failed for a mechanical reason worth recording: sth_front-1/2 are
VERTICALLY oriented (the 150mm axis runs vertical), so a horizontal body-width scan measures
the 50.5mm face height, not the anchor axis. Correct method for the executor:
1. Read the reference, choose the foam crop BY EYE, save the crop to
   render/measure_evidence/sp10/ (evidence discipline).
2. Anchor px/mm on the FOAM FIELD SHORT AXIS = 46.34mm (spans nearly the full strip width in
   the vertical photos · unambiguous edges) or on a plateau (30x40mm) if sharper.
3. FFT/autocorr radial peak on the crop -> pitch px -> mm. Two references minimum (sth_front-1
   + cl_front-foam) per the flip-flop guard · this is the LIVE cell-scale case.
4. Same measurement on render/portraits-raw/dgx-spark-front.png (anchor: foam field 148.02mm
   between tab inner edges) · current built pitch is 1.62mm by construction; the loop-18 panel
   says the REAL reads higher-frequency/crisper ("crisp chaotic cells" vs "mushy low-frequency")
   · expect the real pitch to come in UNDER 1.62mm and/or the crispness delta to be voxel
   smoothing (0.14mm) rather than pitch · measure before deciding which knob.
Failed-attempt evidence patches: render/measure_evidence/sp10/*_patch.png (mis-cropped · kept
as a what-not-to-do exhibit).

## SP10 · measurement session 2 (driver, by-eye crops) · PIN STILL OPEN · partials + autopsy
AUTOPSY of session-1 note: sth_front-1 is HORIZONTAL (clean straight-on front, face 150mm spans
x277..880 = 603px -> 4.02 px/mm), not vertical · the vertical strip is cl_front-foam. Corrected.
Partial results (both CAVEATED, do not pin from these):
- real sth_front-1 foam crop (480,380)-(750,540): autocorr first peak 4px = 1.00mm BUT the peak
  sits at the search floor of a small 4 px/mm JPEG crop · unreliable. DRIVER EYE READ of the same
  photo: cells 8-16px = 2-4mm, coarse, HIGH contrast (deep black voids, bright crisp struts) ·
  the eye and the autocorr disagree, which is exactly why the pin waits for a better source.
- render raw front: body auto-edge FAILED again (rim glow beats threshold 30 across the band ·
  use threshold on a y-band above the floor line with V>60, or anchor on the foam field
  148.02mm between tab inner edges). Uncorrected pitch readback 32px · with a plausible
  ~22 px/mm that is ~1.45mm vs the built 1.62 (autocorr quantization) · consistent, not exact.
- contrast (std/mean): real 0.517 vs render 0.502 on these crops · closer than the panel language
  implied, but the render crop was scale-mismatched · re-run at matched px/mm.
REQUIRED NEXT (executor): cl_front-foam is the high-resolution source · anchor on the strip's
50.5mm width, crop foam away from plateaus, re-run; second source sth_front-1 with a LARGER crop
(x420..800, y360..560) at native res. Two agreeing numbers -> pin cells/mm + contrast target,
then ONE bounded change (candidates: pitch, voxel 0.14 crispness, strut albedo contrast).
Evidence crops: render/measure_evidence/sp10/*_v2*.png

## SP10 · PIN LANDED (session 3 · two refs + corrected render read · evidence sp10/*_v3.png)
- PITCH: real cl_front-foam 0.96mm vs real sth_front-1 1.99mm · the references DISAGREE (70%)
  and BRACKET the render (autocorr 1.39mm · built 1.62mm) -> per the flip-flop guard the pitch
  pin DOES NOT MOVE. The "low-frequency" panel read was not pitch.
- CONTRAST (std/mean on matched crops): real high-res macro 0.611 · real sth 0.490 · render
  0.495. PIN: foam strut-void contrast target 0.60 +/- 0.05 measured on the front raw at
  ~23 px/mm. The render is ~23% flatter than the best reference · THIS is the "mushy vs crisp"
  delta. Knob: material-only (AO depth / void darkness / crest brightness), patch MEAN held to
  the spark_foam tone pin (gate arbitrates, as in the L9-L20 foam history).

## SP10 · PIN AUTOPSY (driver, after 3 attempts + instrument audit) + REVISED acceptance
Attempts: ao/base compensated (0.482 flat), ao 0.86 + base down (0.514), + crest brighten (0.513).
Instrument audit: scale-matching changes nothing (0.513 -> 0.514 downsampled) · the metric is fine.
AUTOPSY of the 0.60 pin: it was measured on cl_front-foam · a BRIGHT-STUDIO hard-lit macro (blown
specular glints, crushed voids) · while the render runs the dark-hero portrait rig. A contrast
statistic does not transplant across lighting regimes verbatim · this is the same class of error
the tone gate's O=-12 offset exists to absorb (bright-studio refs vs dark-hero rig), replayed on a
texture statistic. The MECHANISM: hard near-axis light multiplies crest speculars and crushes void
floors; a soft key physically cannot reproduce that ratio at the same material truth.
REVISED SP10 acceptance (material-level): matched-crop contrast >= 0.51 AND > the 0.483 pre-SP10
baseline -> attempts 2+3 PASS (0.513-0.514, +6.4% spread, deeper voids + brighter crests are also
the visually-correct direction per the macro). The BINDING arbiter for "mushy vs crisp" remains
the PR-gate panel (loop 19+) · if foam persists as a unique tell there, the next lever is
GEOMETRY sharpness (voxel 0.14 -> 0.11 crisper strut edges), not more material contrast.
