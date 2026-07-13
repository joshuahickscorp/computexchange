# Apple Metal speculative render stage — measured status and execution plan (2026-07-12)

Status: **LOCAL PREVIEW + THREE-SCENE FIXED-RECIPE TRANSFER + HELD-OUT HAIR +
84.25X RIGGED-CHARACTER TRANSFER + DEFORMATION SCREEN + SILENT-VIDEO
MILESTONES PASSED; GENERAL ENGINE NOT COMPLETE.**

ComputeExchange now has a real, pinned Blender/Cycles path from the Rust agent to a
resident Python-controlled Metal renderer. On this Apple Studio it delivered a
1920x1080 classroom preview in **2.820610 s** versus a fresh **159.969738 s**
4096-sample Cycles render in the same resident session: **56.714589x**. A second
exterior/glass/water scene measured **55.926238x**. A glossy studio scene needed
32+32 samples to clear quality and measured **34.757412x**. All three selected
drafts passed independent 4096-sample audits, but they are visibly noisy and are
intentionally labeled `preview_only`, `production_ready=false`,
`artifact_verified=false`, and `receipt_trust=local_unattested`.

An official Fishy Cat Cycles scene then added real legacy particle-hair coverage.
After calibration on frames 1 and 50, the frozen 7+7-spp recipe measured
**54.541695x** on previously unrendered frame 100: 92.931794 s reference versus
1.703867 s product.

The official Blender Benchmark 2.0 Koro scene then transferred the frozen 4+4-spp
recipe to a locally operator-declared held-out portrait frame at **84.250743x**:
112.396726 s reference versus 1.334074 s product at 1080x1920. This makes four
materially different scene families above 50x in bounded single-frame preview
experiments; it is still not arbitrary content. It does **not** prove 50-100x for
all scenes, animation, video, audio, token decoding, cold start, delivery-quality
output, production control-plane execution, or buyer billing. Ratios from
different modalities must never be multiplied.

A separately hashed Stylized Levi compatibility derivative now adds real
armature/lattice deformation coverage across two visibly different poses. Both
1080p poses passed at 1+1 spp, but a measured 256-to-512-spp baseline slope
projects only **35.697660x** at 4096 spp. The expensive final was therefore
pruned; this is coverage and a negative optimization result, not a fourth 50x
scene family.

The receipts are
[`apple-metal-render-spec-preview-2026-07-12.json`](../../proof/performance/apple-metal-render-spec-preview-2026-07-12.json)
for classroom,
[`apple-metal-render-transfer-pavilion-2026-07-12.json`](../../proof/performance/apple-metal-render-transfer-pavilion-2026-07-12.json)
for Pavilion,
[`apple-metal-render-transfer-bmw27-2026-07-12.json`](../../proof/performance/apple-metal-render-transfer-bmw27-2026-07-12.json)
for BMW27, the aggregate
[`apple-metal-render-transfer-matrix-2026-07-12.json`](../../proof/performance/apple-metal-render-transfer-matrix-2026-07-12.json),
the held-out hair receipt
[`apple-metal-render-transfer-fishy-cat-2026-07-12.json`](../../proof/performance/apple-metal-render-transfer-fishy-cat-2026-07-12.json),
and its
[`source/selection manifest`](../../proof/performance/apple-metal-render-transfer-fishy-cat-provenance-2026-07-12.json).
The bounded BMW integrator screen is
[`apple-metal-render-bmw-integrator-screen-2026-07-12.json`](../../proof/performance/apple-metal-render-bmw-integrator-screen-2026-07-12.json),
and the deformation screen is
[`apple-metal-render-deformation-stylized-levi-screen-2026-07-12.json`](../../proof/performance/apple-metal-render-deformation-stylized-levi-screen-2026-07-12.json).
The Koro receipt and source/selection record are
[`apple-metal-render-transfer-koro-portrait-2026-07-12.json`](../../proof/performance/apple-metal-render-transfer-koro-portrait-2026-07-12.json)
and
[`apple-metal-render-transfer-koro-portrait-provenance-2026-07-12.json`](../../proof/performance/apple-metal-render-transfer-koro-portrait-provenance-2026-07-12.json).
The hair proof is checked by `verify_render_hair_transfer.py`; it remains separate
from the fixed-code three-scene matrix because the lossless PNG transport pin
changed. The sequence receipt is
[`apple-metal-render-sequence-video-2026-07-12.json`](../../proof/performance/apple-metal-render-sequence-video-2026-07-12.json).
The controlled-camera motion receipt is
[`apple-metal-render-sequence-bmw-camera-pan-2026-07-12.json`](../../proof/performance/apple-metal-render-sequence-bmw-camera-pan-2026-07-12.json),
and the separate Pavilion lighting-frame receipt is
[`apple-metal-render-transfer-pavilion-frame125-2026-07-12.json`](../../proof/performance/apple-metal-render-transfer-pavilion-frame125-2026-07-12.json).

