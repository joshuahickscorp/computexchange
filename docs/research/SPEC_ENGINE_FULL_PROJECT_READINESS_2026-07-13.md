# Spec engine full-project readiness and customer trade-offs

> **Superseded on 2026-07-13.** This design snapshot predates the final
> descriptor-retained measurements, bundle schema v2 directory/stale-snapshot
> hardening, project-contract resource binding, and closed transport replay.
> Use [`SPEC_ENGINE_WHOLE_PROJECT_PASS_2026-07-13.md`](SPEC_ENGINE_WHOLE_PROJECT_PASS_2026-07-13.md)
> for current numbers, tests, and blockers.

Date: 2026-07-13

Status: audited design boundary plus additive project-bundle preflight. No production admission, billing, or delivery authorization was enabled.

## Bottom line

There is still a point in pushing speed, but the next valuable boundary is not another preview micro-optimization. It is making speculation a safe implementation detail behind a final-render contract:

- an exact, already-verified rerender can be extremely fast through content-addressed reuse;
- a new shot may use draft, verify, repair, temporal, and spatial paths;
- any uncertain or failed speculative result must fall back to ordinary pinned Cycles; and
- the customer receives only a verified final artifact, regardless of which internal path won.

The current 9444.805x result is exact-request byte transport. The current 212.992x result is a narrow fresh, independently gated preview experiment. The 352.885x one-render result is an explicitly unauthorized upper bound. None is evidence that an arbitrary full Blender project can be delivered 213x faster at final-render semantics.

Practically, the product should sell a quality/output/deadline contract, not a universal speedup. Cache-heavy iterative workflows can approach transport latency. First renders of novel or difficult scenes may be much slower and can legitimately fall all the way back to basic Cycles.

## What the customer would experience

1. The customer uploads one project bundle and chooses frames, cameras/view layers, output format, passes, quality tier, and deadline.
2. A pinned Blender preflight verifies that every referenced dependency is present, the scene can open without network access or auto-executed code, the requested output is supported, and the project fits bounded resources.
3. The service returns a conservative quote that reserves enough budget for the full Cycles fallback. It may identify an exact verified cache hit, but does not promise a scene-agnostic multiplier.
4. Internally, the engine tries the cheapest authorized path: exact reuse, resident-scene reuse, spatial/temporal speculation, selective repair, or a full reference render.
5. Server-chosen verification checks the artifact. Any ambiguity, unsupported feature, worker crash, quality miss, or evidence mismatch triggers a retry or the pinned basic-Cycles fallback. The quality threshold never loosens to save time or margin.
6. Only a sealed object-store artifact and its authoritative verification receipt can complete the job and release money. Temporary drafts and local manifest paths are never customer deliverables.

That gives a simple customer promise: acceleration is opportunistic; output fidelity and recoverability are mandatory.

## Basic Cycles versus the target spec-render product

| Customer concern | Basic pinned Cycles | Target spec-render product | Trade-off |
|---|---|---|---|
| New unique frame | Runs the requested reference recipe once | May run drafts, independent checks, repairs, and sometimes the reference fallback | Speculation can be much faster when confidence is strong, but a hard scene can cost extra internal work before fallback |
| Exact rerender | Normally renders again unless the application has its own cache | Can deliver a byte-identical, already-verified artifact from an exact content/recipe key | Very large win, but only when every scene, dependency, policy, renderer, frame, and output-contract digest matches |
| Image fidelity | Faithfully executes the requested Cycles settings, subject to ordinary sampling noise and renderer bugs | Must prove equivalence at the contracted tier before delivery | Verification adds latency and complexity; reference-free agreement alone can miss correlated bias |
| Temporal consistency | Native sequence rendering follows the scene at each frame | Temporal prediction/reuse can introduce flicker, ghosting, or stale geometry if not separately gated | Temporal speedups require unseen-sequence trials and frame-to-frame gates, not post-hoc reference checks |
| Features and formats | Blender/Cycles supports its normal scene, compositor, sequencer, view, pass, and output surface | Must preserve the same requested semantics or fall back | The current preview worker does not: it forces PNG RGBA 8-bit and disables compositing, sequencer, Freestyle, multiview, and several scene output choices |
| Reliability | Fewer moving parts, but a renderer/worker failure still needs retry | More stages and therefore more possible internal failures | A durable fallback state machine can make customer-visible failure less likely than a single path, provided it is idempotent and fully tested |
| Price | Reference compute is paid every time | Cache/spec wins can reduce cost; misses and fallback consume more internal compute | Admission must reserve worst-case cost. The platform, not the buyer, should absorb a speculative attempt that fails its own quality gate |
| Predictability | Runtime is comparatively straightforward to model from scene history | Runtime is a mixture of cache hit, accepted draft, repair, and fallback | Quotes need ranges/deadlines and honest evidence labels, not a universal “200x” promise |
| Auditability | A normal renderer log and output | Content, policy, artifact, verifier, fallback, and settlement identities can be bound in one receipt | More evidence is a moat, but only if the control plane—not a naked worker receipt—is the authority |

