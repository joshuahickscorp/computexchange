# Site rebuild · T0 · teardown + claims audit (2026-07-02)

Phase 0 of the interactive-hero rebuild. Nothing new is built until this is committed.
Dash gate: no U+2014, no U+2013 in this file · the separator is the middot.

## Part A · teardown inventory (what dies)

| what | where | why it dies |
|------|-------|-------------|
| thesis "a verified spot market for batch inference" | web/index.html:173 (+ meta :15, :19) | scope widened to compute · "batch inference" as sole descriptor is dead |
| static `<picture>` hero, oracles-pair@{1,2,3}x | web/index.html:26, :164-170 | replaced by a live Three.js hero (Track A) · the stills become the honest WebGL fallback (Track B), re-rendered from the new tabletop scene |
| overhead-ish oracle scene | render/site/oracles.py | camera pitch and framing wrong for the new "look down at a desk" doctrine · superseded by render/build_scene.py (35 degree down-angle, perspective) |
| section rhythm 110px | web/index.html section.wrap margin | rebuilt to 160px void per the layout doctrine |
| how-row "drop your data · the pipeline is detected" | web/index.html:182 | widen wording to any workload (pipeline detection itself stays, it is real) |
| receipts dialog claims (11 rows) | web/index.html:225-235 | re-audited below · the "inference" framing widens, one general-compute row is added, nothing unevidenced survives |

Assets that STAY: cx-mark-white.png, knob-off@3x.png, the favicon set, the `/assets/site/{file}`
whitelist handler (control/api.go), the phone hand-off screen, the receipts-dialog pattern, the
one Cormorant monument ($0.001, a real catalogue price).

## Part B · scope audit · what the tree actually executes

The closed job-type contract is `agent/src/types.rs:60` (enum JobType) resolved by the runner set
in `agent/src/runners.rs` (`default_runners()` :2460). A variant is SHIPPED only if a `JobRunner`
impl executes it.

| capability | verdict | receipt |
|------------|---------|---------|
| embeddings (MiniLM, bge-small) | SHIPPED · verified | runners.rs:740 `impl JobRunner for EmbedRunner` |
| batch inference (Llama 3.2 1B) | SHIPPED · verified | runners.rs:1541 `impl JobRunner for BatchInferRunner` |
| transcription (Whisper tiny/base) | SHIPPED · verified | runners.rs:978 `impl JobRunner for WhisperRunner` |
| classification (top-1 label) | SHIPPED · verified | runners.rs:1737 `BatchClassificationRunner` |
| json extraction (schema) | SHIPPED · verified | runners.rs:1910 `JsonExtractionRunner` |
| rerank (cosine) | SHIPPED · verified | runners.rs:2048 `RerankRunner` |
| general compute · BYO container | SHIPPED · METERED, NOT output-verified · gated Linux+Docker+NVIDIA | runners.rs:2377 `CustomRunner` → sandbox.rs:85 `run_sandboxed` (real `docker run --gpus all`, no net, ro rootfs, caps dropped, timeout); types.go:80-82 "metered per GPU-second + reputation-trusted"; hardware.rs:406 `runs_custom` advertises `custom` only on the container lane |
| rendering (Blender), simulation, HPC | ROADMAP · not a shipped executor | NO render job type, NO Blender path, NO seeded price. These are EXAMPLE workloads for the metered `custom` lane, never a first-class verified capability. Named on the page only as the metered/roadmap lane, never as shipped-and-verified. |
| image generation | KILLED · enum stub, no runner | types.rs `ImageGen {}` variant has no `JobRunner` impl → NoRunner error |
| eval, LoRA finetune | KILLED · enum stubs, no runner | types.rs `Eval {}`, `LoraFinetune {}` have no impl |

## Part C · resolved copy (every sentence traces)

- **thesis** → `a verified spot market for compute` (verified applies to the AI catalogue; compute
  is the market noun · defensible: 6 verified executors + a metered compute lane).
- **ash breadth** → names the shipped-and-verified set only: `inference · embeddings · transcription`
  and the honest identity line stays. Rendering/simulation appear only in the general-compute claim
  row and the download roadmap sentence, both marked metered/not-yet.
- **how-rows** → drop (any workload, pipeline detected: quote.go DetectedFields), cap (max_usd binds
  at dispatch), proof (verified, receipt).
- **receipts dialog** → the 11 verified rows carry (prove-local, queue, binaries, inference,
  scheduler, verification, quote, payouts, api, rails, identity), the `inference` row widens to name
  all six executors, and ONE new row states the general-compute lane honestly:
  `a general-compute lane runs your own container in a locked-down GPU sandbox · metered per
  GPU-second and reputation-trusted, not output-verified · Linux with Docker and the NVIDIA
  Container Toolkit`.
- **monument** → `$0.001` per 1,000 embeddings stays (db/schema.sql:299, a real seeded price).
- **download roadmap sentence** → carries the rendering/simulation ambition in plain words as the
  metered lane, never as a shipped verified capability.

## Part D · prove-local number

Matrix-only run records 168 pass · 0 skip · 0 fail (Test[A-Za-z0-9_]+ parser, fixed this session).
The full live run records 204 pass · 1 skip · 0 fail. The page keeps the labeled matrix-only 168
(honest, reproducible without infra) and re-pins from a fresh run at the honesty gate (Phase 7).

## Gates cleared to advance to Phase 1

- teardown inventory written (Part A)
- scope audit table with verdicts written (Part B)
- rendering correctly classified as roadmap, not shipped (Part B, Part C)
- copy plan traces every sentence (Part C)