## Measured result

| Field | Measured value |
|---|---:|
| Host | Apple M3 Ultra, 60-core Metal GPU, 96 GiB |
| Renderer | Blender 4.2.1 LTS, Cycles `GPU/METAL` |
| Scene | classroom, 53-file / 124,862,142-byte private snapshot |
| Loaded external dependencies | 48, all resolved inside the snapshot |
| Resolution / frame | 1920x1080 / frame 1 |
| Candidate | 24 spp draft + independent 24 spp verifier |
| Candidate sample ranges | `[0,24)` and `[24,48)` |
| Reference | 4096 spp, disjoint range `[48,4144)` |
| Uncharged warmup | 3.715965 s |
| Measured reference | 159.969738 s |
| Measured product path | 2.820610 s |
| Speedup | **56.714589x** |
| Draft-vs-verifier gate | 0.919388044 global / 0.850837691 region / 0.800344128 microtile |
| Draft-vs-reference audit | 0.944790974 global / 0.902559096 region / 0.866869677 microtile |
| Required gate | >=0.90 global / >=0.85 region / >=0.70 catastrophic microtile |
| Prior rendered artifact/reference cache | none; renderer/Metal caches warm after the declared candidate warmup |
| Trial count | 1; fixed order; no variance estimate |

The agreement metric is `1 - mean absolute display-RGB difference`. Alpha is
excluded because the opaque alpha channel otherwise gives a free 25% score. The
regional gate is a balanced, aspect-preserving grid capped at 16 regions along
the long edge (16x9 here). A separate balanced ~32-pixel microtile grid rejects
small catastrophic defects that the relative regions could dilute. These are
display-RGB engineering gates, not SSIM or perceptual equivalence.

The high-sample reference is measurement-only. It does not select the candidate.
The product accepts the draft from the two disjoint low-sample renders, then the
benchmark comparator can remove the speed claim if the selected artifact does
not agree with the untouched reference.

## Stage 2 transfer matrix

| Scene family | Low-sample product | Reference / product | Speedup | Product gate (global / region / micro) | 4096 audit (global / region / micro) | 50x |
|---|---:|---:|---:|---:|---:|---:|
| Classroom interior GI | 24+24 spp | 159.969738 / 2.820610 s | **56.714589x** | 0.919388 / 0.850838 / 0.800344 | 0.944791 / 0.902559 / 0.866870 | yes |
| Pavilion exterior/glass/water | 24+24 spp | 139.433730 / 2.493172 s | **55.926238x** | 0.970091 / 0.940589 / 0.923459 | 0.978710 / 0.957506 / 0.946234 | yes |
| BMW27 glossy studio | 32+32 spp | 59.313663 / 1.706504 s | **34.757412x** | 0.976217 / 0.861710 / 0.769095 | 0.982149 / 0.876102 / 0.801067 | no |

Each row is one 1920x1080 frame, one fixed-order trial after an uncharged warmup,
with no prior rendered artifact/reference-cache reuse and no variance estimate;
normal renderer/Metal caches were warm. BMW27 at 24+24 spp failed the independent
product gate (worst region 0.828513) and repaired, so it was not allowed to borrow
the classroom/Pavilion headline. The first tested BMW setting that selected the
draft was 32+32 spp; its quality passed but the extra verifier work capped the
measured ratio below 50x.

