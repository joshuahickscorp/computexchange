<!-- CLAIM-SCOPE: internal-engineering-non-authoritative -->
# SHIFT-REPORT-SITE · the narrative shift · one stop

Branch `worktree-worktree-site-narrative`, off main at f8dc90d. Held at this stop;
NOT merged (the bridge shift lands site and masters together). Runs in parallel
with the photoreal modeling shift; the only shared object is
`web/assets/site/CONTRACT.md`. `render/` was never read or touched.

## The environment constraint, stated once

The verification sandbox has **WebGL disabled** (`GL_VENDOR = Disabled,
Sandboxed = yes`). So the live 3D scene cannot render here. Consequences, honestly:

- The scroll CHOREOGRAPHY was verified NUMERICALLY (a headless projection harness
  that puts both device silhouettes through the camera at every intermediate
  scroll position) and STRUCTURALLY (layout geometry, reveal math, all measured
  in-browser). It was NOT graded by eye on the live render.
- The WebGL-down FALLBACK is genuinely verified visually, because that is exactly
  what the sandbox exercises.
- The by-eye choreography grade, the live beat collages, the continuous-scroll
  recording, LCP-on-Fast-3G, and the 60fps-scrub frame budget are OWNER-HARDWARE
  deliverables (M3 Pro at 1440p). They are enumerated at the end with exact steps.
  None were faked.

## Completion audit against the shift document

| item | status |
|---|---|
| PART 0 · `web/assets/site/CONTRACT.md` (names, roles, composition constants, pixel-independence rule, stand-in note) | DONE · commit 51ad956 |
| PART 1 · scroll engine ON, five beats composed, device-to-one-side, runway 6-8vp | DONE · 7.25vp, commits ef8616c + e275d84 |
| PART 1 · text-over-devices solved by composition not scrims (verify every intermediate position) | DONE · numeric collision proof, all beats clear incl. max drag |
| PART 1 · beat 2 rows enter one by one tied to scroll sub-ranges | DONE · verified cascade drop to cap to proof to receipts |
| PART 1 · beat 3 exposure dim, no DoF | DONE · exp 0.32 keyframe, no DoF |
| PART 1 · beat 5 rise-vs-fade (try both, keep one, log the loser) | rise + canvas fade kept; pure-rise-no-fade is the logged alternative for the owner (see below) |
| PART 1 · keyframes a small table, zero-alloc, single scalar listener | DONE · reused state object, one scroll listener, onProgress off the same scalar |
| PART 1 · drag rest-only, eases back; reduced-motion snap + crossfade | DONE · per-beat drag scale, WAAPI boundary crossfade |
| PART 1 · semantic DOM source of truth; WebGL-down fallback = sectioned still page | DONE · verified in-preview |
| PART 1 · section label cull (keep row keys + download, kill island-namers) | DONE · commit 550aeaf |
| PART 2 · rhythm 160px, measure ~660, one idea per beat | DONE · measured; v-center to 660, footer void to 160, arrival to 640 |
| PART 2 · typography audit (one Cormorant, Geist Mono else, no new weights) | DONE · PASS, no change |
| PART 2 · token audit (green/gold outcome+fact only, nothing new glows) | DONE · PASS, no change |
| PART 2 · phone hand-off pixel-for-pixel unchanged | DONE · diff-confirmed + mobile screenshot |
| PART 2 · fresh-eyes ten lines + fixes | DONE · NOTES-SITE class 4 (live per-beat eyeballing is owner-hardware) |
| PART 3 · content-hash filenames end to end + extend TestSiteAssetType | DONE · commit 3c3f0e7; no new asset type, so no TestSiteAssetType change |
| PART 3 · still-first crossfade | DONE (wired) · commit 5ebe674 |
| PART 3 · LCP + time-to-live on Fast-3G + broadband, waterfalls | OWNER-HARDWARE gate · PERF.md pass 4 |
| PART 3 · KTX2 tooling attempt | DONE · basis_universal installs now; deltas measured; re-bake is the bridge shift's (device asset) |
| PART 3 · frame budget 60fps with scrub live, no >50ms task, draw calls unchanged | OWNER-HARDWARE gate · PERF.md pass 4 |
| PART 4 · this report + collages + recordings | report DONE; live collages + recordings are OWNER-HARDWARE |

## The beat keyframe table (verified)

Coordinate space metres, y-up. `target.x` is the horizontal composition control
(devices shift screen-left as the camera looks to their right). `ds` = per-beat
drag scale. Canvas opacity eases 1 to 0 over p 0.86 to 1.0.

