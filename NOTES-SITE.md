# NOTES-SITE.md · the narrative-shift change log

The site shift's working ledger. Runs in a dedicated worktree (branch
`worktree-worktree-site-narrative`) off main, in PARALLEL with the photoreal
modeling shift. The two never touch. This shift owns the site; the models own
their own tree. The only shared object is `web/assets/site/CONTRACT.md`.

## Standing rules (verbatim, load-bearing)

- No U+2014, no U+2013. The separator is the middot ( · ). Hyphen-minus for
  compounds and ranges only. Dash grep before every commit.
- ONE change class per commit, declared in this file BEFORE the change is made.
- Banned vocabulary stays banned. Present evidence and measurements; the owner
  closes. No closure vocabulary.
- BLACKHOLE honesty on copy: every factual sentence traces to
  `docs/SITE-CLAIMS.md`; any reworded claim is re-verified against the tree.
- Do not touch `render/`, the model worktree, `MEASUREMENTS.md`, or any tone
  pin. Do not regenerate, re-render, or re-bake any device asset · stand-ins are
  consumed as-is. No framework, scroll library, animation library, or build
  dependency beyond the hashing step. No dependence on any pixel feature of the
  current stills. Do not merge to main · the branch holds at its stop until the
  bridge shift.

## Boundary note · the asset pipeline lives under render/

The site's existing asset scripts (`precompress.sh`, `webify.py`, `og.py`,
`downsize.sh`, the oracle bakes) live under `render/site/`, which is the
do-not-touch tree this shift. The content-hash build step PART 3 adds is
therefore site-local (outside `render/`), consuming the stable names frozen in
the contract.

---

## Change classes (declared before the change · newest last)

### 1 · asset interface contract  ·  done (commit)

Create `web/assets/site/CONTRACT.md`. Freeze the interface between the site and
the future photoreal masters so the final assets drop in with zero site rework:
the named asset set with exact paths, roles, and current intrinsic dimensions;
the composition constants (device world X, ground plane height, camera rest
pose, pair framing) copied from the current scene as encoded in `hero.js` and
declared immutable for the site's purposes; the hard rule that nothing in the
site may depend on a pixel feature of the stills; and the note that current
wave-8 renders are STAND-INS swapped by the bridge shift. Additive only · no
existing file changes in this class.

### 2 · hero.js · the beat engine, composed and grounded  ·  done (commit)

Turn the scroll-scrub engine ON as a first-class path and replace the
first-draft keyframe table with a GROUNDED, VERIFIED one. The keyframes were
tuned in a headless projection harness that replicates the camera math and
projects the two devices' silhouettes through the camera across every
intermediate scroll position: it asserts the device cluster clears each beat's
text rectangle over the whole transition window AND under max drag-yaw at rest.
All five beats pass. Changes to `hero.js`:

- New BEATS table: each beat composes the devices into ONE side (target.x is
  the horizontal composition control · devices shift screen-left when the camera
  looks right of them). arrival center, how left/text-right, monument low+dim,
  earn Studio-right/text-left, release eased-away.
- Per-beat drag scale `ds` (8th column): full orbit at arrival, damped where
  devices are composed or receding, so drag stays live everywhere (doc rule) but
  can never swing a device into the text column.
- `beatState` writes into a reused object · zero per-frame allocation (was
  allocating one object per frame; the property is now actually held).
- Canvas opacity fade over p 0.86 to 1.0 · the scene eases away for release, and
  the page background is the identical void so it reads as the scene receding.
- `opts.onProgress(p)` hook so the page drives text reveals off the SAME single
  scroll scalar · no second scroll listener.
- Reduced-motion: the camera still snaps to the nearest beat (existing path); a
  short canvas opacity crossfade masks the pose jump at boundaries.

Inert until `index.html` passes `opts.beats` (change class 3), so this commit
changes no shipped behaviour on its own.

### 3 · index.html · the pinned choreography layout + activation  ·  done (commit)