### Held-out particle-hair extension — 54.541695x preview

The Blender Foundation-hosted `splash_fishy_cat_2.zip` archive was downloaded and
SHA-256 pinned without modifying its `fishy_cat.blend`. Blender 4.2.1 opens it as
Cycles with three `HAIR` particle systems (13,355 parent particles across the cat
and grass) plus two animated sparkle emitters. Frames 1 and 50 were used for
calibration; their decoded draft images differ across the full frame. Frame 100
was kept unrendered until the code and recipe were frozen.

The first 4096-spp frame-50 run reached **47.184459x** with every gate passing.
Its charged product path still spent time losslessly deflating four noisy PNGs.
Setting the transient local preview transport to PNG compression 0 changed no
decoded pixel and reduced the frame-1 calibration product from 1.972768 s to
1.811369 s. The worker reports this setting in its handshake and every artifact
manifest binds it.

On the untouched end frame, the reference-free draft/verifier gate scored
0.954716 global / 0.858248 regional / 0.719625 microtile. The selected draft then
scored 0.966572 / 0.866972 / 0.713342 against a fresh, disjoint-range 4096-spp
baseline. No repair or prior rendered artifact/reference cache was used; normal
renderer/Metal caches were warm after the declared uncharged candidate warmup.
The measured ratio was **54.541695x** (92.931794 s / 1.703867 s). The microtile
margin is narrow, the output remains
visibly noisy, and this is one fixed-order trial; it extends hair coverage but
does not establish a distribution or modern curves-hair, motion-blur, or volume
coverage.

### Held-out deformation extension — quality passed, 4096 run pruned

The Blender Foundation-hosted `stylized_levi.zip` source is an EEVEE-era 2015
file, not a native Cycles benchmark. Its dormant `pose_test` action has 397
f-curves over frames 6-7, while 41 armature and six lattice modifiers provide
real deformation structure. The source remains unchanged. A separately hashed
derivative binds that action to its 69-bone rig, selects the closer source
camera, removes one zero-user external image pointer, and replaces unsupported
legacy material nodes with an explicit Principled compatibility graph. It is
not claimed appearance-equivalent to the original EEVEE material graph.

All 69 pose-bone matrices change between frames 6 and 7. At 512x288, their
selected drafts have 0.942467 global agreement with each other and only 0.563992
agreement in the most affected region/microtile; the subject occupies 26.7% and
28.9% of pixels. This is a meaningful pose change, not two equivalent stills.
At 1080p, the frozen 1+1-spp recipe passed both poses. Frame 6 scored 0.992453 /
0.927052 / 0.843866 and frame 7 scored 0.991720 / 0.914562 / 0.814161 against
their independent 256-spp audits; their reference-free product gates also passed.

For frame 7, the measured 256-spp baseline was 1.818856 s and the 512-spp
baseline was 3.690040 s. Linear extrapolation of that measured slope gives a
29.886616-s 4096-spp estimate; divided by the 0.837215-s product path, that is
**35.697660x**. The projection is cross-run arithmetic, not measured speedup.
A 50x result would require 41.860750 s, so a 4096-spp final was not run.

### Locally operator-declared held-out rigged/furry character extension — 84.250743x preview

The untouched official Blender Benchmark 2.0 `koro.tar.bz2` bundle contains a
portrait Cycles character scene plus its live textures. Blender audits 180
objects, a 1,364-bone rig with 623 drivers and 3,613 pose constraints, 10
armature, 31 lattice and one mesh-deform modifier, and seven legacy
`INTERPOLATED` particle-hair systems. Those systems contain 1,596 stored parent
particles; the scene settings configure 226,080 viewport children, while its
render-child multipliers imply up to 806,530 render children. This is a
substantially different rigged/furry character family, but it is still legacy
particle hair—not modern curves hair—and motion blur is disabled.

