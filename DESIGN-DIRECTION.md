<!-- CLAIM-SCOPE: internal-engineering-non-authoritative -->
# computexchange · design direction · how to read this

This is a design DIRECTION, research-backed and grounded in the actual codebase, not code.
Hand section 8 (the genuine forks) to the deciding model, then execute here.

**Provenance.** Synthesized from seven web-enabled research briefs (Rick Owens · Tadao Ando ·
editorial and brutalist web references · typography · motion physics · asymmetric layout ·
precision-as-design), consolidated references at the end. The research read the live tokens and
the scrub engine, so the calls are concrete to this repo, not generic.

**Two engines exist right now, and this direction supersedes both framings.** The build you are
viewing on localhost (the "SHOTS" camera-rail version) and a parallel motion-doctrine engine on
the `worktree-worktree-site-narrative` branch (one glide filter, dwell plateaus, perceptual
interpolation) are both attempts to make the LIVE scene better. The core recommendation here is
different: a HYBRID architecture, bake the procession as a pre-rendered frame sequence (Apple's
real technique) and reserve live Three.js for drag-to-inspect at rest. That is a different move
than "tune the live camera," and it is the single biggest lever on the "the render quality is not
showing" complaint.

**The work splits by render-dependency. This answers "even before we have the renders, it has to
look good":**

- **Render-INDEPENDENT (do now · roughly 80 percent of the vibe-coded-to-refined fix · looks good
 with the stand-ins):** warm the palette, delete Cormorant, demote Geist Mono to receipts only,
 install the 60 percent spine and the flush-left asymmetric layout, the tick-scale, 0px radius,
 the price-monument typography, the status-row-as-instrument, the receipts-ledger-as-object,
 tabular numerals and unit labels. None of this needs the final device renders.
- **Render-DEPENDENT (the hero upgrade):** the baked frame-sequence procession. It needs final
 Cycles frames, but the architecture does not change, so you can ship the baked sequence at
 stand-in quality now and swap in reproduction-grade frames later (open question 2).

**One honest flag the research surfaced (open question 4):** the receipts "168 pass" is hardcoded
copy in `web/index.html` today. The design leans hard on the receipts ledger as the hero object of
the whole honesty thesis, so those numbers should be generated at build from the claims ledger in
`docs/`, not hand-typed, or the aesthetic undercuts itself.

The full synthesized direction follows verbatim.

---

# COMPUTEXCHANGE · DESIGN DIRECTION
### One authored point of view. Rick Owens' Concordia, not his runway; Ando's Church of the Light, not "dark mode."

---

## 1 · THE THESIS

**computexchange is an instrument the size of a room.** The aesthetic is *weighted monochrome procession*: a warm-graphite void you walk down, on a visible measured grid, toward monoliths that are lit · never spun · and a price carved like alabaster at the end of the axis. The one idea that unifies everything: **the precision is the ornament.** Every choice removes decoration and replaces it with a real measurement, aligned to the pixel. Owens gives us the mass and the muted aggression; Ando gives us the light-as-only-event and the load-bearing void; the instrument tradition (Braun, Teenage Engineering, the watch chapter-ring, the aviation dial) gives us the discipline that makes rigor read as beauty. The current page fails because it performs *ambience* (a scrub, soft dots, centering) when the product's asset is *rigor*. We stop performing premium and start exposing it.

The through-line, stated as a rule for every downstream decision: **subtraction + alignment, never addition.** Aggression is geometric (scale, placement, mass). Muting is chromatic (no pure black, no pure white, one accent per viewport, color only as fact). Nothing glows. Nothing orbits. Nothing centers except the one monument that earns it.

---

## 2 · PALETTE + MATERIAL

The single biggest "cheap" tell is the flat `#060606` fill and the *cool* greys. Owens over-dyes his black so it never reads as terminal-default; his greys are warm greige "Dust," not steel. **We warm the ramp and make the ground a lit surface, not a fill.**

**The evolved ramp** (keep the near-black + bone philosophy, re-key warm):

