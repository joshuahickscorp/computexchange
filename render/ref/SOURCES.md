# render/ref/SOURCES.md · reference library for the tabletop oracles

Private comparison material only. Never shipped, never a texture, never traced into a
shipped asset. Each device anchors on one known real dimension (Mac Studio front width
197 mm · DGX Spark front long edge 150 mm). Dash gate: middot only.

Device orientation (settled from the labelled multi-angle sets below):
- Mac Studio · 197 x 197 x 95 mm, sits flat. Front face 197 x 95 (2.07:1).
- DGX Spark · 150 x 150 x 50.5 mm, sits flat. FRONT and REAR are the 150 x 50.5 foam
  faces (front: two champagne pill hand-holds + logo, no ports; rear: champagne port
  strip). SIDES are smooth champagne 150 x 50.5. TOP is the 150 x 150 square lid with a
  recessed weave-vent panel. The front is a ~3:1 strip, NOT a square.

## Coverage matrix

Grade key: A = clean near-orthographic / dimensioned vector · B = usable, off-axis or
small-in-frame or busy background · C = detail/macro only · GAP = not obtained.

### Mac Studio

| view | best image | resolution | grade | note |
|---|---|---|---|---|
| front orthographic | mac-studio/apple_front.jpg | 3840x2160 | A | Apple press, dead-on front on flat ground, silhouette + ports + LED + base mesh all clean |
| side orthographic | mac-studio/dim_back-side.svg | vector | B | dimensions.com side elevation (vector exact); square PNG raster clips the right edge, vector intact |
| top / plan | mac-studio/dim_top-front.svg | vector | A | dimensions.com plan; footprint corner radius fits at rms 0.04 mm |
| three-quarter | mac-studio/apple_lifestyle_3q.jpg | 3840x2160 | B | Apple lifestyle, device small-in-frame but true 3/4; base reveal + top edge visible |
| rear | mac-studio/apple_back.jpg | 3840x2160 | A | Apple press, dead-on rear, full port field |
| bottom | GAP | · | GAP | no bottom-face image obtained. Tried: iFixit teardown News/57898 (video only, no still URLs), dimensions.com (front/side/plan only), Apple press kit ZIP (front/back/lifestyle/setup only). The intake band is captured in apple_front (measured 8.55 mm); the base_reveal_gap is NOT resolvable · the front shot floats and every 3/4 conflates the recess with the cast contact shadow. That one value needs a clean side-elevation on a surface (second documented gap). |

### DGX Spark

| view | best image | resolution | grade | note |
|---|---|---|---|---|
| front orthographic | dgx-spark/cl_front-foam.jpg | 1200x800 | A | ChargerLab, front stood vertical on clean grey gradient; both pills, foam, champagne lips |
| side orthographic | dgx-spark/sth_side-1-vertical.jpg | 1199x800 | A | ServeTheHome, smooth champagne side stood vertical, clean; short-edge cross-check |
| top | dgx-spark/cl_side-profile.jpg | 1200x800 | A | ChargerLab, square top near-ortho: recessed weave panel + exhaust slot + foam edges |
| three-quarter | dgx-spark/nv_hero_3q.png | 700x393 | B | NVIDIA official press 3/4, clean but small; macro 3/4 detail in cl_bottom-plate.jpg |
| rear | dgx-spark/nv_rear-panel.png | 2283x1219 | A | NVIDIA docs labelled rear diagram (vector-clean); photo in cl_rear-overview.jpg, sth_rear-2.jpg |
| bottom | dgx-spark/sth_bottom.jpg | 1199x800 | A | ServeTheHome underside; magnetic base + intake also in cl_bottom-intake.jpg, cl_bottom-plate.jpg |

Both devices cover all six views except the Mac Studio bottom face (documented gap, three
avenues named). No view was called unobtainable after a single query.

## File manifest