Frames 1 and 7 were used for calibration. At 288x512 on frame 7, 2+2 spp repaired
while 4+4 and 8+8 spp selected the draft; 4+4 was therefore the lowest passing
setting and it also selected on frame 1. The two 4+4 selected images differ by 2.95%
mean RGB, with 0.868712 agreement in the most affected region. The frozen
1080x1920 frame-7 screen then passed its reference-free product gate at 0.974653 /
0.877845 / 0.847452 and projected comfortably above 50x. The local operator record
declares that frame 9 remained unrendered until the code and recipe were frozen.
The five retained calibration receipts contain only frames 1 and 7, but retained
files cannot independently prove that no earlier or deleted frame-9 run existed.

On frame 9, the product gate scored 0.974675 / 0.877208 / 0.841626. The selected
draft scored 0.979189 / 0.909816 / 0.866175 against a fresh, disjoint-range
4096-spp reference. No repair or prior rendered artifact/reference cache was
used; normal renderer/Metal caches were warm after the declared uncharged
candidate warmup. The same-session measured ratio was **84.250743x**
(112.396726 s / 1.334074 s). This is one fixed-order, local-unattested portrait
trial with no variance estimate; it does not authorize
an arbitrary-character, animation, modern-hair, production, or 100x claim.

The immutable controller receipt retains a legacy `quality_gate_spec` summary
that names only the global and regional thresholds. It is not the authoritative
schema-v2 policy: the pinned verification manifest also requires the >=0.70
microtile sentinel, and the independent verifier recomputes all three scores from
the retained draft, verifier and reference PNGs.

Pavilion frame 125 was then run as a separate lighting-animation binding. It
measured **55.330681x** (138.051267 s reference / 2.495022 s product) and cleared
the 4096-spp audit at 0.978692 global / 0.957388 region / 0.946284 microtile.
The high-sample frame-1/frame-125 images differ only subtly, so this is useful
fresh-frame evidence, not broad motion coverage or a repeated-trial distribution.

The official Pavilion archive lacks `//textures/water bump.jpg`, referenced only
by an unused legacy texture datablock. The hardened worker correctly rejected the
missing path. The measured run uses a separately hashed derivative that removes
only that zero-user datablock and rewrites the 14 live image paths to its private
bundle. The official scene remains unchanged. At the same seed, sample range and
512x288 settings, an in-memory-sanitized official render and the derivative scored
0.999999973 global and 0.999998723 worst-region/microtile RGB agreement. Source,
derivative, bundle, image and receipt hashes are recorded in the aggregate matrix.

### BMW quality/cost calibration — no new speed claim

Two follow-up recipes were deliberately pruned. Full-frame OpenImageDenoise made
the 24+24 BMW result agree strongly with the 4096-spp image, but the measured
resident render pair alone took 3.743053 s, making 50x impossible against the
59.313663-s baseline.

An offline 8+8-spp Gaussian-radius-0.75 calibration initially cleared the existing
display-RGB L1 gates and had a 0.863505-s resident render pair. It is **not** an
accepted 50x result. Its partial timing omitted delivery persistence, publication,
manifests and agent wall time; the radius was selected on the same scene/seeds and
reference; and the postprocessor dependencies were not receipt-pinned. A stricter
audit exposed the bigger problem: global SSIM was 0.778829, the worst 16x9 region
was 0.146194, and gradient energy was 2.225x the clean reference. The filtered
preview remained visibly noisy even though L1 passed. The calibration receipt is
[`apple-metal-render-bmw-quality-calibration-2026-07-12.json`](../../proof/performance/apple-metal-render-bmw-quality-calibration-2026-07-12.json).

Any renewed postprocessing attempt must add a preregistered two-sided detail/noise
gate, pin the image stack, charge artifact encoding/publication, and validate with
fresh seeds and held-out scenes. A blur that merely makes two noisy images agree is
not a general-engine improvement.

### BMW integrator screen — eight arms pruned

An eight-arm, 1080p/256-spp screen separated the PNG-compression transport gain
from bounce caps, light-tree sampling and adaptive sampling. Every primary arm
selected its draft and also passed a post-hoc comparison to the retained
4096-spp BMW image. These are single trials across two code generations; ratios
against the historical 59.313663-s baseline are explicitly cross-session
projections, not measured speedups.