| Token | Now | New | Role |
|---|---|---|---|
| `--bg` | `#060606` | **`#0B0A09`** | Warm graphite ground. Never a flat fill · see below. |
| `--bg-lift` | · | **`#141210`** | A second plane of near-black, for the device field / lit concrete. |
| `--hair` | `rgba(255,255,255,.05)` | **`rgba(231,227,220,.05)`** | Warm-tinted hairline (bone, not white). |
| `--ash` | `#595960` (cool) | **`#5B564E`** (warm) | Faint labels, tick marks. |
| `--mute` | `#83838b` (cool) | **`#8B8477`** (greige) | Body-adjacent, receipt values. |
| `--bone` | `#bfbcb5` | **`#C2BDB2`** | Primary text. |
| `--bone-hi` | `#e7e3dc` | **`#EAE4D8`** (Pearl) | The monument + the single brightest highlight. Never `#fff`. |
| `--green-core` | `#69c096` | keep | Fact only. |
| `--gold-core` | `#e2c178` | **desaturate → `#C9A96A`** | The DGX metal is *oxidized bronze*, not bling. Price accent. |
| `--red-core` | `#dc8079` | keep | Fact only. |

**Light as event (the one structural exception to "no gradients").** The ground is not a hex fill · it is a **single near-imperceptible vertical gradient**, `#0B0A09 → #100E0C` down each tall section, ≤4% lightness delta, sub-threshold as "a gradient." This is Ando's raw-concrete wall: light falls down it. It reads as *material*, not effect. Everywhere else: **zero gradients, zero box-shadow, zero glow, zero glassmorphism.**

**Where the two metals sit.** Both devices are matte, raked by one low key light that reveals grain:
- **Mac Studio** · brushed aluminum, visible grain, cool-neutral. The silver.
- **DGX Spark** · matte anodized black + **oxidized bronze** (dull tarnished ochre `#8A7A55`-ish in the render, `--gold-core #C9A96A` in the UI). Never polished gold · bright gold reads crypto, the exact opposite of anti-hype.

**Discipline:** ≤ 5 to 7 tones on screen at once. Green/gold/red appear in **at most two places** (the status instrument + the price) and only ever state a fact.

---

## 3 · TYPOGRAPHY

**The verdicts, up front, no hedging:**

- **Cormorant Garamond: deleted.** A delicate high-contrast bookish serif is a perfume ad, not ox-bone. It is also a documented AI-slop tell ("the one italic serif on a mono page"). The monument is carved, not calligraphic · so it becomes a *grotesque numeral at extreme size*, which is far more Ando (a concrete slab) than any serif.
- **Geist Mono: kept, but demoted to one job.** Mono-does-everything is *the* vibe-coded signature. Confined to receipts / prices / IDs / hashes / status, mono stops being the brand and becomes semantically true: **"the machine is speaking."** That is the whole receipts-first identity. Its footprint should drop to ~15% of on-screen text.
- **New primary voice: one austere neo-grotesque** carrying display + text + UI (~85% of the page).

**The system · three variables, three jobs, no overlap:**

```
--sans: "Söhne","Inter",system-ui,sans-serif; /* text + UI + most display */
--breit: "Söhne Breit","Söhne","Inter",sans-serif; /* the monument + hero one-liners */
--mono: "Geist Mono",ui-monospace,monospace; /* receipts / prices / system only */
/* --serif DELETED */
```

**Chosen face: Söhne + Söhne Breit (Klim).** Rationale that beats the alternatives: Söhne is Akzidenz "framed through Helvetica," modeled on **NYC subway wayfinding** · it is by lineage an *infrastructure/signage* typeface, exactly the register (signage, not fashion). **Söhne Breit** (extended width) gives the wide, rectangular, poster-concrete letterforms that read as Ando the instant they go big, and Klim ships a matched **Söhne Mono** · so if we ever retire Geist, all three voices come from one family with identical proportions. Web licence is **perpetual, self-hosted WOFF2, no CDN** · it satisfies the hard constraint exactly.

**Ship-today free fallback (zero licence cost, swap later by changing one var):** **Inter + Inter Display** (`font-optical-sizing:auto`) for `--sans`/`--breit`, **Geist Mono** already in-repo for `--mono`. The direction is executable this afternoon on the free stack; upgrading to Söhne touches no layout.

