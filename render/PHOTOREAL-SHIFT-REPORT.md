# CX ORACLES · PHOTOREAL FRONTIER · SHIFT REPORT

Scope: `CX-ORACLES-PHOTOREAL-FRONTIER-V2.md`. Two mandates: (1) fix the inverted pill relief and
freeze the Spark; (2) push the renders toward photographic indistinguishability under a forensic
cold-agent panel, without ever breaking the measurement-locked tone gate. This report is the
completion audit: what shipped, the tone discipline, the panel trajectory, and the honest verdict.

Authority hierarchy held throughout: **measurement-with-evidence > grader rulings > panel blind
verdicts > own eye.** The in-rig tone gate is senior to every photoreal move; post rescues nothing;
FALSE-TELLs (true of the reference device, or deliberate product choices) are logged, not chased.

---

## 1. Completion audit vs the mandate

| mandate | status | evidence |
|---|---|---|
| Commit 1 · fix inverted pill relief (bezel ~flush, slot CONCAVE) | DONE | z-audit -2.50 to -4.90; raking-light frame `commit1-raking.png` |
| Freeze Spark geometry | DONE | SPARK dict frozen; all pins from `sth_front-1`; no geometry moves since |
| Defect ledger (T1-T9 + own re-look) | DONE | `render/PHOTOREAL-LEDGER.md` |
| First ledger pass · one commit per class | DONE | FOAM-GEO-MAP, MATERIAL, CAMERA, POST, LIGHTING (5 commits) |
| Per-surface microrealism / physical light / camera physics / post chain | DONE | see tables 4-6 |
| Forensic cold panel to two consecutive clean | RUN, NOT REACHED | 4+ loops; trajectory + honest verdict in section 7 |
| Tone gate re-run pre-post every commit | DONE · ALL PASS every time | section 3 |

---

## 2. Commit log (this frontier)

```
cea4633  L4 fix: grunge smudge Voronoi->organic noise (kill grid-of-dimples regression), pull amplitude
0682bea  L3 cleanup: un-distort bezel cutouts (warp 0.6), soften bevel 0.16, broaden reflector
31acc4c  L2 response: revert bevel 0.24, foam macro cell-size variation, push aluminium wear
1bc855f  L1 response: foam domain-warp + deeper cells + strut contrast; reflector; grunge; floor
97cd8ce  LIGHTING: ground micro-realism (T6); T5 logged tone-blocked
f91345e  POST: image-formation chain + roll
034d211  CAMERA: physical DOF + pair yaw asymmetry
1578d7d  MATERIAL: bevel shader (T7) + anodize mottle (T8) + foam tone re-verify
187ad19  FOAM-GEO-MAP: de-thread + depth hierarchy
1500802  initialize defect ledger
5e96e60  commit 1: fix inverted pill relief -> concave pocket
```
Standing rules honored: one class per commit, no em/en dashes (middot + "to"), tone gate green
before every commit, autopsy logged for every overturned value, no Claude attribution in git.

---

## 3. Tone gate · SENIOR · pre-post · ALL PASS at every commit

The gate measures Lab patches on portrait renders vs the frozen reference pins (global offset O = -12).
Never violated. Final delivered-frame reading:

| patch | ref L | tgt L | meas L | a | b | dE | tol | verdict |
|---|---|---|---|---|---|---|---|---|
| studio_alu | 84.3 | 72.3 | 74.4 | -0.2 | -0.6 | 2.11 | 4 | PASS |
| spark_champ | 80.0 | 68.0 | 66.0 | 2.7 | 26.6 | 3.16 | 4 | PASS |
| spark_top | 46.9 | 34.9 | 33.2 | 1.3 | 10.4 | 2.99 | 4 | PASS |
| spark_foam | 52.8 | 40.8 | 36.8 | 2.3 | 21.9 | 5.40 | 6 | PASS |

Every photoreal move that fought the pins lost to the pins. The clearest case: a DEFINED softbox
reflection on the matte champagne top (T5) desaturated its gold below the pin at every energy that
read; the tone won, T5 stayed tone-limited on the champagne (a physical truth: the anodized top IS
matte). The foam sits deliberately on the dark edge of its tolerance (deeper cells = more real
self-occlusion) but never crosses dE6.

---

## 4. Camera table (T3 / T9)

| shot | f-stop | intent |
|---|---|---|
| detail | 5.6 | strong foreground-to-background falloff; the macro tell |
| pair | 11 | far device a touch softer; T9 depth separation |
| front / q34 / side / top | 16 | subject sharp end-to-end, keeps the tone patches crisp/gated |

Pair asymmetry (T9): Spark yawed -14.5 vs Studio -14.0; sub-degree camera ROLL applied in post.

## 5. Lighting version table

| version | change | reason |
|---|---|---|
| frozen PORTRAIT_RIG | key/rim/fill on void black, expo -0.70 | tone-locked baseline, never moved |
| +T6 ground | floor micro-normal + low sheen + broad unevenness | soft floor smear, not a mirror |
| +T5 reflector (p-refl) | overhead broad soft card | metal tops catch a soft light instead of void |
| L3 reflector tune | broaden 3.8 / raise +2.7 / dim 1.5 | earlier tighter version cast ghost blobs |