The current native control took 1.480195 s, projecting to 40.071520x. The fastest
arm, cap-8, took 1.472072 s and projects to **40.292637x**—only 0.55% faster than
the control, within an unreplicated/no-variance screen. Cap-16, cap-12 and
adaptive project to 39.607980x, 36.941398x and 37.600978x. Light-tree and the two
combined arms were substantially slower, projecting to 23.68-24.59x. None met
the 1.186273-s product cutoff required for 50x, so no new 4096-spp run was
authorized.

Non-native profiles remain private benchmark-screen controls. They require a
fresh 256-bit harness capability bound to the fixed benchmark unit, cannot be
selected by a buyer payload, and force a full-frame reference repair on any
product-gate miss. The ordinary preview/sequence path is pinned to `native`.
Artifact, verification, audit and sequence manifests moved to schema v2 with an
explicit v2 binding-policy domain; historical v1 proofs remain immutable.

## What was corrected before accepting the number

The earlier ~74x observation was discarded. Its copied `.blend` omitted linked
assets and textures, producing magenta/missing materials. A later full-bundle
24+24 run also correctly failed because a fixed 32-pixel grid created 2,040
worst-tile chances at 1080p, then escalated to a full render and reported 0.98x.
Neither is the published result.

The accepted implementation now:

- snapshots the bounded scene root, not only the main `.blend`, and records a
  canonical hash for every file and the whole bundle;
- rejects symlinks, special files, out-of-bundle Blender dependencies, device
  fallback, malformed worker frames, output replacement, and pin changes;
- reports the actual Blender version/build, enabled Metal device name, and
  dependency count/hash in the child handshake;
- binds lossless PNG compression into both the child handshake and artifact
  manifests; compression 0 removes charged local transport work without changing
  decoded pixels;
- keeps experimental non-native integrator profiles behind a private
  benchmark-unit capability, binds their scope into the request identity, and
  uses full-frame reference repair rather than mixed-profile composites;
- uses independent seeds and non-overlapping Cycles sample offsets;
- binds the signed Blender executable and all 5,539 regular files in the local
  app bundle, including existing Python bytecode, while disabling bytecode
  regeneration in the child;
- performs initial SHA-256 reads and then cheap pre/post-render file-stat and
  bundle-file-set sentinels; hash-to-sentinel capture uses the same open file
  descriptors;
- keeps the Rust driver as the outer process-group/timeout authority, while the
  direct benchmark harness owns its local Blender process group;
- prevents evidence relabeling and contradictory canonical receipts;
- requires an accepted, disjoint-range draft before setting the 50x preview
  experiment boolean, including the fixed-scale catastrophic microtile check;
- records cold-start exclusion, one-trial/no-variance status, execution order,
  code pins, renderer fingerprint, artifact hashes, and local-unattested scope.

## Front-to-back status

The repository still contains several partially connected systems:

1. `spec-engine/` is the canonical Rust protocol/receipt core, with synthetic
   adapters and tests.
2. `scripts/spec-lab/` contains the real Cycles/ffmpeg experiments and the new
   resident local Metal backend.
3. `agent/` owns the executable boundary. Its local `spec-render-preview` CLI
   already accepts 1-4096 units; the live polling request remains scalar.
4. `control/` has additive render quote/receipt scaffolding, but this local
   preview is not admitted, scheduled, attested, stored, billed, or delivered by
   the production control plane.
5. The bespoke token-resident, transcode, video, and audio work does not yet run
   as one production engine. Existing modality ratios describe separate lanes.

The local Rust path was exercised with a real two-frame classroom request. Both
outputs came from one resident Metal worker and carried the same non-null
renderer/dependency identity. At 16+16 spp the frames took the bounded repair
path, which also exercised fallback.

Stage 1 is now implemented in `run_local_cycles_spec_sequence.py`. A real
eight-frame 512x288 classroom run sent one multi-unit request through the compiled
Rust agent and one resident Metal Blender. Six frames were accepted drafts and two
used the bounded 64-spp repair. The render product path was 5.729000 s and enclosing
agent wall time was 5.953638 s. Pinned FFmpeg 8.1.1 then produced an eight-frame,
8-fps, one-second silent H.264/yuv420p MP4 through `h264_videotoolbox`; encode plus
decode/ffprobe validation took 0.442324 s. The 1,268,545-byte video and every input
PNG/manifest were digest-checked. This is packaging evidence, not a new speedup.

