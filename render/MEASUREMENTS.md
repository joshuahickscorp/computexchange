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
