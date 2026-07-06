# OVERNIGHT LOOP · three oracles to photoreal, 360 degrees

You are running an autonomous, multi-hour render→edit→audit loop. Your job is to make THREE real
products look indistinguishable from official product photography, **correct from every one of the
360 degrees** — front, both sides, rear, top, and every 3/4 between. Keep looping. Do not stop to
ask permission. Do not fabricate completion. Leave an honest "next" every time you pause.

## The three products (all metric true scale · 1 Blender unit = 1 m)
1. **Mac Studio** — Apple's desktop, the most popular Apple local-inference platform. 197x197x95 mm.
2. **NVIDIA DGX Spark** — compact AI dev machine. 150x150x50.5 mm.
3. **The home GPU rig** — a 12U open-frame rack on casters (W960 D480 H700 mm) holding **6x NVIDIA
   RTX 5090 Founders Edition** cards, fans out. Plus **the scale trio**: the rack as the base with
   the Studio + Spark on top (the site's frame-of-reference hero).

Standard: **Apple / NVIDIA launch grade.** An owner of the real hardware finds nothing missing,
nothing wrong, at any distance, from any angle.

## THE LOOP (repeat forever until the stop condition)
1. **PICK** the top open item for the object with the LOWEST current grade. The rack has the most
   gaps → weight toward it, but **guarantee desktop progress**: at least 1 of every 4 iterations must
   advance the Spark or the Studio, and no object may go a whole session without its rear/side/top
   getting attention. Never leave a broken state.
2. **RESEARCH if unsure.** The internet is fair game and encouraged. For any dimension, port layout,
   material, vent pattern, feature, or finish you are not certain of, **search the web and fetch real
   references**: manufacturer spec pages, TechPowerUp, iFixit teardowns, professional reviews with
   measurements, official product photography, dimensioned drawings. Convert to mm against a known
   anchor. Write the pin + its source URL into the object's spec/measurements file. Measurement beats
   assumption; when sources conflict, pick the best-supported and log the conflict. Use a **research
   Workflow** (fan out across facets: dimensions / a specific face / materials / I-O) for a new object
   or a whole face; scout inline for a single number.
3. **IMPLEMENT** exactly ONE bounded change in the builder.
4. **RENDER** the single most relevant angle at calibration res (preview: `--preview --samples 40`,
   fast). Cycle the angle across iterations for **360 coverage** — front, q34, rear-q34, side, top,
   detail/macro — so every face gets audited over time, not just the hero front.
5. **AUDIT honestly.** Open the render. Compare to the real reference. Ask "what still reads fake or
   wrong here?" Run the numeric gate (`rack_verify.py` for the rack: clip + tone). **Verify before
   claiming PASS** — read the tool's real exit code, look at the actual pixels. If it regressed or
   reads fake, revert or fix before moving on.
6. **COMMIT** one bounded change with an honest, human-sounding message. Tick the item in the object's
   loop/checklist file. Update the audit scorecard row.
7. **Every ~5 commits or at a wave end:** full-quality reshoot of that object's hero angles, re-gate,
   rebuild any collage/checkpoint.
8. **Escalate = write it down and move on** (do NOT block the whole loop): 3 consecutive fails on one
   item, a pin dispute you cannot resolve from references, or a taste decision genuinely needing the
   owner. Note it and pick the next item.

## 360-degree coverage law
Front is not enough. Every object must be built AND audited from front / left / right / rear /
top / 3-4s. That means modelling the parts you cannot see from the front: the **rear I/O and ports**,
the **backplate**, **vents**, **feet/base**, **top surfaces**, **side profiles**. Keep a per-object
angle checklist (below) and each pass advances the least-covered angle. A model that is perfect
head-on and blank in back is NOT done.

## Research is expected, not a fallback
Do not guess a port count, a vent pitch, a connector type, a dimension, or a colour. Look it up. Cite
it. Real examples this session: the RTX 5090 FE spec came from a 5-agent web pass and corrected a
wrong "silver-edged" assumption to the true "Dark Gun Metal" monochrome. Do the same for the Spark's
rear I/O and the Studio's rear port array — research the EXACT layout before modelling it.

## Where everything lives / how to run
- **Blender:** `/Applications/Blender.app/Contents/MacOS/Blender -b -P render/<builder>.py -- <args>`
- **Rack** (worktree `.claude/worktrees/rack-oracle`, builder `render/build_rack.py`):
  `--part {gpurig|frame|assembly|node|switch} --shot {front|q34} [--preview --samples N]`
  · spec `render/ref/rack/RTX5090FE-SPEC.md` · loop `render/RACK-LOOP.md` · gate `render/rack_verify.py`
- **Desktops** (worktree `.claude/worktrees/model-refinement`, builder `render/build_scene.py`):
  `--only {studio|spark|pair} --shot {front|q34|detail} [--preview]` · measurements in
  `render/MEASUREMENTS.md` · audit `render/GEOMETRY-AUDIT.md`. **build_mac_studio(loc_x, yaw_deg)**
  and **build_dgx_spark(loc_x, yaw_deg)** are the entry builders.
- **Scale trio** (`render/build_trio.py`, rack-oracle — **DOES NOT EXIST YET, WRITE IT EARLY** with
  this recipe): exec build_scene.py with argv `--only none` and build_rack.py with `--part defs` (both
  no-op their dispatch) into two namespaces to load both builders into one scene; set each namespace's
  `__file__` to the real path so the foam/mesh caches resolve. Then: rack `reset_scene()`+`enable_gpu()`,
  `build_frame()`, `build_gpu_row()`; snapshot-diff around `build_mac_studio(loc_x,yaw)` and
  `build_dgx_spark(loc_x,yaw)` to grab their objects and lift them onto the rack cap (`z += RACK["H"]/1000`,
  ~0.700 m), Studio + Spark side by side centred on the top; one trio rig + camera; render. `--shot {front|q34}`.
- **cwd resets between Bash calls** — always `cd` into the worktree or use absolute paths.
- **GPU / render cost:** run only ONE Blender render at a time (concurrent GPU jobs OOM). The lit
  5090 rig full render is ~15-18 min; iterate at preview (40 samples, 40%) and reserve full 512-sample
  renders for commits and heroes. Use `run_in_background` for long renders and keep working on code.

## Per-object angle checklist (update as you go)
```
RACK / 6x RTX 5090 FE   front [x]  q34 [x]  rear-q34 [ ]  side [ ]  top [ ]  macro [ ]
  open: riser cables tray->cards · real mobo + PSU + cabling on the tray · card BACK (rear X +
  flow-through window) · card TOP edge (wordmark, angled 16-pin, exhaust vents) · card short-end I-O
  bracket (3x DP + 1x HDMI) · fin-stack detail · frame cable management · tone / room grounding.
DGX SPARK               front [x]  q34 [~]  rear [ ]  side [ ]  top [ ]  macro [~]
  open: RESEARCH + build the exact REAR I-O (Spark has a specific port layout) · sides · top · confirm
  every dimension from a source · final materials from 360.
MAC STUDIO              front [x]  q34 [~]  rear [ ]  side [ ]  top [~]  macro [~]
  open: ST verify items (corner facet, base reveal, port depth) · RESEARCH + build the exact REAR port
  array for the current M-series Studio · verify all angles.
SCALE TRIO              first render [ ]  → finish build_trio.py, hero q34+front, then site-scroll notes.
```

## Laws (never violate)
- **No git attribution of any kind** — never add `Co-Authored-By`, `Generated with`, or any Claude/AI
  trailer. Commit messages and PR bodies read exactly as a human wrote them.
- **Trademark gate:** never model a real logo, wordmark, or text glyph. Model each as a BLANK plate /
  recess of the correct shape and placement (emissive or etched as the real one is).
- **One bounded change per commit.** Honest messages. Tick the item. No batching unrelated changes.
- **Verify before claiming PASS** — the tool's real exit code, the actual render. The R0.2 false-PASS
  lesson: never write "gate PASS" without reading it.
- **Autopsy on any overturned pin/value** (write why the old value was wrong). **Flip-flop guard:**
  re-measure against 2 references before re-changing a value you already changed once.
- **Clean premium products stay clean** — added imperfection reads FAKE on a Studio/Spark/GPU shroud.
  The rack is HANDLED hardware → honest wear at handled edges is allowed, but every knob cites a photo.
- **Post-chain** (grain / CA / bloom / vignette) is applied AFTER the gate; the gate is pre-post.
- **Metric true scale** so the trio composes. Owner authorised editing build_scene.py for the desktops'
  360 completion (the earlier "don't touch desktop files" rule is lifted for this loop) — but apply the
  same discipline: bounded changes, gates, autopsies.

## Deliverables (definition of done)
1. Per-object hero sets — front, q34, rear-q34, side, top, macro — full-quality, post-chained.
2. The scale-trio hero (rack base + Studio + Spark on top) + a short site-scroll design note.
3. Every angle of every object audited correct; every gate green; every spec file complete with
   sourced, dimensioned pins.
4. Present to the owner; the owner closes.

## Stop condition
Keep looping until every angle of every object is reproduction-grade and every deliverable exists.
This is a long, multi-session grind. When you pause, commit your work and write the single next item.
Never claim done that isn't done — the honesty of the audit is the whole point.