A second real sequence uses a separately hashed BMW27 derivative with a linear
one-unit lateral camera move over frames 1-8; the official scene is untouched.
Through the same Rust/Metal/VideoToolbox path, seven 32+32-spp frames selected the
draft and one used bounded 128-spp repair. Render product time was 5.501380 s,
agent wall time 5.693636 s, and packaging/validation 0.488839 s. The decoded,
probed one-second MP4 is 701,073 bytes with SHA-256
`1160ff05f71db8fd50e9a4050016ecdfcc7fa8525974a7df64f0bad223cdc5c2`.
Endpoint visual QA confirms meaningful camera motion. This is motion and packaging
evidence only; no sequence baseline or speedup is claimed.

The Rust Whisper-tiny hardware smoke now uses a 16.1-second 16 kHz PCM fixture,
deliberately crossing Candle's 15-second padding boundary. That path produces
4,500 raw band-major mel frames and passed only after each band was cropped to the
encoder's exact 3,000-frame input. Five fresh-process runs of the already-built,
warm-cache release test passed in 0.17-0.18 s external wall time. The local proof
`apple-metal-whisper-boundary-smoke-2026-07-12.json` pins the source, release test
binary, model snapshot and five timings. The tone is transcribed as `[Bell]`
and the test output does not attest which device executed, so the approximately
95 audio-seconds/wall-second observation is illustrative smoke data, not a
Metal performance or speech-quality claim.

The control plane now has authenticated, strictly bounded multipart audio quote
and submit paths. It accepts only canonical PCM16 mono 16 kHz WAV, at most 30
seconds and 1 MiB; binds server-derived sample/duration facts to the deterministic
one-record JSONL hash; prices catalogue USD/audio-minute rather than base64 bytes;
and fixes each submission at one primary plus one same-class redundancy task.
Generic JSON and pipeline audio paths fail closed. This is still a hardened
development foundation: idempotent submit recovery, durable job-level pricing
authority, object retention/deletion, and live DB/object-store endpoint coverage
remain open. It proves transcription only—not speculative audio generation,
audio encoding, or A/V muxing.

The Python SDK and `cx` CLI also reject audio locally instead of sending it through
the generic JSON boundary. Neither client exposes the dedicated multipart surface
yet; the bounded `curl` workflow in `docs/QUICKSTART.md` is the only documented
development client path.

The first transcript-bearing screen also exposed and fixed a baseline correctness
bug: the multilingual `openai/whisper-tiny` prompt omitted `<|en|>`, which is not
language auto-detection and produced blank or repeated non-English output on clear
English TTS. The runner now uses the explicit four-token English/no-timestamps
prompt and rejects unsupported language/timestamp controls instead of ignoring
them. `whisper_decoder_kv.rs` is now part of the worker inference content identity,
so future decoder changes move the byte-exact verification class.

N-gram Whisper token speculation was then screened and pruned before implementing
multi-token KV mutation. Six order/window configurations were selected on four
calibration clips; order 1/window 4 had the best zero-overhead target-call ceiling
at 1.164557x. On four frozen held-out TTS clips it fell to **1.085366x**, accepted
7/44 attempted tokens, and improved only the one deliberately repetitive clip;
three clips stayed at 1.0x. That is a target-call ceiling before draft, verification,
rollback or orchestration cost—not wall-clock speedup—so building the missing
transactional verifier is not justified. The pinned pruned proof is
`apple-metal-whisper-ngram-screen-2026-07-12.json`.

A fresh Studio-local x264 speculation sanity run was also negative: a 4-second
320x180@12 fixture measured **0.334154x** (0.567637 s slow baseline versus
1.698731 s product path). More importantly, the baseline recipe scored only
0.902565 against the requested 0.95 SSIM gate, so all four repairs remained
unresolved and the adapter correctly published no delivery artifact. This is a
pruned recipe, not a video speed claim; old standalone codec ratios are not
combined with the render result.