## Current codebase boundary

### What is already strong

- `scripts/spec-lab/cx_cycles_render_preview_backend.py` snapshots the entire bounded operator scene root, not just the main `.blend`; copies through no-follow descriptors; hashes every regular file; rejects symlinks and special entries; and revalidates the private snapshot before rendering.
- The resident Blender child opens only the private copy, uses a pinned executable and fixed child code, disables auto-execution, sanitizes its environment, bounds commands/time/output, and checks common external dependencies: linked libraries, file images, movie clips, sounds, fonts, cache files, and volumes.
- Artifact manifests bind the computed bundle digest, renderer identity, scene, frame, samples, seeds, device, policies, and output digest.
- `agent/src/render_preview.rs` independently pins the driver/backend/controller/Blender identities, bounds process trees and I/O, revalidates the closed result envelope, and structurally keeps it preview-only, unattested, non-production-ready, and non-billable.
- `control/runtime_matrix.go` rejects the preview job/model before storage because no advertised production runtime cell exists. The result validator accepts only the preview honesty envelope, and settlement has no production render authority.

### Immediate additive hardening landed

`scripts/spec-lab/cx_render_project_bundle_v1.py` now creates and verifies a deterministic project-bundle manifest outside the project root. It:

- treats every regular-file content type as opaque bytes, so textures, volumes, caches, audio, fonts, libraries, and unknown extensions can share one content identity;
- binds the lowercase `.blend` entry path/header/SHA, every file path/size/SHA, total bytes, file count, bundle SHA, and manifest SHA;
- rejects absolute/dot/parent paths, backslashes, NUL/control characters, non-NFC names, reserved/non-portable names, case-fold aliases, symlinks, hard links, devices, sockets, FIFOs, and non-regular entries;
- applies fixed file/directory/depth/path/byte/manifest limits;
- detects source-file and directory mutation during hashing;
- verifies the complete current file set rather than trusting a submitted list; and
- publishes new manifests without clobbering, retaining and rechecking the descriptor across file and parent-directory sync.

This closes a packaging/preflight gap, not the product authority gap. The existing v1 job contract still binds only the main `.blend` SHA. The new bundle and manifest digests must be carried through a versioned wire contract before control can prove that the worker rendered the project the buyer submitted.

### Why the current lane cannot deliver a whole project yet

- One request is one frame and one local operator-root scene; there is no buyer project-object reference, archive extraction contract, multi-frame checkpoint, or durable project upload lifecycle.
- The persisted job descriptor does not bind the full project bundle digest.
- The current worker intentionally changes final semantics: at most 4,194,304 pixels, PNG RGBA 8-bit output, compositing/sequencer/Freestyle/multiview disabled, one active scene/view layer, and CPU or Metal only in this backend.
- Common dependency classes are checked, but “every Blender dependency” is not yet proven. Image sequences/UDIMs, simulation and Geometry Nodes bakes, IES files, modifier-specific caches, custom nodes/add-ons, and future Blender data blocks need a version-pinned dependency census and fixtures.
- Auto-executed scripts/add-ons and network-fetched assets are intentionally unavailable. A project requiring them must be normalized at ingest, explicitly supported by a pinned extension image, or rejected/fallback-routed; silently enabling them would turn a `.blend` into code execution.
- Preview outputs are local relative manifest descriptors inside the worker output root. The preview envelope/result path is capped at 32 MiB and is not a final EXR/multilayer/multi-frame object pipeline; control validates the honesty envelope but does not ingest and seal those referenced local image bytes.
- The current two-low-sample agreement gate is useful preview evidence but cannot authorize final delivery by itself. Independent drafts can share a biased integrator, denoiser, reconstruction, or missing-asset error.
- No final-render runtime-matrix cell, catalogue price, worst-case economic plan, artifact-authority policy, or render-specific settlement rule exists. This is correct fail-closed behavior.

