<!-- CLAIM-SCOPE: internal-engineering-non-authoritative -->
# web/assets/site · the asset interface contract

Freezes the interface between the public site and the future photoreal device
masters. The photoreal shift changes PIXELS; it does not change the names,
roles, or placement declared here. The site builds its choreography against the
constants in this file so the final masters drop in with zero site rework.

Source of truth for the constants below is the site's own code
(`web/assets/site/hero.js`, `web/index.html`), not `render/` · this shift does
not read or touch the model tree. The values are copied here and declared
immutable for the site's purposes.

---

## 1 · the named asset set

Names are STABLE. Content-hashing happens at build time (PART 3) and must map
the hashed emit (`name-<8+ hex>.ext`) from these stable sources automatically.
Reference every asset by the stable name; never hard-code a hash.

### photoreal-master-owned (the bridge shift re-shoots these)

| stable path | role | current intrinsic | notes |
|---|---|---|---|
| `oracles.glb` | live hero geometry + embedded foam maps · loaded by `hero.js` | 1.0 MB wire | re-baked from the photoreal masters at the bridge shift |
| `oracles-pair@1x.png` | arrival-beat fallback still · LCP image · still-first crossfade base | 1066 x 666 | must stay a faithful capture of the arrival rest pose (see 2) |
| `oracles-pair@2x.png` | srcset upscale | 2133 x 1333 | |
| `oracles-pair@3x.png` | srcset upscale | 3200 x 2000 | |
| `og-image.png` | social share crop | 1200 x 630 | re-shot from masters |
| `tex/foam-normal.png` | desk foam PBR normal · embedded into the glb at bake | 512 px class | not referenced at runtime · lives inside the glb |
| `tex/foam-rough.png` | desk foam PBR roughness · embedded into the glb at bake | 512 px class | " |
| `tex/foam-ao.png` | desk foam PBR ao · embedded into the glb at bake | 512 px class | " |
| `mac-studio@{1x,2x,3x}.png` | per-device still · AVAILABLE, not consumed by the live-canvas beats | 682 / 1365 / 2048 sq | kept stable for a static-composition or per-device fallback |
| `dgx-spark@{1x,2x,3x}.png` | per-device still · AVAILABLE, not consumed by the live-canvas beats | 682 / 1365 / 2048 sq | " |

The five beats are driven by the LIVE glb camera (dolly, exposure, framing), not
by swapping per-device stills. The per-device stills are frozen here so a future
fallback that composes them statically inherits stable names, but the current
site does not consume them.

### UI ornament (NOT part of the photoreal device swap · brand assets)

| stable path | role | current intrinsic |
|---|---|---|
| `cx-mark-white.png` | wordmark · masthead, hand-off, footer | 682 sq |
| `dot-ring@3x.png` | anodized job-status ring · the how rows | 320 sq |
| `knob-off@3x.png` | earn-section instrument | 720 sq |
| `fonts/geist-mono.woff2` | the mono, 400-600, subset | woff2 |
| `fonts/cormorant-600.woff2` | the single serif monument, 600 | woff2 |
| `vendor/three.module.js`, `vendor/addons/loaders/GLTFLoader.js` | self-hosted Three · no CDN at runtime | js |

---

## 2 · composition constants (immutable for the site's purposes)

Copied from `hero.js`. Coordinate space is metres, y-up (glb convention). The
photoreal relight changes pixels, not placement · the site keys its beat
choreography off these numbers.

- **Ground / desk plane height:** `y = 0` (the shadow-catcher plane, rotated
  -90 deg about x, at the origin). Devices rest on it.
- **Device world X (the choreography anchors):**
  - Mac Studio `STUDIO_X = -0.135` (camera-left of centre)
  - DGX Spark `SPARK_X = +0.159` (camera-right of centre)
- **Nominal look target:** `(0, 0.03, 0)`. The arrival beat targets
  `(0.012, 0.03, 0)`.
- **Camera:** perspective, **FOV 30**, near 0.05, far 50.
- **Arrival (beat 1) rest pose:** target `(0.012, 0.03, 0)` · distance
  `0.92 m` · pitch `0.63 rad` (~36 deg down) · yaw `0` (rest) · exposure `1.00`.
  Derived world position at yaw 0: `(0.012, 0.572, 0.743)`.
- **Drag give at rest:** yaw `+/- 0.35 rad` (~+/- 20 deg), pitch `+/- 0.05 rad`.
- **Photometry:** ACES Filmic tone mapping · sRGB output · PCF soft shadows.
  Per-beat **exposure** is the only photometric key the choreography moves.

### pair framing

`oracles-pair` frames BOTH devices on the tabletop in the arrival rest pose. It
is the LCP image AND the WebGL-down fallback, so it must remain a faithful
capture of the arrival beat (section 2 rest pose) · the still-first crossfade
(PART 3) fades it into the live canvas from the identical composition, and any
drift between still and canvas would read as a jump.

Aspect is ~1.601 (16:10 class). Rendered at `max-width: 1140px` in `.hero-still`
with `sizes="(max-width:1240px) 92vw, 1140px"`.

---

## 3 · the hard rule · no pixel-feature dependence

NOTHING in the site may depend on a specific highlight, reflection, or shadow
position inside the stills or the glb. The photoreal relight WILL move all of
them. Beats key off **geometry** (device world X, ground plane) and **tokens**
(colours, type) only · never off a pixel feature of the current renders. A beat
that reads correctly only because a highlight happens to sit where it does today
is a broken beat.

---

## 4 · stand-in status

The current wave-8 renders behind every photoreal-master-owned name above are
STAND-INS. The bridge shift (a separate document, after both this shift and the
modeling shift are graded) swaps in the photoreal masters, runs ONE `hero.js`
light-matching pass against the final rig, re-bakes the glb, re-shoots the pair
and og stills, re-measures, and ships. That light-match is budgeted there, not
here. This shift consumes the stand-ins as-is and depends on none of their
pixels.