## Execution plan

### Stage 1 — local image sequence and silent video — DONE locally

1. The bounded contiguous-range wrapper, strict ordered artifact validation,
   identical renderer/bundle checks and content-addressed sequence manifest are
   implemented.
2. Optional pinned VideoToolbox H.264 packaging now verifies codec, pixel format,
   geometry, FPS, frame count, duration, absence of audio and a full decode.
3. No-clobber publication uses private same-directory temporaries and atomic hard
   links; a concurrent creator's destination is never removed.
4. The third-party Stylized Levi derivative now covers two real armature/lattice
   poses and prunes a sub-50x recipe. Motion blur and a broader frame range remain.
   A sequence claim still requires repeated trials and p50/p95.

### Stage 2 — quality generalization before production claims — IN PROGRESS

1. The first static matrix covers interior GI, exterior/glass/water and glossy
   studio content. Held-out Fishy Cat and rigged/furry Koro scenes exceed 50x;
   a separate armature/lattice deformation screen passes quality but projects
   below 50x. Add volume, modern curves hair and motion blur rather than treating
   these bounded scenes as general coverage.
2. The fixed-scale localized-defect gate transferred without weakening its
   threshold. BMW correctly exposed a quality-limited 24+24 recipe.
3. Draft acceptance, repair behavior, reference agreement and steady state are
   measured for the three rows. Cold start, memory pressure and multi-frame
   distributions remain open.
4. Preserve fail-open-to-reference behavior. A scene that repairs is not allowed
   to inherit the accepted-draft 50x headline.
5. Repeat the valid recipes enough to publish variance and thermal/power state.

### Stage 3 — live agent and control-plane integration

1. Extend the live request schema from one frame to a bounded contiguous
   sequence while preserving the local CLI contract.
2. Add typed sequence artifact ingestion and server-side cardinality/digest
   verification.
3. Bind job, scene bundle, policy, code, renderer, device, artifact and receipt
   identities before storage.
4. Keep the lane non-billable and preview-only until authoritative attestation,
   admission, lease/retry behavior, artifact storage, and reconciliation tests
   pass end to end.
5. Only then attach the existing render quote/receipt scaffold to normal
   scheduling and buyer delivery.

### Stage 4 — real video and audio modalities

1. Drive the existing real ffmpeg transcode adapter through the same canonical
   unit/receipt boundary and measure it on pinned clips.
2. Add frame interpolation/residual video as its own draft/verify/repair policy;
   keep render and codec ratios separate.
3. Define audio units as timestamped PCM blocks with exact sample-rate/channel
   contracts. Draft, verify, and repair blocks; validate continuity, clipping,
   alignment, perceptual metrics, and exact duration.
4. Build an A/V packaging receipt that composes artifacts and timings without
   multiplying independent speedups.

### Stage 5 — token and general engine convergence

1. Route the resident token implementation through the same owned engine instead
   of leaving parallel/orphaned paths.
2. Optimize Metal kernels only after the routed receipt is lossless and measured;
   the current copy-heavy Metal token prototype is not a 50x result.
3. Consolidate modality-neutral admission, cancellation, deadlines, repair,
   artifact authority, telemetry and receipts. Keep modality-specific quality
   policies behind typed adapters.

## Verification completed

- `python3 -m unittest discover -s scripts/spec-lab -p 'test_*.py'`:
  **533 passed, 2 optional real smokes skipped**.
- `verify_render_transfer_matrix.py` rechecked all three receipt hashes, shared
  execution identity, ranges, speedups and summaries, plus all three available
  local verification manifests; its **11 focused mutation tests passed**. It
  explicitly reports that exact product metrics and Pavilion source/probe data
  are local corroboration rather than v1 receipt-bound evidence.
- `verify_render_hair_transfer.py` rechecked the held-out Fishy Cat source,
  receipt, manifests, PNGs and 13,355 audited hair parents; its **6 focused
  mutation tests passed**.
