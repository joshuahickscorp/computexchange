<!-- CLAIM-SCOPE: internal-engineering-non-authoritative -->
# spec-lab — the deterministic distributed-speculation experiment engine

A self-driving, money-safe experiment harness for the **distributed speculative
rendering/decoding** frontier: can ComputeExchange render (and infer) *cheaper* than
renting a cloud GPU, by speculating cheap drafts on the fleet and verifying on a GPU?

The thesis (see `docs/research/SPECULATIVE_ORCHESTRATOR_PLAN.md`): prediction = compression,
so a cheap predictor can *draft* the next chunk of ANY output (tokens, bytes, video frames,
render samples) and a verifier accepts-or-corrects it. The general artifact isn't a model —
it's the `draft → verify → gate` **protocol** plus the distributed orchestrator.

## Fire it

```bash
export RUNPOD_API_KEY=...           # your key; the balance you load is the hard $ ceiling
bash scripts/spec-lab/run.sh        # provisions ONE reachable GPU, runs the whole ladder, tears down
bash scripts/spec-lab/run.sh --dry-run       # print the ladder + bars, no GPU, no spend
MAX_MIN=90 bash scripts/spec-lab/run.sh      # tighter time cap
bash scripts/spec-lab/run.sh --only A1-ngram,A3-bytes   # a subset
SPEC_LAB_VLLM_VERSION=latest SPEC_LAB_TRANSFORMERS_VERSION=auto \
  python3 scripts/spec-lab/orchestrator.py --max-minutes 100 --only A5-vllm-suffix-upgrade,A6-vllm-draft-upgrade
python3 scripts/spec-lab/runpod.py cleanup   # emergency: nuke any tracked pod
```

The primary architecture branch is CX-native speculation: `SpecUnit -> DraftProducer
-> Verifier -> AcceptancePolicy -> RepairPolicy -> SpecReceipt`. vLLM, Hawking,
Cycles, ffmpeg, and future kernels are things to mine, fork, or compare when they
help that branch; they are not the boundary of the design.

Current closure (2026-07-13): exact-request byte transport is 6,815.52x median,
the fresh two-render Spatial75 preview is 207.644x, and the deliberately ungated
one-render ceiling is 339.701x. Only the exact repeat crosses 1,000x; none of
these receipts authorizes a final whole-project render, billing, or production
delivery. The strict project-bundle/v2 wire boundary, customer trade-offs,
hardening ledger, and remaining isolation/artifact-authority gates are recorded
in [`SPEC_ENGINE_WHOLE_PROJECT_PASS_2026-07-13.md`](../../docs/research/SPEC_ENGINE_WHOLE_PROJECT_PASS_2026-07-13.md).

Latest Apple Metal milestone: the real resident Cycles preview path measured
56.714589x on a 1080p classroom frame and 55.926238x on a materially different
Pavilion exterior under a local-unattested dual regional + fixed-microtile preview
contract. BMW27 cleared quality at 32+32 spp and measured 34.757412x, so the
current three-scene matrix is honestly 2/3 above 50x, not an arbitrary-scene claim.
An independently pinned official Fishy Cat particle-hair scene then reached
54.541695x on a held-out end frame after lossless local PNG transport was removed
from the charged critical path; it is a separate code generation, not a silent
rewrite of that matrix.
An eight-arm BMW integrator screen then pruned bounce caps, light-tree and
adaptive-sampling variants: the best cross-session projection was 40.292637x,
not a measured speedup. A separately hashed Stylized Levi derivative adds two
meaningfully different armature/lattice poses; both pass the 1080p gate at 1+1
spp, but its measured baseline slope projects only 35.697660x and prunes the
4096-spp final.
The untouched official Koro rigged/furry portrait scene then transferred at
84.250743x on a locally operator-declared held-out frame (112.396726 s /
1.334074 s), with no repair or prior rendered-result cache reuse and all
product/4096 gates passing. Normal renderer/Metal caches were warm after the
declared uncharged candidate warmup. This is a fourth bounded scene family above
50x, not an arbitrary-character or production claim; retained files cannot prove
the absence of an earlier or deleted held-out-frame run.
The bounded Rust multi-frame wrapper also produced and fully validated an
eight-frame silent H.264 VideoToolbox MP4, then repeated the path on a controlled
BMW camera move (seven accepted drafts, one bounded repair). Receipts,
limitations, rejected preliminary runs, and the remaining video/audio plan are in
[`APPLE_METAL_SPEC_RENDER_STAGE_2026-07-12.md`](../../docs/research/APPLE_METAL_SPEC_RENDER_STAGE_2026-07-12.md).