> Runner-up worth naming for the deciding model: **ABC Monument Grotesk** (Dinamo) is *more overtly brutal* · raw, unpolished, and it ships Mono + Semi-Mono cuts. Pick Monument for *edge/signature*, Söhne for *authority*. My call is Söhne; the name of the product is not "Monument," and authority is the safer monument-holder.

**The scale · one exponential ramp with a violent gap** (the gap *is* the negative space made typographic; kill the 24 to 32px "landing-page" mid-tier):

| Token | px (desktop) | Face | Weight | Tracking | Leading | Case |
|---|---|---|---|---|---|---|
| `monument` | `clamp(120px, 22vw, 340px)` | Breit | 600 | −0.02em | 0.9 | as-is |
| `display` | 56 to 88 | Breit/Sans | 500 | −0.015em | 1.02 | sentence |
| `h1` | 34 to 44 | Sans | 500 | −0.01em | 1.08 | sentence |
| `h2` | 22 to 26 | Sans | 500 | −0.005em | 1.15 | sentence |
| `lede` | 18 to 20 | Sans | 400 | 0 | 1.55 | sentence |
| `body` | 15 to 16 | Sans | 400 | 0 | **1.65** | sentence |
| `label` | 10 to 11 | **Mono** | 500 | **+0.22em** | 1.2 | **UPPERCASE** |
| `receipt` | 13 to 14 | **Mono** | 400 | +0.01em | 1.5 | tabular |

**Non-negotiable optical hygiene** (this is where "engineered" vs "vibe-coded" actually lives): `font-feature-settings:"kern"1,"liga"1`; **tabular numerals** (`"tnum"1`) on every price/receipt, proportional in prose; **no `letter-spacing` on body** (0 is correct for a good grotesque · tracked body is the amateur move); **never a weight below 400** on the dark ground (thin type shimmers = the Android tell); measure capped ~72ch lede / 85ch hard max.

---

## 4 · LAYOUT + COMPOSITION

**Kill the center line.** The 660px centered `.measure` + pervasive `text-align:center` is the template tell. Install **one hard vertical spine at ~60% (column 8 of a 12-col grid)** inside a widened frame with deliberately oversized outer margins.

**The grid:**
- 12 columns; `margin-inline: clamp(48px, 8vw, 160px)` · wide margins make the frame *monumental*, not full.
- Content is **flush-left, ragged-right**, hung off the spine. Never justified, never centered.
- Modular proportion nods to Ando's **1800×900 (2:1) formwork panel**; spacing on a strict token scale **8 / 16 / 24 / 32 / 48 / 64 / 96 / 128** only · precision means *never a stray 21px*.
- **One 1px vertical hairline runs the full page height at the spine** (`--hair`). This single rule says "there is a system here" louder than any panel. It is the drafting sheet / watch chapter-ring, translated.

**The asymmetry rule (device vs void+type):**
- WebGL canvas is **left-anchored, full-bleed-left**: `position:fixed; left:0; width:62vw; height:100vh`. The device renders in the *left third of its own canvas* · hard toward the frame edge, base-anchored, heavy, **never floating dead-center at eye level**.
- The scrolling narrative lives entirely in the **right field (cols 8 to 12)**, flush to the spine.
- The **void between them is designed, load-bearing *ma*** · a small dense type block counterweighting a large sculptural device across emptiness. A single small device in a large void reads *bigger* than a device filling the frame.

**Per-section vertical rhythm = procession.** Each beat enters at a **different vertical anchor** and alternates which flank the monolith sits on, so the eye walks a zig down the page rather than reading a stack:

- Arrival → device left, copy bottom-anchored low.
- How-it-works → device arcs toward right, copy high.
- Price → the one centered event (below).
- Earn → device low-left, copy bottom-anchored.
- Release → device recedes, copy top.

Between beats: **full-viewport (100vh) empty thresholds** · nothing but the void, the device mid-transit, and one 10px mono label pinned to the bottom (`01 / ARRIVAL`). Emptiness is content. The test: if a void feels like the page is *waiting to load*, it's too vague; if it feels like a held breath before the reveal, it's *ma*. **Do not fill it.**

**Bottom-anchor copy far more than top** · type set as a caption *below* a large void reads as the most editorial/expensive move available, and it is literally Ando (emptiness above the object).

