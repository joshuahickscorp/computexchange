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
| foam_field_span_long | 86.90 | mm | med | cl_front-foam.jpg | wave3-spark-front-struct.png | foam CENTER span between the two end-caps (per-row median-std band). ~SUPERSEDES foam_field_long 148.02~ AUTOPSY: the old value measured the foam as EDGE-TO-EDGE because the model assumption (full-width foam) leaked into the measurement window; the real foam is bounded by solid champagne end-caps. Check: 2x31.5 caps + 86.9 = 149.9 ~ 150 |
| endcap_width | 31.50 | mm | med | cl_front-foam.jpg | wave3-spark-front-struct.png | solid champagne end-cap at each 150-axis end (top 30.4 / bottom 32.6). Houses the recessed pill (pill center 15.2mm from end, inside the cap) |
| slot_recess_depth | 4.20 | mm | low | cl_front-foam.jpg | wave3-spark-front-struct.png | pill finger-slot pocket depth, APPROX from pill shadow extent; = builder POCK |
| foam_field_short | 45.70 | mm | med | cl_front-foam.jpg | wave3-spark-front-struct.png | foam field short axis (per-col median-std band); ~2.4mm champagne side lip each |
| foam_border_margin | 2.53 | mm | med | cl_front-foam.jpg | dgx_border.png | champagne lip, long edge (thin) |
| foam_end_band | 0.99 | mm | med | cl_front-foam.jpg | dgx_border.png | champagne band at short/pill ends |
| top_width_anchor | 150.00 | mm | anchor | cl_side-profile.jpg | dgx_top_silhouette.png | NVIDIA spec 150 mm; the one absolute this image scales from |
| top_depth_spec | 150.00 | mm | spec | cl_side-profile.jpg | dgx_top_silhouette.png | absolute from spec; this source reads 148.6 (-0.9%) |
| top_aspect_W:D_meas | 1.01 | ratio | high | cl_side-profile.jpg | dgx_top_silhouette.png | top face is square (spec 1.00) |
| top_plan_corner_R | 21.04 | mm | med | cl_side-profile.jpg | dgx_top_corner.png | footprint corner (foam-edge softens fit), rms=0.64mm |
| top_panel_width | 114.15 | mm | med | cl_side-profile.jpg | dgx_top_panel.png | recessed vent panel |
| top_panel_height | 105.06 | mm | med | cl_side-profile.jpg | dgx_top_panel.png |  |
| top_panel_inset_margin | 17.92 | mm | med | cl_side-profile.jpg | dgx_top_panel.png | frame edge -> panel, mean L/R |
| foam_cells_per_cm_A | 13.00 | cells/cm | med | storagereview_front.jpg | dgx_foam_A.png | 26 ridges / 20mm strip |
| foam_cells_per_cm_B | 14.50 | cells/cm | med | storagereview_front.jpg | dgx_foam_B.png | 29 ridges / 20mm strip |
| champagne_Lab_L | 72.52 | L* | med | storagereview_front.jpg | dgx_champ_patch.png | left rail patch |
| champagne_Lab_a | 7.78 | a* | med | storagereview_front.jpg | dgx_champ_patch.png |  |
| champagne_Lab_b | 42.78 | b* | med | storagereview_front.jpg | dgx_champ_patch.png |  |
| foam_mean_Lab_L | 38.09 | L* | med | storagereview_front.jpg | dgx_foam_patch.png | mean over foam patch |
| foam_mean_Lab_a | 1.19 | a* | med | storagereview_front.jpg | dgx_foam_patch.png |  |
| foam_mean_Lab_b | 20.50 | b* | med | storagereview_front.jpg | dgx_foam_patch.png |  |
| foam_web_Lab_L | 67.98 | L* | med | storagereview_front.jpg | dgx_foam_patch.png | wave-0 tone gate; top-quartile L of the foam patch (strut web) |
| foam_pore_Lab_L | 10.66 | L* | med | storagereview_front.jpg | dgx_foam_patch.png | wave-0 tone gate; bottom-quartile L (pore); a*0.03 b*11.13 (warm, vs render neutral · wave-5c albedo) |
| side_thickness_persp_check | 48.63 | mm | med | sth_side-1-vertical.jpg | dgx_side_silhouette.png | smooth side reads 48.6 vs spec 50.5 (-3.7%); a per-source perspective indicator, NOT the absolute (spec 50.5 governs) |