Recheck the local three-scene receipt matrix without network access:

```bash
python3 scripts/spec-lab/verify_render_transfer_matrix.py \
  --matrix proof/performance/apple-metal-render-transfer-matrix-2026-07-12.json

python3 scripts/spec-lab/verify_render_hair_transfer.py \
  --require-local-artifacts

python3 scripts/spec-lab/verify_render_bmw_integrator_screen.py \
  --require-local-artifacts

python3 scripts/spec-lab/verify_render_deformation_screen.py \
  --require-local-artifacts

python3 scripts/spec-lab/verify_render_koro_transfer.py \
  --require-local-artifacts
```

The verifier distinguishes receipt-bound fields from v1 product/provenance data
that can only be corroborated from the still-present local artifact roots.

## How it drives itself (deterministic auto-progression)

`orchestrator.py` walks `experiments.py::LADDER` in order. For each rung:

1. run the pod-side runner → parse its one-line JSON metrics;
2. check the rung's **bar** (viability predicate);
3. **PASS** → record + advance to the next rung automatically;
4. **FAIL** → walk the rung's **remediations** in order (inject a different knob and
   re-run — the aggressive auto-improve), until one passes or they're exhausted; then
   follow `on_fail` (`advance` default, or `stop`).

Every attempt is appended to `docs/speed-lane-reports/spec-lab/ledger.jsonl` (real
measured numbers — the standing discipline). A rung already recorded PASS is **skipped on
re-run**, so the lab resumes where it stopped without re-billing finished work.

## Money-safety (we're on borrowed GPU time)

- The pod id is written to `.tracked_pods.json` **before** anything else, so a crash can
  never orphan a billing pod.
- `register_cleanup()` tears the pod down on **every** exit path (finish, exception,
  Ctrl-C, SIGTERM).
- A hard `--max-minutes` **deadline watchdog** force-terminates the pod and exits.
- `provision_reachable()` only keeps a pod it can **actually SSH to** (this network can't
  route to many RunPod datacenters), terminating unreachable attempts — no silent billing.
- Your loaded RunPod balance is the ultimate ceiling: RunPod stops pods at $0.

## The ladder (see `experiments.py` for the live source of truth)

- **Track A — AR speculative decode (lossless anchor + generality):** CX-native
  token-unit/spec-receipt loops first, then vLLM n-gram (A1), draft-model/EAGLE
  (A2), and version-selected suffix/draft branches (A5/A6) only as reference or
  mining lanes. Byte-level spec-dec on *arbitrary files* (A3) and entropy
  thresholding (A4) remain generality tests.
- **Track B — video frame speculation (the keystone):** interp-draft + gated verify (B1)
  → motion-compensated **residual** "render the delta" (B2) → cheap-reject failure guard
  (B3) → **distributed** draft-on-fleet/verify-on-GPU (B4).
- **Track C — 3D/path-traced render (“render bits”):** low-spp + neural denoise (C1) →
  adaptive sampling by predicted variance (C2) → distributed tiled render (C3).
- **Track D — orchestrator + the PRICE proof:** one protocol drives all modalities (D1) →
  transparent **$/job vs RunPod DIY** cost model (D2) → receipt demo that beats
  rent-it-yourself (D3).

Each runner emits real measurements; where a real renderer/codec isn't installed, a runner
emits an **honest modeled** number with `modeled: true` and a note stating exactly what was
measured vs modeled. Nothing is fabricated; a rung that can't measure honestly errors out.