**The price monument spec (the single sanctioned break from every rule above · it earns its power *because* everything else broke from center):**
- Face: **Söhne Breit**, `font-size: clamp(120px, 22vw, 340px)`, weight 600, `letter-spacing:-0.02em`, `line-height:0.9`, color `--bone-hi`.
- **Flush-left against the far frame (col 1), bleeding off the right edge** · `overflow:visible`, the final digit clipped by the viewport. Bleeding mass past the frame is the Owens "invade the space" gesture; the crop makes it exceed the page. *(This resolves the layout brief's "flush-left/bleed" against the Ando brief's "centered cross": we bleed-left. It is more aggressive, more Owens, and it keeps the one axis of drama without a symmetric wedding-invite center.)*
- The `$` is small, superscripted, **mono, `--mute`** · the numeral does the work.
- Three instrument registers stacked: `EMBEDDINGS` (label) / **`$0.001`** (monument) / `USD · PER 1,000 UNITS` (label). No number naked; no label without a number.
- Sits on a **thin plinth line** · a 1px `--hair` rule directly beneath, giving the monument a base. A monument has a plinth.
- Color: monochrome Pearl on ground. The *one* permitted accent is `--gold-core` on the price itself (price *is* the fact). The verified-result receipt tucked in the lower corner may carry the one green fact · the "back pillow against the concrete slab": monumental claim, humble proof.
- **≥80vh of vertical *ma*** around it; nothing else shares the screen.

**Pinned canvas vs scrolling void/type composition:** the canvas is fixed and full-bleed-left (the *set*); the type layer scrolls over the right field (the *script*). Full-bleed emptiness on the left, hard-contained argument on the right · that contrast is the whole game. Retire `--r:14px` on all content containers (rounded corners = the #1 Android tell); **0px radius everywhere.** Keep one 1px frame for exactly one designed object: the receipts ledger.

---

## 5 · MOTION LANGUAGE

**A correction the briefs missed:** the engine already scrubs a *held-shot camera path* with `RESTS`, `smootherstep`, `dwellRemap`, and a `YAW_RANGE` of 0.18 rad of *interaction give* · it is **not** a literal turntable orbit. So the owner's "goes in a circle" is really *"reads as ambient because it's too smooth, too fast, too symmetric, and the render quality doesn't show."* That reframes the fix.

**The architecture decision · HYBRID, and here is the reason.** The owner's core complaint is that *the render quality isn't showing*. A live WebGL rasterizer **throws away** the Blender Cycles lighting/DOF/GI and re-approximates it · which is precisely why it looks cheap. So:

- **The procession is BAKED.** Render the 5-beat camera move in Blender (the existing `cx_*.py` Cycles pipeline) and **scrub a pre-rendered frame sequence painted to `<canvas>`** · Apple's actual technique. This makes the *real photoreal render* the hero and kills "toy in a circle" by construction (a baked dolly has a start frame and an end frame and goes nowhere when you stop). This is the single biggest visual upgrade available.
- **Live Three.js is reserved for drag-to-inspect at rest**, at the price-monument dwell and the release beat: cross-fade the canvas to one live, spotlit device the buyer can rotate. This is the *one* place a little inertia/underdamping is welcome, because it's direct manipulation, not ambient loop · resistance and reward in the same gesture. (Lusion's SOTY build proves live WebGL stays lean when reserved for interaction.)

**Concrete pipeline:** ~**150 frames** (Apple's AirPods = 148; below ~90 you see stepping), ~**1600×1000**, WebP/AVIF q72 to 80, on a **pure `#0B0A09` background** so frames composite invisibly. Delivery: **individual content-hashed frames** (3 to 4.5× faster scrub-ready than client-side video-unpack, HTTP/2-friendly, and it *is* your existing hashed-asset model). Preload into `ImageBitmap`s, draw on `requestAnimationFrame`. Keep the still-image fallback for WebGL-off / `prefers-reduced-motion` (snap to key frames, don't scrub).