## Format and feature policy

“Every file type” cannot safely mean executing every file as code or claiming every Blender extension works. The support contract should be split:

- **Bundle transport:** any bounded, portable, non-linked regular file is allowed as opaque bytes and content-addressed by the new manifest.
- **Entrypoint:** v1 full-project execution accepts a `.blend` entrypoint only. FBX, USD, OBJ, glTF, and similar interchange files can be dependencies or must be imported into a normalized `.blend` in a separate pinned ingest step.
- **Renderer:** Cycles only at first. EEVEE, Workbench, third-party engines, add-ons, and arbitrary Python are separate versioned runtime cells with their own evidence.
- **Output:** enable formats/passes one tested cell at a time—initially PNG and OpenEXR, then multilayer EXR/AOV/Cryptomatte, TIFF/JPEG, multiview, compositor, sequencer, and Freestyle. Unsupported requests fail before billing or run through unmodified basic Cycles if that fallback cell supports them.
- **Dependencies:** use Blender’s version-pinned broad path census (`bpy.utils.blend_paths`) plus explicit scanners/fixtures for libraries, all external image source modes and tiles/sequences, movie/audio/font/volume/cache data, IES, simulation/bake directories, and modifier/node-specific paths. Every resolved path must stay beneath the extracted manifest root.

The matrix is a safety feature: an unknown combination is not “probably supported.” It is unadvertised and cannot be scheduled.

## Sequenced path to production

### Gate 1 — versioned full-project job identity

Add a new job contract without changing the current preview tag. Required immutable fields include project object key/version, manifest SHA, bundle SHA, scene SHA/path, frames, cameras/view layers, resolution, color management, output format/passes, reference and speculative policies, Blender/runtime image SHA, verifier policy SHA, maximum duration/storage, and requested quality tier.

Generate the Rust and Go wire forms from one schema and add the runtime cell as `wire_only` or `soak_only`. It remains absent from `generatedAdvertisedRuntimeCapabilities`.

### Gate 2 — immutable ingest and sandboxed preflight

Upload to a versioned object key; stream and hash every file against the manifest; reject archive traversal, links, sparse/resource bombs, collisions, and extra/missing bytes; then mount the extracted bundle read-only. Run the pinned Blender dependency census in an OS sandbox or disposable VM with no network, no operator home/add-ons, bounded CPU/GPU/memory/disk/PIDs/time, and a writable scratch/output mount only.

A Blender parser crash is a worker failure, never a partially admitted project. The sandbox/VM boundary is required because `--disable-autoexec` prevents expected script execution but is not a memory-safety boundary for a complex native file parser.

### Gate 3 — final-semantics executor and fallback

Create a final executor separate from the preview worker. It must preserve every requested scene/output semantic and record the actual Blender RNA values after frame evaluation. Split frames into idempotent chunks, persist checkpoints, and use deterministic object keys. Its state machine is:

`preflight -> speculate -> verify -> accept | repair -> verify -> accept | fallback Cycles -> verify -> seal`

Any unsupported feature, timeout, crash, missing evidence, or quality miss moves forward to fallback/retry; it never converts into an accepted speculative artifact. Repeated attempts use the same immutable project/policy identity and cannot double-publish or double-settle.

### Gate 4 — final artifact verification

Use server-selected, unpredictable verification work bound to the job snapshot. Check final dimensions, channels, bit depth, color space, alpha, frame/view/pass cardinality, artifact bytes, and format structure. For speculative delivery, add full-reference frames or tiles selected after commit, independent sample ranges, linear-light and perceptual/tile metrics, temporal flicker/geometry checks, and AOV/depth/normal/alpha consistency where contracted.