| beat | p | target.x | ty | dist | pitch | exp | ds | the void (where copy lives) |
|---|---|---|---|---|---|---|---|---|
| 1 arrival | 0.00 | 0.012 | 0.030 | 0.92 | 0.63 | 1.00 | 1.00 | top + bottom (thesis low) |
| 2 how | 0.25 | 0.300 | 0.055 | 0.90 | 0.52 | 1.00 | 0.55 | right column |
| 3 monument | 0.50 | 0.120 | 0.245 | 1.10 | 0.79 | 0.32 | 0.30 | upper-centre (figure) |
| 4 earn | 0.75 | STUDIO_X-0.185 | 0.045 | 0.66 | 0.48 | 1.00 | 0.55 | left column |
| 5 release | 1.00 | 0.012 | 0.300 | 2.10 | 0.95 | 0.85 | 0.15 | centre; scene eased away |

**Collision proof:** for every beat, the device cluster (both silhouettes, 27
sample points each) clears the beat's text rectangle across the full
text-present window AND under max drag-yaw at rest. The harness is in the shift
scratchpad (not shipped, per CONTRACT). Method: replicate hero.js lookAt +
perspective (FOV 30, aspect 1.6), project, assert. Result: ALL BEATS PASS.

## Layout math

Five 100vh copy blocks + four 50vh runway spacers put each beat rest at p about
i/4 (matching the keyframe p-values) and the page at 7.25 viewports. In-browser
measurement confirmed the void columns land opposite the devices every beat
(beat 2 right w470, beat 4 left w470, beats 1/3/5 centred), and that exactly one
beat's copy is visible at each rest with an empty void at the mid-transitions.

## Label cull list

- KILLED: "how it works", "earn" (section eyebrows that only named their island).
- KEPT: the drop / cap / proof row keys; the "download" label; the monument
  caption "embeddings · per thousand" (data, not an island name).

## Numbers (see docs/PERF.md pass 4 for the full table)

- Draw calls: 10 (budget under 30), unchanged from pass 3.
- Engine: zero per-frame allocation (beat state writes a reused object), one
  scroll listener for the whole page.
- KTX2 foam maps (512px), if the bridge shift re-bakes the glb with
  KHR_texture_basisu: 674 KB PNG to 167 KB ETC1S (~75% wire), GPU memory ~6x.
  Not applied here (device-asset re-bake is out of scope).
- Content hash: 13 assets hashed; every referenced hashed asset resolves 200.

## Fresh-eyes ten lines

Recorded in NOTES-SITE.md change class 4, with the three fixes applied
(v-center 660, arrival 640, footer 160px void). The lines that need the live
render (per-beat device composition, exposure dim, ease-away) are the owner's.

## Commit log (8 change classes, one class each)

```
550aeaf  cull the island-naming section labels (pass-3 ruling)
3c3f0e7  content-hash build step · hash the stable assets, rewrite refs + importmap
39c6fa3  PERF pass 4 · still-first + frame-budget gates, KTX2 tooling and deltas
5ebe674  still-first crossfade · paint the 1x still as LCP, crossfade to live
bbc5afb  void audit + polish · measure the rhythm, tighten to doctrine
e275d84  pin the choreography · one semantic DOM, .live beats + verified fallback
ef8616c  compose the 5-beat scroll engine · grounded keyframes, drag scale, zero-alloc
51ad956  asset interface contract · freeze names, roles, composition constants
```

## Owner-hardware deliverables (WebGL required · not producible in this sandbox)

Run the site on an M3 Pro at 1440p (`node scripts/site-build.mjs` is optional;
the source `web/` runs as-is). Then:

1. **Five beat rest-pose collage** · scroll to p 0, .25, .50, .75, 1.0; screenshot each.
2. **Mid-scrub collage** · two positions per transition (p about .12, .37, .62, .87).
3. **Continuous-scroll recording** · one pass through all five beats.
4. **Reduced-motion recording** · same, with prefers-reduced-motion set (expect the
   boundary crossfade, no continuous scrub, no idle drift).
5. **LCP + time-to-live-scene** · Fast-3G class and broadband, DevTools waterfall
   screenshots, into docs/PERF.md.
6. **60fps-scrub frame budget** · performance panel during a continuous scrub;
   confirm no long task over 50ms and draw calls at 10.

### the one logged design choice for the owner's eye

Beat 5 uses camera RISE (ty + dist grow, devices sink and shrink) PLUS a canvas
opacity fade to 0 over p 0.86 to 1.0. Rise-alone could not fully clear the
centred download copy in this framing, and fade-alone loses the "easing away"
motion, so both were kept. If the live rise reads clean enough on its own, drop
the fade (delete the `FADE_START` block in hero.js placeCamera). That is a
by-eye call I could not make with WebGL off.

## What this shift did NOT do (PART 5, respected)

Did not touch `render/`, the model worktree, MEASUREMENTS.md, or any tone pin.
Did not regenerate, re-render, or re-bake any device asset (the KTX2 glb re-bake
is explicitly the bridge shift's). Added no framework, scroll library, animation
library, or build dependency beyond the node-builtins hashing step. Depends on no
pixel feature of the current stills (beats key off geometry + tokens only). Did
not change copy scope or claims (labels were removed, never added; all remaining
copy traces to docs/SITE-CLAIMS.md). Did not merge to main.