**Resistance / weight / held-film parameters:**
- **Damping on the frame index (and camera) = `0.06` per frame** (`current += (target-current)*0.06`). Lenis defaults 0.1 and recommends going lower for a heavier feel; 0.06 sits between that and the Frontend-Masters 0.08. Below ~0.045 it reads laggy, not weighty. Apply the *same* damping to the frame index so fast scrolls don't strobe and the sequence inherits the mass. If GSAP is used, `scrub: 1.0 to 1.4` · **never `scrub:true`** (that's the frictionless glue that reads cheap).
- **Critically-to-slightly-overdamped, ratio ≥ 1. No spring bounce / no overshoot on the narrative** · overshoot reads "playful/toy/Android," wrong brand. (Overshoot allowed *only* on drag-to-inspect.)
- **Discrete detents that snap and settle.** Lean into the existing `RESTS`. Each beat arrives at an exact composed pose and **holds dead-still** · a machined stop, like a knob with a click. The scene should look like it *stopped on a mark*.
- **Scroll budget ~500vh, allocated unevenly:** arrival 80vh · transition 70vh · **price 140vh (the long dwell)** · earn 90vh · release 120vh. ~15 to 20% of each beat maps to *no scene change* (held frame) so the eye rests. Overall ~1.6 to 2× current length. Monumentality is slow; if it feels slightly too slow, it's right.
- **One verb per beat** (swap the current every-reveal `smootherstep` + 18px float-up): dolly-in only / arc only / hold+rack-focus / one emissive change / pull-back only. Everything-at-once is mush.
- **Type is the metronome · hold on the headline.** Scroll velocity → 0 while each beat's `display`/`monument` line sits in its optical rest, holds ~200 to 400ms of scroll distance, then releases. The "resistance" the owner wants is *the word refusing to blur past*. This does more than any easing curve.
- **Velocity coupling is seasoning, capped hard:** ≤2° skew / ≤3px directional blur on **type layers only** (never the device · a blurred hero device looks broken), decaying within ~250ms of scroll stop. For this austere brand, err toward *almost imperceptible*.
- Discrete UI reveals resolve with `cubic-bezier(0.16, 1, 0.3, 1)` (expo-out, decelerate hard into rest), 700 to 900ms, no bounce. Drop the universal float-up; let type resolve in place with a short opacity settle.

---

## 6 · PRECISION MADE VISIBLE

The unifying idea, made concrete. Every one of these is *a real measurement, aligned* · not a HUD ornament. **Precise-and-quiet is subtraction; sci-fi-HUD is addition.** No corner brackets, no scan-lines, no reticles, no glowing dividers, no animated bar-graphs.

1. **The visible grid + tick-scale.** The full-height spine hairline, plus a **minute-track of ticks down the left grid rule** · short 4px ticks at every 8px baseline step (`rgba(231,227,220,.04)`), longer 8px ticks at section boundaries. Borrowed from a watch's *chemin de fer* and an oscilloscope graticule. It signals "this page is measured" without a single glow.

2. **Tabular numerals, stacked and right-aligned.** Everywhere a quantity appears, give it its own right-aligned column so decimals stack: `$0.001 / $0.002 / $0.004` line up their points; the receipts ledger splits into `label │ value(right-aligned, mono) │ source path`. Alignment carries emphasis, never bolding. Columnar numeric truth is the one thing a monospace market page can own.

3. **The silkscreen label layer.** One unified label token (`10px, +0.22em, uppercase, --ash`) always paired with a value in `--bone-hi`. Every number gets a unit label; no label without a number. Attach *real* units: `45s TARGET`, `0.25 AUDIT FLOOR`, `3% TAKE`, `M-SERIES · 24-CORE`. This is what turns marketing copy into engineering documentation.

4. **The status row as a genuine instrument** (this is where the page currently drifts to HUD). Turn the three how-it-works rows into a strict 4-column console line: `[flat lit lens] [KEY] [statement] [state, right-aligned tabular]` → `● PROOF · results come back verified · 168/168`. **Kill the `breathe` keyframe and the `box-shadow` bloom** on `.status-dot` · a pulsing halo is the archetypal HUD tell; a *flat hard-edged filled disc inside the turned-metal ring* is an instrument. If you want life, animate a **state change on scroll** (gold→red→green, 200ms crossfade as the row enters) · motion means *a transition happened*, never idle breathing. Aviation-dial law: red = limit, green = normal.

