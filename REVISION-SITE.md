# REVISION-SITE · why it is slow, and how to make the effects refined

Written for a revision pass, not as a closure. The choreography was built and
VERIFIED (the collision proof, the reveal math, the fallback) but tuned for
CORRECTNESS, not for runtime cost or for feel on the live render. This report
separates the two problems the owner named ("really really slow" and "if we do
these effects they need to be extremely refined"), diagnoses each in the actual
code, and lays out a prioritized plan.

## 0 · honest framing

- I cannot profile here: the sandbox has WebGL disabled, so I never saw the live
  scene move. This is a CODE-GROUNDED diagnosis of the mechanisms that produce
  the slowness, with file:line and expected impact. The confirming numbers
  (performance panel, frame times) are the owner's on the M3 Pro.
- Two different problems, addressed separately:
  - **A · slow** = the frame budget. The scene costs far more per frame than it
    needs to, and it pays that cost continuously.
  - **B · unrefined** = the feel. Cadence, easing, reveal timing, settle
    behaviour · the things graded by eye.
- Most of the frame-cost mechanisms below are PRE-EXISTING in `hero.js` (they
  predate this shift). What this shift did was make them BITE: the stage went
  from a ~62vh panel to a full-viewport fixed canvas (roughly 4x the pixels), and
  scrubbing now drives rendering the whole way down the page. So the honest
  statement is: the pinned-canvas approach is only viable if the scene is made
  cheap, and right now it is not.

---

## A · why it is slow (frame budget)

Ranked by impact. Each: the mechanism, why it is expensive, the fix, the payoff.

### A1 · the render loop never idles (P0, biggest)

`hero.js` tick ends with (paraphrased):

```
const settling = |dispYaw-dragYaw|>1e-4 || |dispPitch-dragPitch|>1e-4;
if (settling || (!reduceMotion && !dragging)) frame();   // <- re-arms forever
```

Because `!reduceMotion && !dragging` is true whenever the user is idle, the tick
re-requests an animation frame ON EVERY FRAME, FOREVER. The scene re-renders at
60fps even when nothing is happening. The only reason it does this is the idle
drift (`idle = Math.sin(idlePhase)*0.006`, ~0.3 degrees of wobble). So the page
pays a full re-render, every frame, for the rest of the session, to animate a
sub-degree wobble almost nobody will notice.

- **Fix:** render ON DEMAND. Only run the loop while the camera is actually
  moving (scroll changed scrollP, a drag is active, or the ease-back is
  settling). When everything is at rest, STOP requesting frames. Either delete
  the idle drift, or give it a hard budget (a few seconds after load, then
  stop), or gate it behind a "prefers no idle" default-off.
- **Payoff:** at rest the GPU/CPU cost drops to ZERO instead of a continuous
  60fps. This is the single biggest win and the most likely cause of "the whole
  machine feels slow / the fan spins on a static page."

### A2 · full-viewport canvas at devicePixelRatio 2 (P0)

`renderer.setPixelRatio(Math.min(devicePixelRatio, 2))` (`hero.js:34`) combined
with `.live .stage{position:fixed;inset:0;height:100vh}` (index.html) means the
canvas renders at, on a 1440x900 2x display, **2880 x 1800 = ~5.2M pixels** every
frame, with MSAA, shadows, and PMREM env reflections on metal. The old stage was
~62vh, roughly a quarter of that.

- **Fix (any / all):**
  - Cap DPR lower for this scene: `Math.min(devicePixelRatio, 1.5)` cuts pixel
    work ~44% vs 2.0 with little visible loss on a dark metal render.
  - Or render at a fraction and let CSS scale the canvas up (a common trick for
    heavy full-bleed WebGL): render at 0.75x, `image-rendering` handles the rest.
  - Consider whether the beats even need a full 100vh canvas, or whether a large
    but bounded stage (e.g. 80vh centred) would read as well and cost less.
- **Payoff:** pixel-bound cost scales linearly with this. 2.0 -> 1.5 DPR is a
  free ~44% on the most expensive term.

### A3 · a 2048x2048 soft shadow re-rendered every frame (P0)

`key.shadow.mapSize.set(2048,2048)` + `PCFSoftShadowMap` + `shadow.radius = 6`
(`hero.js:39,54,58`). Three.js re-renders every enabled shadow map every frame by
default. But this scene is STATIC GEOMETRY under a DIRECTIONAL light. A
directional light's shadow map is CAMERA-INDEPENDENT · it depends only on the
light and the geometry, neither of which ever changes here. So the shadow map is
being recomputed 60 times a second to produce the identical result.

- **Fix:** render the shadow once, then freeze it:
  `renderer.shadowMap.autoUpdate = false; renderer.shadowMap.needsUpdate = true;`
  after the glb loads (set `needsUpdate = true` once more if the canvas resizes).
  Optionally drop the map to 1024 and radius to ~3 · at this on-screen size the
  difference is invisible and it halves shadow fill.
- **Payoff:** removes an entire 2048^2 depth pass from every frame. Large, and it
  compounds with A1 (no idle loop means it renders even less).

### A4 · reveal runs per scroll event, not per frame, and reads layout (P1)

`onScroll` (`hero.js:195`) calls `opts.onProgress` synchronously on EVERY scroll
event, and the page's `reveal()` (index.html) calls `getBoundingClientRect()` on
all five sections there. Scroll events fire faster than frames on a good
trackpad/wheel (100-120+/s), so the five rect reads + opacity writes run more
often than they can paint. Even though opacity/transform are composite-only (so
this is not classic layout thrash), it is redundant work on the hot path and it
couples scrolling to forced style reads.

- **Fix:** rAF-throttle. `onScroll` should only stash `scrollY` and request a
  frame; compute `scrollP`, the camera, AND the reveals ONCE inside the tick.
  Cache the section rects and recompute them on resize, not on every scroll.
- **Payoff:** reveal work is bounded to the frame rate, and scrolling stops
  triggering synchronous style reads.

### A5 · the LCP still loads at 2x/3x on retina (P1)

The still uses `<picture>` with `srcset ... 2400w, ... 3600w`. On a 2x display
the browser picks the 2x (2133x1333) or 3x (3200x2000, ~2.3 MB) image. The 1x
`<link rel=preload fetchpriority=high>` I added does not override the picture's
DPR selection, so the "fast" 1x may be fetched AND then a 2x/3x fetched on top.

- **Fix:** for the crossfade base, the still only needs to be good enough for a
  400ms transition · serve 1x (or a max ~1600px) as the actual `<img>` and drop
  3x, or use `sizes`/`imagesrcset` on the preload to match what `<picture>` will
  pick so there is exactly one fetch. Keep the crisp srcset only if the still is
  ALSO the permanent fallback and you accept the retina weight there.
- **Payoff:** faster LCP, fewer bytes on the critical path, no double-fetch.

### A6 · MSAA on a full-viewport 2x canvas (P1)

`antialias: true` (`hero.js:27`) is MSAA across ~5.2M pixels. On dark metal edges
it matters, but it is not free.

- **Fix:** if A2 (lower DPR) is applied, MSAA on a smaller buffer is cheaper and
  may be enough. Otherwise evaluate FXAA/SMAA as a post pass, or antialias:false
  at 1.5+ DPR (higher resolution hides aliasing).
- **Payoff:** modest, situational · measure before/after.

### A7 · load-time weight (P2, dev-only vs prod)

`three.module.js` is 1.27 MB raw (200 KB brotli) and the glb is 1.0 MB. In the
LOCAL preview (python http.server, no brotli) the raw bytes are served, so local
load feels much heavier than production, which serves the `.br` siblings. If the
slowness is at LOAD in the local preview, that is largely the missing brotli, not
the runtime. On the real deploy the control plane serves brotli + immutable
cache, so this is smaller · but still, three.js parse/compile is real work on the
main thread at startup.

- **Fix:** none new required for prod (brotli + hashing are wired). If startup
  parse is an issue, consider a slimmer three build (tree-shaken import of only
  what is used) as a later optimisation.

---

## The core realization

Everything in this scene is CONSTANT except the camera: two devices on a desk,
three lights, one env map, one shadow. A static scene viewed by a moving camera
should be nearly free · it should render only while the camera moves, reuse a
frozen shadow, and not pay for a 5-megapixel buffer it does not need. Right now
it renders a dynamic-scene budget continuously. Fixing A1 + A2 + A3 together
(render-on-demand, capped DPR, frozen shadow) should move it from "renders the
whole scene 60fps forever at full res" to "renders a cheap frame only while you
scroll or drag, then stops." That is the difference the owner is feeling.

---

## B · making the effects refined (feel)

These are the by-eye items. I cannot grade them without WebGL; each is a
hypothesis with a proposed refinement.

### B1 · scroll cadence is non-uniform

The camera interpolates each beat segment with its own smoothstep and the beats
sit at even quarters. Smoothstep eases in and out of EACH segment, so the camera
decelerates into every beat and accelerates in the middle · four separate
ease-in/out cycles down the page. That can read as lurchy ("stop, go, stop, go").

- **Refinement options:** (a) a single global easing across the whole scroll
  rather than per-segment; (b) lighter easing (ease-out only) so it settles into
  beats without the mid-segment surge; (c) tune per-beat runway (the doc allows
  it · monument shorter, earn longer) so the pacing is deliberate, not uniform.

### B2 · no scroll smoothing

`scrollP` maps raw `window.scrollY` straight to the camera. Wheel/trackpad scroll
is steppy, so the camera moves in the same steps · on a heavy frame that reads as
judder. A one-line critically-damped lerp of a displayed scrollP toward the raw
target (like the drag easing already does) would smooth the camera independent of
input granularity. This is often the single biggest "feels premium" change for
scroll-driven scenes. It must stay coupled to the reveal progress so they do not
desync.

### B3 · the reveal fights continuous scroll

`reveal()` sets `opacity` directly every update while a CSS `transition:opacity
.12s` is also on the element. Scroll-driven direct sets + a CSS transition can
double-smooth or lag. Pick one: either drive opacity purely from a smoothed
progress (no CSS transition on opacity), or trigger a one-shot class and let CSS
own it. Also the how-row stagger still has slight overlap (drop and cap nearly
co-enter) · widen the gate spacing or the row spacing for a cleaner one-at-a-time.

### B4 · the idle drift never lets it settle

Beyond its cost (A1), the sub-degree sine wobble means the hero is never truly
still. On a "refined" bar, a scene that subtly drifts forever can read as
unsettled. Consider removing it, or replacing it with a single gentle settle on
load that then stops.

### B5 · two owners of canvas opacity

The still-first crossfade (page adds `.stage.ready`, still fades) and the beat-5
ease-away (`hero.js` writes `canvas.style.opacity`) both touch canvas visibility,
plus the reduced-motion WAAPI crossfade. These paths are individually fine but
have never run together on a live canvas. Unify opacity ownership (one place
decides canvas alpha) so an early scroll during the intro crossfade cannot fight
the beat-5 fade.

### B6 · the mid-transition void

By design, at each beat midpoint the text fully crossfades out (verified: nothing
visible at p=.37, .62, .87) so the camera can move devices across. Between beats
that is a stretch of scroll with only the moving devices and no copy. If it reads
as a dead gap, shorten the runway or let adjacent copy overlap slightly at the
seams · a tradeoff against the collision margins (which would need re-proving).

### B7 · reduced-motion + fallback still unverified live

The reduced-motion boundary crossfade (WAAPI) and the still->canvas crossfade
have only been reasoned about, not seen. Both need an eyes-on pass on WebGL
hardware.

---

## The strategic question · keep the live scrub, or pre-render?

Worth deciding before investing in refinement.

- **Option A · refine the live scene (recommended).** Do A1-A3 (render-on-demand,
  frozen shadow, capped DPR) and B1-B3 (scroll smoothing, cadence, reveal).
  Keeps the honest live render and drag-to-orbit. The perf fixes are
  well-understood and should make the live scrub cheap and smooth. Risk: it is
  real engineering and needs on-device measurement.
- **Option B · pre-render the beat path.** Bake the five-beat camera move to a
  short image sequence or a muted video and scrub THAT on scroll (canvas 2D
  blit, or `video.currentTime`). Guaranteed smooth, near-zero runtime cost,
  trivial to make buttery. Costs: loses live drag-to-orbit; adds bytes (a
  sequence/video); AND it is a DEVICE-ASSET RENDER, which is the bridge shift's
  territory, not this one · so it crosses a boundary and cannot be done here.
- **Option C · hybrid.** Live, drag-orbitable scene at arrival; hand off to a
  cheap pre-rendered scrub for beats 2-5. Most work, best of both.

Recommendation: **A.** The slowness is not inherent to a live scene · it is the
render-loop-forever + full-res + per-frame-shadow trio. Fix those and the live
approach is both cheap and honest, and keeps the drag interaction the doc values.

---

## Prioritized revision plan (one change class each, measured on device)

- **R1 (P0, makes it not slow):** render-on-demand (kill the forever loop; only
  render while scrolling/dragging/settling), freeze the shadow map, cap DPR to
  1.5. One commit, then measure the frame time at rest and during scroll.
- **R2 (P1, smoothness):** rAF-throttle scroll + reveal, cache section rects, add
  critically-damped scroll smoothing coupling camera and reveals.
- **R3 (P1, load):** fix the still to a single 1x/1600px fetch on the crossfade
  path; keep or drop the retina srcset deliberately.
- **R4 (P2, feel):** cadence/easing pass (global vs per-segment), settle the idle
  drift, unify canvas-opacity ownership, tighten the how-row stagger.
- **R5:** eyes-on reduced-motion + crossfade pass on WebGL hardware.

Each is small and independently measurable. R1 is the one that most likely turns
"really really slow" into "fine," and it should come first and be measured before
anything else.

## what I need from the owner to target this precisely

1. Where is it slow · on LOAD, or during SCROLL, or is the machine warm/fan-spun
   on a STATIC page (that last one points straight at A1)?
2. A performance-panel capture (or even: does scrolling drop frames, and does the
   fan spin when you stop scrolling and just leave it)?
3. Which display / DPR (Retina vs external 1x monitor changes A2 a lot).

With that I can commit R1 and put a real before/after frame time against it,
rather than reasoning blind.
