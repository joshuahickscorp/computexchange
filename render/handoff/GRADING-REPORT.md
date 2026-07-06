# GRADING REPORT · photoreal renders vs reality (research + grade pass, 2026-07-06)

Role of this document: the research + grading handoff for the Opus 4.8 iteration session.
Everything here was produced by (1) exhaustive web research on the three real products (three
parallel research agents, all facts sourced with URLs) and (2) a facet-by-facet visual audit of
every render in `~/Downloads/cx-render-handoff/images/` against that research. No 3D code was
touched. The goal loop to paste into the Opus 4.8 session is at the bottom.

Method note (honesty): facts below are graded **[CONFIRMED]** (2+ independent sources or a
measured teardown), **[SOURCED]** (1 credible source), or **[UNRESOLVED]** (conflicting/absent
sources · do NOT model against these without re-research). Grades are conservative: a facet
only scores 8+ when the render would survive a side-by-side with the real product photo at that
angle. The handoff LAWS (trademark gate, dash gate, one bounded change per commit, gate-verified
tone pins, no attribution) all still apply and are not repeated per item.

---

# OBJECT 1 · NVIDIA RTX 5090 Founders Edition (PRIORITY — weakest object)

## 1.1 Sourced research (per face)

**Geometry** [CONFIRMED] · 304 × 137 mm, 2-slot ([NVIDIA](https://www.nvidia.com/en-us/geforce/graphics-cards/50-series/rtx-5090/)); thickness **40 mm** ([Overclocking.com](https://en.overclocking.com/review-nvidia-rtx-5090-founders-edition/2/), [pokde](https://pokde.net/review/nvidia-geforce-rtx-5090-founders-edition-review), [club386](https://www.club386.com/nvidia-geforce-rtx-5090-founders-edition-unboxing-and-first-look/); one 48 mm outlier rejected). Weight 1814 g ([TechPowerUp](https://www.techpowerup.com/review/nvidia-geforce-rtx-5090-founders-edition/4.html)). Stands ~17 mm proud of the 120 mm bracket. **Dual flow-through**: ultra-compact central PCB module between two end fin-stacks; 2/3 of the card is cooler ([pokde](https://pokde.net/review/nvidia-geforce-rtx-5090-founders-edition-review), [PCPer](https://pcper.com/2025/01/nvidia-geforce-rtx-5090-founders-edition-review/)); 3-PCB design with FPC link, PCIe fingers on an offset daughterboard at the bracket end ([GamersNexus](https://gamersnexus.net/gpus/nvidia-geforce-rtx-5090-founders-edition-review-benchmarks-gaming-thermals-power), [Overclocking.com](https://en.overclocking.com/review-nvidia-rtx-5090-founders-edition/2/)). Corners "more smooth" than the 4090 FE ([TPU](https://www.techpowerup.com/review/nvidia-geforce-rtx-5090-founders-edition/4.html)).

**Fans** [CONFIRMED] · **two identical 115 mm fans, BOTH on one face** (one per end), each with **7 large wide-chord thick sickle blades** ([LanOC](https://lanoc.org/review/video-cards/nvidia-rtx-5090-founders-edition?start=2), [club386](https://www.club386.com/nvidia-geforce-rtx-5090-founders-edition-unboxing-and-first-look/), [Overclocking.com](https://en.overclocking.com/review-nvidia-rtx-5090-founders-edition/2/)); **blade tips fused into an outer rim ring** ("a ring around it" — [LanOC](https://lanoc.org/review/video-cards/nvidia-rtx-5090-founders-edition?start=2)); hub caps plain, flat, **very dark grey** ([LanOC](https://lanoc.org/review/video-cards/nvidia-rtx-5090-founders-edition?start=2)). Counter-rotation: **no evidence — both fans identically clocked** (trap denied). Air: intake face → black fins → exits the opposite face + angled top/bottom vents ([TPU hands-on](https://www.techpowerup.com/330857/nvidia-geforce-rtx-5090-founders-edition-hands-on-taken-apart), [HWCooling](https://www.hwcooling.net/en/nvidia-geforce-rtx-5090-fe-review-next-level-gaming/)).

**Front (fan face)** · two fan windows flanking a solid central module; X accents wrap the windows and converge center — the "infinity loop" motif ([LanOC](https://lanoc.org/review/video-cards/nvidia-rtx-5090-founders-edition?start=2)); "RTX 5090" text was **moved off the front to the rear etching** — front is clean ([club386](https://www.club386.com/nvidia-geforce-rtx-5090-founders-edition-unboxing-and-first-look/)). **Two-tone direction: gunmetal OUTER frame + X, BLACK center panels + fans** ([club386](https://www.club386.com/nvidia-geforce-rtx-5090-founders-edition-unboxing-and-first-look/), [LanOC](https://lanoc.org/review/video-cards/nvidia-rtx-5090-founders-edition?start=2)).

**Rear (window face)** [CONFIRMED] · **two open through-windows showing the fin stack directly** — no full backplate; fins **black, vertical, concave (dished) where the fans sit, gradually curving** ([HWCooling](https://www.hwcooling.net/en/nvidia-geforce-rtx-5090-fe-unboxing-ultimate-design/2/), [LanOC](https://lanoc.org/review/video-cards/nvidia-rtx-5090-founders-edition?start=2), [Overclocking.com](https://en.overclocking.com/review-nvidia-rtx-5090-founders-edition/2/)); you can see heat pipes through the card with a light behind it ([Gizmodo](https://gizmodo.com/the-nvidia-rtx-5090-is-a-thick-slab-of-graphics-processing-promise-2000552978)). Central module: etched NVIDIA cartouche + "RTX 5090" printed in the top section + a **triangle-shaped sliding service cover with a machined groove** ([LanOC](https://lanoc.org/review/video-cards/nvidia-rtx-5090-founders-edition?start=2), [TPU](https://www.techpowerup.com/review/nvidia-geforce-rtx-5090-founders-edition/5.html)). Illuminated X accents repeat on this face.

**Top edge** · backlit **GEFORCE RTX** wordmark on the solid central section, toward the tail half ([club386](https://www.club386.com/nvidia-geforce-rtx-5090-review/), [Overclocking.com](https://en.overclocking.com/review-nvidia-rtx-5090-founders-edition/12/)); **16-pin 12V-2×6, RECESSED into the shroud and ANGLED (~45°, leaning tailward)** at the central module ([HotHardware](https://hothardware.com/reviews/nvidia-geforce-rtx-5090-review), [club386](https://www.club386.com/nvidia-geforce-rtx-5090-founders-edition-unboxing-and-first-look/), [LanOC](https://lanoc.org/review/video-cards/nvidia-rtx-5090-founders-edition?start=2)); exhaust leaves the top windows at 20–30° via angled covers ([GamersNexus schlieren](https://gamersnexus.net/gpus/nvidia-geforce-rtx-5090-founders-edition-review-benchmarks-gaming-thermals-power)).

**Bottom edge** · primary intake side when horizontal; the two black center sections carry two vent slots; shroud wraps tight to the PCB ([LanOC](https://lanoc.org/review/video-cards/nvidia-rtx-5090-founders-edition?start=2)).

**Bracket end** [CONFIRMED] · **3× DisplayPort 2.1b + 1× HDMI 2.1b** (HDMI at the bottom, orientations reversed vs prior gen); **bracket is SOLID — NO vent perforations** ("doesn't require venting due to the flow-through design"), dark matte anti-fingerprint finish, 2 screws ([NVIDIA](https://www.nvidia.com/en-us/geforce/graphics-cards/50-series/rtx-5090/), [HotHardware](https://hothardware.com/reviews/nvidia-geforce-rtx-5090-review), [HWCooling](https://www.hwcooling.net/en/nvidia-geforce-rtx-5090-fe-unboxing-ultimate-design/2/), [LanOC](https://lanoc.org/review/video-cards/nvidia-rtx-5090-founders-edition?start=2)).

**LEDs** [CONFIRMED ×4 — flip-flop guard STAYS: the panel's "no LEDs" claim remains wrong] · illuminated zones: **X/V accents around the fan windows on BOTH faces + top-edge GEFORCE RTX wordmark; all static white, non-RGB** ([OC3D](https://overclock3d.net/reviews/gpu_displays/nvidia-rtx-5090-founders-edition-review/3/), [TheFPSReview](https://www.thefpsreview.com/2025/01/23/nvidia-geforce-rtx-5090-founders-edition-video-card-review/), [pokde](https://pokde.net/review/nvidia-geforce-rtx-5090-founders-edition-review), [LanOC](https://lanoc.org/review/video-cards/nvidia-rtx-5090-founders-edition?start=2)); character = soft LED strips along the X arms "converging in the centre" + tracing the window rims — one continuous loop figure ([club386](https://www.club386.com/nvidia-geforce-rtx-5090-founders-edition-unboxing-and-first-look/)). One outlier ("only the wordmark lights") rejected 4-to-1.

**Materials** · official colour "**Dark Gun Metal**" ([Best Buy listing](https://www.bestbuy.com/product/nvidia-geforce-rtx-5090-32gb-gddr7-founders-edition-graphics-card-dark-gun-metal/J3GWYHGPCP)); darker than the 4090 FE, no silver sides ([Gizmodo](https://gizmodo.com/the-nvidia-rtx-5090-is-a-thick-slab-of-graphics-processing-promise-2000552978)); all metal ([LanOC](https://lanoc.org/review/video-cards/nvidia-rtx-5090-founders-edition?start=2)). PBR synthesis [UNCONFIRMED, derived]: shroud albedo ≈0.09–0.13 linear, metallic 0.7–1.0, rough 0.45–0.60 isotropic micro-grain; black centers/fans 0.03–0.05, rough 0.5–0.7; fins near-black w/ slight along-fin anisotropy; brighter machined chamfer highlights (r≈0.3). Matches the worktree spec's correction direction (fans down to 0.05).

**Reference galleries**: [TPU pictures](https://www.techpowerup.com/review/nvidia-geforce-rtx-5090-founders-edition/4.html) + [teardown](https://www.techpowerup.com/review/nvidia-geforce-rtx-5090-founders-edition/5.html), [LanOC layout](https://lanoc.org/review/video-cards/nvidia-rtx-5090-founders-edition?start=2), [HWCooling unboxing](https://www.hwcooling.net/en/nvidia-geforce-rtx-5090-fe-unboxing-ultimate-design/2/), [GN teardown video](https://www.youtube.com/watch?v=IyeoVe_8T3A), NVIDIA + Best Buy pages (JS-gated galleries — scrape in a browser).

## 1.2 Facet grades (render vs reality)

Renders audited: `rtx5090fe/gpu-{front,q34,rear,rearq34,side,top,bottom,macro}.png`, rig + trio frames.
Blade count in our render measured by direct count on a 2× crop: **11 blades. Real: 7.**

| # | Facet | Grade | 10/10 truth (source above) | Path to 10/10 (bounded, prioritised) |
|---|---|---|---|---|
| G1 | Fan blade count/chord | **2/10** | 7 large wide-chord thick sickle blades per 115 mm fan | Rebuild `_fan_blades`: 7 blades, ~2× chord width, keep the lofted airfoil approach |
| G2 | Blade-tip rim ring | **1/10** | Blade tips fuse into a smooth outer ring band | Add the rim ring to the fan bmesh (one revolve, joins all tips) |
| G3 | Hub cap | **3/10** | Flat, plain, very dark grey cap | Flatten the dome; drop albedo to ~0.05; no marking |
| G4 | Two-tone direction | **2/10** | BLACK center panels + fans; gunmetal outer frame + X | Swap the center-module material to the black 0.03–0.05 set; frame stays gunmetal — currently inverted |
| G5 | X accent geometry | **3/10** | One integrated wide X whose arms wrap the fan-window rims → continuous infinity loop; flush, no overlap step | Rebuild X as a single flush piece; extend arms to meet the window rims; kill the two-bar overlap |
| G6 | Illumination motif | **6/10** | Rings + X + top wordmark, static white — PRESENT and correct in kind (flip-flop guard: LEDs are REAL) | Merge ring-glow and X-glow into one continuous traced loop; thin the strips; keep co-located point lights (they solved clip) |
| G7 | Rear through-windows | **1/10** | Two OPEN windows showing black vertical concave curving fins; light passes through | Cut the windows open; instance a curved fin array behind each; verify a backlight actually shows through |
| G8 | Rear central module | **2/10** | Blank etched cartouche + blank "RTX 5090" plate + triangle sliding service cover w/ groove | Add the 3 blank features (trademark gate) after G7 |
| G9 | Top wordmark strip | **3/10** | Thin backlit GEFORCE RTX strip on the central third (blank per gate) | Shrink the current giant glowing tile to a text-height strip at the sourced position |
| G10 | Power connector | **2/10** | 12V-2×6 recessed in a shroud scallop, angled ~45° tailward, at the central module | Replace the protruding cylinder with a recessed angled socket + scallop |
| G11 | Edge vents | **4/10** | Angled exhaust covers over the top/bottom window spans; 2 slots in the black center sections | Add angled louver geometry to both thin edges |
| G12 | I/O bracket | **3/10** | SOLID dark matte bracket (no vents), 3×DP + HDMI-at-bottom reversed, 2 screws | Rebuild bracket solid; correct port stack; remove any vent notches (rig frames show notched brackets) |
| G13 | Proportions | **7/10** | 304×137×40, corners smoother than 4090 | Ortho check 137:40 = 3.43:1 on a side render; round the shroud corners slightly |
| G14 | Shroud cut-lines | **4/10** | Sculpted panel splits per TPU/LanOC galleries; angled shroud walls into the windows | One panel-split pass per face, traced from the galleries |
| G15 | Shroud/fin materials | **5/10** | Dark Gun Metal PBR (values above); black fins slightly anisotropic along length | Apply the dossier PBR set; keep the tone gates as arbiter |
| G16 | Fan blade material | **4/10** | Black plastic, glossy coat, edge highlights only — no silver faces | Drop blade albedo to 0.03–0.05 + coat 0.35 (already spec'd in RTX5090FE-SPEC.md); kill the foil-like facet highlights (needs G1's smoother geometry too) |
| G17 | 360 audit coverage | **2/10** | Every face auditable | `gpu-bottom.png` is solid black and the rig side is near-black — re-shoot with audit fill |
| G18 | See-through read | **3/10** | With light behind, fins + heat-pipe silhouettes visible through the card | After G7: place a dim rim light behind the rig cards so windows read open |

**Overall RTX 5090 FE: 3.5/10** — confirmed the weakest object; the miss is concentrated exactly
where the owner said: FE-specific geometry.
**Single highest-leverage fix: G1+G2 — the 7-blade + rim-ring fan rebuild.** The fans dominate every
money shot (12 of them face the camera in the rig front); at 11 thin ringless blades the card is
recognisably NOT a 5090 FE to any owner. Then G7 (open rear windows), then G4 (two-tone inversion).

---

# OBJECT 2 · Apple Mac Studio (2025 · M4 Max / M3 Ultra · chassis unchanged since 2022)

## 2.1 Sourced research (per face)

⚠️ **SPEC-FILE CORRECTION REQUIRED**: `model-refinement/render/MAC-STUDIO-360-SPEC.md` claims the rear
vent is "the large CIRCULAR perforated exhaust vent" and puts the AC inlet at the far left — **both
wrong** against Apple's own press photography (below). Fix the spec file FIRST, then the builder.
Its `[x] rear … reads as the real Mac Studio back` tick is a false completion — untick it.

**Geometry** [CONFIRMED] · 197 × 197 × 95 mm (7.7×7.7×3.7 in), no taper; M4 Max 2.74 kg / M3 Ultra 3.64 kg (copper heatsink) ([Apple specs](https://www.apple.com/mac-studio/specs/), [support doc](https://support.apple.com/en-us/122211)). Single aluminium extrusion ([Apple Newsroom 2022](https://www.apple.com/newsroom/2022/03/apple-unveils-all-new-mac-studio-and-studio-display/)). **Vertical corner radius ≈38 mm** (derived: circle fit on [iFixit bottom photo](https://guide-images.cdn.ifixit.com/igi/AHNqBC6RI1y1ZjhM.huge), scale 197 mm = 1098 px). Top-edge = soft roll ≈4–6 mm, not a chamfer (derived from [Apple 2025 front press image](https://www.apple.com/newsroom/images/2025/03/apple-unveils-new-mac-studio-the-most-powerful-mac-ever/article/Apple-Mac-Studio-front-250305_big.jpg.large.jpg)).

**Front** [CONFIRMED] · M4 Max: 2× USB-C (10 Gb/s); M3 Ultra: 2× TB5; both + SDXC UHS-II ([Apple specs](https://www.apple.com/mac-studio/specs/)). **The two front ports are mounted VERTICALLY (portrait pills ≈3×9 mm) — vertical is CORRECT** ([Apple 2022 front](https://www.apple.com/newsroom/images/product/mac/standard/Apple-Mac-Studio-front-220308_big_carousel.jpg.large.jpg), [iFixit trio](https://valkyrie.cdn.ifixit.com/media/2022/03/24204553/Mac_Studio_1.jpg)). Derived mm map (facing front, from left edge; scale 197 mm = 621 px on the 2025 press front): port centers ≈**32 / 47 mm** (pitch 15 mm) → SD slot spans ≈**61–87 mm** (26 mm, rounded ends) → white status LED ≈**165 mm** (Ø≈2 mm, steady white awake/sleep — [Apple guide](https://support.apple.com/guide/mac-studio/take-a-tour-apd0fd69f4be/mac)). Row sits **LOW: centers ≈25 mm above the desk** (~27% of height), not vertically centred. No text/glyphs on the front.

**Rear** [CONFIRMED — overrides the worktree spec] · Exhaust = **rectangular hex-packed perforation field ≈173 mm wide × 53 mm tall** (≈12 mm side margins, starts ≈5 mm below the top roll, ends ≈37 mm above the desk), hole Ø≈1.3–2 mm at ≈2 mm pitch, ~25–30 staggered rows ≈2,000–2,600 holes (derived from [Apple 2025 back press image](https://www.apple.com/newsroom/images/2025/03/apple-unveils-new-mac-studio-the-most-powerful-mac-ever/article/Apple-Mac-Studio-back-250305_big.jpg.large.jpg); Apple states only "over 4,000 perforations on the back and bottom" combined — [Newsroom](https://www.apple.com/newsroom/2022/03/apple-unveils-all-new-mac-studio-and-studio-display/)). **Port row (facing rear, L→R, centers ≈23 mm above desk, derived mm from left edge):** 4× TB5 vertical pills at ≈30/40/50/60 → RJ-45 ≈74 → **AC inlet ≈98.5 = DEAD CENTER** (black 3-lobe cloverleaf recess) → 2× USB-A vertical ≈120/132 → HDMI horizontal ≈149 → 3.5 mm jack ≈166 → **power button ≈180** (flush ~11 mm circle, far right) — corroborated by the [2022 back image](https://www.apple.com/newsroom/images/product/mac/standard/Apple-Mac-Studio-back-220308_big_carousel.jpg.large.jpg) and ["power in the middle and a power button to one corner"](https://appleinsider.com/articles/25/04/01/2025-mac-studio-review-one-clear-purchase-choice-for-most-buyers). Gray printed glyphs above the row (skip: trademark gate). No rear antenna window (antennas live in the bottom plastic ring — [Instrumental teardown](https://instrumental.com/resources/teardown/change-notice-mac-studio-teardown/)). Airflow: bottom intake → rear exhaust, double-sided blowers ([Newsroom](https://www.apple.com/newsroom/2022/03/apple-unveils-all-new-mac-studio-and-studio-display/)).

**Bottom** [CONFIRMED] · concentric (derived from [iFixit bottom photo](https://guide-images.cdn.ifixit.com/igi/AHNqBC6RI1y1ZjhM.huge), 0.179 mm/px): flat central disc Ø≈142 mm (embossed wordmark + regulatory — model blank) → foot-ring channel Ø≈148 centerline (~6.5 mm wide, rubber-like ring hiding 4× T10 screws — [iFixit guide](https://www.ifixit.com/Guide/Mac+Studio+2023+Bottom+Cover+Replacement/165048)) → **intake annulus Ø157–179 mm**, "more than 1500 holes drilled at a 45 degree slope", color-matched plastic antenna ring at the perimeter ([Instrumental](https://instrumental.com/resources/teardown/change-notice-mac-studio-teardown/)) → clean skirt. Oblong Kensington-style hole near one corner ([MacRumors](https://www.macrumors.com/2022/03/18/lock-adapter-mac-studio-soon/)). Stance: rides the rubber ring, skirt clears the desk ≈3–5 mm. **KEY RENDER FACT: the intake annulus's outer edge is ~9 mm inboard of the side wall → the perforations are NOT visible from a straight-on front/side view — only the smooth skirt + shadow gap.**

**Top** [CONFIRMED] · bare aluminium except the centered Apple logo (≈50 mm wide, glossier darker mirror-tone inlay vs the matte bead-blast — [Gizmodo](https://gizmodo.com/apple-mac-studio-review-a-hefty-little-powerhouse-2000574271), [iFixit trio](https://valkyrie.cdn.ifixit.com/media/2022/03/24204553/Mac_Studio_1.jpg)). **No vents on top** (perforations are "back and bottom" only — [Newsroom](https://www.apple.com/newsroom/2022/03/apple-unveils-all-new-mac-studio-and-studio-display/)).

**Sides** [CONFIRMED] · completely clean; the enclosure's only split line hides underneath ([Instrumental](https://instrumental.com/resources/teardown/change-notice-mac-studio-teardown/)).

**Materials** · 100% recycled aluminium, Silver, bead-blasted ([Apple specs](https://www.apple.com/mac-studio/specs/), [Instrumental](https://instrumental.com/resources/teardown/change-notice-mac-studio-teardown/)). PBR start: F0 ≈(0.916, 0.923, 0.924), metallic 1.0 ([physicallybased.info](https://physicallybased.info/)); bead-blast → GGX roughness ≈0.35–0.45, **isotropic — no brushed anisotropy**; logo plate near-mirror (r≈0.05–0.1), slightly darker. The 38 mm corners read as wide soft vertical highlight bands; the top roll as a tight bright rim.

## 2.2 Facet grades (render vs reality)

Renders audited: `mac-studio/{front,q34,side,detail}.png`, `audit-studio-{rear-audit,rearq34,side,fixcheck}.png`, trio frames.

| # | Facet | Grade | 10/10 truth (source above) | Path to 10/10 (bounded, prioritised) |
|---|---|---|---|---|
| M1 | Rear exhaust vent | **2/10** | ≈173×53 mm rounded-rect hex-packed field, 5 mm under the top roll; NOT a circle | Replace the circular disc with the rect field (per-mm map above); this also fixes the worktree spec error |
| M2 | Rear port row | **3/10** | Distinct shapes at exact mm: TB×4 pills, RJ-45, cloverleaf AC dead-center, USB-A×2, HDMI, jack, 11 mm power button far right | Rebuild the row on the mm map at 23 mm height; one port-shape class per commit; lit-angle integrity check after each boolean (LAW) |
| M3 | Rear row placement | **3/10** | A row IN the face, 23 mm up; render cuts notches into the bottom edge | Fixed by M2 (move row up; restore the aluminium below) |
| M4 | Front port layout | **9/10** | Ports 32/47 mm, SD 61–87 mm, LED 165 mm, row 25 mm up — render matches within ~3 mm | Nothing structural; keep |
| M5 | Front port shape | **7/10** | Vertical 3×9 mm pills with visible tongue — VERTICAL IS CORRECT (trap denied) | Widen pills to true 3×9 proportion (currently too thin), add inner tongue geometry |
| M6 | Front SD slot + LED | **8/10** | 26 mm slot w/ rounded ends + Ø2 mm steady-white LED | Add slot inner blade; confirm LED Ø2 mm and subtle emission |
| M7 | Base/skirt read (front+side) | **4/10** | Smooth skirt + 3–5 mm shadow gap; intake holes NOT visible from front/side | Remove the wrapped perforation band from the lower front/side; keep only skirt curve + gap + soft AO |
| M8 | Bottom face | **2/10** (unbuilt/unaudited) | Ø142 disc + Ø148 foot ring + Ø157–179 45°-hole annulus + Kensington oblong | Build the concentric bottom; render a low bottom-audit angle |
| M9 | Top face | **6/10** | Centered ≈50 mm glossy logo inlay on matte | OWNER DECISION (trademark gate vs Apple silhouette): blank glossy ~50 mm plate or leave bare; currently bare = reads empty vs real |
| M10 | Corner/edge geometry | **7/10** | 38 mm plan-view corner radius; 4–6 mm top roll; no taper | Ortho-verify both radii numerically; adjust if off by >15% |
| M11 | Proportions | **9/10** | Exact 197:95 slab, no taper | Ortho front render pixel check (197:95 = 2.074:1) |
| M12 | Side faces | **8/10** | Perfectly clean walls | Fixed by M7 (band removal); otherwise correct |
| M13 | Material/finish | **8/10** | Bead-blast isotropic r≈0.35–0.45, F0 0.92; no anisotropy | Verify no anisotropy is set; keep `studio_alu` Lab pin; slight roughness lift if gate holds |
| M14 | Grounding/stance | **7/10** | Rides an invisible rubber ring; 3–5 mm skirt clearance + soft shadow | Set the clearance to 3–5 mm true scale; contact-shadow only under the ring |
| M15 | Rear/side audit lighting | **6/10** | Every face auditable in a lit frame | Brighter audit fills (audit frames only), rear especially |

**Overall Mac Studio: 6/10.** Front face is launch-grade (9); the rear face is the opposite —
wrong vent shape, wrong port order, wrong row placement — and the bottom is unbuilt.
**Single highest-leverage fix: M1+M2 — rebuild the rear face to the Apple press-photo mm map.**
Note for the loop: the front-port VERTICAL orientation and the SD/LED layout were verified correct —
do NOT "fix" them horizontal (flip-flop guard; the real trap runs the other way).

---

# OBJECT 3 · NVIDIA DGX Spark (Founders Edition, GB10)

## 3.1 Sourced research (per face)

**Geometry** · 150 × 150 × 50.5 mm, 1.2 kg official ([NVIDIA hardware docs](https://docs.nvidia.com/dgx/dgx-spark/hardware.html)); ChargerLAB measured ~51.2 mm thick, ~1,250 g ([ChargerLAB teardown](https://www.chargerlab.com/teardown-of-nvidia-dgx-spark-4tb/)). Squat square slab, **crisp near-sharp arrises: ~1 mm chamfer/radius on the long edges, 1–2 mm corner radii on the top face** (photo-derived from [STH top](https://www.servethehome.com/wp-content/uploads/2025/10/NVIDIA-DGX-SPARK-Top.jpg) + [ChargerLAB 3/4](https://www.chargerlab.com/wp-content/uploads/2026/04/2026043007135332.jpg); exact radii unpublished). CNC-machined aluminium shell wrapping top + sides as one visual unit; horizontal orientation only ([Quick Start Guide p.6](https://www.nvidia.com/content/dam/en-zz/Solutions/dgx-spark/DGX-Spark-Quick-Start-Guide.pdf)).

**Front** [CONFIRMED] · full-face genuine open-cell **metal foam** (not machined): "metal foam decoration" ([ChargerLAB](https://www.chargerlab.com/teardown-of-nvidia-dgx-spark-4tb/)); "looks like foam, but it actually is hard and allows airflow" ([ServeTheHome](https://www.servethehome.com/nvidia-dgx-spark-review-the-gb10-machine-is-so-freaking-cool/)); "both front and rear panels employ metal foam" ([LMSYS](https://www.lmsys.org/blog/2025-10-13-nvidia-dgx-spark/)). Pore scale ≈1.5–3 mm (photo-derived, [STH logo macro](https://www.servethehome.com/wp-content/uploads/2025/10/NVIDIA-DGX-SPARK-NVIDIA-Logo-1.jpg)). The foam panel is a **FLAT INSET panel framed by narrow (~3–4 mm) flat champagne shell bands at the left/right edges — it does NOT wrap the vertical corners**; dead side-on, the foam is fully hidden ([STH front](https://www.servethehome.com/wp-content/uploads/2025/10/NVIDIA-DGX-SPARK-Front-2.jpg), [ChargerLAB side](https://www.chargerlab.com/wp-content/uploads/2026/04/2026043007135240.jpg)). **Two hand-hold pods are REAL** [CONFIRMED — flip-flop guard: the forensic panel that called them "fabricated ovals" was wrong]: solid champagne rounded-rect pods inset in the foam near each edge, each with an elongated concave scoop, ≈24 × 33 mm (photo-derived), "miniature hand-hold cutouts" ([StorageReview](https://www.storagereview.com/review/nvidia-dgx-spark-review-the-ai-appliance-bringing-datacenter-capabilities-to-desktops)). The **left pod carries the green NVIDIA eye + wordmark rotated 90°** ([STH macro](https://www.servethehome.com/wp-content/uploads/2025/10/NVIDIA-DGX-SPARK-NVIDIA-Logo-1.jpg)) → per trademark gate: a blank green-tinted plate + blank vertical cartouche. **No front power button, no LED anywhere on the unit** ([NVIDIA forum — owners literally check airflow by hand](https://forums.developer.nvidia.com/t/how-to-verify-if-dgx-spark-is-fully-powered-off/350509)). Air intakes with dust filters at the top + bottom exposed-foam edge gaps ([ChargerLAB](https://www.chargerlab.com/teardown-of-nvidia-dgx-spark-4tb/)).

**Rear** [CONFIRMED] · **also full-face metal foam**, with a **polished champagne rounded-rect I/O plate inset LOW** across the face ([STH rear](https://www.servethehome.com/wp-content/uploads/2025/10/NVIDIA-DGX-SPARK-Rear-2.jpg), [LMSYS](https://www.lmsys.org/blog/2025-10-13-nvidia-dgx-spark/)). Port order left→right per NVIDIA's own labeled diagram ([Quick Start Guide p.5](https://www.nvidia.com/content/dam/en-zz/Solutions/dgx-spark/DGX-Spark-Quick-Start-Guide.pdf)): **ON/OFF button | POWER USB-C (240 W PD 3.1, DC glyph below) | USB-C ×3 (20 Gbps, DP alt-mode) | HDMI 2.1a | RJ-45 10 GbE | QSFP56 ×2 (ConnectX-7, 200 GbE)**. The USB-C ports are mounted **vertically (portrait)**; HDMI + RJ-45 horizontal; both QSFP cages share a single wide dark cutout at the right end ([STH rear-left](https://www.servethehome.com/wp-content/uploads/2025/10/NVIDIA-DGX-SPARK-Rear-Left.jpg), [rear-right](https://www.servethehome.com/wp-content/uploads/2025/10/NVIDIA-DGX-SPARK-Rear-Right.jpg)). Power button = small flush vertical pill, far left, unlit ([STH](https://www.servethehome.com/wp-content/uploads/2025/10/NVIDIA-DGX-SPARK-Rear-Left.jpg)). Power via USB-C only — no barrel jack ([ChargerLAB PSU teardown](https://www.chargerlab.com/teardown-of-the-nvidia-dgx-spark-original-240w-usb-c-power-adapter/)). No Kensington slot. **The Spark has NO USB-A ports anywhere** — the build note in `DGX-SPARK-360-SPEC.md` TODO ("11 cavities … 2x USB-A") contradicts its own spec header and NVIDIA's diagram; if the builder cut USB-A cavities they are invented and must go.

**Top** [CONFIRMED] · **completely blank matte champagne aluminium — no logo, no etching, no dark panel, no groove** ([STH top-down photo](https://www.servethehome.com/wp-content/uploads/2025/10/NVIDIA-DGX-SPARK-Top.jpg); "on the sides and top, the system is just flat" — [STH](https://www.servethehome.com/nvidia-dgx-spark-review-the-gb10-machine-is-so-freaking-cool/)). The only visual breaks: the top plate stops short of the front/rear edges, exposing recessed foam-edge strips (the filtered intakes), with tiny solid corner tabs.

**Bottom** · large rounded-square **magnetically-attached non-slip cover** (the only plastic part) acting as the foot; full-width machined intake slot with rounded ends along the front edge; regulatory text printed INSIDE the cover, not visible outside ([StorageReview](https://www.storagereview.com/review/nvidia-dgx-spark-review-the-ai-appliance-bringing-datacenter-capabilities-to-desktops), [ChargerLAB](https://www.chargerlab.com/teardown-of-nvidia-dgx-spark-4tb/), [STH bottom](https://www.servethehome.com/wp-content/uploads/2025/10/NVIDIA-DGX-SPARK-Bottom.jpg)).

**Sides** · completely clean matte champagne, no vents/seams; foam invisible dead side-on ([STH side](https://www.servethehome.com/wp-content/uploads/2025/10/NVIDIA-DGX-SPARK-Side-1-Vertical.jpg)).

**Airflow** · front intake (foam + filtered edge gaps + bottom front slot) → rear exhaust through fin stacks behind the rear foam; 2× Delta NS8CC50 fans ([ChargerLAB](https://www.chargerlab.com/teardown-of-nvidia-dgx-spark-4tb/)); NVIDIA clearance spec 10 cm front / 40 cm rear implies front→rear flow ([QSG p.6](https://www.nvidia.com/content/dam/en-zz/Solutions/dgx-spark/DGX-Spark-Quick-Start-Guide.pdf)).

**Materials/colour** · "champagne-gold aluminum alloy chassis", CNC-machined, matte top/sides ([ChargerLAB](https://www.chargerlab.com/teardown-of-nvidia-dgx-spark-4tb/)); "gold-speckled metallic finish" ([StorageReview](https://www.storagereview.com/review/nvidia-dgx-spark-review-the-ai-appliance-bringing-datacenter-capabilities-to-desktops)). Pixel-sampled swatches (lighting-dependent, treat as cross-checks for the Lab gate, not truth): top studio-lit **#C1A589**, front shell band **#BCA279**, foam field average **#816D52** (studio) / **#9E8E72** (NVIDIA press), side in dim light **#726553** (sources: STH top/front photos, [NVIDIA press 5760px](https://iprsoftwaremedia.com/219/files/202510/workstation-dgx-spark.png), ChargerLAB side). Derived PBR guidance [UNCONFIRMED]: shell albedo ≈(0.77, 0.66, 0.53), metallic ~1.0, roughness 0.45–0.6 with micro-speckle; **pods + I/O plate glossier (roughness ≈0.2–0.3)** than the shell; foam = brighter ligament glints over near-black pore AO.

## 3.2 Facet grades (render vs reality)

Renders audited: `dgx-spark/{front,q34,top,side,detail}.png`, `audit-spark-{rearq34,side,front-macro,front-macro-verify}.png`, trio frames.

| # | Facet | Grade | 10/10 truth (source above) | Path to 10/10 (bounded, prioritised) |
|---|---|---|---|---|
| S1 | Rear face identity | **2/10** | Rear is full-face metal foam with a polished rounded-rect I/O plate inset low | Rebuild rear: extend gated `foam3d` to the rear face + add the polished champagne inset plate; gate with `spark_foam`/`spark_champ` pins |
| S2 | Rear port row | **2/10** | ON/OFF pill · PD USB-C · 3× vertical USB-C · HDMI · RJ-45 · QSFP×2 in one wide cutout at RIGHT | After S1: cut the 8 correct cavities in the plate (USB-C portrait!); kill any USB-A cavities (invented); QSFP pair at right end, power pill far left |
| S3 | Top face | **3/10** | Blank matte champagne; no dark panel, no groove; foam-edge reveal strips at front/rear only | Delete the invented dark inset panel + groove; make the top the same champagne shell; keep/refine the foam-edge strips + corner tabs |
| S4 | Edge/corner sharpness | **4/10** | Crisp ~1 mm chamfers; 1–2 mm top-corner radii; reads as a machined brick, not a soap bar | Reduce all shell fillets to ≤1.5 mm; re-render q34 + side, re-gate tone (specular lines will change) |
| S5 | Foam panel framing | **4/10** | Flat inset foam + ~3–4 mm flat shell bands left/right; foam never wraps the corner; hidden dead side-on | Flatten the foam panel; add the narrow shell bands; stop the foam at the panel boundary |
| S6 | Front hand-hold pods | **6/10** | ~24×33 mm pods near each edge, vertically centred, concave scoop, glossier than shell | Resize/reposition pods off the corners; add scoop depth; raise pod gloss (rough ~0.25) |
| S7 | Front logo plates | **2/10** | Green NVIDIA eye + 90°-rotated wordmark on the LEFT pod | Add blank green-tinted square plate + blank vertical cartouche on the left pod only (trademark gate) |
| S8 | Foam material read | **7/10** | Sparkly ligament glints over deep pore shadows, pores 1.5–3 mm, slightly matte | Keep (gated, at depth limit); optional: soften the spikiest corner ligaments only if the pin holds |
| S9 | Front button/LED absence | **10/10** | No front power button, no LEDs anywhere | Nothing — trap correctly avoided; never add an LED |
| S10 | Side faces | **7/10** | Clean matte champagne; foam invisible side-on | Fixed by S5 (foam sliver currently peeks at the edge); add micro-speckle |
| S11 | Shell colour/finish | **6/10** | Soft champagne (#C1A589-ish studio-lit), matte micro-speckled, metallic | Re-check `spark_champ` Lab pin against the STH swatches; current side reads over-saturated mustard; add speckle bump/noise |
| S12 | Bottom face | **2/10** (unbuilt/unaudited) | Magnetic squircle non-slip cover + front intake slot | Model cover + slot; render a bottom audit angle (currently none exists) |
| S13 | Finish differentiation | **5/10** | Pods/I-O plate noticeably glossier than shell | One material tweak after S6/S1 |
| S14 | Proportions/scale | **8/10** | 150×150×50.5 slab; reads palm-size vs the 197 mm Studio | Verify with an ortho front render measured against 150:50.5 = 2.97:1; adjust only if off |
| S15 | Grounding | **8/10** | Sits solid, slight shadow gap above base cover | Keep; add the thin base-cover shadow line once S12 lands |

**Overall Spark: 5.5/10.** The front face alone is an 8; the object fails 360° because the rear/top/bottom diverge from the teardown truth.
**Single highest-leverage fix: S1+S2 — rebuild the rear as foam + the inset polished I/O plate with the 8 correct cavities.** (S3 second: the invented dark top panel is visible in every elevated hero.)

---

# OBJECT 4 · The 6-GPU rig + the scale trio (composition facets)

No single real product to cite — the rig is our own 12U open-frame cart. It is graded on physical
plausibility (an owner of six 5090s must believe this rig runs) and on composition. Card-level facets
live in the 5090 section. Renders audited: `rig/gpurig-{front,q34,side}.png`, `trio/trio-{q34,front}.png`,
contact sheet.

| # | Facet | Grade | 10/10 truth | Path to 10/10 (bounded, prioritised) |
|---|---|---|---|---|
| R1 | Power delivery plausibility | **2/10** | Six 575 W cards = six 12V-2×6 cables + visible PSU(s); a cable-less rig cannot run. Cite real multi-GPU open-rig photos before modelling | Add 6 recessed angled 12V-2×6 connectors + gently draped sleeved cables to a PSU shelf below; one bounded change per element, each knob citing a photo |
| R2 | PCIe/riser linkage | **3/10** | Cards can't float on exposed gold fingers; real rigs seat cards in riser slots with visible ribbon/board | Model slot bodies over the fingers + riser ribbons to a tray; hide the raw gold edge |
| R3 | Frame structure | **6/10** | Extrusion/steel frame with visible fasteners, gussets, gauge-plausible members | Add corner brackets + bolt heads at joints (cite a photo per knob; honest wear allowed at handled edges) |
| R4 | Card mounting | **4/10** | Cards clamp via bracket thumbscrews to a rail; perfectly floating cards read CG | Add a top mounting rail + bracket screws touching each card |
| R5 | Card-to-card uniformity | **5/10** | Real cards sit with ~0.5–1 mm seat variance; clones at identical pitch read CG | Add tiny per-card jitter (yaw/height ≤0.3°/0.5 mm), keep pitch |
| R6 | Casters/shelf | **7/10** | Casters + shelves read plausible now | Small: caster swivel plates + bolt patterns |
| R7 | Side/rear rig audit coverage | **3/10** | Every rig angle audit-able; `gpurig-side.png` is near-black | Re-light the side/rear audit shots (fill only for audit frames, not heroes) |
| R8 | Trio composition/scale | **8/10** | Studio 197 mm vs Spark 150 mm vs 304 mm cards, all true scale on the cart | Numeric ortho scale check of all three; adjust only if off |
| R9 | Floor grounding | **7/10** | Faint glossy-floor reflection (owner taste call pending: blur-vs-crisp) | Leave until the owner rules (escalated decision, do not churn) |

**Overall rig: 5/10. Single highest-leverage fix: R1 — power cabling + PSU presence.** It is the
one thing every hardware owner notices instantly ("what powers these?"), and it also fixes the
empty under-shelf that currently reads as staged.

---

# OVERALL STANDINGS + PRIORITY ORDER

| Object | Overall | Single highest-leverage fix |
|---|---|---|
| RTX 5090 FE | **3.5/10** | G1+G2: 7-blade fan + rim ring |
| Rig (composition) | **5/10** | R1: power cabling + PSU presence |
| DGX Spark | **5.5/10** | S1+S2: rear foam + inset I/O plate |
| Mac Studio | **6/10** | M1+M2: rear vent field + port row |

Global work order for the loop (lowest-graded first, GPU leads):
**G1→G2→G7→G4→G5→G10→G12→G9 · then M1→M2 · then S1→S2→S3 · then R1→R2 · then the remaining facets by grade.**
Spec-file corrections FIRST where flagged (MAC-STUDIO-360-SPEC.md rear section; DGX-SPARK-360-SPEC.md USB-A line).

Two OPEN OWNER DECISIONS (do not churn, escalate in writing): Spark foam depth vs its tone pin;
floor reflection blur-vs-crisp; (new, this report) Mac Studio top logo plate vs trademark gate.

---

# READY-TO-PASTE /goal FOR THE OPUS 4.8 SESSION

```
/goal Run the continuous photoreal render loop from ~/Downloads/cx-render-handoff/HANDOFF.md, driven by
~/Downloads/cx-render-handoff/GRADING-REPORT.md (the sourced research + facet grades). Work the
lowest-graded facets first, GPU geometry leads: G1→G2→G7→G4→G5→G10→G12→G9, then Mac Studio M1→M2,
then Spark S1→S2→S3, then rig R1→R2, then remaining facets by ascending grade. Before touching a
builder, fix the two flagged spec-file errors (MAC-STUDIO-360-SPEC.md rear = rectangular ~173x53mm
vent field + correct port order; DGX-SPARK-360-SPEC.md has no USB-A anywhere) and update each spec
with the report's sourced facts. Every iteration: PICK the top open facet · RE-RESEARCH the exact
geometry from the report's cited URLs (fetch the reference photos; never model from memory) · make
ONE bounded change in the right worktree builder (rack-oracle for GPU/rig, model-refinement for
Studio/Spark) · RENDER the single most relevant angle at --preview, cycling angles so all 360 faces
(front/rear/side/top/bottom/macro) get audited over time — including the currently-black
gpu-bottom and near-black rig-side audit frames, which must be re-lit first · AUDIT honestly against
the real reference photo + run the numeric gates (rack_verify.py / rig_patches.py / clipcheck.py) ·
VERIFY exit codes and pixels BEFORE claiming PASS · commit one clean human-authored change per the
LAWS (no attribution, trademark gate = blank plates only, middot only, no ambient-env reflection,
keep the co-located LED point lights). Every ~5 commits: full-quality hero reshoot + re-gate + contact
sheet rebuild. After each wave: run the forensic panel Workflow for rendering-quality tells but NEVER
trust its hardware-fact claims — the sourced dossiers in GRADING-REPORT.md override it (the 5090 LEDs
ARE real, the Studio front ports ARE vertical, the Spark pods ARE real). Autopsy any reverted value.
Respect the three escalated owner decisions (Spark foam depth, floor reflection, Studio top logo
plate) — do not churn them. Never fabricate completion; leave an honest "next" in
render/MORNING-REPORT.md at every pause; iterate until every facet in the report re-grades 9+ from
its cited reference angle, then reshoot all heroes and stop.
```

*End of report. This session researched and graded only — no renders or builders were touched.*