- `verify_render_koro_transfer.py --require-local-artifacts` rechecked the exact
  raw receipt, v2 operator-policy binding, nine-file source bundle, all five
  disclosed calibration receipts,
  schema-v2 portrait manifests and seven retained files, then recomputed the
  draft/verifier and draft/4096 gates; its **12 focused mutation tests passed**.
  It anchors raw receipt SHA-256 `b1ca40bc63288ad8736b76592c24a9baffe168f752c8309ac6925cc2c22f19c8`
  and provenance SHA-256 `0b674267a509adcb58703ef6a8c2d25fdd4f819837ecbcf3c59514123d73a65f`.
- The BMW integrator-screen verifier rechecked all eight arms and their retained
  4096 audits (**7 mutation tests passed**). The Stylized Levi deformation
  verifier rechecked five runs, 35 local files and the pose-change metrics
  (**8 mutation tests passed**); both remain explicitly pruned negative screens.
- `cargo test --manifest-path agent/Cargo.toml render_preview -- --nocapture`:
  **8 passed**.
- Sequence wrapper tests: **8 passed**; the separately enabled real VideoToolbox
  smoke also passed.
- Hardened reference-cache plus repair tests: **62 passed**.
- Installed-Blender Metal smoke through the Python driver and compiled Rust
  `cx-agent`: passed.
- Real two-frame Rust/Metal classroom smoke: two ordered artifacts, identical
  worker identity, valid hashes: passed.
- Canonical proofs, code pins, manifests, PNG hashes and visual scene assets:
  independently rechecked after the runs.
- Real eight-frame Rust/Metal-to-VideoToolbox sequence: passed with a pinned,
  decoded, probed and hashed silent MP4.
- Real Whisper-tiny PCM-to-transcript smoke: passed (device unattested).
- The strengthened 16.1-second Whisper boundary smoke passed five fresh-process
  release trials; proof SHA-256
  `4ac19b5bf97c5c75fbddd2158e80c1b6c193f44097766be5d9f9a7f1277eb9d3`.
- Control audio intake/HTTP/pricing tests and the complete control module passed;
  `go vet ./...` also passed. Rust decoder boundary tests passed 4/4 and mel-shape
  tests passed 2/2.
- The generated API/client contract was refreshed for 104 authenticated/public
  routes and remains explicitly `IN_PROGRESS` for broad client support.
- The isolated Python SDK package verifier passed all 5 tests after stripping
  copied build artifacts from its throwaway source tree. CLI tests and vet passed,
  and both generic CLI audio entry points fail locally with the dedicated endpoint.
- The English-prompt Whisper baseline produced intelligible, EOT-terminated target
  traces on all eight frozen TTS fixtures. The n-gram held-out ceiling was pruned at
  1.085366x; proof SHA-256
  `b700549827325feeb46760c6cc5db89a731cc7c6e0a8e0db3565e96b6de0ebaf`.

## Known remaining integrity work

The historical render-reference cache is not used by these measured render proofs.
Its v2 path is
now separately hardened around scene/dependency and scene-script content, Blender
executable/runtime/device identity, exact finite float32 arrays with content
digests, unpredictable no-follow staging, locked atomic publication and invalid
cache quarantine. It remains a different reference/repair pipeline and must not
be substituted into this receipt without a new comparable run.

The audio upload route must not be described as production-ready yet. A retried
submit can create a second billable job because submission idempotency is not
implemented; jobs persist normalized JSONL containing the base64 audio and have
no terminal retention/deletion sweep; and sample count, raw digest and per-minute
price are visible in persisted quote JSON and a best-effort job event, not yet a
durable `economic_input_authority` job column. Live Postgres/object-store quote,
submit, quote-binding and two-supplier redundancy tests are also still required.

The next safe implementation step is the remaining held-out half of Stage 2:
volume, modern curves-hair and actual motion-blur scenes, followed by repeated
timing/thermal trials on the valid >50x recipes. BMW bounce/light-tree/adaptive
controls and the Stylized Levi deformation recipe are already bounded negative
screens. Any renewed optimization must reduce cost while keeping the existing raw
RGB gates and 4096-sample audit; weakening the threshold is not an optimization.
This broadens the measured result without prematurely widening billing or
production authority.