## 6. Post chain (T4) · applied AFTER the gated raw · `post_chain.py`

roll 0.3deg · radial chromatic aberration +/-0.18% · specular bloom (thr 0.88) · gentle vignette
(corner ~0.80) · fine luminance grain (sigma 0.008, deterministic per frame). Post-delta on the tone
patches < 0.6 L (measured) · the gate stays pre-post, so post never rescues a failing pin.

---

## 7. Forensic panel · protocol, trajectory, honest verdict

**Protocol.** Each loop, 5 fresh cold vision agents (5 distinct lenses: hardware reviewer, lookdev
artist, product photographer, materials specialist, meticulous buyer). Each is shown my post gate
frames MIXED with real-photo controls (actual Spark/Studio hardware from `render/ref/`) under neutral
filenames in a neutral folder, and forced to call each image PHOTOGRAPH or CG_RENDER with confidence
+ up to 3 tells. No agent knows the mix or the purpose. A gate frame FAILS a loop if >=2 of 5 agents
call it render, or one tell is named by >=2. Calibrated against the real controls' render-call rate.

**Trajectory (render-call rate · lower = more photographic):**

| loop | change under test | MINE | REAL (control) | gap | verdict |
|---|---|---|---|---|---|
| 1 | baseline (post-first-pass ledger) | 0.97 | 0.14 | 0.83 | NOT CLEAN |
| 2 | foam warp+deeper+contrast, reflector, grunge, bevel 0.42 | 0.90 | 0.06 | 0.84 | NOT CLEAN |
| 3 | bevel 0.24, foam macro cell-size variation, wear up | 0.87 | 0.14 | 0.73 | NOT CLEAN |
| 4 | un-distort cutouts, bevel 0.16, reflector broadened | 0.83 | 0.29 | 0.54 | NOT CLEAN |
| 5 | grunge Voronoi->organic noise (kill dimple grid), 4K frames | _pending_ | _pending_ | _pending_ | _pending_ |

The gap is closing (0.83 -> 0.54): as the renders improve the panel increasingly mistakes REAL
photos for renders (real rate rose 0.06 -> 0.29). But the renders are still distinguished.

**What the panel taught (and what it changed):**
- **Foam** was the #1 tell every loop. The domain-warp + deeper cells + strut contrast + macro
  cell-size variation moved it from "tiled procedural" toward "organically random with real depth"
  (some agents now cite the foam as the reason a frame reads REAL). Still the leading residual.
- **The grid-of-dimples was a self-inflicted bug, not a model defect** (see the autopsy, section 8).
  Fixing it in L4 is expected to drop the studio render-calls; loop 5 measures the true post-fix rate.
- **Bevel uniformity** is a real residual: an edge highlight that runs continuously around an edge
  reads as CG regardless of radius. Dialed to 0.16; the rim-on-fillet highlight is the frozen-rig
  part and is left alone (tone-senior).
- **The void-black background and the matte champagne are FALSE-TELLs**: the black sweep is a
  deliberate site-match (the public site renders on black), and the champagne top IS matte anodized.
  Chasing these would break either the site integration or the measured tone. Not chased.

---

## 8. Autopsies (values overturned, with cause)

- **Bevel 0.30 -> 0.42 -> 0.24 -> 0.16.** L1 bumped it to catch more edge light (T7); the panel read
  the bigger catch as a "razor-thin CG bevel line" (L2). Reverted below the original; its UNIFORMITY,
  not its size, is the residual tell.
- **Grunge smudge 45 mm Voronoi -> organic noise (L4).** The Voronoi tiled the 197 mm studio top into
  a ~4x4 grid of roughness cells, which on the reflective surface read as a regular grid of dark
  dimples. Confirmed intrinsic: persisted at 1100 samples (not denoiser) and with the reflector
  disabled (not the reflector). Replaced by large non-cellular noise; the top now reads as real
  mottled aluminium. This regression inflated the studio render-calls for three loops.
- **Foam warpXZ 1.05 -> 0.6 (L3).** The lateral warp that de-tiles the foam was also distorting the
  bezel/port cutouts ("warped L and O"). Reduced until the cutouts read clean and the de-tile held.
- **Foam mean drift.** Deeper + bigger cells darkened the foam patch (L41 -> L36.8); held inside dE6
  by lifting the strut albedo a hair. The geometry now carries the darkness, not the albedo.

---

## 9. Ledger final status

See `render/PHOTOREAL-LEDGER.md`. Summary: T2/O1 (foam) much improved, leading residual; T3/T4/T9
(camera/post) landed; T6 (ground) landed; T7 (edge) dialed to residual-uniformity; T8 (mottle)
landed; T1 (surface) fixed after the grunge autopsy; T5 (reflections) tone-limited on the champagne
(FALSE-TELL) and improved on the silver.

## 10. Deliverables

- Gate frames (4K, post): `render/portraits/` · Studio + Spark front/q34/side/top, pair, details.
- Collages: `render/collages/` (gate frames, settlement pairs, microrealism evidence, loop history).
- Panel: `render/panel/` (per-loop neutral sets, private keys, raw verdicts, PANEL-LOG.md).
- This report + the ledger. Export mirror: `~/Downloads/cx-oracles-final-2026-07-03/`.