Reference-free agreement can route or reject; it cannot be the only final authority until unseen-scene studies establish a safe error bound. A verification miss falls back to full Cycles and quarantines the speculative artifact.

### Gate 5 — durable large-artifact authority

Render outputs go directly to bounded multipart object uploads, never through the 32 MiB preview JSON. Commit a small signed manifest containing every object key/version, SHA-256, bytes, frame/view/pass identity, renderer/policy identity, and completion nonce. Control seals those exact object versions, verifies them, and publishes a buyer artifact only from the sealed authority row. Abort/garbage-collect partial uploads after lease loss.

### Gate 6 — economics and customer contract

Admission reserves the worst-case reference render plus storage/egress within the buyer budget. Settlement occurs only after artifact verification and finalization. Exact-cache and accepted speculative paths can earn/charge their declared product price; an internally rejected speculative attempt is platform risk. A fallback receipt plainly says that full Cycles was delivered and never claims the rejected speedup.

Quotes expose evidence-bound scenarios (exact hit, expected speculative range, worst-case fallback and deadline), not a universal multiplier. Queue/provisioning/upload/download are reported separately from render time.

### Gate 7 — soak and failure campaign

Before advertising one production cell, run a corpus that spans:

- linked libraries, packed/unpacked textures, UDIMs/sequences, volumes, hair, particles, geometry nodes, simulation caches, motion blur, displacement, transparency, caustics, compositing, sequencer, fonts/audio, color management, AOVs, Cryptomatte, multiview, and long animation;
- corrupt and adversarial bundles, parser crashes, shader compile failures, out-of-memory/disk/PID/time limits, worker loss, lease expiry, control restart, object-store timeout, duplicate commit, verifier crash, cancellation, and retry/fallback races;
- CPU, Metal, CUDA/OptiX cells separately, never cross-comparing nondeterministic bytes as if they were one verification class; and
- cold/warm execution, first render, edits, exact rerenders, temporal discontinuities, camera cuts, topology changes, and high-motion frames.

Release gates should require zero silent corruptions, zero unsupported combinations admitted, zero duplicate artifact/settlement outcomes, complete fallback convergence in injected failures, and per-cell quality/latency distributions with independently replayable receipts. Only then promote that exact matrix cell from soak to advertised production.

### Gate 8 — combined render and inference

The spec engine can eventually coordinate render and inference, but “everything at once” needs explicit resource isolation and one end-to-end contract. Render, denoise/generation, and token workloads need separate GPU-memory reservations, queue priorities, deadlines, cancellation, and receipts so one resident engine cannot starve or corrupt another. No render multiplier may be multiplied by an inference multiplier. A combined claim exists only after one nested customer job is timed and verified end to end.

## Where more speed is worth pursuing

1. **Verified exact reuse:** highest practical return for iteration, farm retries, duplicate frames, and unchanged deliverables. Measure real hit rate and tail latency; the current modeled 1000x aggregate threshold requires about 99.910578% exact hits.
2. **Incremental scene compilation/residency:** cache BVH, shader, texture, and scene state by full project/policy identity while proving invalidation for every execution-affecting edit. This attacks the current render endpoint without lowering quality.
3. **Native reconstruction/encode/publication:** the one-render study shows this non-render stage is material at subsecond scale. Move copies/conversion/validation out of Python where evidence shows it helps, while retaining exact artifact checks.
4. **Predeclared temporal authorization:** unseen sequences, integrated wall time, camera-cut/topology/change detection, reference-free product decisions, and sampled post-commit references. The current >1000x temporal numbers are research estimates, not a product path.
5. **Scene-aware routing:** learn which exact feature/scene cells reliably clear a quality tier. Route hard or unsupported scenes directly to Cycles instead of paying for a speculative attempt that will fail.
6. **Parallel frame/tile scheduling:** useful for throughput and deadlines, but report wall-clock and total compute separately; fan-out is not an algorithmic speedup and can cost more.

The stopping rule is customer value: optimize while an end-to-end, final-semantics, independently verified path improves latency or cost after upload, queue, fallback, verification, storage, and failure rates are charged. Stop a branch when it reaches the target only by removing required work, weakening quality, narrowing evidence after seeing the answer, or shifting cost outside the measured interval.
