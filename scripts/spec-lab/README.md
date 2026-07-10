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