### mac-studio/
| file | origin | angle | res | use |
|---|---|---|---|---|
| apple_front.jpg | apple.com newsroom press kit (Images-of-Apple-Mac-Studio-250305.zip) | front orthographic | 3840x2160 | PRIMARY front measure: scale, corners, ports, SD, LED, base |
| apple_back.jpg | apple.com newsroom press kit | rear | 3840x2160 | rear port layout reference |
| apple_lifestyle_3q.jpg | apple.com newsroom press kit | three-quarter | 3840x2160 | base reveal + top edge in perspective |
| apple_desk-setup.jpg | apple.com newsroom press kit | 3/4 in scene | 3840x2160 | scale-in-context sanity |
| dim_top-front.svg (+ .svg.png) | dimensions.com/element/mac-studio-2022 | plan + front elevation, dimensioned vector | vector / 2400x2400 | PRIMARY plan/footprint corner radius |
| dim_back-side.svg (+ .svg.png) | dimensions.com/element/mac-studio-2022 | back + side elevation, dimensioned vector | vector / 2400x2400 | rear + side profile reference |
| wikimedia_front.jpg | Wikimedia Commons "Mac Studio (2022) front" (independent photographer B) | front 3/4-from-above | 4275x2850 | USB-C orientation TIEBREAK: ports read H/W 1.87 (vertical), agreeing with Apple's H/W 3.25 |
| legacy_front-ref.jpg | prior repo ref (StorageReview-era front) | front | 758x372 | low-res front cross-check |
| apple_LEGAL_NOTICE.rtf | Apple press kit | · | · | usage terms for the press images |

### dgx-spark/
| file | origin | angle | res | use |
|---|---|---|---|---|
| cl_front-foam.jpg | chargerlab.com DGX Spark 4TB teardown | FRONT (foam + 2 pills), stood vertical | 1200x800 | PRIMARY front measure: scale, edge R, pills, foam field |
| cl_side-profile.jpg | chargerlab.com teardown | TOP (square weave-vent lid) | 1200x800 | PRIMARY top measure: footprint corner, vent panel |
| cl_rear-overview.jpg | chargerlab.com teardown | rear (port strip in foam) | 1200x800 | rear port layout |
| cl_front-lower-logo.jpg | chargerlab.com teardown | rear port close-up | 1200x800 | port detail (filename from source was mislabelled) |
| cl_front-intake.jpg | chargerlab.com teardown | front intake detail | 1200x800 | foam intake detail |
| cl_bottom-intake.jpg | chargerlab.com teardown | bottom intake / dust filter | 1200x800 | bottom detail |
| cl_bottom-plate.jpg | chargerlab.com teardown | 3/4 macro (foam edge, pill, top vent) | 1200x800 | foam cell + edge macro |
| cl_side-matte.jpg | chargerlab.com teardown | smooth side | 1200x800 | side finish cross-check |
| sth_front-1.jpg | servethehome.com GB10 review | front (landscape, natural pose) | 1200x800 | front cross-check, pills L/R |
| sth_front-2.jpg | servethehome.com review | front detail | 1184x800 | foam detail |
| sth_side-1-vertical.jpg | servethehome.com review | SIDE (smooth, stood vertical) | 1199x800 | PRIMARY side thickness cross-check (50.5 mm) |
| sth_side-2.jpg | servethehome.com review | side | 1199x800 | side cross-check |
| sth_bottom.jpg | servethehome.com review | bottom | 1199x800 | underside reference |
| sth_rear-2.jpg | servethehome.com review | rear | 1199x800 | rear cross-check |
| storagereview_front.jpg | storagereview.com DGX Spark review | front (busy bg, hi-res foam) | 1500x1015 | PRIMARY foam cell density + champagne/foam Lab |
| nv_rear-panel.png | docs.nvidia.com/dgx/dgx-spark/hardware.html | rear labelled diagram | 2283x1219 | rear port geometry (vector-clean) |
| nv_hero_3q.png | nvidianews.nvidia.com (DGX Spark arrives) press asset | three-quarter hero | 700x393 | official 3/4 proportion reference |

## Search avenues worked (phase 0)

Manufacturer product + press (apple.com newsroom + press-kit ZIP, nvidia.com product,
NVIDIA newsroom, NVIDIA docs hardware overview) · dimensioned drawings (dimensions.com
Mac Studio vector plan/elevations) · teardowns (ChargerLab DGX Spark, iFixit Mac Studio)
· major review galleries (ServeTheHome, StorageReview) · prior-repo reference. Multiple
phrasings per device; the ChargerLab source's own filename captions were unreliable and
were re-classified against the labelled ServeTheHome and NVIDIA sets.
