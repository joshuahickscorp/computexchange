<!-- CLAIM-SCOPE: internal-engineering-non-authoritative -->
# computexchange · public site rebuild · report (2026-07-02)

What changed, why, and where the evidence is. The site is live at https://computexchange.net/.

## The headline

The public page went from a static image hero to a **live, draggable 3D scene** of a Mac Studio and
an NVIDIA DGX Spark on a desk, with the copy widened from "batch inference" to a **verified spot
market for compute**, a **desktop-only** layout with a **phone hand-off** screen, and status markers
that now match the operator console for one material language. Every factual sentence on the page
traces to a `path:line` receipt in `docs/SITE-CLAIMS.md`.

## What was done, in order

### 1 · Scope audit (honesty first)
Before writing copy, the closed job-type contract was audited against the source tree
(`docs/SITE-REBUILD-T0.md`). Findings:
- **Six executors ship today and are verified**: embeddings, batch inference, transcription,
  classification, extraction, rerank (`agent/src/runners.rs`, six `JobRunner` impls).
- **A general-compute container lane exists** but is metered per GPU-second and reputation-trusted,
  **not output-verified**, and gated to Linux + Docker + NVIDIA (`CustomRunner` + `sandbox.rs`).
- **Rendering and simulation are roadmap**, not shipped · they run only through that metered lane,
  so the page names them as the road ahead, never as a shipped capability. Image-gen, eval, and
  LoRA finetune are enum stubs with no runner and are not mentioned at all.

The copy reflects exactly this: thesis "a verified spot market for compute", the general-compute
lane stated with its honest caveat in the receipts, rendering in a plain roadmap sentence.

### 2 · The two devices, modeled from scratch
Both machines are built procedurally in Blender (`render/build_scene.py`, metric scale, no imported
meshes, no trademarks), lit on a matte desk seen from a standing eye line looking down ~36 degrees.
Three look-fix iterations per device are logged in `render/NOTES.md` with a written pass:
- **Mac Studio** (197 x 197 x 95 mm): generous corner radius, the front USB-C pair + SD slot + power
  LED, a satin bead-blast aluminum (not a mirror).
- **NVIDIA DGX Spark** (150 x 150 x 50.5 mm): the open-cell metal-foam front face with its two pill
  cutouts, a muted anodized champagne shell, the exact flat-square proportion against the taller
  Studio.

Finals render at 1024 samples / 3200 px. They are the WebGL fallback image, so the fallback is the
same scene, not a placeholder.

### 3 · The live hero (Three.js)
`web/assets/site/hero.js` loads a low-poly glb of the same scene and lights it to match the Cycles
render. Self-hosted Three r160 under `/assets/site/vendor` · no CDN at runtime.
- **Drag to orbit** within a locked ~40 degree pitch band, easing back to rest on release.
- **Hover** lifts a device 3 percent and fades in its spec label with a hairline leader.
- Sub-degree idle drift, killed by `prefers-reduced-motion`.
- **Honest fallback**: any WebGL or vendor failure swaps to the Cycles still via dynamic import.
- The DGX foam runs on baked normal/roughness/AO maps (`render/foam_maps.py`, the same Voronoi field
  as the Cycles shader) so the glb stays 1.6 MB.

Measured in-browser: **10 draw calls** (budget was under 30), hover and the context-loss fallback
both verified.

### 4 · Layout and copy
- **Desktop-only** by design. Under 900 px the page becomes a hand-off screen: the mark, one line,
  and three working keys · share/AirDrop, email-me-the-link, copy. Phones fetch zero hero bytes.
- Text is deliberately spare: the hero, three status rows, the one serif price ($0.001 per 1,000
  embeddings, a real catalogue constant), the earn row, the closed-alpha state, footer. The full
  claim ledger lives in a **receipts dialog** behind one control.
- 160 px of void between sections.

### 5 · System unity (this pass)
The three how-it-works markers are now the operator console's **exact job-status light**: the
turned-metal ring (`dot-ring@3x`, flat, black rim, no angle) with a CSS colour lens inside, the same
asset and technique the app uses on job rows. Traffic-light lenses · gold on drop, red on the spend
cap (a stop), green on proof (verified, breathing like a live run). The site and the console now
speak one material language.

## Backend change
`control/api.go` `handleSiteAsset` now serves the scoped asset tree (png, js, glb, wasm, json) with a
traversal guard and an extension whitelist, pinned by `TestSiteAssetType`. The public site is served
at `GET /` (`handleRoot`, `SITE_PATH`); the operator console stays at `/admin`, untouched.

## Gates cleared
- prove-local matrix green.
- Dash gate (no em or en dashes) clean on every shift-authored file · the separator is the middot.
- Every referenced asset resolves; traversal and bad extensions 404.
- Draw calls 10 (< 30), hover verified, fallback verified.
- Screenshots committed under `docs/screenshots/`.
- Live-verified on production: the page, the glb, the vendor tree, the foam maps, the status ring all
  serve with correct content types.

## Where things live
- Page: `web/index.html` · Hero module: `web/assets/site/hero.js` · Scene: `render/build_scene.py`
- Foam maps: `render/foam_maps.py` · Verify log: `render/NOTES.md` · Shift report: `render/SHIFT-REPORT.md`
- Claims ledger: `docs/SITE-CLAIMS.md` · Scope audit: `docs/SITE-REBUILD-T0.md`
- Assets: `web/assets/site/` (renders, glb, foam maps, self-hosted Three, the status ring)

## Honest limits
The real-time aluminum is matte PBR without the Cycles bead-blast microspecular, and the foam is a
baked normal map on a flat plane rather than true displaced geometry · both are standard real-time
simplifications, and the full-fidelity Cycles still is the fallback, so nobody is shown a lie. The
environment is a hand-authored dark gradient, not a photographic studio HDRI.