Verified in the preview (WebGL is disabled in the sandbox, so the live 3D render
is the owner's by-eye grade; everything below was checked here):
- fallback (no `.live`): linear, semantic, readable · still fits the stage · masthead clear.
- `.live` geometry (forced on for measurement): 7.19 viewports; stage fixed z0;
  beats pointer-events none; void columns land opposite the devices each beat
  (b1 RIGHT w470, b3 LEFT w470, b0/b2/b4 centred) matching the collision proof.
- reveal math (identical formula run at each rest + transition): exactly one
  beat's copy per rest, empty void at the mid-transitions (matches the proof's
  crossfade windows), and the how rows stagger label to drop to cap to proof to
  receipts, settling at rest.

Two layouts off ONE semantic DOM. Linear sections stay the source of truth; the
canvas mount adds `.live`, which switches CSS to the pinned choreography. No
WebGL, no `.live` → the current proven linear still-page is the honest fallback.

- DOM restructured into: fixed masthead (brand chrome) · the stage (canvas +
  still) · `main.beats` with five `section.beat` (arrival copy, how, monument,
  earn, download) separated by `.runway` spacers · footer. Copy is moved
  verbatim, traced to `docs/SITE-CLAIMS.md`, not reworded.
- `.live`: stage pins full-bleed (z0); beats scroll over it (z2,
  pointer-events none so the canvas keeps drag-to-orbit, interactive children
  re-enabled); each beat is 100vh with the copy composed to its void side
  (arrival bottom-centre, how right, monument centre, earn left, release
  centre); 50vh runway spacers put the total near 7 viewports.
- Layout math: 100vh copy blocks + 50vh runways land each beat rest near p=i/4,
  matching the verified keyframe p-values.
- Reveals: the page's `onProgress` reveals each beat's copy by its measured
  element-centre distance (robust to layout drift), and staggers the three how
  rows across sub-ranges of the how beat. Driven by hero.js's single scroll
  scalar · no second listener.
- Reduced-motion + fallback: copy is fully visible with no scroll reveal; the
  arrival thesis band top is held at or below y=-0.66 (verified safe under full
  arrival drag).
- The phone hand-off and receipts dialog are unchanged.

### 4 · void audit + design polish  ·  done (commit)

One commit, class design, after the beats. Audits and the fresh-eyes pass.

TYPOGRAPHY audit (grep): `--serif` (Cormorant 600) appears on `.monument .fig`
ONLY · the single serif monument. No new weights (400 body / 600 monument, both
shipped). Everything else Geist Mono. PASS, no change.

TOKEN audit (grep): `--green`/`--gold`/`--red` map only to status/state
(proof/cap/drop dots + the closed-alpha pill). No colour appears once with no
role. Only the status dots glow. PASS, no change.

RHYTHM (measured, not assumed): between-beat runway 450px (>> 160px floor); how
rows 126px tall, hairline-separated; release column was 700px and the arrival
column 560px. Fixes: v-center to the 660 measure, v-hero widened to 640 so the
ash lines breathe, and the `.live` footer keeps the base 160px top void (was a
108px override). Phone hand-off re-verified pixel-for-pixel unchanged (diff
against f8dc90d shows zero handoff lines touched; mobile screenshot confirms).