5. **The receipts dialog is the hero object · a printed audit ledger, not a modal.** Header set like a receipt head: `COMPUTEXCHANGE · CLAIMS LEDGER` / rule / `168 PASS · 0 SKIP · 0 FAIL` / `GENERATED 2026-07-03` / rule. Three monospace columns, source `path:line` right-aligned into a clean vertical edge. 0.5px hairline between rows. Foot: the existing devastating line · *"what could not be evidenced was not softened, it was killed"* · set small, flush-left, in `--ash`, as the audit stamp. This one object can carry the entire "designed by engineers with taste" verdict, because here *showing every source IS the aesthetic*. It keeps the single 1px frame; everything else on the page is frameless.

6. **Hairlines at 0.5px physical, never boxes.** The existing `--hair` is right; render rules at physical 0.5px on retina (halve the current `box-shadow:0 1px 0`). Grouping comes from space + the tick-scale, not from boxing anything.

---

## 7 · SECTION-BY-SECTION

**01 · ARRIVAL** · *The blind forecourt.* Device seen in raking silhouette, far, low, off toward the left frame; you cannot yet read the machine. Camera dollies *forward* only. Copy bottom-anchored low in the right field, small: one `h1` line + a `lede`. The label rail reads `01 / ARRIVAL`. Motion: slow forward creep, then a hard detent. Muted, heavy, unhurried · the first thing the visitor feels is *resistance*.

**02 · HOW IT WORKS** · *The alley and the turn.* Camera **tracks laterally** (never rotates) so the Mac's face resolves edge-to-front as if you walked around a wall. Both devices land in one frame across the desk-as-landscape. Copy high in the right field. This beat carries the **status instrument** (§6.4): three console rows, flat lit lenses, tabular `168/168`, state-changes crossfading in as the rows enter. One verb: the arc. Devices themselves static.

**03 · PRICE MONUMENT** · *The cross / the carved slab.* The camera **stops dead**; motion halts entirely for the longest dwell (~140vh). The baked canvas cross-fades toward the live drag-to-inspect device, spotlit, planted on a visible plinth. The `$0.001` monument (§4 spec) · Söhne Breit, `clamp(120px,22vw,340px)`, flush-left, **bleeding off the right edge**, on its plinth hairline, `EMBEDDINGS` above / `USD · PER 1,000 UNITS` below, the one gold accent on the figure. A small green verified-result receipt tucks into the lower corner: the humble proof beside the monumental claim. ≥80vh of *ma*, nothing else on screen. This is the emotional center; it earns the only centered-drama moment on the page.

**04 · EARN** · *The descent.* Camera **lowers**; the desk plane rises past the frame as if descending Ando's stair into the earth. The DGX warms · its single vermilion/bronze moment · via **one emissive/material change** (the status light comes up green, the metal catches the key). This is color-as-motion-event: the accent is the only thing that changes. Copy low-left, bottom-anchored: *flip a Mac online, it earns while it idles.* One verb: the material shift.

**05 · RELEASE** · *The withdrawal.* Camera **pulls back**; the devices return to silhouette; light fades to the black room. Copy top of the right field, terse. A second live drag-to-inspect moment is permitted here. The page ends in stillness and void · Ando's silence · with the receipts-ledger entry point as the last, quiet, precise mark. No CTA shouting; the fact, then the void.

---

## 8 · OPEN QUESTIONS FOR THE DECIDING MODEL

These are the genuine forks · everything above is a made call; these are the few worth a first-principles human decision:

1. **Söhne (paid, authority) vs ABC Monument Grotesk (paid, edge) vs Inter (free, ship-today).** I chose Söhne with an Inter fallback. If the owner wants a more *distinctive signature* over safe authority, Monument Grotesk is the swap · and its native Mono/Semi-Mono could even retire Geist for a single-family system. Worth a real look at both set at 340px before locking. (Budget/licence tier is a business input I can't resolve.)

2. **Baked frame-sequence · is the Blender pipeline production-ready enough to bake 150 photoreal frames per beat?** My whole "make the render show" thesis rests on Cycles frames being genuinely photoreal. The memory notes the final photoreal renders *aren't done yet* (current devices are stand-ins). If the renders won't be reproduction-grade by ship, the honest move is to ship the baked sequence at stand-in quality and upgrade the frames later (the architecture doesn't change) · but someone should confirm the render timeline before committing the page's hero to it.

3. **Price monument: bleed-left (my call, Owens) vs centered cross (Ando).** I resolved to bleed-left as the more aggressive, more on-brand gesture. But the centered "shaft of light through the concrete" is the purer Ando reading and the only place a symmetric axis could be *earned*. This is the one place the two references genuinely conflict; a human eye on both comps is warranted.

4. **How live is the receipts ledger?** The strongest move is a *real* audit artifact (actual `path:line`, real `168 PASS`, real timestamp). Confirm those numbers are live-generated at build (from the claims ledger in `docs/`) and not hand-typed · a stale hardcoded receipt would undercut the entire honesty thesis the design is built on.

5. **Motion floor: do we ship the hybrid, or baked-only for v1?** Drag-to-inspect (live Three.js at rest) is the richest beat but the most code. If time is tight, baked-sequence-only still fixes 80% of the "cheap" read; the live inspect can be a fast-follow. A scope call, not an aesthetic one.

---

**One-line brief for execution:** Warm the black, delete the serif, demote the mono to receipts, hang everything off a 60% spine with a full-height hairline and a tick-scale, bake the procession from Cycles and scrub it with 0.06 damping into dead-still detents, carve the price in Söhne Breit bleeding off the left, and make every number an aligned, unit-labeled instrument reading · so the rigor already in the codebase becomes the only ornament on the page.

**Relevant files for execution:** `/Users/scammermike/Downloads/computexchange/web/index.html` (tokens at `:root` lines 41 to 56, the two `@font-face` blocks + Cormorant preload at lines 23/28/31, `.wrap`/`.measure`/`text-align:center`, `.status-dot` breathe/glow, `.claim` receipts), `/Users/scammermike/Downloads/computexchange/web/assets/site/hero.js` (`RESTS`/`smootherstep`/`dwellRemap`/`YAW_RANGE` scrub engine · where damping, detents, and the baked-frame-index scrub live), and the `cx_*.py` Blender Cycles scripts (the pipeline that bakes the ~150-frame procession).

---

## References consulted

Seven web-enabled briefs, deduped to the strongest sources by dimension.

**Rick Owens.** publicdelivery.org/rick-owens-furniture · elements.envato.com/learn/rick-owens-aesthetic · 10magazine.com Rick Owens interview.

**Tadao Ando.** archeyes.com (Church of the Light, Koshino House) · architecturalmoleskine (Church of the Light) · danslegris.com/blogs/journal/tadao-ando · Honpukuji Water Temple (Google Arts and Culture) · kiryoku.it Ando essay.

**Editorial and brutalist web.** bureauborsche.com (and the Balenciaga identity) · hassanrahim.com · 2x4.org · siiimple.com/ssense · fontsinuse Aesop · "vibecoded-design-tells" (GitHub) · "Why Your AI Keeps Building the Same Purple Gradient Website" (prg.sh).

**Typography.** klim.co.nz (Söhne, Söhne Mono, web-font licence) · abcdinamo.com Monument Grotesk · pangrampangram Neue Montreal · usgraphics Berkeley Mono · Overused Grotesk (open, GitHub) · alistapart web-typography tables · optimo.ch Theinhardt.

**Motion.** css-tricks.com "fancy scrolling animations used on Apple product pages" · frontendmasters.com virtual-scroll-driven 3D scenes · Lenis (github.com/darkroomengineering/lenis, lenis.darkroom.engineering) · gsap.com ScrollTrigger · scroll.locomotive.ca · kota.co.uk motion and pace essays · blog.maximeheckel.com spring physics.

**Layout and grid.** Müller-Brockmann, Grid Systems in Graphic Design (archive.org) · Wim Crouwel (designmuseum.org) · International Typographic Style (Wikipedia) · swissthemes.design and docs.mew.design Swiss-grid guides · Canons of page construction (Wikipedia).

**Precision as design.** Dieter Rams (Wikipedia) · Teenage Engineering guide (blakecrosley.com) · Linear / Stripe / Vercel premium-UI analyses (mantlr.com, voltagent design.md) · Receiptor Mono (studio2am) · Berkeley Mono (usgraphics).