FRESH-EYES ten lines (from the arrival `.live`-over-void render, the fallback
full page, and the measured geometry · the per-beat LIVE device composition is
the owner's by-eye grade on WebGL hardware, verified here only numerically):
 1. Arrival thesis sits in the bottom ~18% with a generous void above for the
    devices · calm, on-doctrine.
 2. Fixed masthead at .55 opacity reads as quiet brand chrome, does not compete.
 3. how column at 470px wraps the longer rows to two lines · acceptable, and
    widening it would eat the verified device clearance, so it stays 470.
 4. how rows at 126px with hairlines are generous, not cramped.
 5. release column at 700px was wider than the 660 measure · tightened to 660.
 6. arrival ash lines at 560px were tight · widened to 640.
 7. `.live` footer void was 108px, under the 160px floor · restored to 160px.
 8. Token/type audit clean · nothing new glows, one serif, no orphan colour.
 9. Fallback: still fits the stage, masthead clear, linear order readable · the
    honest WebGL-down page.
10. Between-beat rhythm (450px runway) is generous; within-beat groups are tight
    but cohesive (24 to 34px), matching "one idea per beat, settling into a
    column." The DRAG-TO-LOOK hint fades on scroll (opacity tied to arrival).

### 5 · still-first crossfade  ·  done (commit)

PART 3 (delivery). The 1x still is preloaded fetchpriority high and painted
immediately as the LCP element; the live canvas sits behind it and, the moment
the glb has loaded and rendered its first frame (new `hero.js` `opts.onReady`),
the still crossfades out over ~400ms from the identical arrival rest pose. The
still is now visible BY DEFAULT (not just on fallback), so a WebGL failure
simply keeps it · the fallback needs no extra work. LCP and time-to-live-scene
on a throttled Fast-3G class profile and on broadband, with waterfalls, are the
owner's profiling step on WebGL hardware · recorded as a gate in docs/PERF.md,
not faked here.

### 6 · PERF.md · delivery measurements, KTX2 tooling, and the gates  ·  done (commit)

PART 3 recording. docs/PERF.md gains a Pass 4 section: the still-first wiring and
its LCP measurement gate (owner hardware); the frame-budget-with-scrub gate
(owner hardware); and the KTX2 result. KTX2 tooling (basis_universal 2.10.0) now
installs cleanly (corrects the Pass 3 "could not install"). Measured foam-map
deltas: 674 KB PNG to 167 KB KTX2-ETC1S (~75%), GPU memory ~6x. But applying it
is a glb re-bake (KHR_texture_basisu) = a device-asset re-bake owned by the
bridge shift (PART 5); wiring KTX2Loader now would add the ~200 KB transcoder for
zero benefit. So: tooling proven, deltas recorded, re-bake deferred. Sharp PNGs
stand; wire budget still governs.

### 7 · content-hash build step  ·  done (commit)

PART 3. `scripts/site-build.mjs` (node builtins only, site-local because the
render/site/ pipeline is off-limits) hashes the stable contract-named assets and
rewrites every reference to the hashed names, in dependency order: leaves
(images, fonts, glb, vendor JS) then hero.js (its direct glb path) then
index.html (all refs + the importmap). The document keeps its stable name (the
server gives it no-cache + ETag); hashed assets get the one-year immutable cache
already wired server-side. Output is a complete servable tree under `web/dist/`
(gitignored · rebuilt on demand) plus `asset-manifest.json`.

Verified: 13 assets hashed; built importmap maps `three` and
`three/addons/loaders/GLTFLoader.js` to hashed files; hero's glb ref hashed;
preload + import refs hashed; serving `web/dist/` and curling every referenced
hashed asset returns 200 with zero unhashed `/assets/site` refs remaining.
`TestSiteAssetType` needs no change · every hashed extension (png, woff2, js,
glb) is already whitelisted. The brotli precompression of the hashed files is a
downstream step (render/site/ precompress territory), not generated here.

### 8 · section-label cull (pass-3 ruling)  ·  done (commit)

PART 1 mechanic, applied late as its own class. The ruling: keep the drop/cap/
proof row keys and the download label; kill labels that only named an island.
The "how it works" and "earn" section eyebrows only named their island · the
rows (drop/cap/proof) and the earn copy carry those beats on their own. Removed
both. Kept: the row keys, the download label, the monument caption (data, not an
island name). No claim changed · labels removed, nothing added, so no
SITE-CLAIMS re-trace needed. The how beat's reveal list drops from 5 to 4
(rows + receipts); HOW_GATE re-staggered to match (gates moved into the active
ep range, verified drop to cap to proof to receipts one at a time).

### 9 · SHIFT-REPORT-SITE.md · packaging + the stop  ·  done (commit)

PART 4. One report: the completion audit against the shift document, the
verified beat keyframe table + collision-proof method, the layout math, the
label cull list, the PERF numbers, the fresh-eyes pointer, the 8-commit log, the
owner-hardware deliverables (live collages, continuous-scroll + reduced-motion
recordings, LCP/Fast-3G waterfalls, 60fps-scrub frame budget) with exact steps,
the one logged beat-5 rise-vs-fade decision, and the PART 5 boundaries respected.
The branch holds here until the bridge shift.

### 10 · REVISION-SITE.md · slowness diagnosis + refinement plan  ·  done (commit)

Owner reported the live page is "really really slow" and wants the effects
extremely refined. `REVISION-SITE.md` is the revision report (doc only, no code
changed). Diagnosis, code-grounded: the scene is a STATIC set with a moving
camera but renders like a dynamic one. Ranked perf issues (mostly pre-existing in
hero.js, amplified by the pinned full-bleed canvas + scrubbing): A1 the rAF loop
never idles (renders 60fps forever for a sub-degree idle drift), A2 full-viewport
canvas at DPR 2 (~4x the old stage's pixels), A3 a 2048^2 soft shadow re-rendered
every frame though a directional shadow on static geometry is camera-independent
and should be frozen, A4 reveal runs per scroll event with layout reads (not
rAF-throttled), A5 the LCP still loads at 2x/3x on retina, A6 MSAA on the big
buffer. Feel issues: per-segment smoothstep cadence, no scroll smoothing, reveal
fighting the CSS transition, idle drift never settles, two owners of canvas
opacity. Strategic call: refine the live scene (render-on-demand + frozen shadow
+ capped DPR) rather than pre-render (which crosses the device-asset boundary).
Plan R1-R5, R1 first and measured. Awaiting owner's go + on-device profile.

### 11 · R1 · frame budget · render-on-demand, frozen shadow, capped DPR  ·  done (commit)

Owner said implement all. R1 is the "make it not slow" pass, hero.js only:
- Render ON DEMAND · the tick re-arms only while a drag is active or the ease-back
  is settling. Removed the perpetual `!reduceMotion && !dragging` re-arm and the
  sub-degree idle sine drift that forced it. At rest the loop now STOPS (zero
  render cost) instead of running 60fps forever.
- Froze the shadow map · the key light is directional and the geometry is static,
  so the 2048 shadow is camera-independent · bake once (autoUpdate=false,
  needsUpdate=true after load), not every frame.
- Capped DPR at 1.5 (was 2.0) · halves fill on Retina for a dark metal render.
Not observable in this sandbox (WebGL off → the render loop never runs here);
syntax-checked, reasoned. On-device before/after frame time is the owner's.

### 12 · R2 · scroll smoothing + rAF-throttled reveal  ·  done (commit)

- hero.js: the scroll listener now stashes only a raw `scrollTarget` and requests
  a frame; the tick eases `scrollP` toward it (critically-damped, SCROLL_EASE
  0.15) so the camera GLIDES independent of wheel/trackpad granularity. Reduced
  motion jumps instantly so its snap-to-beat survives. The reveal (onProgress)
  now fires once per frame off the smoothed scalar, not per scroll event.
- index.html: section metrics (doc-centre, height, reveal children) are cached
  once and on resize; `reveal(p)` maps the smoothed fraction to each section's
  viewport position with ZERO per-frame layout reads, and stays perfectly in
  step with the camera (same smoothed p). Verified: one beat per rest, empty void
  at the mid-transitions, how rows staggered · identical pattern to before, now
  glided and layout-read-free.

### 13 · R3 · single-fetch LCP still  ·  done (commit)

The still is a transient crossfade base (replaced by the live scene in ~1s) and
the WebGL-down fallback. Dropped the `<picture>` srcset so it is ONE 1x fetch
(1066x666) that matches the `<link rel=preload>` · no DPR double-fetch on Retina,
fastest first paint. Fixed the intrinsic dims (was mislabeled 1200x750 to
1066x666, avoids CLS). Dropped @2x/@3x from the build's hash list (no longer
referenced). Tradeoff: the fallback is 1x on Retina (slightly soft) · acceptable
for the degraded path, revertible. Build re-verified: 11 assets, all refs 200.

R4 (cadence curve, how-row stagger width) and R5 (reduced-motion + crossfade
eyes-on) are left for the owner's on-device pass · changing the easing curve or
grading the crossfades blind (WebGL off here) would be guessing. The opacity
"two owners" flag in REVISION-SITE resolved on inspection to a non-issue: the
still-first fade targets `.hero-still`, the beat-5 fade targets the canvas
(different elements), and the reduced-motion canvas crossfade is mutually
exclusive with the beat-5 fade via the reduceMotion guard.
